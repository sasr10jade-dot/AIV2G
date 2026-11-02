"""회원정보 변경 이력 — 마스킹·화이트리스트·본인/관리자 시야 분리·접속 IP·체인 무결성."""
import json

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store
from src.privacy import audit, member_history

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


def _first_registration_id():
    _login("opuser", "op-password-1")
    return client.get("/registrations").json()["registrations"][0]["registration_id"]


# ------------------------------------------------------------ record() --

def test_unknown_action_and_field_are_rejected():
    with pytest.raises(ValueError):
        member_history.record("u", "u", "hack")
    with pytest.raises(ValueError):
        member_history.record("u", "u", "profile_update", [("phone", None, "010-1234-5678")])   # 흰 목록 밖


def test_sensitive_values_are_masked_before_storage():
    member_history.record("u1", "u1", "profile_update",
                          [("plate", "43오3450", "12가3456"), ("password", "old-secret-1", "new-secret-2")])
    raw = json.dumps(audit.recent(5), ensure_ascii=False)
    for secret in ("43오3450", "12가3456", "old-secret-1", "new-secret-2"):
        assert secret not in raw
    item = member_history.timeline("u1")[0]
    by_field = {c["field"]: c for c in item["changes"]}
    assert by_field["plate"]["before"] == "43오**50" and by_field["plate"]["after"] == "12가**56"
    assert by_field["password"]["after"] == "(변경됨)" and by_field["password"]["label"] == "비밀번호"


def test_timeline_is_scoped_to_the_member_and_newest_first():
    member_history.record("a", "a", "signup", [("occupation", None, "office")])
    member_history.record("b", "b", "signup", [("occupation", None, "student")])
    member_history.record("a", "a", "profile_update", [("occupation", "office", "student")])
    a = member_history.timeline("a")
    assert [i["action"] for i in a] == ["profile_update", "signup"]
    assert len(member_history.timeline("b")) == 1
    assert member_history.timeline("nobody") == []


def test_prefix_match_does_not_treat_input_as_pattern():
    member_history.record("a", "a", "signup")
    assert audit.entries("member%") == []        # % 가 와일드카드로 해석되지 않는다
    assert len(audit.entries("member.")) == 1


# ------------------------------------------------------------- 가입 흐름 --

def test_signup_leaves_masked_history_with_consent_version_and_ip():
    reg_id = _signup()
    mine = client.get("/owner/history").json()["history"]
    assert [i["action"] for i in mine] == ["signup"]
    item = mine[0]
    assert item["label"] == "회원가입" and item["by"] == "본인" and item["note"] == reg_id
    fields = {c["field"]: c["after"] for c in item["changes"]}
    assert fields["plate"] == "43오**50" and fields["occupation"] == "office"
    assert fields["profile_consent"] == "profile-v1"
    assert "ip" not in item and "actor" not in item          # 본인용에는 접속 IP·처리자 계정을 싣지 않는다
    stored = audit.entries("member.signup", target="newdriver")[0]
    assert json.loads(stored["detail"])["ip"] == "testclient"  # 서버 기록에는 IP가 남아 있다


def test_member_sees_admin_actions_without_admin_account_name():
    _signup()
    reg = _first_registration_id()
    client.post(f"/registrations/{reg}/reveal-plate", json={"reason": "자동차등록증 대조"})
    client.post(f"/registrations/{reg}/approve", json={})
    _login("newdriver", "driver-pass-1")
    body = client.get("/owner/history").json()
    actions = [i["action"] for i in body["history"]]
    assert actions == ["registration_approve", "admin_view", "signup"]
    view = next(i for i in body["history"] if i["action"] == "admin_view")
    assert view["by"] == "관리자" and "자동차등록증 대조" in view["note"]
    assert "opuser" not in json.dumps(body, ensure_ascii=False)   # 관리자 계정명은 본인에게 숨긴다
    approve = next(i for i in body["history"] if i["action"] == "registration_approve")
    assert {c["field"] for c in approve["changes"]} == {"status", "ev_id"}


def test_reject_reason_is_visible_to_member():
    _signup()
    reg = _first_registration_id()
    client.post(f"/registrations/{reg}/reject", json={"reason": "번호판 확인 불가"})
    _login("newdriver", "driver-pass-1")
    top = client.get("/owner/history").json()["history"][0]
    assert top["action"] == "registration_reject" and top["note"] == "번호판 확인 불가"


def test_consent_change_recorded_only_when_state_changes():
    auth_store.create_user("owner1", "owner-pass-1", "owner", owned_ev_id="EV-001")
    _login("owner1", "owner-pass-1")
    client.put("/owner/consent/v2g", json={"granted": True})
    client.put("/owner/consent/v2g", json={"granted": True})      # 변화 없음 — 이력 추가 없음
    client.put("/owner/consent/v2g", json={"granted": False})
    history = client.get("/owner/history").json()["history"]
    assert [i["action"] for i in history] == ["consent_withdraw", "consent_grant"]
    change = history[1]["changes"][0]
    assert (change["field"], change["before"], change["after"]) == ("v2g_discharge_consent", "False", "True")


def test_resubmit_after_reject_is_recorded():
    _signup()
    reg = _first_registration_id()
    client.post(f"/registrations/{reg}/reject", json={"reason": "번호판 확인 불가"})
    _login("newdriver", "driver-pass-1")
    body = {k: PAYLOAD[k] for k in ("vehicle_model", "occupation", "sido", "sigungu", "consent")}
    assert client.post("/owner/registration", json={**body, "plate": "12가3456"}).status_code == 200
    top = client.get("/owner/history").json()["history"][0]
    assert top["action"] == "registration_submit"
    assert {c["field"]: c["after"] for c in top["changes"]}["plate"] == "12가**56"


# ------------------------------------------------------------------ 권한 --

def test_owner_history_requires_owner_account():
    assert client.get("/owner/history").status_code == 401
    _login("opuser", "op-password-1")
    assert client.get("/owner/history").status_code == 403


def test_one_member_cannot_see_anothers_history():
    _signup()
    client.post("/auth/logout")
    _signup(username="second1", plate="12가3456", sigungu="광산구")
    mine = json.dumps(client.get("/owner/history").json()["history"], ensure_ascii=False)
    assert "광산구" in mine and "북구" not in mine


def test_admin_history_view_includes_actor_ip_and_is_itself_logged():
    _signup()
    _login("opuser", "op-password-1")
    r = client.get("/registrations/member/newdriver/history")
    assert r.status_code == 200
    item = r.json()["history"][0]
    assert item["actor"] == "newdriver" and item["ip"] == "testclient"
    viewed = audit.entries("member_history_view", target="newdriver")
    assert len(viewed) == 1 and viewed[0]["actor"] == "opuser"


def test_admin_history_endpoint_forbidden_for_owner():
    _signup()
    assert client.get("/registrations/member/newdriver/history").status_code == 403


def test_audit_chain_stays_intact():
    _signup()
    reg = _first_registration_id()
    client.post(f"/registrations/{reg}/reveal-plate", json={"reason": "승인 심사"})
    client.post(f"/registrations/{reg}/approve", json={})
    assert audit.verify_chain()["ok"] is True
