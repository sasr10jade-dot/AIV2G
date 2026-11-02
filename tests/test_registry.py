"""모바일 가입·차량 등록부 — 입력 검증, 중복 방지, 승인 후에만 차량 연결, 번호판 마스킹."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store
from src.privacy import consent
from src.registry import options, service

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


def _signup(**over):
    return client.post("/signup", json={**PAYLOAD, **over})


def _admin():
    r = client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    assert r.status_code == 200


def _login(username="newdriver", password="driver-pass-1"):
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200


def _pending_id():
    _admin()
    return client.get("/registrations", params={"status": "pending"}).json()["registrations"][0]["registration_id"]


# ------------------------------------------------------------- 입력 검증 --

@pytest.mark.parametrize("field,value", [
    ("plate", "ABC1234"), ("plate", "3오3450"), ("plate", "43오345"),
    ("vehicle_model", "nope"), ("occupation", "astronaut"), ("sido", "광주"),
    ("sigungu", "용봉동"), ("sigungu", "북구 용봉동 123번지"), ("sigungu", "123"), ("sigungu", ""),
])
def test_profile_validation_rejects_bad_input(field, value):
    key = {"vehicle_model": "vehicle_model", "plate": "plate", "occupation": "occupation",
           "sido": "sido", "sigungu": "sigungu"}[field]
    r = _signup(**{key: value})
    assert r.status_code == 422, (field, value, r.text)
    assert auth_store.get_user("newdriver") is None      # 검증 실패 시 계정이 남지 않는다


@pytest.mark.parametrize("sigungu", ["북구", "광산구", "수원시 영통구", "영광군", "제주시"])
def test_sigungu_accepts_valid_names(sigungu):
    assert options.SIGUNGU_RE.match(sigungu)


def test_signup_requires_consent_and_strong_password():
    assert _signup(consent=False).status_code == 422
    assert _signup(password="short1").status_code == 422
    assert _signup(password="onlyletters").status_code == 422
    assert _signup(username="a b").status_code == 422
    assert auth_store.get_user("newdriver") is None


def test_options_endpoint_lists_choices_and_consent_text():
    body = client.get("/signup/options").json()
    assert {o["id"] for o in body["occupations"]} == {"office", "self_employed", "homemaker", "student", "other"}
    assert "광주광역시" in body["sido"] and len(body["sido"]) == 17
    assert any(m["id"] == "ioniq5" for m in body["vehicle_models"])
    assert body["consent"]["version"] == options.CONSENT_VERSION and "수집 항목" in body["consent"]["text"]


# --------------------------------------------------------------- 가입 흐름 --

def test_signup_creates_pending_registration_without_vehicle_access():
    r = _signup()
    assert r.status_code == 200 and r.json()["status"] == "pending"
    user = auth_store.get_user("newdriver")
    assert user["role"] == "owner" and user["owned_ev_id"] is None
    # 가입 직후 로그인 상태지만 차량 데이터에는 접근할 수 없다
    assert client.get("/auth/me").json()["role"] == "owner"
    assert client.get("/owner/me").status_code == 403
    assert client.get("/owner/charging-guide").status_code == 403
    st = client.get("/owner/registration").json()
    assert st["linked"] is False and st["registration"]["status"] == "pending"


def test_plate_masked_everywhere_and_labels_present():
    _signup()
    mine = client.get("/owner/registration").json()["registration"]
    assert mine["plate"] == "43오**50" and "3450" not in mine["plate"]
    _admin()
    row = client.get("/registrations").json()["registrations"][0]
    assert row["plate"] == "43오**50"                      # 관리자 목록도 기본은 마스킹
    assert row["occupation_label"] == "회사원"
    assert row["region"] == "광주광역시 북구" and row["vehicle_label"] == "현대 아이오닉 5"
    assert "plate_enc" not in row and "plate_hash" not in row   # 암호문·해시는 응답에 나가지 않는다


def test_plaintext_plate_is_never_stored():
    """DB의 어떤 컬럼에도, 접속기록에도 평문 번호판이 없다."""
    _signup()
    _admin()
    reg_id = client.get("/registrations").json()["registrations"][0]["registration_id"]
    client.post(f"/registrations/{reg_id}/reveal-plate", json={"reason": "승인 심사"})
    client.post(f"/registrations/{reg_id}/approve", json={})
    from src.pipeline import store
    con = store.get_connection()
    dump = " ".join(str(v) for row in con.execute("SELECT * FROM vehicle_registrations") for v in row)
    con.close()
    assert "43오3450" not in dump   # 전체 번호판 문자열만 검사 — 짧은 숫자열은 해시·시각에 우연히 나올 수 있다
    from src.privacy import audit
    log_dump = " ".join(str(r) for r in audit.recent(200))
    assert "43오3450" not in log_dump


def test_reveal_plate_requires_reason_and_is_logged_with_ip():
    from src.privacy import audit
    _signup()
    _admin()
    reg_id = client.get("/registrations").json()["registrations"][0]["registration_id"]
    assert client.post(f"/registrations/{reg_id}/reveal-plate", json={"reason": "x"}).status_code == 422
    assert client.post(f"/registrations/{reg_id}/reveal-plate", json={}).status_code == 422
    assert client.post("/registrations/REG-nope/reveal-plate", json={"reason": "승인 심사"}).status_code == 404
    r = client.post(f"/registrations/{reg_id}/reveal-plate", json={"reason": "자동차등록증 대조"})
    assert r.status_code == 200 and r.json()["plate"] == "43오3450"
    entries = [e for e in audit.recent(50) if e["action"] == "pii_view"]
    assert len(entries) == 1                                 # 실패한 요청은 기록하지 않고, 성공 1건만 남는다
    assert entries[0]["actor"] == "opuser" and entries[0]["target"] == reg_id
    assert "자동차등록증 대조" in entries[0]["detail"] and "ip" in entries[0]["detail"]
    assert audit.verify_chain()["ok"] is True


def test_admin_actions_are_logged_with_ip():
    from src.privacy import audit
    _signup()
    _admin()
    reg_id = client.get("/registrations").json()["registrations"][0]["registration_id"]
    client.post(f"/registrations/{reg_id}/approve", json={})
    actions = {e["action"] for e in audit.recent(50)}
    assert {"registrations_list", "registration_approve", "member.signup", "member.registration_approve"} <= actions
    listed = [e for e in audit.recent(50) if e["action"] == "registrations_list"][0]
    assert "ip" in listed["detail"] and "43오3450" not in listed["detail"]


def test_owner_cannot_reveal_plate():
    _signup()
    reg_id = service.list_all()[0]["registration_id"]
    assert client.post(f"/registrations/{reg_id}/reveal-plate", json={"reason": "내 번호판"}).status_code == 403


def test_duplicate_plate_and_username_blocked_and_no_orphan_account():
    assert _signup().status_code == 200
    client.post("/auth/logout")
    r = _signup(username="other1")                              # 같은 번호판
    assert r.status_code == 422 and "번호판" in r.json()["detail"]
    assert auth_store.get_user("other1") is None                # 등록 실패 시 방금 만든 계정도 지운다
    assert _signup(plate="12가3456").status_code == 422         # 같은 아이디
    assert len(service.list_all()) == 1


def test_signup_rate_limited(monkeypatch):
    monkeypatch.setenv("SIGNUP_RATE_PER_HOUR", "2")
    signup_router.reset_rate_limit()
    assert _signup(consent=False).status_code == 422
    assert _signup(consent=False).status_code == 422
    assert _signup(consent=False).status_code == 429


# ----------------------------------------------------------------- 승인 --

def test_approval_links_vehicle_and_grants_profile_consent():
    _signup()
    reg_id = _pending_id()
    r = client.post(f"/registrations/{reg_id}/approve", json={})
    assert r.status_code == 200, r.text
    ev_id = r.json()["ev_id"]
    assert ev_id == "EV-U0001"
    assert auth_store.get_user("newdriver")["owned_ev_id"] == ev_id
    assert consent.has_consent(ev_id, "profile")
    _login()
    assert client.get("/owner/me").json()["ev_id"] == ev_id
    assert client.get("/owner/registration").json()["linked"] is True


def test_approval_can_link_existing_obd_vehicle():
    _signup()
    reg_id = _pending_id()
    r = client.post(f"/registrations/{reg_id}/approve", json={"ev_id": "EV-TORRES-01"})
    assert r.json()["ev_id"] == "EV-TORRES-01"
    assert service.profile_for_ev("EV-TORRES-01")["occupation"] == "office"
    assert service.profile_for_ev("EV-NOPE") is None


def test_cannot_link_vehicle_already_used_by_another_owner():
    _signup()
    reg1 = _pending_id()
    client.post(f"/registrations/{reg1}/approve", json={"ev_id": "EV-TORRES-01"})
    client.post("/auth/logout")
    _signup(username="second1", plate="12가3456")
    _admin()
    reg2 = [r for r in client.get("/registrations", params={"status": "pending"}).json()["registrations"]][0]["registration_id"]
    r = client.post(f"/registrations/{reg2}/approve", json={"ev_id": "EV-TORRES-01"})
    assert r.status_code in (409, 422) and "이미" in r.json()["detail"]
    assert auth_store.get_user("second1")["owned_ev_id"] is None
    assert client.get("/registrations", params={"status": "pending"}).json()["pending_count"] == 1


def test_auto_ev_ids_increment():
    _signup()
    r1 = client.post(f"/registrations/{_pending_id()}/approve", json={}).json()["ev_id"]
    client.post("/auth/logout")
    _signup(username="second1", plate="12가3456")
    r2 = client.post(f"/registrations/{_pending_id()}/approve", json={}).json()["ev_id"]
    assert (r1, r2) == ("EV-U0001", "EV-U0002")


def test_approve_twice_and_bad_ev_id_rejected():
    _signup()
    reg_id = _pending_id()
    assert client.post(f"/registrations/{reg_id}/approve", json={"ev_id": "bad id!"}).status_code == 422
    assert client.post(f"/registrations/{reg_id}/approve", json={}).status_code == 200
    assert client.post(f"/registrations/{reg_id}/approve", json={}).status_code == 409


def test_approve_fails_cleanly_if_account_was_deleted():
    _signup()
    reg_id = _pending_id()
    auth_store.delete_user("newdriver")
    r = client.post(f"/registrations/{reg_id}/approve", json={})
    assert r.status_code in (409, 422) and "계정" in r.json()["detail"]
    assert client.get("/registrations", params={"status": "approved"}).json()["registrations"] == []


# ----------------------------------------------------------------- 반려 --

def test_reject_then_resubmit_with_new_plate():
    _signup()
    reg_id = _pending_id()
    assert client.post(f"/registrations/{reg_id}/reject", json={"reason": "x"}).status_code == 422   # 사유 너무 짧음
    r = client.post(f"/registrations/{reg_id}/reject", json={"reason": "번호판 확인 불가"})
    assert r.status_code == 200 and r.json()["registration"]["status"] == "rejected"
    _login()
    st = client.get("/owner/registration").json()
    assert st["linked"] is False and st["registration"]["status"] == "rejected"
    assert st["registration"]["reject_reason"] == "번호판 확인 불가"
    body = {k: PAYLOAD[k] for k in ("vehicle_model", "occupation", "sido", "sigungu", "consent")}
    assert client.post("/owner/registration", json={**body, "plate": "12가3456"}).status_code == 200
    assert client.get("/owner/registration").json()["registration"]["status"] == "pending"
    # 심사 중에는 중복 신청 불가
    assert client.post("/owner/registration", json={**body, "plate": "99나9999"}).status_code == 409


def test_rejected_plate_can_be_registered_again_by_someone_else():
    _signup()
    client.post(f"/registrations/{_pending_id()}/reject", json={"reason": "확인 불가"})
    client.post("/auth/logout")
    assert _signup(username="second1").status_code == 200       # 반려된 번호판은 다시 쓸 수 있다


# ------------------------------------------------------------------ 권한 --

def test_registration_admin_endpoints_require_admin():
    assert client.get("/registrations").status_code == 401
    _signup()
    assert client.get("/registrations").status_code == 403       # owner는 심사 목록을 볼 수 없다
    reg_id = service.list_all()[0]["registration_id"]
    assert client.post(f"/registrations/{reg_id}/approve", json={}).status_code == 403
    assert client.post(f"/registrations/{reg_id}/reject", json={"reason": "테스트"}).status_code == 403


def test_registration_status_filter_validated():
    _admin()
    assert client.get("/registrations", params={"status": "bogus"}).status_code == 422


# ------------------------------------------------ 시·군·구 선택지(시·도별 목록) --

def test_signup_options_expose_sigungu_by_sido_covering_every_sido():
    body = client.get("/signup/options").json()
    by = body["sigungu_by_sido"]
    assert set(by) == set(body["sido"])                       # 모든 시·도에 목록이 있다
    assert "광산구" in by["광주광역시"] and "나주시" in by["전라남도"] and "수원시" in by["경기도"]
    assert all(options.SIGUNGU_RE.match(n) for names in by.values() for n in names)   # 목록 값은 서버 입력 규칙을 통과한다


def test_sigungu_must_belong_to_the_selected_sido():
    assert _signup(sido="서울특별시", sigungu="북구").status_code == 422               # 서울에는 북구가 없다
    assert _signup(sido="전라남도", sigungu="광산구").status_code == 422
    r = _signup(sido="전라남도", sigungu="나주시")
    assert r.status_code == 200


def test_profile_correction_also_checks_sido_and_sigungu_match():
    assert _signup().status_code == 200                                                # 광주광역시 북구
    assert client.patch("/owner/profile", json={"sigungu": "수원시"}).status_code == 422   # 광주에 없는 시
    assert client.patch("/owner/profile", json={"sido": "경기도", "sigungu": "수원시"}).status_code == 200
