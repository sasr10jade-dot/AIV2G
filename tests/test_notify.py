"""차량 등록자 알림함 — 동의 게이트, 중복 방지, 차량 범위 격리, 읽음 처리."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.notify import service as notify
from src.privacy import consent

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_users_file(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("driver1", "driver-pass-1", "owner", owned_ev_id="EV-001")
    auth_store.create_user("driver2", "driver-pass-2", "owner", owned_ev_id="EV-002")
    auth_store.create_user("opuser", "op-password-1", "admin")
    yield
    client.post("/auth/logout")


def _login(username, password):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _owner(n=1):
    _login(f"driver{n}", f"driver-pass-{n}")


def _admin():
    _login("opuser", "op-password-1")


def _send(ev_id="EV-001", title="충전 알림", body="오늘 밤 12시~6시가 가장 쌉니다.", **extra):
    _admin()
    return client.post("/notify/send", json={"ev_id": ev_id, "title": title, "body": body, **extra})


# ------------------------------------------------------------- service --

def test_no_notification_without_consent():
    assert notify.create("EV-001", "system", "제목", "내용") is None
    assert notify.list_for("EV-001") == []


def test_created_after_consent_and_listed_newest_first():
    consent.grant("EV-001", "notify", "notify-v1")
    a = notify.create("EV-001", "system", "첫째", "내용 A")
    b = notify.create("EV-001", "price_window", "둘째", "내용 B", payload={"hours": [0, 1]})
    assert a and b and a != b
    items = notify.list_for("EV-001")
    assert [i["title"] for i in items] == ["둘째", "첫째"]
    assert items[0]["payload"] == {"hours": [0, 1]} and items[0]["read"] is False
    assert notify.unread_count("EV-001") == 2


def test_dedupe_key_prevents_repeats_per_vehicle():
    consent.grant("EV-001", "notify", "notify-v1")
    consent.grant("EV-002", "notify", "notify-v1")
    assert notify.create("EV-001", "price_window", "t", "b", dedupe_key="price:2026-10-23")
    assert notify.create("EV-001", "price_window", "t", "b", dedupe_key="price:2026-10-23") is None
    assert notify.create("EV-002", "price_window", "t", "b", dedupe_key="price:2026-10-23")  # 다른 차량은 별개
    assert notify.create("EV-001", "price_window", "t", "b", dedupe_key="price:2026-10-24")
    assert len(notify.list_for("EV-001")) == 2


def test_validation():
    consent.grant("EV-001", "notify", "notify-v1")
    with pytest.raises(notify.NotifyError):
        notify.create("EV-001", "bogus", "t", "b")
    with pytest.raises(notify.NotifyError):
        notify.create("EV-001", "system", "  ", "b")
    with pytest.raises(notify.NotifyError):
        notify.create("EV-001", "system", "t" * (notify.MAX_TITLE + 1), "b")
    with pytest.raises(notify.NotifyError):
        notify.create("EV-001", "system", "t", "b" * (notify.MAX_BODY + 1))


def test_mark_read_is_scoped_to_vehicle():
    consent.grant("EV-001", "notify", "notify-v1")
    nid = notify.create("EV-001", "system", "t", "b")
    assert notify.mark_read("EV-002", nid) is False       # 남의 알림은 읽음 처리 불가
    assert notify.unread_count("EV-001") == 1
    assert notify.mark_read("EV-001", nid) is True
    assert notify.mark_read("EV-001", nid) is False       # 이미 읽음
    assert notify.unread_count("EV-001") == 0


def test_withdraw_stops_new_notifications_but_keeps_old():
    consent.grant("EV-001", "notify", "notify-v1")
    notify.create("EV-001", "system", "받은 것", "b")
    consent.withdraw("EV-001", "notify")
    assert notify.create("EV-001", "system", "새것", "b") is None
    assert [i["title"] for i in notify.list_for("EV-001")] == ["받은 것"]


# ------------------------------------------------------------------ API --

def test_owner_consent_roundtrip_and_audit():
    _owner()
    assert client.get("/owner/consent").json()["notify"] is False
    assert client.put("/owner/consent/notify", json={"granted": True}).json() == {"notify": True}
    assert client.get("/owner/consent").json()["notify"] is True
    assert client.put("/owner/consent/notify", json={"granted": False}).json() == {"notify": False}
    rec = [c for c in consent.list_consents("EV-001") if c["purpose"] == "notify"]
    assert len(rec) == 1 and rec[0]["recorded_by"] == "driver1" and rec[0]["withdrawn_at"]


def test_owner_consent_only_affects_own_vehicle():
    _owner(1)
    client.put("/owner/consent/notify", json={"granted": True})
    assert consent.has_consent("EV-001", "notify") and not consent.has_consent("EV-002", "notify")


def test_admin_send_respects_consent():
    r = _send()
    assert r.status_code == 200 and r.json() == {"sent": False, "notification_id": None}
    _owner()
    client.put("/owner/consent/notify", json={"granted": True})
    r = _send()
    assert r.json()["sent"] is True


def test_end_to_end_inbox_flow():
    _owner(1)
    client.put("/owner/consent/notify", json={"granted": True})
    nid = _send(title="충전 시간 안내", body="내일 00~06시 요금이 가장 낮아요.").json()["notification_id"]
    _owner(1)
    inbox = client.get("/owner/notifications").json()
    assert inbox["unread"] == 1 and inbox["notify_consent"] is True
    assert inbox["notifications"][0]["title"] == "충전 시간 안내" and inbox["notifications"][0]["read"] is False
    assert client.post(f"/owner/notifications/{nid}/read").status_code == 200
    assert client.post(f"/owner/notifications/{nid}/read").status_code == 404   # 이미 읽음
    assert client.get("/owner/notifications").json()["unread"] == 0
    assert client.get("/owner/notifications", params={"unread_only": True}).json()["notifications"] == []


def test_owner_cannot_see_or_read_other_vehicles_notifications():
    _owner(1)
    client.put("/owner/consent/notify", json={"granted": True})
    nid = _send("EV-001", title="1번 차량 전용").json()["notification_id"]
    _owner(2)
    assert client.get("/owner/notifications").json()["notifications"] == []
    assert client.post(f"/owner/notifications/{nid}/read").status_code == 404
    _owner(1)
    assert client.get("/owner/notifications").json()["unread"] == 1   # 2번이 읽음 처리하지 못했다


def test_read_all_marks_only_own():
    for n in (1, 2):
        _owner(n)
        client.put("/owner/consent/notify", json={"granted": True})
    _send("EV-001", title="a"); _send("EV-001", title="b"); _send("EV-002", title="c")
    _owner(1)
    assert client.post("/owner/notifications/read-all").json() == {"marked": 2}
    _owner(2)
    assert client.get("/owner/notifications").json()["unread"] == 1


def test_send_endpoint_is_admin_only_and_validates():
    _owner()
    assert client.post("/notify/send", json={"ev_id": "EV-001", "title": "t", "body": "b"}).status_code == 403
    client.post("/auth/logout")
    assert client.post("/notify/send", json={"ev_id": "EV-001", "title": "t", "body": "b"}).status_code == 401
    _admin()
    assert client.post("/notify/send", json={"ev_id": "EV-001", "kind": "bogus", "title": "t", "body": "b"}).status_code in (200, 422)
    r = client.post("/notify/send", json={"ev_id": "EV-001", "kind": "bogus", "title": "t", "body": "b"})
    # 동의가 없으면 kind 검증 전에 막히지 않는다 — 잘못된 kind는 항상 422여야 한다
    assert r.status_code == 422
