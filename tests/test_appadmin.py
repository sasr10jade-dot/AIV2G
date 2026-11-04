"""웹앱 관리 — 서포터즈 공지사항, 앱 안내 문구, 권한."""
import pytest
from fastapi.testclient import TestClient

from src.advisory import owner_guide as og
from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store

client = TestClient(app)
PAYLOAD = {"username": "admindriver", "password": "driver-pass-1", "vehicle_model": "v2g_demo", "plate": "43오3450",
           "occupation": "office", "sido": "광주광역시", "sigungu": "북구", "consent": True}


def _login(user, pw):
    assert client.post("/auth/login", json={"username": user, "password": pw}).status_code == 200


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("opuser", "op-password-1", "admin")
    signup_router.reset_rate_limit()
    og.reset_caches()
    assert client.post("/signup", json=PAYLOAD).status_code == 200
    _login("opuser", "op-password-1")
    reg = client.get("/registrations").json()["registrations"][0]["registration_id"]
    assert client.post(f"/registrations/{reg}/approve", json={}).status_code == 200
    yield
    client.post("/auth/logout")


def test_notices_admin_crud_and_owner_sees_published_pinned_first(env):
    a = client.post("/admin/notices", json={"title": "일반 공지", "body": "내용1"}).json()["id"]
    client.post("/admin/notices", json={"title": "비공개", "body": "내용2", "published": False})
    b = client.post("/admin/notices", json={"title": "고정 공지", "body": "내용3", "pinned": True}).json()["id"]
    assert len(client.get("/admin/notices").json()["notices"]) == 3
    assert client.put(f"/admin/notices/{a}", json={"title": "일반 공지(수정)", "body": "내용1"}).status_code == 200
    assert client.post("/admin/notices", json={"title": " ", "body": "x"}).status_code in (422,)
    _login("admindriver", "driver-pass-1")
    titles = [n["title"] for n in client.get("/owner/notices").json()["notices"]]
    assert titles == ["고정 공지", "일반 공지(수정)"]                      # 비공개 제외, 고정이 먼저
    assert client.post("/admin/notices", json={"title": "x", "body": "y"}).status_code == 403   # 이용자는 관리 불가
    _login("opuser", "op-password-1")
    assert client.delete(f"/admin/notices/{b}").status_code == 200
    assert client.delete(f"/admin/notices/{b}").status_code == 422


def test_app_content_whitelist_override_and_reset(env):
    assert client.put("/admin/app-content/not_a_key", json={"ko": "x"}).status_code == 422
    assert client.put("/admin/app-content/home_intro", json={"ko": "새 첫 문장", "en": "New lead"}).status_code == 200
    _login("admindriver", "driver-pass-1")
    assert client.get("/owner/app-content").json()["overrides"]["home_intro"] == {"ko": "새 첫 문장", "en": "New lead"}
    _login("opuser", "op-password-1")
    assert client.delete("/admin/app-content/home_intro").status_code == 200
    assert client.get("/admin/app-content").json()["overrides"] == {}


def test_english_notice_shown_only_when_both_fields_exist(env):
    # 영어 제목·내용은 함께여야 하고, 영어 앱에서만 영어로 보인다
    assert client.post("/admin/notices", json={"title": "공지", "body": "내용", "title_en": "Only title"}).status_code == 422
    assert client.post("/admin/notices", json={"title": "공지", "body": "내용", "title_en": "Notice", "body_en": "Body"}).status_code == 200
    client.post("/admin/notices", json={"title": "한국어만", "body": "내용2"})
    _login("admindriver", "driver-pass-1")
    ko = {n["title"] for n in client.get("/owner/notices").json()["notices"]}
    en = {n["title"] for n in client.get("/owner/notices", params={"lang": "en"}).json()["notices"]}
    assert ko == {"공지", "한국어만"} and en == {"Notice", "한국어만"}          # 영어가 없으면 원문
