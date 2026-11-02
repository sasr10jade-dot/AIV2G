"""개발용 간편 로그인 계정 생성 — .env의 DEMO_ADMIN_* / DEMO_OWNER_* 값으로 계정을 만든다.

비밀번호는 코드에 두지 않고 .env에서만 읽는다(.env는 git 제외). 이미 있는 계정은 건드리지 않는다.
  python scripts/create_demo_accounts.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src.auth import store  # noqa: E402


def ensure(role: str, user_key: str, pass_key: str, owned_ev_id: str | None = None) -> None:
    username, password = os.environ.get(user_key), os.environ.get(pass_key)
    if not username or not password:
        print(f"[건너뜀] {role}: .env에 {user_key}/{pass_key}가 없습니다")
        return
    if store.get_user(username):
        print(f"[유지] {role}: '{username}' 계정이 이미 있습니다(변경하지 않음)")
        return
    try:
        store.create_user(username, password, role, created_by="create_demo_accounts", owned_ev_id=owned_ev_id)
    except ValueError as exc:
        print(f"[실패] {role}: {exc}")
        return
    print(f"[생성] {role}: '{username}'" + (f" (차량 {owned_ev_id})" if owned_ev_id else ""))


if __name__ == "__main__":
    ensure("admin", "DEMO_ADMIN_USERNAME", "DEMO_ADMIN_PASSWORD")
    ensure("owner", "DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD", os.environ.get("DEMO_OWNER_EV_ID", "EV-003"))
