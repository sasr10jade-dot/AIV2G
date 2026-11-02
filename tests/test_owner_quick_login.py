"""사용자 모바일웹 개발용 간편 로그인 — .env 설정 기반, 비밀번호는 서버에서만 다룬다."""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
DEMO_ENV = ("DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD", "DEMO_ADMIN_USERNAME", "DEMO_ADMIN_PASSWORD", "DEMO_OWNER_EV_ID")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "USERS_FILE", tmp_path / "users.json")
    for key in DEMO_ENV:  # 개발자 .env에 실제 값이 있어도 테스트는 영향받지 않게
        monkeypatch.delenv(key, raising=False)
    yield
    client.post("/auth/logout")


def _enable_owner(monkeypatch, user="demo-owner", password="demo-owner-pw1"):
    monkeypatch.setenv("DEMO_OWNER_USERNAME", user)
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", password)
    store.create_user(user, password, "owner", owned_ev_id="EV-001")


def test_available_is_false_when_not_configured():
    r = client.get("/auth/quick-login/available")
    assert r.status_code == 200
    assert r.json()["owner"] is False


def test_available_needs_both_username_and_password(monkeypatch):
    monkeypatch.setenv("DEMO_OWNER_USERNAME", "demo-owner")
    assert client.get("/auth/quick-login/available").json()["owner"] is False
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "demo-owner-pw1")
    assert client.get("/auth/quick-login/available").json()["owner"] is True


def test_available_exposes_only_booleans(monkeypatch):
    _enable_owner(monkeypatch)
    body = client.get("/auth/quick-login/available").json()
    assert set(body) == {"super_admin", "admin", "owner"}
    assert all(isinstance(v, bool) for v in body.values())
    assert "demo-owner" not in client.get("/auth/quick-login/available").text


def test_owner_quick_login_opens_owner_session(monkeypatch):
    _enable_owner(monkeypatch)
    r = client.post("/auth/quick-login", json={"role": "owner"})
    assert r.status_code == 200
    assert r.json() == {"username": "demo-owner", "role": "owner"}
    assert "demo-owner-pw1" not in r.text           # 비밀번호는 응답에 없다
    assert client.get("/owner/me").json()["ev_id"] == "EV-001"


def test_owner_quick_login_without_config_is_503():
    r = client.post("/auth/quick-login", json={"role": "owner"})
    assert r.status_code == 503 and "DEMO_OWNER_USERNAME" in r.json()["detail"]


def test_owner_quick_login_rejects_mismatched_account(monkeypatch):
    monkeypatch.setenv("DEMO_OWNER_USERNAME", "demo-owner")
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "wrong-password-1")
    store.create_user("demo-owner", "real-password-1", "owner", owned_ev_id="EV-001")
    assert client.post("/auth/quick-login", json={"role": "owner"}).status_code == 401
    assert client.get("/owner/me").status_code == 401   # 세션이 만들어지지 않았다


def test_quick_login_cannot_escalate_role(monkeypatch):
    """owner 간편 로그인으로 만든 세션은 운영 API에 닿지 못한다."""
    _enable_owner(monkeypatch)
    client.post("/auth/quick-login", json={"role": "owner"})
    assert client.get("/auth/users").status_code == 403
    assert client.get("/dashboard/data").status_code == 403


def test_unknown_role_still_rejected():
    assert client.post("/auth/quick-login", json={"role": "bogus"}).status_code == 422


def test_mobile_page_has_dev_login_but_no_secrets():
    html = client.get("/app").text
    assert "개발용 간편 로그인" in html and "/auth/quick-login/available" in html
    assert "DEMO_OWNER" not in html and "driver-pass" not in html


def _load_script():
    spec = importlib.util.spec_from_file_location("create_demo_accounts", ROOT / "scripts" / "create_demo_accounts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_account_script_creates_owner_once(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("DEMO_OWNER_USERNAME", "demo-owner")
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "demo-owner-pw1")
    monkeypatch.setenv("DEMO_OWNER_EV_ID", "EV-007")
    mod.ensure("owner", "DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD", "EV-007")
    user = store.get_user("demo-owner")
    assert user["role"] == "owner" and user["owned_ev_id"] == "EV-007"
    first_hash = user["password_hash"]
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "changed-password-2")
    mod.ensure("owner", "DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD", "EV-007")
    assert store.get_user("demo-owner")["password_hash"] == first_hash   # 기존 계정은 덮어쓰지 않는다
    assert "이미 있습니다" in capsys.readouterr().out


def test_demo_account_script_skips_when_env_missing(capsys):
    mod = _load_script()
    mod.ensure("owner", "DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD", "EV-001")
    assert store.get_user("demo-owner") is None
    assert "건너뜀" in capsys.readouterr().out
