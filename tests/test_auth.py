"""계정관리·로그인 테스트 (최고관리자/관리자 2단계 권한).

`src.auth.store.USERS_FILE`을 매 테스트마다 임시 경로로 monkeypatch해서 실제 프로젝트의
`data/users.json`(실제 관리자 계정이 들어있음)을 절대 건드리지 않는다.
"""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_users_file(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "USERS_FILE", tmp_path / "users.json")
    yield


@pytest.fixture
def super_admin():
    store.create_user("root", "root-password-1", "super_admin")
    return "root", "root-password-1"


@pytest.fixture
def admin_user():
    store.create_user("opuser", "op-password-1", "admin")
    return "opuser", "op-password-1"


# --------------------------------------------------------------- store.py --

def test_create_and_verify_password_roundtrip():
    store.create_user("alice", "s3cret-pw", "admin")
    user = store.get_user("alice")
    assert user["role"] == "admin"
    assert store.verify_password("s3cret-pw", user["password_hash"])
    assert not store.verify_password("wrong-pw", user["password_hash"])


def test_create_user_rejects_duplicate_username():
    store.create_user("bob", "pw12345678", "admin")
    with pytest.raises(ValueError):
        store.create_user("bob", "different-pw", "admin")


def test_create_user_rejects_unknown_role():
    with pytest.raises(ValueError):
        store.create_user("carol", "pw12345678", "bogus")


def test_delete_user_removes_account():
    store.create_user("dave", "pw12345678", "admin")
    store.delete_user("dave")
    assert store.get_user("dave") is None


def test_delete_user_blocks_removing_last_super_admin():
    store.create_user("only-root", "pw12345678", "super_admin")
    with pytest.raises(PermissionError):
        store.delete_user("only-root")
    assert store.get_user("only-root") is not None  # 삭제되지 않고 그대로 남아있어야 함


def test_delete_user_allows_removing_one_of_several_super_admins():
    store.create_user("root-a", "pw12345678", "super_admin")
    store.create_user("root-b", "pw12345678", "super_admin")
    store.delete_user("root-b")  # 다른 최고관리자가 남아있으니 허용
    assert store.get_user("root-a") is not None
    assert store.get_user("root-b") is None


def test_bootstrap_skips_when_users_already_exist(monkeypatch):
    store.create_user("existing", "pw12345678", "admin")
    monkeypatch.setenv("SUPERADMIN_USERNAME", "shouldnotappear")
    monkeypatch.setenv("SUPERADMIN_PASSWORD", "shouldnotappear-pw")
    store.ensure_bootstrap_super_admin()
    assert store.get_user("shouldnotappear") is None


# -------------------------------------------------------------- API: auth --

def test_me_requires_login():
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_login_with_wrong_password_returns_401(admin_user):
    username, _ = admin_user
    r = client.post("/auth/login", json={"username": username, "password": "totally-wrong"})
    assert r.status_code == 401


def test_login_then_me_returns_user(admin_user):
    username, password = admin_user
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    assert r.json() == {"username": username, "role": "admin"}

    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == username

    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401


def test_admin_cannot_manage_accounts(admin_user):
    username, password = admin_user
    client.post("/auth/login", json={"username": username, "password": password})
    assert client.get("/auth/users").status_code == 403
    assert client.post("/auth/users", json={"username": "x", "password": "pw12345678", "role": "admin"}).status_code == 403
    client.post("/auth/logout")


def test_super_admin_can_create_and_delete_accounts(super_admin):
    username, password = super_admin
    client.post("/auth/login", json={"username": username, "password": password})

    r = client.post("/auth/users", json={"username": "newop", "password": "pw12345678", "role": "admin"})
    assert r.status_code == 200

    r = client.get("/auth/users")
    assert r.status_code == 200
    usernames = [u["username"] for u in r.json()]
    assert "newop" in usernames

    r = client.delete("/auth/users/newop")
    assert r.status_code == 200
    assert "newop" not in [u["username"] for u in client.get("/auth/users").json()]
    client.post("/auth/logout")


def test_super_admin_cannot_delete_self(super_admin):
    username, password = super_admin
    client.post("/auth/login", json={"username": username, "password": password})
    r = client.delete(f"/auth/users/{username}")
    assert r.status_code == 422
    client.post("/auth/logout")


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "ab", "password": "pw123456", "role": "admin"},  # 아이디 3자 미만
        {"username": "has space", "password": "pw123456", "role": "admin"},  # 허용 안 되는 문자
        {"username": "한글아이디", "password": "pw123456", "role": "admin"},  # 허용 안 되는 문자
        {"username": "shortpw", "password": "abc123", "role": "admin"},  # 비밀번호 8자 미만
        {"username": "noDigit", "password": "onlyletters", "role": "admin"},  # 숫자 없음
        {"username": "noLetter", "password": "12345678", "role": "admin"},  # 영문 없음
        {"username": "badrole", "password": "pw123456", "role": "bogus"},  # 알 수 없는 role
    ],
)
def test_create_user_rejects_invalid_payloads(super_admin, payload):
    username, password = super_admin
    client.post("/auth/login", json={"username": username, "password": password})
    r = client.post("/auth/users", json=payload)
    assert r.status_code == 422
    client.post("/auth/logout")


# ------------------------------------------------ API: protected routers --

def test_protected_endpoint_blocked_without_login():
    r = client.get("/models")
    assert r.status_code == 401


def test_protected_endpoint_allowed_after_admin_login(admin_user):
    username, password = admin_user
    client.post("/auth/login", json={"username": username, "password": password})
    r = client.get("/models")
    assert r.status_code == 200
    client.post("/auth/logout")


def test_dashboard_page_itself_stays_public():
    """대시보드 HTML 셸은 로그인 없이도 열려야 프런트 JS가 /auth/me로 리다이렉트 판단을 할 수 있다."""
    r = client.get("/dashboard")
    assert r.status_code == 200


# ------------------------------------------------------ API: quick-login --

def test_quick_login_super_admin_without_env_returns_503(monkeypatch):
    monkeypatch.delenv("SUPERADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)
    r = client.post("/auth/quick-login", json={"role": "super_admin"})
    assert r.status_code == 503


def test_quick_login_admin_without_env_returns_503(monkeypatch):
    monkeypatch.delenv("DEMO_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    r = client.post("/auth/quick-login", json={"role": "admin"})
    assert r.status_code == 503


def test_quick_login_super_admin_logs_in_when_env_matches_real_account(monkeypatch, super_admin):
    username, password = super_admin
    monkeypatch.setenv("SUPERADMIN_USERNAME", username)
    monkeypatch.setenv("SUPERADMIN_PASSWORD", password)
    r = client.post("/auth/quick-login", json={"role": "super_admin"})
    assert r.status_code == 200
    assert r.json() == {"username": username, "role": "super_admin"}

    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == username
    client.post("/auth/logout")


def test_quick_login_rejects_when_env_password_does_not_match_account(monkeypatch, admin_user):
    username, _real_password = admin_user
    monkeypatch.setenv("DEMO_ADMIN_USERNAME", username)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "not-the-real-password")
    r = client.post("/auth/quick-login", json={"role": "admin"})
    assert r.status_code == 401


def test_quick_login_rejects_unknown_role():
    r = client.post("/auth/quick-login", json={"role": "bogus"})
    assert r.status_code == 422


# --------------------------------------------------------- owned vehicle --

def test_set_owned_vehicle_updates_store():
    store.create_user("driver1", "pw1234567", "admin")
    store.set_owned_vehicle("driver1", "EV-007")
    assert store.get_user("driver1")["owned_ev_id"] == "EV-007"


def test_set_owned_vehicle_unknown_user_raises():
    with pytest.raises(ValueError):
        store.set_owned_vehicle("no-such-user", "EV-007")


def test_me_includes_owned_ev_id(admin_user):
    username, password = admin_user
    client.post("/auth/login", json={"username": username, "password": password})
    assert client.get("/auth/me").json()["owned_ev_id"] is None

    r = client.patch("/auth/me/owned-vehicle", json={"owned_ev_id": "EV-007"})
    assert r.status_code == 200
    assert client.get("/auth/me").json()["owned_ev_id"] == "EV-007"

    client.patch("/auth/me/owned-vehicle", json={"owned_ev_id": None})
    assert client.get("/auth/me").json()["owned_ev_id"] is None
    client.post("/auth/logout")


# ------------------------------------------------ 로그인 시도 제한 · 보안 헤더 --

def test_login_is_locked_after_repeated_failures_and_success_resets(monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_FAILS", "3")
    store.create_user("throttleuser", "Right-pass-123", "admin")
    for _ in range(2):
        assert client.post("/auth/login", json={"username": "throttleuser", "password": "wrong-pass-1"}).status_code == 401
    assert client.post("/auth/login", json={"username": "throttleuser", "password": "Right-pass-123"}).status_code == 200   # 성공하면 기록을 지운다
    for _ in range(3):
        client.post("/auth/login", json={"username": "throttleuser", "password": "wrong-pass-1"})
    r = client.post("/auth/login", json={"username": "throttleuser", "password": "Right-pass-123"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0                 # 잠금 중에는 맞는 비밀번호도 거절
    assert client.post("/auth/login", json={"username": "someone-else", "password": "x" * 9}).status_code == 401   # 다른 계정은 영향 없음


def test_security_headers_and_no_store_on_api_responses():
    r = client.get("/auth/me")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    page = client.get("/app")
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-cache"   # 화면은 기존 캐시 정책 유지


# ------------------------------------------------ 이용자 전용 데모 링크 --

def test_demo_access_link_logs_in_owner_only_with_the_secret_code(monkeypatch):
    store.create_user("demo-owner", "Owner-pass-1234", "owner", owned_ev_id="EV-003")
    store.create_user("demo-admin", "Admin-pass-1234", "admin")
    assert client.get("/auth/demo-access?code=abc", follow_redirects=False).status_code == 404        # 설정이 없으면 없는 경로처럼 보인다
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", "secret-code-0123456789")
    assert client.get("/auth/demo-access?code=wrong", follow_redirects=False).status_code == 404
    assert client.get("/auth/demo-access", follow_redirects=False).status_code == 404
    client.post("/auth/logout")
    r = client.get("/auth/demo-access?code=secret-code-0123456789", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    me = client.get("/auth/me").json()
    assert me["username"] == "demo-owner" and me["role"] == "owner"
    client.post("/auth/logout")
    monkeypatch.setenv("DEMO_ACCESS_OWNER", "demo-admin")                                              # 관리자 계정은 이 경로로 열리지 않는다
    assert client.get("/auth/demo-access?code=secret-code-0123456789", follow_redirects=False).status_code == 404
    assert client.get("/auth/me").status_code == 401


def test_demo_access_wrong_codes_get_locked(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", "secret-code-0123456789")
    monkeypatch.setenv("LOGIN_MAX_FAILS", "3")
    codes = [client.get("/auth/demo-access?code=nope", follow_redirects=False).status_code for _ in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]


def test_throttle_tracking_stays_bounded_when_many_unknown_usernames_are_tried(monkeypatch):
    from src.auth import throttle

    monkeypatch.setattr(throttle, "MAX_TRACKED_KEYS", 50)
    monkeypatch.setenv("LOGIN_WINDOW_S", "0")          # 창이 0초라 이전 실패는 곧바로 만료로 취급된다
    for i in range(120):
        client.post("/auth/login", json={"username": f"ghost-{i}", "password": "wrong-pass-123"})
    assert len(throttle._fails) <= 60
