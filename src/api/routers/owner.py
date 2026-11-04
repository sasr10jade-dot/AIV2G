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
from src.advisory import owner_guide, service as advisory
from src.privacy import audit, consent, member_history
from src.appadmin import service as appadmin
from src.auth import throttle
from src.timeutil import to_kst_naive
from src.registry import options, service as registry, withdrawal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/owner", tags=["owner"])


HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class ConsentIn(BaseModel):
    granted: bool


class ResubmitIn(BaseModel):
    vehicle_model: str
    plate: str
    occupation: str
    sido: str
    sigungu: str
    consent: bool
    # 계획서 4.1 서포터즈 입력(최적화 입력) — 비우면 충전 안내 기본값(다음 08시 출발·목표 80%·최소 20%)을 쓴다
    departure_time: str | None = Field(None, pattern=HHMM, description="평소 출발 시각(HH:MM)")
    target_soc_pct: float | None = Field(None, ge=20, le=100)
    min_soc_pct: float | None = Field(None, ge=10, le=90)
    odometer_km: float | None = Field(None, ge=0, le=2_000_000)


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
            options.CONSENT_VERSION, operating=registry.operating_inputs(
                body.departure_time, body.target_soc_pct, body.min_soc_pct, body.odometer_km))
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


V2G_CONSENT_VERSION = "v2g-discharge-v1"


@router.get("/consent/v2g", summary="내 방전(V2G) 참여 상태 — 기본은 꺼짐(명시적으로 켜야 방전을 안내)")
def get_v2g_consent(user: dict = Depends(require_owner)) -> dict:
    return {"v2g": consent.has_consent(user["owned_ev_id"], "v2g_discharge"), "version": V2G_CONSENT_VERSION}


@router.put("/consent/v2g", summary="방전 참여 켜기/즉시 중지 — 본인 차량만, 변경 이력과 접속기록을 남긴다")
def set_v2g_consent(body: ConsentIn, request: Request, user: dict = Depends(require_owner)) -> dict:
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


@router.get("/notices", summary="공지사항(공개된 것만, 고정 먼저)")
def owner_notices(lang: str = Query("ko", pattern="^(ko|en)$"), user: dict = Depends(require_owner)) -> dict:
    return {"notices": appadmin.notices(published_only=True, limit=20, lang=lang)}


@router.get("/app-content", summary="관리자가 바꾼 앱 문구(없으면 앱 기본 문구를 쓴다)")
def owner_app_content(user: dict = Depends(require_owner)) -> dict:
    return {"overrides": appadmin.content_overrides()}


@router.get("/history", summary="내 회원정보 변경 이력 — 처리자는 본인/관리자로만, 접속 IP는 제외")
def my_history(limit: int = Query(100, ge=1, le=300), user: dict = Depends(require_owner_account)) -> dict:
    return {"history": member_history.timeline(user["username"], limit)}


# ---------------------------------------------------------------- 정보주체 권리(마이페이지) --

class ProfileUpdateIn(BaseModel):
    vehicle_model: str | None = Field(None, max_length=40)
    occupation: str | None = Field(None, max_length=20)
    sido: str | None = Field(None, max_length=20)
    sigungu: str | None = Field(None, max_length=30)
    departure_time: str | None = Field(None, pattern=HHMM)
    target_soc_pct: float | None = Field(None, ge=20, le=100)
    min_soc_pct: float | None = Field(None, ge=10, le=90)
    odometer_km: float | None = Field(None, ge=0, le=2_000_000)

    @model_validator(mode="after")
    def _at_least_one(self):
        if not any(v is not None for v in self.model_dump().values()):
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
        "history_count": len(member_history.timeline(user["username"], 500)),
        "retention": "회원 탈퇴 시까지 보관하며, 탈퇴하면 지체 없이 삭제합니다. 접속기록·회원 이력은 법정 보관 기간 동안 보존합니다.",
    }


@router.patch("/profile", summary="내 정보 정정 — 차종·직업군·거주지·운행 입력(출발 시각·목표/최소 배터리·누적 주행거리). 번호판은 관리자 재심사")
def update_profile(body: ProfileUpdateIn, request: Request, user: dict = Depends(require_owner_account)) -> dict:
    updates = {k: v for k, v in {
        "vehicle_model": body.vehicle_model, "occupation": body.occupation,
        "region_sido": body.sido, "region_sigungu": body.sigungu,
        "departure_time": body.departure_time, "target_soc_pct": body.target_soc_pct,
        "min_soc_pct": body.min_soc_pct, "odometer_km": body.odometer_km}.items() if v is not None}
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


# -------------------------------------------------------------- 충전 시간 안내·수요 예측 --

def _audit_guide(user: dict, guide: dict) -> None:   # 추천 기록 번호는 guide["guide_id"]에 담는다
    """추천을 advisories에 발행 기록으로 남기고(응답 대상), 추천 결과와 안전 검증 결과를 해시체인 감사로그에
    남긴다(AI 관리체계 8장 운영 로그).

    입력 조건과 검증 통과 여부·실패 항목만 담고, 번호판 등 식별정보는 넣지 않는다. 기록 실패가 이용자 응답을
    막지 않도록 예외는 삼키고 로그로만 남긴다.
    """
    try:
        safety = guide.get("safety") or {}
        plan = guide.get("plan") or {}
        guide["guide_id"] = advisory.issue(user["owned_ev_id"], {
            "charge": plan.get("charge", []), "discharge": plan.get("discharge", []), "period": guide.get("period"),
            "assumptions": {"verified": bool(guide.get("verified"))}}, channel="app", inputs=guide.get("inputs"))
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
def charging_guide(current_soc_pct: float = Query(50.0, ge=0, le=100),
                   need_soc_pct: float | None = Query(None, ge=20, le=100, description="출발할 때 필요한 배터리(%) — 비우면 가입 때 입력한 목표 배터리(없으면 80%)"),
                   departure: str | None = Query(None, description="출발 시각(한국시간 ISO) — 비우면 가입 때 입력한 평소 출발 시각(없으면 다음 08시)"),
                   compensation_id: str = Query(owner_guide.DEFAULT_COMPENSATION_ID, max_length=40),
                   lang: str = Query("ko", pattern="^(ko|en)$"),
                   user: dict = Depends(require_owner)) -> dict:
    """차량은 세션에서만 정한다(파라미터로 받지 않는다). 시간 안내는 모의 요금·보상 가정으로 계산한다.
    출발 시각·목표 배터리를 비우면 서포터즈 가입 때 입력한 값(계획서 4.1 서포터즈 입력)을 쓴다."""
    try:
        reg = registry.latest_for(user["username"]) or {}
        dep = to_kst_naive(departure).to_pydatetime() if departure else owner_guide.next_departure(reg.get("departure_time"))
        if need_soc_pct is None:
            need_soc_pct = float(reg.get("target_soc_pct") or 80.0)
        profile = owner_guide.vehicle_profile(user["username"], user["owned_ev_id"])
        inputs = owner_guide.GuideInputs(current_soc_pct, need_soc_pct, dep, compensation_id, reg.get("min_soc_pct"))
        guide = owner_guide.build_guide(profile, inputs,
                                        participating=consent.has_consent(user["owned_ev_id"], "v2g_discharge"))
        _audit_guide(user, guide)
        return owner_guide.localize(guide, lang)
    except (owner_guide.GuideError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class GuideResponseIn(BaseModel):
    status: Literal["accepted", "declined"]


@router.post("/guide/{guide_id}/respond", summary="내 충전 추천에 응답 — accepted(추천대로 할게요) / declined(나중에). 본인 추천만")
def respond_guide(guide_id: str, body: GuideResponseIn, user: dict = Depends(require_owner)) -> dict:
    try:
        advisory.respond(guide_id, body.status, ev_id=user["owned_ev_id"])
    except advisory.AdvisoryError as exc:
        if "이미 응답" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=404, detail="추천 기록을 찾을 수 없습니다") from exc
    audit.log(user["username"], "owner_guide_respond", target=user["owned_ev_id"], detail={"guide_id": guide_id, "status": body.status})
    return {"status": body.status}


@router.get("/demand-forecast", summary="충전 수요·V2G 가용량 48시간 예측 요약(시뮬레이션 차량 전체 기준)")
def demand_forecast(lang: str = Query("ko", pattern="^(ko|en)$"), user: dict = Depends(require_owner)) -> dict:
    try:
        return owner_guide.localize(owner_guide.demand_forecast(), lang)
    except Exception as exc:   # noqa: BLE001 — 예측 모델이 아직 학습되지 않았을 수 있다
        logger.exception("수요 예측을 불러오지 못했습니다")
        raise HTTPException(status_code=503, detail="수요 예측을 아직 불러올 수 없어요.") from exc


# ------------------------------------------------------------------ 내 충전 리포트 --

class OdometerIn(BaseModel):
    odometer_km: float = Field(..., ge=0, le=2_000_000)


@router.post("/odometer", summary="누적 주행거리 기록(계기판 값) — 주간 주행·전비·1km당 요금 계산에 쓴다. 직전 값보다 작으면 거부")
def record_odometer(body: OdometerIn, request: Request, user: dict = Depends(require_owner)) -> dict:
    before = (registry.latest_for(user["username"]) or {}).get("odometer_km")
    try:
        registry.record_odometer(user["username"], body.odometer_km)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    member_history.record(user["username"], user["username"], "profile_update",
                          [("odometer_km", before, body.odometer_km)], ip=client_ip(request))
    return {"odometer_km": body.odometer_km, "history": registry.odometer_history(user["username"])[-10:]}


@router.get("/report", summary="내 충전 리포트 — 최근 4주 충전·주행 지표, 다른 실증 차량과 비교, 최소 보장 배터리 제안")
def my_report(lang: str = Query("ko", pattern="^(ko|en)$"), user: dict = Depends(require_owner)) -> dict:
    from src.advisory import owner_report
    from src.pipeline import source
    ev_df, _ch, grid_df, _real = source.load_scenario()
    reg = registry.latest_for(user["username"]) or {}
    profile = owner_guide.vehicle_profile(user["username"], user["owned_ev_id"])
    report = owner_report.build_report(user["owned_ev_id"], ev_df, grid_df, profile["battery_kwh"],
                                       odometer=registry.odometer_history(user["username"]),
                                       min_soc_pct=reg.get("min_soc_pct"), target_soc_pct=reg.get("target_soc_pct"), lang=lang)
    report["odometer"] = registry.odometer_history(user["username"])[-5:]
    report["is_real_data"] = bool(_real)
    return report
