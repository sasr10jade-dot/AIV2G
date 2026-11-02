"""사용자 모바일웹(차량 등록자) API — 본인에게 연결된 차량의 정보만 돌려준다.

보안 원칙: 이 라우터의 모든 엔드포인트는 `require_owner`로 보호하고, 차량은 요청 파라미터가
아니라 세션의 `owned_ev_id`로만 정한다. 경로·쿼리에 ev_id를 받지 않으므로 다른 사람의 차량
정보를 요청할 방법이 없다. 운영 API(관리자 전용)와 달리 owner에게는 관리자 권한이 없다.
"""
import logging
from typing import Literal
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from src.api.routers.auth import validate_password_strength
from src.auth import store as auth_store
from src.auth.deps import client_ip, require_owner, require_owner_account
from src.advisory import owner_guide
from src.copilot import owner_bot
from src.notify import service as notify
from src.privacy import audit, consent, member_history
from src.appadmin import service as appadmin
from src.auth import throttle
from src.rewards import service as rewards
from src.timeutil import to_kst_naive
from src.registry import options, service as registry, withdrawal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/owner", tags=["owner"])


class NotifyConsentIn(BaseModel):
    granted: bool


class ResubmitIn(BaseModel):
    vehicle_model: str
    plate: str
    occupation: str
    sido: str
    sigungu: str
    consent: bool


@router.get("/registration", summary="내 차량 등록 상태(승인 대기·승인·반려)")
def my_registration(user: dict = Depends(require_owner_account)) -> dict:
    """승인 전에도 호출할 수 있는 유일한 owner 엔드포인트 — 차량 데이터는 담지 않는다."""
    reg = registry.latest_for(user["username"])
    return {
        "registration": registry.view(reg) if reg else None,   # 번호판은 마스킹해서 내보낸다
        "linked": bool(user.get("owned_ev_id")),
        "ev_id": user.get("owned_ev_id"),
    }


@router.post("/registration", summary="반려된 뒤 차량 정보를 고쳐 다시 신청")
def resubmit_registration(body: ResubmitIn, request: Request, user: dict = Depends(require_owner_account)) -> dict:
    latest = registry.latest_for(user["username"])
    if latest and latest["status"] != "rejected":
        raise HTTPException(status_code=409, detail="이미 심사 중이거나 승인된 신청이 있습니다")
    if not body.consent:
        raise HTTPException(status_code=422, detail="개인정보 수집·이용에 동의해야 신청할 수 있습니다")
    try:
        registration_id = registry.submit(
            user["username"], body.plate, body.vehicle_model, body.occupation, body.sido, body.sigungu,
            options.CONSENT_VERSION)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    member_history.record(
        user["username"], user["username"], "registration_submit",
        [("vehicle_model", None, body.vehicle_model), ("plate", None, body.plate), ("occupation", None, body.occupation),
         ("region_sido", None, body.sido), ("region_sigungu", None, body.sigungu)],
        ip=client_ip(request), note=registration_id)
    return {"status": "pending", "registration_id": registration_id}


@router.get("/me", summary="내 계정·연결 차량 정보")
def me(user: dict = Depends(require_owner)) -> dict:
    return {"username": user["username"], "role": user["role"], "ev_id": user["owned_ev_id"]}


@router.get("/consent", summary="내 알림 수신 동의 상태")
def get_consent(user: dict = Depends(require_owner)) -> dict:
    return {"notify": consent.has_consent(user["owned_ev_id"], "notify"), "version": notify.NOTIFY_CONSENT_VERSION}


@router.put("/consent/notify", summary="알림 수신 동의/철회 — 본인 차량만")
def set_notify_consent(body: NotifyConsentIn, request: Request, user: dict = Depends(require_owner)) -> dict:
    ev_id = user["owned_ev_id"]
    before = consent.has_consent(ev_id, "notify")
    if body.granted:
        consent.grant(ev_id, "notify", notify.NOTIFY_CONSENT_VERSION, recorded_by=user["username"])
    else:
        consent.withdraw(ev_id, "notify")
    audit.log(user["username"], "owner_notify_consent", target=ev_id, detail={"granted": body.granted})
    after = consent.has_consent(ev_id, "notify")
    if before != after:   # 실제로 상태가 바뀐 경우만 이력에 남긴다
        member_history.record(
            user["username"], user["username"], "consent_grant" if after else "consent_withdraw",
            [("notify_consent", before, after)], ip=client_ip(request))
    return {"notify": after}


V2G_CONSENT_VERSION = "v2g-discharge-v1"


@router.get("/consent/v2g", summary="내 방전(V2G) 참여 상태 — 기본은 꺼짐(명시적으로 켜야 방전을 안내)")
def get_v2g_consent(user: dict = Depends(require_owner)) -> dict:
    return {"v2g": consent.has_consent(user["owned_ev_id"], "v2g_discharge"), "version": V2G_CONSENT_VERSION}


@router.put("/consent/v2g", summary="방전 참여 켜기/즉시 중지 — 본인 차량만, 변경 이력과 접속기록을 남긴다")
def set_v2g_consent(body: NotifyConsentIn, request: Request, user: dict = Depends(require_owner)) -> dict:
    ev_id = user["owned_ev_id"]
    before = consent.has_consent(ev_id, "v2g_discharge")
    if body.granted:
        consent.grant(ev_id, "v2g_discharge", V2G_CONSENT_VERSION, recorded_by=user["username"])
    else:
        consent.withdraw(ev_id, "v2g_discharge")
    audit.log(user["username"], "owner_v2g_consent", target=ev_id, detail={"granted": body.granted})
    after = consent.has_consent(ev_id, "v2g_discharge")
    if before != after:
        member_history.record(
            user["username"], user["username"], "consent_grant" if after else "consent_withdraw",
            [("v2g_discharge_consent", before, after)], ip=client_ip(request))
    return {"v2g": after}


@router.get("/consent/incentive", summary="내 시범 포인트 적립 동의 상태 — 동의해야 미션 포인트가 쌓인다")
def get_incentive_consent(user: dict = Depends(require_owner)) -> dict:
    return {"incentive": consent.has_consent(user["owned_ev_id"], "incentive"), "version": rewards.INCENTIVE_CONSENT_VERSION}


@router.put("/consent/incentive", summary="시범 포인트 적립 동의/철회 — 철회하면 이후 적립이 멈춘다(쌓은 포인트는 유지)")
def set_incentive_consent(body: NotifyConsentIn, request: Request, user: dict = Depends(require_owner)) -> dict:
    ev_id = user["owned_ev_id"]
    before = consent.has_consent(ev_id, "incentive")
    if body.granted:
        consent.grant(ev_id, "incentive", rewards.INCENTIVE_CONSENT_VERSION, recorded_by=user["username"])
    else:
        consent.withdraw(ev_id, "incentive")
    audit.log(user["username"], "owner_incentive_consent", target=ev_id, detail={"granted": body.granted})
    after = consent.has_consent(ev_id, "incentive")
    if before != after:
        member_history.record(
            user["username"], user["username"], "consent_grant" if after else "consent_withdraw",
            [("incentive_consent", before, after)], ip=client_ip(request))
    return {"incentive": after}


@router.get("/missions", summary="내 미션 진행·시범 포인트 — 달성은 서버가 판정하고 동의한 경우에만 적립")
def my_missions(lang: str = Query("ko", pattern="^(ko|en)$"), user: dict = Depends(require_owner)) -> dict:
    return rewards.evaluate(user["owned_ev_id"], user["username"], lang)


@router.get("/points", summary="내 시범 포인트 적립 내역")
def my_points(limit: int = Query(50, ge=1, le=200), user: dict = Depends(require_owner)) -> dict:
    return {"ledger": rewards.ledger(user["owned_ev_id"], limit), "note": rewards.NOTE}


@router.get("/notices", summary="공지사항(공개된 것만, 고정 먼저)")
def owner_notices(lang: str = Query("ko", pattern="^(ko|en)$"), user: dict = Depends(require_owner)) -> dict:
    return {"notices": appadmin.notices(published_only=True, limit=20, lang=lang)}


@router.get("/app-content", summary="관리자가 바꾼 앱 문구(없으면 앱 기본 문구를 쓴다)")
def owner_app_content(user: dict = Depends(require_owner)) -> dict:
    return {"overrides": appadmin.content_overrides()}


@router.post("/points/redeem", summary="상품권 교환 신청 — 기준 포인트를 차감하고 관리자가 코드를 등록하면 쿠폰함에 나타난다")
def redeem(user: dict = Depends(require_owner)) -> dict:
    try:
        rid = appadmin.redeem_request(user["owned_ev_id"])
    except appadmin.AdminError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.log(user["username"], "owner_redeem_request", target=user["owned_ev_id"], detail={"redemption": rid})
    return {"status": "requested", "id": rid}


@router.get("/coupons", summary="내 교환 내역·쿠폰함 — 발급된 코드는 본인에게만 보인다")
def my_coupons(lang: str = Query("ko", pattern="^(ko|en)$"), user: dict = Depends(require_owner)) -> dict:
    return {"coupons": appadmin.coupons(user["owned_ev_id"], lang), "redeem": appadmin.redeem_state(user["owned_ev_id"])}


@router.get("/notifications", summary="내 알림함")
def list_notifications(
    limit: int = Query(50, ge=1, le=200), unread_only: bool = False, lang: str = Query("ko", pattern="^(ko|en)$"),
    user: dict = Depends(require_owner),
) -> dict:
    ev_id = user["owned_ev_id"]
    items = notify.list_for(ev_id, limit=limit, unread_only=unread_only)
    if lang == "en":   # 관리자가 영어 제목·내용을 함께 보낸 알림만 영어로 바꾼다(없으면 원문)
        for n in items:
            en = (n.get("payload") or {}).get("en")
            if en and en.get("title") and en.get("body"):
                n["title"], n["body"] = en["title"], en["body"]
    return {
        "notifications": items,
        "unread": notify.unread_count(ev_id),
        "notify_consent": consent.has_consent(ev_id, "notify"),
    }


@router.post("/notifications/read-all", summary="알림 모두 읽음")
def read_all(user: dict = Depends(require_owner)) -> dict:
    return {"marked": notify.mark_all_read(user["owned_ev_id"])}


@router.post("/notifications/{notification_id}/read", summary="알림 읽음 처리")
def read_one(notification_id: str, user: dict = Depends(require_owner)) -> dict:
    if not notify.mark_read(user["owned_ev_id"], notification_id):
        raise HTTPException(status_code=404, detail="읽지 않은 알림을 찾을 수 없습니다")
    return {"status": "ok"}


@router.get("/history", summary="내 회원정보 변경 이력 — 처리자는 본인/관리자로만, 접속 IP는 제외")
def my_history(limit: int = Query(100, ge=1, le=300), user: dict = Depends(require_owner_account)) -> dict:
    return {"history": member_history.timeline(user["username"], limit)}


# ---------------------------------------------------------------- 정보주체 권리(마이페이지) --

class ProfileUpdateIn(BaseModel):
    vehicle_model: str | None = Field(None, max_length=40)
    occupation: str | None = Field(None, max_length=20)
    sido: str | None = Field(None, max_length=20)
    sigungu: str | None = Field(None, max_length=30)

    @model_validator(mode="after")
    def _at_least_one(self):
        if not any(v is not None for v in (self.vehicle_model, self.occupation, self.sido, self.sigungu)):
            raise ValueError("수정할 항목을 하나 이상 입력하세요")
        return self


class PasswordChangeIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, v: str) -> str:
        return validate_password_strength(v)


class WithdrawIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=128)
    confirm: bool = Field(..., description="탈퇴하면 되돌릴 수 없음을 확인")


@router.get("/my-data", summary="내 개인정보 열람 — 보유 중인 정보와 동의·이력 현황")
def my_data(user: dict = Depends(require_owner_account)) -> dict:
    """정보주체 열람권: 이 서비스가 나에 대해 가진 정보를 한곳에서 보여준다. 번호판은 마스킹해서 내보낸다."""
    ev_id = user.get("owned_ev_id")
    account = auth_store.get_user(user["username"]) or {}
    reg = registry.latest_for(user["username"])
    return {
        "account": {"username": user["username"], "created_at": account.get("created_at")},
        "vehicle": {"ev_id": ev_id},
        "registration": registry.view(reg) if reg else None,
        "consents": consent.list_consents(ev_id) if ev_id else [],
        "notifications": {"total": notify.count_all(ev_id) if ev_id else 0, "unread": notify.unread_count(ev_id) if ev_id else 0},
        "history_count": len(member_history.timeline(user["username"], 500)),
        "retention": "회원 탈퇴 시까지 보관하며, 탈퇴하면 지체 없이 삭제합니다. 접속기록·회원 이력은 법정 보관 기간 동안 보존합니다.",
    }


@router.patch("/profile", summary="내 정보 정정 — 차종·직업군·거주지(번호판은 관리자 재심사)")
def update_profile(body: ProfileUpdateIn, request: Request, user: dict = Depends(require_owner_account)) -> dict:
    updates = {k: v for k, v in {
        "vehicle_model": body.vehicle_model, "occupation": body.occupation,
        "region_sido": body.sido, "region_sigungu": body.sigungu}.items() if v is not None}
    try:
        changes = registry.update_profile(user["username"], updates)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if changes:
        member_history.record(user["username"], user["username"], "profile_update", changes, ip=client_ip(request))
    return {"changed": [c[0] for c in changes]}


@router.post("/password", summary="비밀번호 변경")
def change_password(body: PasswordChangeIn, request: Request, user: dict = Depends(require_owner_account)) -> dict:
    throttle.check(request, user["username"])
    record = auth_store.get_user(user["username"])
    if not record or not auth_store.verify_password(body.current_password, record["password_hash"]):
        throttle.fail(request, user["username"])
        raise HTTPException(status_code=403, detail="현재 비밀번호가 올바르지 않습니다")
    throttle.success(request, user["username"])
    if body.new_password == body.current_password:
        raise HTTPException(status_code=422, detail="새 비밀번호가 현재 비밀번호와 같습니다")
    auth_store.set_password(user["username"], body.new_password)
    member_history.record(user["username"], user["username"], "profile_update",
                          [("password", None, "changed")], ip=client_ip(request))
    return {"status": "ok"}


@router.post("/account/withdraw", summary="회원 탈퇴 — 개인정보 삭제(되돌릴 수 없음)")
def withdraw(body: WithdrawIn, request: Request, user: dict = Depends(require_owner_account)) -> dict:
    if not body.confirm:
        raise HTTPException(status_code=422, detail="탈퇴하면 되돌릴 수 없습니다. 확인에 체크하세요")
    throttle.check(request, user["username"])
    record = auth_store.get_user(user["username"])
    if not record or not auth_store.verify_password(body.password, record["password_hash"]):
        throttle.fail(request, user["username"])
        raise HTTPException(status_code=403, detail="비밀번호가 올바르지 않습니다")
    throttle.success(request, user["username"])
    try:
        summary = withdrawal.withdraw_member(user["username"], ip=client_ip(request))
    except registry.RegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.session.clear()
    return {"status": "withdrawn", "deleted": summary}


# -------------------------------------------------------------------- 이용자용 EV Bot --

class HistoryTurnIn(BaseModel):
    role: str = Field(..., pattern="^(user|ai)$")
    text: str = Field(..., max_length=owner_bot.MAX_TURN_CHARS)


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=owner_bot.MAX_QUESTION_CHARS)
    history: list[HistoryTurnIn] | None = Field(None, max_length=owner_bot.MAX_HISTORY_TURNS * 2)
    lang: str = Field("ko", pattern="^(ko|en)$")   # 앱 표시 언어 — 답변 언어만 바꾼다


BOT_FACTS_TTL_S = 90
_bot_facts_cache: dict = {}


@router.post("/ask", summary="이용자용 EV Bot — 앱 사용법·용어·개인정보 안내(본인 차량 정보만 근거)")
def ask(body: AskIn, user: dict = Depends(require_owner)) -> dict:
    """운영자용 EV Bot과 달리 이용자용 안내 문서와 본인 차량 정보만 근거로 답한다. 차량은 세션에서만 정해진다."""
    info = owner_bot.build_vehicle_info(user["username"], user["owned_ev_id"])
    extra = None
    try:   # 충전 시간 질문에 실제 계산 결과로 답하게 한다(계산 실패는 일반 안내로 대체)
        participating = consent.has_consent(user["owned_ev_id"], "v2g_discharge")
        key = (user["username"], user["owned_ev_id"], participating)
        hit = _bot_facts_cache.get(key)
        if hit and time.monotonic() - hit[0] < BOT_FACTS_TTL_S:   # 같은 이용자의 연속 질문마다 최적화를 다시 풀지 않는다
            extra = hit[1]
        else:
            extra = owner_guide.bot_facts(owner_guide.vehicle_profile(user["username"], user["owned_ev_id"]),
                                          participating=participating)
            if len(_bot_facts_cache) > 500:   # 이용자가 늘어도 메모리가 무한정 자라지 않게 오래된 것부터 비운다
                for old in sorted(_bot_facts_cache, key=lambda k: _bot_facts_cache[k][0])[:250]:
                    _bot_facts_cache.pop(old, None)
            _bot_facts_cache[key] = (time.monotonic(), extra)
    except Exception:   # noqa: BLE001
        logger.exception("EV Bot: 충전 시간 계산을 건너뜁니다")
    history = [h.model_dump() for h in body.history] if body.history else None
    try:
        answer = owner_bot.ask_owner(user["username"], body.question, info, history, extra, body.lang)
    except owner_bot.OwnerBotRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except owner_bot.CopilotNotConfigured as exc:
        raise HTTPException(status_code=503, detail="지금은 EV Bot을 쓸 수 없어요. 운영자에게 문의해 주세요.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:   # 질문·답변 본문은 개인정보가 섞일 수 있어 남기지 않고, 호출 사실과 길이만 기록한다
        audit.log(user["username"], "owner_ask", target=user["owned_ev_id"], detail={
            "lang": body.lang, "question_chars": len(body.question), "answer_chars": len(answer),
            "history_turns": len(body.history or [])})
    except Exception:   # noqa: BLE001
        logger.exception("EV Bot 감사로그 기록에 실패했습니다")
    return {"answer": answer}


# -------------------------------------------------------------- 충전 시간 안내·수요 예측 --

def _audit_guide(user: dict, guide: dict) -> None:   # 추천 기록 번호는 guide["guide_id"]에 담는다
    """추천 결과와 안전 검증 결과를 해시체인 감사로그에 남긴다(AI 관리체계 8장 운영 로그).

    입력 조건과 검증 통과 여부·실패 항목만 담고, 번호판 등 식별정보는 넣지 않는다. 기록 실패가 이용자 응답을
    막지 않도록 예외는 삼키고 로그로만 남긴다.
    """
    try:
        safety = guide.get("safety") or {}
        guide["guide_id"] = rewards.record_plan(user["owned_ev_id"], guide)   # 추천 시간대 저장 — 미션 확인과 '추천대로 할게요' 응답의 대상
        audit.log(user["username"], "owner_guide_issue", target=user["owned_ev_id"], detail={
            "inputs": guide.get("inputs"),
            "participating": safety.get("participating"),
            "safety_passed": safety.get("passed"),
            "safety_fallback": safety.get("fallback"),
            "failed_checks": [c["id"] for c in safety.get("checks", []) if not c.get("ok")],
            "v2g_recommended": (guide.get("v2g") or {}).get("recommended"),
            "verified": guide.get("verified"),
        })
    except Exception:   # noqa: BLE001
        logger.exception("추천 결과 감사로그 기록에 실패했습니다")


@router.get("/charging-guide", summary="내 차량의 충전(·방전) 추천 시간과 예상 절감 — 가정값 기준 모의 계산")
def charging_guide(current_soc_pct: float = Query(50.0, ge=0, le=100), need_soc_pct: float = Query(80.0, ge=20, le=100),
                   departure: str | None = Query(None, description="출발 시각(한국시간 ISO, 비우면 다음 08시)"),
                   compensation_id: str = Query(owner_guide.DEFAULT_COMPENSATION_ID, max_length=40),
                   lang: str = Query("ko", pattern="^(ko|en)$"),
                   user: dict = Depends(require_owner)) -> dict:
    """차량은 세션에서만 정한다(파라미터로 받지 않는다). 시간 안내는 모의 요금·보상 가정으로 계산한다."""
    try:
        dep = to_kst_naive(departure).to_pydatetime() if departure else None
        profile = owner_guide.vehicle_profile(user["username"], user["owned_ev_id"])
        guide = owner_guide.build_guide(profile, owner_guide.GuideInputs(current_soc_pct, need_soc_pct, dep, compensation_id),
                                        participating=consent.has_consent(user["owned_ev_id"], "v2g_discharge"))
        _audit_guide(user, guide)
        return owner_guide.localize(guide, lang)
    except (owner_guide.GuideError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class GuideResponseIn(BaseModel):
    status: Literal["accepted", "declined"]


@router.post("/guide/{guide_id}/respond", summary="내 충전 추천에 응답 — accepted(추천대로 할게요) / declined(나중에). 본인 추천만")
def respond_guide(guide_id: int, body: GuideResponseIn, user: dict = Depends(require_owner)) -> dict:
    if not rewards.respond_plan(user["owned_ev_id"], guide_id, body.status):
        raise HTTPException(status_code=404, detail="추천 기록을 찾을 수 없습니다")
    audit.log(user["username"], "owner_guide_respond", target=user["owned_ev_id"], detail={"guide_id": guide_id, "status": body.status})
    return {"status": body.status}


@router.get("/demand-forecast", summary="충전 수요·V2G 가용량 48시간 예측 요약(시뮬레이션 차량 전체 기준)")
def demand_forecast(lang: str = Query("ko", pattern="^(ko|en)$"), user: dict = Depends(require_owner)) -> dict:
    try:
        return owner_guide.localize(owner_guide.demand_forecast(), lang)
    except Exception as exc:   # noqa: BLE001 — 예측 모델이 아직 학습되지 않았을 수 있다
        logger.exception("수요 예측을 불러오지 못했습니다")
        raise HTTPException(status_code=503, detail="수요 예측을 아직 불러올 수 없어요.") from exc
