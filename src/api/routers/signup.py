"""모바일 회원가입 API — 계정 + 차량·페르소나 정보를 한 번에 받아 '승인 대기'로 등록한다.

가입해도 차량 데이터에는 접근할 수 없다: 계정은 만들어지지만 owned_ev_id가 비어 있고, PC에서 관리자가
승인해 차량이 연결돼야 알림·추천이 시작된다. 로그인이 필요 없는 공개 엔드포인트라 요청 횟수를 제한한다.
"""
import os
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.api.routers.auth import USERNAME_PATTERN, validate_password_strength
from src.auth import store as auth_store
from src.auth.deps import client_ip
from src.privacy import member_history
from src.registry import options, service

router = APIRouter(prefix="/signup", tags=["signup"])

_WINDOW_S = 3600
_attempts: dict[str, deque] = defaultdict(deque)


def reset_rate_limit() -> None:
    _attempts.clear()


def _check_rate_limit(request: Request) -> None:
    limit = int(os.environ.get("SIGNUP_RATE_PER_HOUR", "20"))
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status_code=429, detail="가입 요청이 너무 많습니다. 잠시 후 다시 시도하세요.")
    q.append(now)


class SignupIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=USERNAME_PATTERN)
    password: str = Field(..., min_length=8, max_length=128)
    vehicle_model: str = Field(..., max_length=40)
    plate: str = Field(..., max_length=12)
    occupation: str = Field(..., max_length=20)
    sido: str = Field(..., max_length=20)
    sigungu: str = Field(..., max_length=30)
    consent: bool = Field(..., description="개인정보 수집·이용 동의(필수)")
    # 계획서 4.1 서포터즈 입력(선택) — 최적화가 차량 운행에 지장 없는 범위에서 결과를 내는 기준
    departure_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="평소 출발 시각(HH:MM)")
    target_soc_pct: float | None = Field(None, ge=20, le=100, description="출발 시 목표 배터리(%)")
    min_soc_pct: float | None = Field(None, ge=10, le=90, description="최소 보장 배터리(%)")
    odometer_km: float | None = Field(None, ge=0, le=2_000_000, description="누적 주행거리(km)")

    @field_validator("username")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        return validate_password_strength(v)


@router.get("/options", summary="가입 선택지(차종·직업군·시도)와 동의 문구")
def signup_options() -> dict:
    catalog = options.load_catalog()
    return {
        "vehicle_models": catalog.public_list(),
        "occupations": [{"id": k, "label": v} for k, v in options.OCCUPATIONS.items()],
        "sido": list(options.SIDO),
        "sigungu_by_sido": options.load_regions(),   # 시·도를 고르면 해당 시·군·구만 선택지로 보여 준다
        "consent": {"version": options.CONSENT_VERSION, "text": options.CONSENT_TEXT,
                    "policy_url": options.POLICY_URL, "policy_version": options.POLICY_VERSION},
    }


@router.post("", summary="회원가입 — 승인 대기로 등록하고 로그인 상태로 만든다")
def signup(body: SignupIn, request: Request) -> dict:
    _check_rate_limit(request)
    if not body.consent:
        raise HTTPException(status_code=422, detail="개인정보 수집·이용에 동의해야 가입할 수 있습니다")
    try:
        # 계정을 만들기 전에 입력 검증을 먼저 끝낸다(실패 시 남는 계정이 없게)
        service.validate_profile(body.plate, body.vehicle_model, body.occupation, body.sido, body.sigungu)
        operating = service.operating_inputs(body.departure_time, body.target_soc_pct, body.min_soc_pct, body.odometer_km)
        auth_store.create_user(body.username, body.password, "owner", created_by="signup", allow_unlinked_owner=True)
    except (service.RegistryError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        registration_id = service.submit(
            body.username, body.plate, body.vehicle_model, body.occupation, body.sido, body.sigungu,
            options.CONSENT_VERSION, operating=operating)
    except service.RegistryError as exc:
        auth_store.delete_user(body.username)   # 등록에 실패하면 방금 만든 계정도 남기지 않는다
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    member_history.record(
        body.username, body.username, "signup",
        [("vehicle_model", None, body.vehicle_model), ("plate", None, body.plate), ("occupation", None, body.occupation),
         ("region_sido", None, body.sido), ("region_sigungu", None, body.sigungu),
         *[(f, None, v) for f, v in operating.items() if v is not None],
         ("profile_consent", None, options.CONSENT_VERSION)],
        ip=client_ip(request), note=registration_id)
    request.session["username"] = body.username
    return {"status": "pending", "registration_id": registration_id, "username": body.username}
