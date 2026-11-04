"""㈜아크릴 예측모델 연계 — 학습 데이터 제공(가명처리·manifest)과 예측 결과 수신(토큰·IP·검증·멱등)."""
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.acryl import link
from src.api import app
from src.auth import store as auth_store
from src.datagen.config import SimConfig
from src.pipeline import store
from src.privacy import consent

client = TestClient(app)
TOKEN = "test-partner-token-1234"
SMALL = SimConfig(n_ev=4, weeks=4, n_chargers=6, charger_days=28)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CURATED_DB", tmp_path / "curated.db")
    monkeypatch.setattr(link, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setenv("ACRYL_API_TOKEN", TOKEN)
    monkeypatch.delenv("ACRYL_ALLOWED_IPS", raising=False)
    yield
    client.post("/auth/logout")


def _batch(**over):
    base = {"batch_id": "B-20270110", "model_id": "acryl-demand", "model_version": "1.0.0",
            "target": "ev_charging_demand_kw", "issued_at": "2027-01-10T06:00:00+09:00",
            "points": [{"timestamp": f"2027-01-10T{h:02d}:00:00", "value": 10.0 + h, "lower": 8.0 + h, "upper": 12.0 + h}
                       for h in range(24)]}
    return {**base, **over}


# ------------------------------------------------------------------ 데이터 제공 --

def test_export_pseudonymizes_and_writes_manifest_with_hashes():
    m = link.export_training_dataset("opuser", cfg=SMALL)
    names = {f["name"] for f in m["files"]}
    assert names == {"ev_telemetry.csv", "charger_sessions.csv", "grid_signal.csv", "city_hourly.csv"}
    ev = pd.read_csv(link.export_file(m["export_id"], "ev_telemetry.csv"))
    assert ev["ev_id"].str.startswith("PE-").all()                       # 가명 ID
    assert not any(c in ev.columns for c in ("plate", "username"))         # 식별정보 컬럼 없음
    assert set(ev["data_origin"]) == {"synthetic"} and m["is_real_data"] is False and m["note"]
    assert all(len(f["sha256"]) == 64 for f in m["files"]) and m["pseudonymized"] == ["ev_id"]
    saved = json.loads(link.export_file(m["export_id"], "manifest.json").read_text(encoding="utf-8"))
    assert saved["export_id"] == m["export_id"]
    assert link.list_exports()[0]["export_id"] == m["export_id"]


def test_export_period_filter_and_bad_period():
    full = link.export_training_dataset("opuser", cfg=SMALL)
    ev = pd.read_csv(link.export_file(full["export_id"], "ev_telemetry.csv"), parse_dates=["timestamp"])
    day = ev["timestamp"].dt.normalize().min() + pd.Timedelta(days=3)
    part = link.export_training_dataset("opuser", period_start=str(day.date()), period_end=str(day.date()), cfg=SMALL)
    ev2 = pd.read_csv(link.export_file(part["export_id"], "ev_telemetry.csv"), parse_dates=["timestamp"])
    assert ev2["timestamp"].dt.normalize().nunique() == 1
    with pytest.raises(link.LinkError):
        link.export_training_dataset("opuser", period_start="2027-02-01", period_end="2027-01-01", cfg=SMALL)


def test_export_excludes_vehicle_that_withdrew_external_sharing(monkeypatch):
    from src.pipeline.source import load_scenario
    ev_df, *_ = load_scenario(SMALL)
    target = ev_df["ev_id"].iloc[0]
    consent.grant(target, "share_external", "v1")
    consent.withdraw(target, "share_external")
    m = link.export_training_dataset("opuser", cfg=SMALL)
    assert m["excluded_vehicles"] == 1
    ev = pd.read_csv(link.export_file(m["export_id"], "ev_telemetry.csv"))
    assert ev["ev_id"].nunique() == ev_df["ev_id"].nunique() - 1


def test_export_file_rejects_unknown_names_and_ids():
    m = link.export_training_dataset("opuser", cfg=SMALL)
    for name in ("../users.json", "secret.csv"):
        with pytest.raises(link.LinkError):
            link.export_file(m["export_id"], name)
    with pytest.raises(link.LinkError):
        link.export_file("../../etc", "manifest.json")


# ------------------------------------------------------------------ 예측 수신 --

def test_receive_batch_is_idempotent_and_latest_issue_wins():
    r = link.receive_forecast_batch(_batch())
    assert r["inserted"] == 24 and r["duplicate"] is False and r["generation"] == "inferred"
    assert link.receive_forecast_batch(_batch())["duplicate"] is True        # 같은 배치 재전송
    newer = _batch(batch_id="B-20270110b", model_version="1.0.1", issued_at="2027-01-10T07:00:00+09:00",
                   points=[{"timestamp": "2027-01-10T05:00:00", "value": 99.0}])
    link.receive_forecast_batch(newer)
    fc = link.latest_forecast("ev_charging_demand_kw")
    assert len(fc) == 24
    row = fc[fc["timestamp"] == pd.Timestamp("2027-01-10 05:00")].iloc[0]
    assert row["value"] == 99.0 and row["model_version"] == "1.0.1"
    st = {s["target"]: s for s in link.forecast_status()}
    assert st["ev_charging_demand_kw"]["batches"] == 2 and st["power_demand_mw"]["batches"] == 0


@pytest.mark.parametrize("over, msg", [
    ({"target": "weather"}, "target"),
    ({"batch_id": "bad id!"}, "batch_id"),
    ({"points": []}, "비어"),
    ({"points": [{"timestamp": "2027-01-10T00:00", "value": -1}]}, "0 이상"),
    ({"points": [{"timestamp": "2027-01-10T00:00", "value": 5, "lower": 6}]}, "lower"),
    ({"points": [{"timestamp": "2027-01-10T00:00", "value": 5}, {"timestamp": "2027-01-10T00:00", "value": 6}]}, "두 번"),
])
def test_receive_batch_validation(over, msg):
    with pytest.raises(link.LinkError, match=msg):
        link.receive_forecast_batch(_batch(**over))


def test_tz_aware_points_are_stored_in_korea_time():
    link.receive_forecast_batch(_batch(points=[{"timestamp": "2027-01-09T15:00:00+00:00", "value": 1.0}]))
    assert link.latest_forecast("ev_charging_demand_kw")["timestamp"].iloc[0] == pd.Timestamp("2027-01-10 00:00")


def test_evaluate_against_actual_reports_error_and_rate():
    link.receive_forecast_batch(_batch())
    actual = pd.DataFrame({"timestamp": pd.date_range("2027-01-10", periods=24, freq="h"),
                           "ev_charging_demand_kw": [10.0 + h + 1 for h in range(24)]})
    ev = link.evaluate_against_actual("ev_charging_demand_kw", actual)
    assert ev["matched_hours"] == 24 and ev["mae"] == 1.0 and 0 < ev["prediction_rate_pct"] < 100


# ------------------------------------------------------------------ API·권한 --

def test_partner_endpoints_need_token_and_configuration(monkeypatch):
    assert client.post("/partner/acryl/forecasts", json=_batch()).status_code == 401
    assert client.post("/partner/acryl/forecasts", json=_batch(), headers={"X-Partner-Token": "wrong"}).status_code == 401
    r = client.post("/partner/acryl/forecasts", json=_batch(), headers={"X-Partner-Token": TOKEN})
    assert r.status_code == 200 and r.json()["inserted"] == 24
    monkeypatch.setenv("ACRYL_ALLOWED_IPS", "10.0.0.7")                       # 고정 IP만 허용
    assert client.get("/partner/acryl/exports", headers={"X-Partner-Token": TOKEN}).status_code == 403
    monkeypatch.delenv("ACRYL_API_TOKEN")
    assert client.get("/partner/acryl/exports", headers={"X-Partner-Token": TOKEN}).status_code == 503   # 설정 전에는 닫힘


def test_admin_creates_export_and_partner_downloads_it():
    auth_store.create_user("opuser", "op-password-1", "admin")
    assert client.post("/acryl/exports", json={}).status_code == 401
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    m = client.post("/acryl/exports", json={}).json()
    client.post("/auth/logout")
    h = {"X-Partner-Token": TOKEN}
    assert client.get("/partner/acryl/exports", headers=h).json()["exports"][0]["export_id"] == m["export_id"]
    r = client.get(f"/partner/acryl/exports/{m['export_id']}/city_hourly.csv", headers=h)
    assert r.status_code == 200 and r.text.startswith("timestamp,")
    assert client.get(f"/partner/acryl/exports/{m['export_id']}/..%2Fx", headers=h).status_code == 404



def test_destruction_record_blocks_further_downloads():
    m = link.export_training_dataset("op", cfg=SMALL)
    eid = m["export_id"]
    assert link.export_file(eid, "manifest.json").exists()
    with pytest.raises(link.LinkError):
        link.record_destruction(eid, "op", "x")                                       # 근거가 너무 짧다
    r = link.record_destruction(eid, "op", "2027-11-30 아크릴 파기 확인서 수령")
    assert r["destroyed_by"] == "op" and r["destroyed_at"]
    row = next(x for x in link.list_exports() if x["export_id"] == eid)
    assert row["destruction_note"].startswith("2027-11-30") and row["destroyed_at"]
    with pytest.raises(link.LinkError, match="내려받을 수 없습니다"):
        link.export_file(eid, "manifest.json")
    with pytest.raises(link.LinkError, match="이미"):
        link.record_destruction(eid, "op", "다시 기록")
