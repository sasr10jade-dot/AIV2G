"""이용자 알림함 테스트 — 충전시간 안내 및 혼잡 회피 알림 (계획서 B2C 서비스 목업)."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store

client = TestClient(app)


@pytest.fixture
def owner_env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("driver-notif", "pass1234", "owner", owned_ev_id="EV-003")
    r = client.post("/auth/login", json={"username": "driver-notif", "password": "pass1234"})
    assert r.status_code == 200


def test_list_notifications(owner_env):
    r = client.get("/owner/notifications")
    assert r.status_code == 200
    data = r.json()
    assert "notifications" in data
    assert data["total_count"] >= 3
    assert data["unread_count"] >= 1

    types = [n["type"] for n in data["notifications"]]
    assert "charge_window" in types
    assert "peak_avoid" in types


def test_mark_notification_read(owner_env):
    r = client.get("/owner/notifications")
    notifs = r.json()["notifications"]
    target_id = notifs[0]["id"]

    r_read = client.post(f"/owner/notifications/{target_id}/read")
    assert r_read.status_code == 200

    r_after = client.get("/owner/notifications")
    updated = next(n for n in r_after.json()["notifications"] if n["id"] == target_id)
    assert updated["read"] is True


def test_mark_all_read(owner_env):
    r_all = client.post("/owner/notifications/read-all")
    assert r_all.status_code == 200

    r_after = client.get("/owner/notifications")
    assert r_after.json()["unread_count"] == 0
