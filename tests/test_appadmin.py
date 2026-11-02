"""웹앱 관리 — 공지사항, 앱 문구, 상품권 교환·코드 등록, 권한과 감사로그."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.advisory import owner_guide as og
from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store
from src.copilot import owner_bot
from src.privacy import audit
from src.rewards import service as rewards

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
    owner_bot.reset_rate_limit()
    og.reset_caches()
    assert client.post("/signup", json=PAYLOAD).status_code == 200
    _login("opuser", "op-password-1")
    reg = client.get("/registrations").json()["registrations"][0]["registration_id"]
    assert client.post(f"/registrations/{reg}/approve", json={}).status_code == 200
    yield
    client.post("/auth/logout")


def _give_points(ev_id, points):
    con = rewards._connect()
    try:
        con.execute("INSERT INTO point_ledger (ev_id, mission_id, period_key, points, created_at) VALUES (?,?,?,?,?)",
                    (ev_id, "test_grant", f"p{points}-{datetime.now().timestamp()}", points, datetime.now(timezone.utc).isoformat()))
        con.commit()
    finally:
        con.close()


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
    assert client.put("/admin/app-content/ms_intro_lead", json={"ko": "새 첫 문장", "en": "New lead"}).status_code == 200
    _login("admindriver", "driver-pass-1")
    assert client.get("/owner/app-content").json()["overrides"]["ms_intro_lead"] == {"ko": "새 첫 문장", "en": "New lead"}
    _login("opuser", "op-password-1")
    assert client.delete("/admin/app-content/ms_intro_lead").status_code == 200
    assert client.get("/admin/app-content").json()["overrides"] == {}


def test_redemption_flow_code_is_encrypted_and_only_owner_sees_it(env):
    _login("admindriver", "driver-pass-1")
    ev = client.get("/owner/me").json()["ev_id"]
    assert client.post("/owner/points/redeem").status_code == 409            # 1,000P 미만
    _give_points(ev, 1000)
    assert client.get("/owner/coupons").json()["redeem"]["can_request"] is True
    rid = client.post("/owner/points/redeem").json()["id"]
    state = client.get("/owner/coupons").json()["redeem"]
    assert state["balance"] == 0 and state["pending"] == 1 and state["can_request"] is False   # 신청 즉시 차감
    assert client.post("/owner/points/redeem").status_code == 409            # 처리 중에는 중복 신청 불가

    _login("opuser", "op-password-1")
    listed = client.get("/admin/redemptions", params={"status": "requested"}).json()["redemptions"]
    assert [r["id"] for r in listed] == [rid] and "code" not in listed[0]
    assert client.post(f"/admin/redemptions/{rid}/fulfill", json={"code": "GIFT-SECRET-1234"}).status_code == 200
    assert client.post(f"/admin/redemptions/{rid}/fulfill", json={"code": "OTHER"}).status_code == 422   # 이미 처리됨
    assert "GIFT-SECRET-1234" not in json.dumps(client.get("/admin/redemptions").json())

    _login("admindriver", "driver-pass-1")
    c = client.get("/owner/coupons").json()["coupons"][0]
    assert c["status"] == "fulfilled" and c["code"] == "GIFT-SECRET-1234" and c["label"] == "문화상품권"

    con = audit._connect()
    try:
        dump = json.dumps(con.execute("SELECT detail, target FROM audit_log").fetchall())
        stored = con.execute("SELECT code_enc FROM redemptions").fetchone()[0]
    finally:
        con.close()
    assert "GIFT-SECRET-1234" not in dump and "GIFT-SECRET-1234" not in stored and stored.startswith("v1:")


def test_rejected_redemption_refunds_points_and_shows_reason(env):
    _login("admindriver", "driver-pass-1")
    ev = client.get("/owner/me").json()["ev_id"]
    _give_points(ev, 1000)
    rid = client.post("/owner/points/redeem").json()["id"]
    _login("opuser", "op-password-1")
    assert client.post(f"/admin/redemptions/{rid}/reject", json={"reason": "x"}).status_code == 422   # 사유 2자 이상
    assert client.post(f"/admin/redemptions/{rid}/reject", json={"reason": "재고 부족"}).status_code == 200
    _login("admindriver", "driver-pass-1")
    body = client.get("/owner/coupons").json()
    assert body["redeem"]["balance"] == 1000 and body["coupons"][0]["status"] == "rejected" and body["coupons"][0]["reason"] == "재고 부족"
    assert client.get("/owner/missions").json()["points"]["month_earned"] == 1000   # 지급분만 센다 — 환급·차감은 월 적립에 들어가지 않는다


def test_redemption_can_be_disabled(env, monkeypatch):
    from src.appadmin import service
    monkeypatch.setattr(service, "reward_config", lambda: {"redeem_enabled": False, "threshold_points": 1000, "label": "문화상품권"})
    _login("admindriver", "driver-pass-1")
    _give_points(client.get("/owner/me").json()["ev_id"], 1000)
    assert client.post("/owner/points/redeem").status_code == 409
    assert client.get("/owner/coupons").json()["redeem"]["can_request"] is False


def test_english_notice_notification_and_coupon_label(env):
    from src.notify import service as notify
    # 영어 제목·내용은 함께여야 하고, 영어 앱에서만 영어로 보인다
    assert client.post("/admin/notices", json={"title": "공지", "body": "내용", "title_en": "Only title"}).status_code == 422
    assert client.post("/admin/notices", json={"title": "공지", "body": "내용", "title_en": "Notice", "body_en": "Body"}).status_code == 200
    client.post("/admin/notices", json={"title": "한국어만", "body": "내용2"})
    _login("admindriver", "driver-pass-1")
    ev = client.get("/owner/me").json()["ev_id"]
    ko = {n["title"] for n in client.get("/owner/notices").json()["notices"]}
    en = {n["title"] for n in client.get("/owner/notices", params={"lang": "en"}).json()["notices"]}
    assert ko == {"공지", "한국어만"} and en == {"Notice", "한국어만"}          # 영어가 없으면 원문
    # 알림: payload.en 이 있으면 영어 앱에서 영어로
    client.put("/owner/consent/notify", json={"granted": True})
    notify.create(ev, "system", "한글 알림", "한글 본문", payload={"en": {"title": "English alert", "body": "English body"}})
    notify.create(ev, "system", "영어 없음", "본문만")
    en_list = {n["title"] for n in client.get("/owner/notifications", params={"lang": "en"}).json()["notifications"]}
    assert en_list == {"English alert", "영어 없음"}
    # 쿠폰 기본 이름
    _give_points(ev, 1000)
    rid = client.post("/owner/points/redeem").json()["id"]
    _login("opuser", "op-password-1")
    client.post(f"/admin/redemptions/{rid}/fulfill", json={"code": "C-1"})
    _login("admindriver", "driver-pass-1")
    assert client.get("/owner/coupons", params={"lang": "en"}).json()["coupons"][0]["label"] == "Cultural gift certificate"
    assert client.get("/owner/coupons").json()["coupons"][0]["label"] == "문화상품권"


def test_coupon_box_survives_an_undecryptable_code(env):
    from src.appadmin import service as appadmin
    from src.privacy import vault

    _login("admindriver", "driver-pass-1")
    ev = client.get("/owner/me").json()["ev_id"]
    _give_points(ev, 1000)
    rid = client.post("/owner/points/redeem").json()["id"]
    _login("opuser", "op-password-1")
    client.post(f"/admin/redemptions/{rid}/fulfill", json={"code": "C-9"})
    con = appadmin._connect()
    try:
        con.execute("UPDATE redemptions SET code_enc=? WHERE id=?", ("v1:not-a-valid-token", rid))
        con.commit()
    finally:
        con.close()
    _login("admindriver", "driver-pass-1")
    r = client.get("/owner/coupons")
    assert r.status_code == 200 and r.json()["coupons"][0]["code"] is None and r.json()["coupons"][0]["code_error"] is True
