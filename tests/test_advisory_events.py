"""어드바이저리 발행·응답, 방전 이벤트 기록 테스트 (curated.db는 임시 파일로 격리)."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src import kpi
from src.advisory import service as adv
from src.api import app
from src.auth import store as auth_store
from src.events import discharge as ev
from src.pipeline import store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CURATED_DB", tmp_path / "curated.db")
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")


def test_discharge_event_idempotent_noise_filtered_and_kpi_counts_real_only():
    assert ev.record_discharge_event("EV1", "2026-09-01T20:00", 1.0, 3.0, "V2G", "C1", "ocpp") is True
    assert ev.record_discharge_event("EV1", "2026-09-01T20:00", 1.0, 3.0, "V2G", "C1", "ocpp") is False  # 중복
    assert ev.record_discharge_event("EV1", "2026-09-02T20:00", 0.1, 0.01, "V2G") is False             # 잡음
    ev.record_discharge_event("EV2", "2026-09-02T20:00", 1.0, 2.0, "TEST")
    with pytest.raises(ValueError):
        ev.record_discharge_event("EV1", "x", 1.0, 1.0, "BAD")
    con = store.get_connection()
    assert kpi.count_discharge_events(con) == 1
    assert len(ev.list_events("EV1", con=con)) == 1


def test_default_kind_is_test_unless_configured(monkeypatch):
    monkeypatch.delenv("DISCHARGE_RECORD_KIND", raising=False)
    assert ev.default_kind() == "TEST"
    monkeypatch.setenv("DISCHARGE_RECORD_KIND", "V2G")
    assert ev.default_kind() == "V2G"
    monkeypatch.setenv("DISCHARGE_RECORD_KIND", "junk")
    assert ev.default_kind() == "TEST"


def test_advisory_issue_respond_once_and_stats():
    plan = {"net_krw": 880, "assumptions": {"verified": False}}
    a1, a2 = adv.issue("EV1", plan, "kakao"), adv.issue("EV1", plan, "app")
    adv.respond(a1, "accepted")
    adv.respond(a2, "declined")
    with pytest.raises(adv.AdvisoryError, match="이미"):
        adv.respond(a1, "declined")
    with pytest.raises(adv.AdvisoryError):
        adv.respond("ADV-none", "accepted")
    with pytest.raises(adv.AdvisoryError):
        adv.issue("EV1", plan, "fax")
    s = adv.acceptance_stats()
    assert s["acceptance_rate"] == 0.5 and s["counts"] == {"accepted": 1, "declined": 1}
    assert all(r["verified"] == 0 for r in adv.list_advisories("EV1"))


def test_advisory_api_end_to_end_with_user_inputs():
    auth_store.create_user("opuser", "op-password-1", "admin")
    c = TestClient(app)
    assert c.post("/advisory/issue", json={"ev_id": "EV-001"}).status_code == 401
    assert c.post("/auth/login", json={"username": "opuser", "password": "op-password-1"}).status_code == 200
    r = c.post("/advisory/issue", json={"ev_id": "NO-SUCH-EV", "departure_time": "2030-01-01T08:00"})
    assert r.status_code == 404
    from src.pipeline.source import load_scenario
    from src.datagen.config import SimConfig
    ev_df, *_ = load_scenario(SimConfig())
    ev_id = ev_df["ev_id"].iloc[0]
    dep = (ev_df[ev_df["ev_id"] == ev_id]["timestamp"].max() + pd.Timedelta(hours=14)).isoformat()
    r = c.post("/advisory/issue", json={"ev_id": ev_id, "departure_time": dep, "current_soc_pct": 45, "need_soc_pct": 80})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["assumptions"]["verified"] is False and body["plan"]["observed_start_soc_pct"] == 45.0
    aid = body["advisory_id"]
    assert c.post(f"/advisory/{aid}/respond", json={"status": "accepted"}).status_code == 200
    assert c.post(f"/advisory/{aid}/respond", json={"status": "declined"}).status_code == 409
    listing = c.get("/advisory").json()
    assert listing["stats"]["acceptance_rate"] == 1.0


def test_ocpp_session_end_records_discharge_event(monkeypatch):
    from src.ocpp import central_system as cs

    monkeypatch.setenv("DISCHARGE_RECORD_KIND", "V2G")
    cs.clear()
    started = "2026-09-01T10:00:00+00:00"
    cs.CHARGE_POINTS["CP1"] = {"session_started_at": started, "id_tag": "EV-9", "energy_export_wh": 5000.0, "export_start_wh": 1000.0}
    cs._record_discharge_if_any("CP1", cs.CHARGE_POINTS["CP1"])
    rows = ev.list_events("EV-9")
    assert len(rows) == 1 and rows[0]["energy_kwh"] == 4.0 and rows[0]["kind"] == "V2G" and rows[0]["source"] == "ocpp"
    # 방전이 없으면 기록하지 않고, 기록 오류는 예외로 새어 나오지 않는다
    cs._record_discharge_if_any("CP2", {"session_started_at": started, "energy_export_wh": 0.0, "export_start_wh": 0.0})
    cs._record_discharge_if_any("CP3", {"session_started_at": "garbage", "energy_export_wh": 9000.0, "export_start_wh": 0.0})
    assert len(ev.list_events()) == 1
    cs.clear()
