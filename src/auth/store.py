"""사용자 계정 저장소 — 로컬 JSON 파일(data/users.json). 비밀번호는 bcrypt로 해싱해 저장한다.

역할은 3종이다:
  - super_admin(최고관리자): 계정 생성·삭제 권한 + 일반 운영 권한 전체
  - admin(관리자): 일반 운영(시나리오 실행·에뮬레이터 조작·데이터 업로드 등) 권한만
  - owner(차량 등록자): 사용자 모바일웹 전용. 운영 API 권한은 없고, 본인에게 연결된 차량
    (owned_ev_id)의 정보만 /owner/* 에서 볼 수 있다. 계정은 최고관리자가 차량과 함께 발급한다.

최초 super_admin 계정은 서버 기동 시 `.env`의 SUPERADMIN_USERNAME/SUPERADMIN_PASSWORD로
자동 프로비저닝된다(계정이 하나도 없을 때만) — 코드에 자격증명을 하드코딩하지 않기 위함.
"""
import json
import os
from datetime import datetime, timezone

import bcrypt

from src.paths import DATA_DIR

USERS_FILE = DATA_DIR / "users.json"
ROLES = ("admin", "super_admin", "owner")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict:
    if not USERS_FILE.exists():
        return {}
    return json.loads(USERS_FILE.read_text(encoding="utf-8"))


def _write(users: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def get_user(username: str) -> dict | None:
    return _read().get(username)


def list_users() -> list[dict]:
    return [
        {
            "username": u, "role": info["role"], "created_at": info["created_at"], "created_by": info.get("created_by"),
            "owned_ev_id": info.get("owned_ev_id"),
        }
        for u, info in sorted(_read().items())
    ]


def create_user(
    username: str, password: str, role: str, created_by: str | None = None, owned_ev_id: str | None = None,
    allow_unlinked_owner: bool = False,
) -> None:
    """allow_unlinked_owner: 모바일 가입처럼 차량 승인 전에 owner 계정을 먼저 만드는 경우에만 True.
    승인 전 owner는 owned_ev_id가 없어 차량 데이터(/owner/me 등)에 접근할 수 없다."""
    if role not in ROLES:
        raise ValueError(f"알 수 없는 역할입니다: {role} (admin, super_admin, owner 중 하나)")
    if role == "owner" and not owned_ev_id and not allow_unlinked_owner:
        raise ValueError("owner 계정은 연결할 차량(owned_ev_id)이 필요합니다")
    users = _read()
    if username in users:
        raise ValueError(f"이미 존재하는 계정입니다: {username}")
    users[username] = {
        "password_hash": hash_password(password),
        "role": role,
        "created_at": _now(),
        "created_by": created_by,
        # "내 차" 개인화(EV Bot 등)를 위한 계정-차량 매핑 — 없으면 매번 ev_id를
        # 직접 입력해야 한다. 지금은 실제 소유권 검증 없이 자기 자신이 자유롭게
        # 설정하는 셀프서비스 필드다(PoC 범위 — 실제 서비스라면 소유권 검증 필요).
        "owned_ev_id": owned_ev_id,
    }
    _write(users)


def set_owned_vehicle(username: str, ev_id: str | None) -> None:
    users = _read()
    if username not in users:
        raise ValueError(f"존재하지 않는 계정입니다: {username}")
    users[username]["owned_ev_id"] = ev_id
    _write(users)


def set_password(username: str, new_password: str) -> None:
    users = _read()
    if username not in users:
        raise ValueError(f"존재하지 않는 계정입니다: {username}")
    users[username]["password_hash"] = hash_password(new_password)
    _write(users)


def _count_super_admins(users: dict) -> int:
    return sum(1 for info in users.values() if info["role"] == "super_admin")


def delete_user(username: str) -> None:
    users = _read()
    if username not in users:
        raise ValueError(f"존재하지 않는 계정입니다: {username}")
    if users[username]["role"] == "super_admin" and _count_super_admins(users) <= 1:
        raise PermissionError("마지막 남은 최고관리자 계정은 삭제할 수 없습니다")
    del users[username]
    _write(users)


def has_any_user() -> bool:
    return bool(_read())


def ensure_bootstrap_super_admin() -> None:
    """계정이 하나도 없을 때만 .env의 SUPERADMIN_USERNAME/PASSWORD로 최초 최고관리자를
    만든다 — 기존 계정이 있으면 아무 것도 하지 않는다(덮어쓰기 방지)."""
    if has_any_user():
        return
    username = os.environ.get("SUPERADMIN_USERNAME")
    password = os.environ.get("SUPERADMIN_PASSWORD")
    if not username or not password:
        return
    create_user(username, password, "super_admin", created_by=None)
