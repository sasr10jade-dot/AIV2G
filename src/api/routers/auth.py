"""로그인·로그아웃·계정관리 API. 계정 생성/삭제는 최고관리자(super_admin) 전용이다."""
import os
import re
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

from src.auth import store, throttle
from src.auth.deps import get_current_user, require_super_admin

router = APIRouter(prefix="/auth", tags=["auth"])

USERNAME_PATTERN = r"^[a-zA-Z0-9_-]+$"

# 로그인 화면의 "빠른 로그인" 버튼이 역할별로 어떤 .env 변수를 조회할지 — 실제
# 비밀번호는 서버에서만 조회하고 브라우저로는 절대 보내지 않는다(quick-login은
# 아이디/비밀번호 대신 "role"만 브라우저에서 서버로 보낸다).
QUICK_LOGIN_ENV: dict[str, tuple[str, str]] = {
    "super_admin": ("SUPERADMIN_USERNAME", "SUPERADMIN_PASSWORD"),
    "admin": ("DEMO_ADMIN_USERNAME", "DEMO_ADMIN_PASSWORD"),
    "owner": ("DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD"),  # 사용자 모바일웹 개발용 간편 로그인
}


def validate_password_strength(v: str) -> str:
    """계정 발급과 모바일 가입이 같은 비밀번호 규칙을 쓰도록 공용으로 둔다."""
    if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        raise ValueError("비밀번호는 영문과 숫자를 각각 최소 1자 포함해야 합니다")
    return v


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class QuickLoginRequest(BaseModel):
    role: Literal["admin", "super_admin", "owner"] = Field(..., description="빠른 로그인할 역할")


class SetOwnedVehicleRequest(BaseModel):
    owned_ev_id: str | None = Field(None, description="내 차량으로 등록할 ev_id — null이면 등록 해제")


class CreateUserRequest(BaseModel):
    username: str = Field(
        ..., min_length=3, max_length=50, pattern=USERNAME_PATTERN,
        description="영문·숫자·언더스코어(_)·하이픈(-)만 사용, 3~50자",
    )
    password: str = Field(..., min_length=8, max_length=128, description="8~128자, 영문과 숫자를 각각 최소 1자 포함")
    role: Literal["admin", "super_admin", "owner"] = Field(
        "admin", description="'admin'(일반운영) / 'super_admin'(계정관리 포함 전체) / 'owner'(차량 등록자, 사용자 모바일웹)")
    owned_ev_id: str | None = Field(
        None, max_length=50, description="owner 계정에 연결할 차량 ev_id — owner는 필수, 그 외 역할은 선택")

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, v: str) -> str:
        return v.strip()

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, v: str) -> str:
        return validate_password_strength(v)


@router.post("/login", summary="로그인")
def login(req: LoginRequest, request: Request) -> dict:
    throttle.check(request, req.username)   # 실패가 많으면 잠시 거절(무차별 대입 방어)
    user = store.get_user(req.username)
    if not user or not store.verify_password(req.password, user["password_hash"]):
        throttle.fail(request, req.username)
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")
    throttle.success(request, req.username)
    request.session["username"] = req.username
    return {"username": req.username, "role": user["role"]}


@router.get("/demo-access", include_in_schema=False)
def demo_access(request: Request, code: str = "") -> RedirectResponse:
    """이용자 전용 데모 링크 — 비밀 코드를 아는 사람만 로그인 없이 데모 이용자 화면(`/app`)으로 들어간다.

    서버에 `DEMO_ACCESS_TOKEN`(비밀 코드)이 설정돼 있을 때만 동작한다(없으면 404). 들어가는 계정은 `DEMO_ACCESS_OWNER`
    (기본 demo-owner)이고 **이용자(owner) 역할일 때만** 허용한다 — 관리자 계정은 이 경로로 열리지 않는다.
    틀린 코드는 404로만 답하고(존재 여부를 알리지 않음), 실패가 누적되면 429로 잠근다.
    """
    token = os.environ.get("DEMO_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=404)
    throttle.check(request, "demo-access")
    if not secrets.compare_digest(code.encode("utf-8"), token.encode("utf-8")):
        throttle.fail(request, "demo-access")
        raise HTTPException(status_code=404)
    name = os.environ.get("DEMO_ACCESS_OWNER", "demo-owner")
    user = store.get_user(name)
    if not user or user["role"] != "owner":
        raise HTTPException(status_code=404)
    request.session["username"] = name
    from src.privacy import audit
    audit.log(name, "demo_access", detail={"via": "demo-link"})
    return RedirectResponse(url="/app", status_code=303)


@router.get("/quick-login/available", summary="빠른 로그인이 설정된 역할 — 화면이 버튼 표시 여부를 정하는 데 사용")
def quick_login_available() -> dict:
    """역할별로 `.env`에 아이디·비밀번호 변수가 둘 다 있는지만 알려준다(값은 노출하지 않는다).
    설정이 없으면 화면에 버튼 자체를 보이지 않게 하려는 용도다."""
    return {
        role: bool(os.environ.get(user_key) and os.environ.get(pass_key))
        for role, (user_key, pass_key) in QUICK_LOGIN_ENV.items()
    }


@router.post("/quick-login", summary="데모용 빠른 로그인 — .env에 설정된 역할별 계정으로 즉시 로그인")
def quick_login(req: QuickLoginRequest, request: Request) -> dict:
    """로그인 화면에서 아이디·비밀번호 입력을 건너뛰고, 서버 `.env`에 미리 설정해둔
    역할별 계정으로 바로 로그인한다 — 브라우저는 `role`만 서버로 보내고 실제
    비밀번호는 서버에서만 조회하므로 화면·네트워크 로그에 노출되지 않는다.

    `super_admin`은 최초 부트스트랩 계정(`SUPERADMIN_USERNAME`/`PASSWORD`, 이미
    설정돼 있으면 즉시 동작)을, `admin`은 별도로 `.env`에 `DEMO_ADMIN_USERNAME`/
    `DEMO_ADMIN_PASSWORD`를 설정해야 활성화된다(안 돼 있으면 503) — 로컬 데모/개발
    편의 기능이며, 실제 운영 노출 환경에서는 쓰지 않는 것을 권장한다."""
    username_key, password_key = QUICK_LOGIN_ENV[req.role]
    username = os.environ.get(username_key)
    password = os.environ.get(password_key)
    if not username or not password:
        raise HTTPException(
            status_code=503,
            detail=f"{req.role} 빠른 로그인이 설정되지 않았습니다 — .env에 {username_key}/{password_key}를 설정하세요.",
        )
    user = store.get_user(username)
    if not user or not store.verify_password(password, user["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail="빠른 로그인 계정 인증에 실패했습니다 — .env 값과 실제 계정 정보가 일치하는지 확인하세요.",
        )
    request.session["username"] = username
    return {"username": username, "role": user["role"]}


@router.post("/logout", summary="로그아웃")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"status": "ok"}


@router.get("/me", summary="현재 로그인 사용자 정보")
def me(user: dict = Depends(get_current_user)) -> dict:
    return user


@router.patch("/me/owned-vehicle", summary="\"내 차\" 등록 — EV Bot 개인화(차량 ID 자동입력)에 사용")
def set_my_owned_vehicle(req: SetOwnedVehicleRequest, user: dict = Depends(get_current_user)) -> dict:
    """로그인 계정에 차량 ID를 등록해두면, 대시보드가 EV Bot 질문마다 `ev_id`를 매번
    직접 입력하지 않아도 자동으로 채워준다. 지금은 소유권 검증 없이 본인이 자유롭게
    등록하는 셀프서비스 필드다(PoC 범위) — 실제 서비스에서는 차량 등록 시 소유권
    확인 절차가 필요하다."""
    if user["role"] == "owner":
        # 소유권 검증이 없는 셀프서비스 필드라, owner가 임의 차량으로 바꾸면 남의 데이터를 보게 된다.
        raise HTTPException(status_code=403, detail="차량 연결 변경은 관리자에게 요청하세요")
    store.set_owned_vehicle(user["username"], req.owned_ev_id)
    return {"status": "ok", "owned_ev_id": req.owned_ev_id}


@router.get("/users", summary="계정 목록 (최고관리자 전용)")
def get_users(_: dict = Depends(require_super_admin)) -> list:
    return store.list_users()


@router.post("/users", summary="계정 생성 (최고관리자 전용)")
def add_user(req: CreateUserRequest, user: dict = Depends(require_super_admin)) -> dict:
    try:
        store.create_user(
            req.username, req.password, req.role, created_by=user["username"],
            owned_ev_id=req.owned_ev_id.strip() if req.owned_ev_id else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "username": req.username, "role": req.role, "owned_ev_id": req.owned_ev_id}


@router.delete("/users/{username}", summary="계정 삭제 (최고관리자 전용)")
def remove_user(username: str, user: dict = Depends(require_super_admin)) -> dict:
    if username == user["username"]:
        raise HTTPException(status_code=422, detail="자기 자신의 계정은 삭제할 수 없습니다")
    try:
        store.delete_user(username)
    except PermissionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}
