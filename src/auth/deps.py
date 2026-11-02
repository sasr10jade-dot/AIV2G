"""FastAPI 의존성 — 세션 쿠키에서 로그인 사용자를 조회하고 역할을 검사한다."""
import os

from fastapi import Depends, HTTPException, Request

from src.auth import store


def client_ip(request: Request) -> str | None:
    """접속기록에 남길 접속지(IP). 프록시 뒤에서는 프록시 주소가 찍히므로 운영 배치 시 확인 필요."""
    if os.environ.get("TRUST_PROXY_HEADERS") == "1":   # 터널·프록시 뒤에서만 켠다(헤더는 위조될 수 있다)
        forwarded = request.headers.get("cf-connecting-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else None


# owner는 0 — 운영 API(require_admin 이상)는 자동으로 막히고 /owner/* 에서만 쓴다.
ROLE_RANK = {"owner": 0, "admin": 1, "super_admin": 2}


def get_current_user(request: Request) -> dict:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    user = store.get_user(username)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return {"username": username, "role": user["role"], "owned_ev_id": user.get("owned_ev_id")}


def require_role(min_role: str):
    def _dep(user: dict = Depends(get_current_user)) -> dict:
        if ROLE_RANK.get(user["role"], 0) < ROLE_RANK.get(min_role, 999):
            raise HTTPException(status_code=403, detail="권한이 없습니다")
        return user

    _dep.__name__ = f"require_{min_role}"   # API 문서가 의존성 이름으로 필요한 권한을 표시한다(docs_meta)
    return _dep


def require_owner_account(user: dict = Depends(get_current_user)) -> dict:
    """차량 등록자 계정(승인 전 포함). 차량 데이터가 아니라 가입·승인 상태 확인에만 쓴다."""
    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="차량 등록자 계정이 필요합니다")
    return user


def require_owner(user: dict = Depends(get_current_user)) -> dict:
    """차량 등록자 전용. 반환 dict의 owned_ev_id는 항상 값이 있다 — /owner/* 는 요청 파라미터로
    차량을 받지 않고 이 값만 쓰므로, 다른 사람의 차량 데이터에는 접근할 방법이 없다."""
    if user["role"] != "owner" or not user.get("owned_ev_id"):
        raise HTTPException(status_code=403, detail="차량 등록자 계정이 필요합니다")
    return user


require_admin = require_role("admin")
require_super_admin = require_role("super_admin")
