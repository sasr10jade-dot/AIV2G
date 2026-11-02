"""정보주체 권리(마이페이지) — 열람·정정·비밀번호 변경·탈퇴(삭제)와 삭제 범위."""
import json

import pytest
from fastapi.testclient import TestClient

from src.advisory import service as advisory
from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store
from src.pipeline import store
from src.privacy import audit, consent, member_history

client = TestClient(app)

PAYLOAD = {
    "username": "newdriver", "password": "driver-pass-1", "vehicle_model": "ioniq5", "plate": "43오3450",
    "occupation": "office", "sido": "광주광역시", "sigungu": "북구", "consent": True,
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("opuser", "op-password-1", "admin")
    signup_router.reset_rate_limit()
    yield
    client.post("/auth/logout")


def _login(username, password):
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200


def _signup(**over):
    r = client.post("/signup", json={**PAYLOAD, **over})
    assert r.status_code == 200, r.text
    return r.json()["registration_id"]


def _approve(reg_id=None, ev_id=None):
    _login("opuser", "op-password-1")
    reg_id = reg_id or client.get("/registrations", params={"status": "pending"}).json()["registrations"][0]["registration_id"]
    r = client.post(f"/registrations/{reg_id}/approve", json={"ev_id": ev_id} if ev_id else {})
    assert r.status_code == 200, r.text
    return r.json()["ev_id"]


def _count(sql, *args):
    con = store.get_connection()
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def _populated_member(ev_id=None):
    """가입→승인→방전 참여 동의→추천 1건까지 갖춘 회원. ev_id 반환."""
    _signup()
    ev = _approve(ev_id=ev_id)
    _login("newdriver", "driver-pass-1")
    assert client.put("/owner/consent/v2g", json={"granted": True}).json() == {"v2g": True}
    advisory.issue(ev, {"net_krw": 100, "assumptions": {"verified": False}}, channel="app")
    return ev


# --------------------------------------------------------------- 열람 --

def test_my_data_for_pending_member_has_no_vehicle_and_masked_plate():
    _signup()
    d = client.get("/owner/my-data").json()
    assert d["account"]["username"] == "newdriver" and d["vehicle"]["ev_id"] is None
    assert d["registration"]["plate"] == "43오**50" and d["registration"]["status"] == "pending"
    assert d["consents"] == [] and "notifications" not in d
    assert d["history_count"] == 1 and "43오3450" not in json.dumps(d, ensure_ascii=False)
    assert "password" not in json.dumps(d)


def test_my_data_for_approved_member_lists_consents():
    ev = _populated_member()
    d = client.get("/owner/my-data").json()
    assert d["vehicle"]["ev_id"] == ev
    purposes = {c["purpose"] for c in d["consents"]}
    assert {"profile", "v2g_discharge"} <= purposes
    assert d["retention"]


def test_my_data_requires_owner_account():
    assert client.get("/owner/my-data").status_code == 401
    _login("opuser", "op-password-1")
    assert client.get("/owner/my-data").status_code == 403


# --------------------------------------------------------------- 정정 --

def test_profile_update_changes_fields_and_records_history():
    _signup()
    r = client.patch("/owner/profile", json={"occupation": "student", "sigungu": "광산구"})
    assert r.status_code == 200 and set(r.json()["changed"]) == {"occupation", "region_sigungu"}
    reg = client.get("/owner/registration").json()["registration"]
    assert reg["occupation"] == "student" and reg["region_sigungu"] == "광산구"
    top = client.get("/owner/history").json()["history"][0]
    assert top["action"] == "profile_update" and top["by"] == "본인"
    changes = {c["field"]: (c["before"], c["after"]) for c in top["changes"]}
    assert changes == {"occupation": ("office", "student"), "region_sigungu": ("북구", "광산구")}


def test_signup_and_profile_update_keep_supporter_operating_inputs():
    """계획서 4.1 서포터즈 입력(출발 예정 시각·목표/최소 보장 배터리·누적 주행거리)은 가입·정정 모두 받는다."""
    _signup(departure_time="07:30", target_soc_pct=85, min_soc_pct=30, odometer_km=12000)
    reg = client.get("/owner/registration").json()["registration"]
    assert (reg["departure_time"], reg["target_soc_pct"], reg["min_soc_pct"], reg["odometer_km"]) == ("07:30", 85, 30, 12000)
    r = client.patch("/owner/profile", json={"departure_time": "08:10", "odometer_km": 12500})
    assert r.status_code == 200 and set(r.json()["changed"]) == {"departure_time", "odometer_km"}
    assert client.patch("/owner/profile", json={"min_soc_pct": 90}).status_code == 422   # 최소 보장 ≥ 목표는 거부
    assert client.patch("/owner/profile", json={"departure_time": "25:00"}).status_code == 422


def test_signup_rejects_min_soc_not_below_target():
    r = client.post("/signup", json={**PAYLOAD, "target_soc_pct": 50, "min_soc_pct": 60})
    assert r.status_code == 422 and auth_store.get_user("newdriver") is None   # 계정도 남기지 않는다


def test_profile_update_noop_leaves_no_history():
    _signup()
    assert client.patch("/owner/profile", json={"occupation": "office"}).json() == {"changed": []}
    assert [i["action"] for i in client.get("/owner/history").json()["history"]] == ["signup"]


@pytest.mark.parametrize("body", [
    {}, {"plate": "12가3456"}, {"occupation": "astronaut"}, {"sido": "광주"}, {"sigungu": "용봉동"}, {"vehicle_model": "nope"},
])
def test_profile_update_validation_and_plate_is_not_editable(body):
    _signup()
    assert client.patch("/owner/profile", json=body).status_code == 422
    assert client.get("/owner/registration").json()["registration"]["plate"] == "43오**50"


def test_profile_update_not_allowed_after_rejection():
    _signup()
    _login("opuser", "op-password-1")
    reg = client.get("/registrations").json()["registrations"][0]["registration_id"]
    client.post(f"/registrations/{reg}/reject", json={"reason": "번호판 확인 불가"})
    _login("newdriver", "driver-pass-1")
    r = client.patch("/owner/profile", json={"occupation": "student"})
    assert r.status_code == 422 and "다시 신청" in r.json()["detail"]


# ------------------------------------------------------------ 비밀번호 --

def test_password_change_flow_and_history_never_stores_passwords():
    _signup()
    assert client.post("/owner/password", json={"current_password": "wrong-pass-9", "new_password": "brand-new-pass-2"}).status_code == 403
    assert client.post("/owner/password", json={"current_password": "driver-pass-1", "new_password": "weakpass"}).status_code == 422
    assert client.post("/owner/password", json={"current_password": "driver-pass-1", "new_password": "driver-pass-1"}).status_code == 422
    assert client.post("/owner/password", json={"current_password": "driver-pass-1", "new_password": "brand-new-pass-2"}).status_code == 200
    client.post("/auth/logout")
    assert client.post("/auth/login", json={"username": "newdriver", "password": "driver-pass-1"}).status_code == 401
    _login("newdriver", "brand-new-pass-2")
    top = client.get("/owner/history").json()["history"][0]
    assert top["changes"][0]["field"] == "password" and top["changes"][0]["after"] == "(변경됨)"
    raw = json.dumps(audit.recent(100), ensure_ascii=False)
    assert "driver-pass-1" not in raw and "brand-new-pass-2" not in raw


# --------------------------------------------------------------- 탈퇴 --

def test_withdraw_requires_password_and_confirmation():
    _signup()
    assert client.post("/owner/account/withdraw", json={"password": "wrong-pass-9", "confirm": True}).status_code == 403
    assert client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": False}).status_code == 422
    assert auth_store.get_user("newdriver") is not None


def test_withdraw_deletes_personal_data_for_app_registered_vehicle():
    ev = _populated_member()
    assert ev.startswith("EV-U")
    r = client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": True})
    assert r.status_code == 200
    deleted = r.json()["deleted"]
    assert deleted["registrations"] == 1 and deleted["advisories"] == 1
    assert deleted["consents_withdrawn"] >= 2
    # 계정·등록·추천이 모두 사라졌다
    assert auth_store.get_user("newdriver") is None
    assert _count("SELECT COUNT(*) FROM vehicle_registrations WHERE username=?", "newdriver") == 0
    assert _count("SELECT COUNT(*) FROM advisories WHERE ev_id=?", ev) == 0
    # 세션이 끊기고 다시 로그인할 수 없다
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/login", json={"username": "newdriver", "password": "driver-pass-1"}).status_code == 401
    # 유효한 동의는 철회 처리, 동의 이력 행은 증빙으로 남는다
    rows = consent.list_consents(ev)
    assert rows and all(c["withdrawn_at"] for c in rows)
    assert not consent.has_consent(ev, "v2g_discharge")


def test_withdraw_keeps_access_log_and_records_withdrawal():
    _populated_member()
    client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": True})
    entries = audit.entries("member.", target="newdriver", limit=50)
    actions = {e["action"] for e in entries}
    assert {"member.signup", "member.withdraw"} <= actions       # 접속기록·이력은 보존
    assert audit.verify_chain()["ok"] is True
    _login("opuser", "op-password-1")
    hist = client.get("/registrations/member/newdriver/history").json()["history"]
    assert hist[0]["action"] == "withdraw" and "삭제" in hist[0]["note"]


def test_withdraw_keeps_linked_obd_vehicle_data_but_unlinks_person():
    """기존 OBD 실증 차량(EV-TORRES-…)은 차량 데이터·추천 기록을 유지하고 사람과의 연결만 끊는다."""
    ev = _populated_member(ev_id="EV-TORRES-01")
    assert ev == "EV-TORRES-01"
    client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": True})
    assert _count("SELECT COUNT(*) FROM advisories WHERE ev_id=?", ev) == 1      # 실증 데이터 유지
    assert _count("SELECT COUNT(*) FROM vehicle_registrations WHERE ev_id=?", ev) == 0
    assert all(u["owned_ev_id"] != ev for u in auth_store.list_users() if u["role"] == "owner")


def test_withdraw_pending_member_without_vehicle():
    _signup()
    r = client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": True})
    assert r.status_code == 200 and r.json()["deleted"]["registrations"] == 1
    assert auth_store.get_user("newdriver") is None


def test_plate_can_be_registered_again_after_withdrawal():
    _signup()
    client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": True})
    assert client.post("/signup", json={**PAYLOAD, "username": "another1"}).status_code == 200


def test_withdraw_does_not_touch_other_members():
    _populated_member()
    client.post("/auth/logout")
    _signup(username="second1", plate="12가3456", sigungu="광산구")
    _approve()
    _login("second1", "driver-pass-1")
    client.put("/owner/consent/v2g", json={"granted": True})
    ev2 = client.get("/owner/me").json()["ev_id"]
    advisory.issue(ev2, {"net_krw": 50, "assumptions": {"verified": False}}, channel="app")
    _login("newdriver", "driver-pass-1")
    client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": True})
    assert auth_store.get_user("second1") is not None
    assert _count("SELECT COUNT(*) FROM vehicle_registrations WHERE username=?", "second1") == 1
    assert _count("SELECT COUNT(*) FROM advisories WHERE ev_id=?", ev2) == 1
    assert consent.has_consent(ev2, "v2g_discharge")


def test_withdraw_is_owner_only():
    assert client.post("/owner/account/withdraw", json={"password": "x", "confirm": True}).status_code == 401
    _login("opuser", "op-password-1")
    assert client.post("/owner/account/withdraw", json={"password": "op-password-1", "confirm": True}).status_code == 403
    assert auth_store.get_user("opuser") is not None


def test_member_history_helper_still_scoped_after_withdrawal():
    _signup()
    client.post("/owner/account/withdraw", json={"password": "driver-pass-1", "confirm": True})
    assert [i["action"] for i in member_history.timeline("newdriver")][0] == "withdraw"
