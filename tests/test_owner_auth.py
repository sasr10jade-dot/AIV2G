"""차량 등록자(owner) 역할 — 계정 발급·접근 범위. 핵심은 owner가 운영 API와 남의 차량에 닿지 못한다는 것."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store

client = TestClient(app)

# owner가 2xx를 받아도 되는 엔드포인트 — 인증 없이 열린 공용 경로와 owner 전용 경로뿐
OWNER_ALLOWED = {
    ("GET", "/"), ("GET", "/login"), ("GET", "/health"),
    ("POST", "/auth/login"), ("POST", "/auth/quick-login"), ("POST", "/auth/logout"),
    ("GET", "/auth/me"), ("GET", "/owner/me"), ("GET", "/auth/quick-login/available"),
    # owner 전용 경로 — 모두 세션의 차량으로만 동작한다
    ("GET", "/signup/options"), ("GET", "/owner/registration"), ("GET", "/owner/history"),
    ("GET", "/owner/my-data"), ("PATCH", "/owner/profile"), ("POST", "/owner/password"),
    ("POST", "/owner/account/withdraw"),   # 마이페이지 — 본인 정보만(빈 본문은 422로 막힘)   # 가입 선택지(공개)·내 승인 상태(승인 전에도 가능)
    ("GET", "/owner/charging-guide"), ("GET", "/owner/demand-forecast"),   # 본인 차량·집계 예측만
    ("GET", "/owner/report"), ("POST", "/owner/odometer"),             # 내 충전 리포트·누적 주행거리 기록(본인 차량만)
    ("GET", "/owner/consent/v2g"), ("PUT", "/owner/consent/v2g"),   # 방전 참여 켜기·즉시 중지(본인 차량만)
    ("GET", "/owner/notices"), ("GET", "/owner/app-content"),       # 공지·문구(공통)
}


@pytest.fixture(autouse=True)
def isolated_users_file(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "USERS_FILE", tmp_path / "users.json")
    yield
    client.post("/auth/logout")


@pytest.fixture
def owner():
    store.create_user("driver1", "driver-pass-1", "owner", owned_ev_id="EV-001")
    return "driver1", "driver-pass-1"


def _login(username, password):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_owner_requires_vehicle():
    with pytest.raises(ValueError):
        store.create_user("novehicle", "pw12345678", "owner")


def test_super_admin_issues_owner_with_vehicle():
    store.create_user("root", "root-password-1", "super_admin")
    _login("root", "root-password-1")
    r = client.post("/auth/users", json={
        "username": "driver2", "password": "driver-pass-2", "role": "owner", "owned_ev_id": "EV-002"})
    assert r.status_code == 200, r.text
    assert store.get_user("driver2")["owned_ev_id"] == "EV-002"
    # 차량 없이 owner 발급은 거부
    r = client.post("/auth/users", json={"username": "driver3", "password": "driver-pass-3", "role": "owner"})
    assert r.status_code == 422


def test_admin_cannot_issue_owner_accounts():
    store.create_user("opuser", "op-password-1", "admin")
    _login("opuser", "op-password-1")
    r = client.post("/auth/users", json={
        "username": "driver4", "password": "driver-pass-4", "role": "owner", "owned_ev_id": "EV-004"})
    assert r.status_code == 403


def test_owner_me_returns_only_own_vehicle(owner):
    _login(*owner)
    r = client.get("/owner/me")
    assert r.status_code == 200
    assert r.json() == {"username": "driver1", "role": "owner", "ev_id": "EV-001"}


def test_owner_endpoint_rejects_anonymous_and_non_owner():
    assert client.get("/owner/me").status_code == 401
    store.create_user("opuser", "op-password-1", "admin")
    _login("opuser", "op-password-1")
    assert client.get("/owner/me").status_code == 403


def test_owner_cannot_change_linked_vehicle(owner):
    _login(*owner)
    r = client.patch("/auth/me/owned-vehicle", json={"owned_ev_id": "EV-999"})
    assert r.status_code == 403
    assert store.get_user("driver1")["owned_ev_id"] == "EV-001"


def test_owner_blocked_from_every_operational_endpoint(owner):
    """OpenAPI에 있는 모든 엔드포인트를 owner 세션으로 호출해, 허용 목록 밖에서는 2xx가 나오면 안 된다."""
    _login(*owner)
    leaks = []
    for path, item in app.openapi()["paths"].items():
        for method in item:
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            if (method.upper(), path) in OWNER_ALLOWED:
                continue
            url = path.replace("{", "x").replace("}", "")  # 경로 변수는 아무 값이나 — 권한 검사가 먼저 걸려야 한다
            r = client.request(method.upper(), url, json={})
            if r.status_code < 400:
                leaks.append((method.upper(), path, r.status_code))
    assert not leaks, f"owner가 접근 가능한 운영 엔드포인트: {leaks}"
