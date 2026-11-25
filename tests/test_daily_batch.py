"""일 배치(C1 예측 갱신·C2 계획/추천·C3 아크릴 오차·C4 데이터 결합) 테스트."""
import json
from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.automation import daily_batch
from src.pipeline import store

from tests.test_dataset import _ch, _ev, _grid

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_batch, "BATCH_DIR", tmp_path / "daily")
    monkeypatch.setattr(daily_batch, "HISTORY", tmp_path / "daily_history.jsonl")
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    yield
    client.post("/auth/logout")


@pytest.fixture
def one_supporter(monkeypatch):
    from src.advisory import owner_guide
    from src.registry import service as registry
    reg = {"username": "sup1", "ev_id": "EV-A", "status": "approved", "departure_time": "08:00",
           "target_soc_pct": 80, "min_soc_pct": 30}
    monkeypatch.setattr(registry, "list_all", lambda status=None, limit=200: [reg] if status == "approved" else [])
    monkeypatch.setattr(owner_guide, "vehicle_profile", lambda u, e: {
        "label": "m1", "battery_kwh": 70.0, "battery_assumed": False, "v2g_capable": False, "source": "fleet", "demo": False})
    return reg


def test_dataset_step_writes_summary_and_pseudonymized_csv(tmp_path):
    day = tmp_path / "d"
    r = daily_batch.build_daily_dataset(_ev(), _ch(), _grid("2026-12-01", 24), day)
    assert r["counted"] == 4 and r["join_rate_pct"] == 100.0 and r["by_kind"] == {"drive": 1, "charge": 1, "site_charge": 2}
    assert json.loads((day / "dataset_summary.json").read_text(encoding="utf-8"))["counted"] == 4
    csv = (day / "dataset.csv").read_text(encoding="utf-8-sig")
    assert "EV-A" not in csv and "PE-" in csv


def test_recommend_uses_latest_obd_soc_and_issues_app_advisory(tmp_path, one_supporter):
    day = tmp_path / "d"
    r = daily_batch.plan_and_recommend(_ev(), day, now=datetime(2026, 12, 1, 20, 0))
    assert r == {"vehicles": 1, "issued": 1, "failed": 0, "soc_assumed": 0}
    plans = json.loads((day / "plans.json").read_text(encoding="utf-8"))
    assert plans[0]["ev_id"].startswith("PE-") and plans[0]["soc_source"] == "obd" and plans[0]["safety_passed"] is True
    con = store.get_connection()
    channel, inputs = con.execute("SELECT channel, inputs FROM advisories WHERE ev_id='EV-A'").fetchone()
    con.close()
    inputs = json.loads(inputs)
    assert channel == "app" and inputs["source"] == "daily_batch" and inputs["current_soc_pct"] == 80.0   # 마지막 관측 SOC


def test_recommend_dry_run_does_not_issue(tmp_path, one_supporter):
    r = daily_batch.plan_and_recommend(_ev().iloc[0:0], tmp_path / "d", now=datetime(2026, 12, 1, 20, 0), issue=False)
    assert r["issued"] == 0 and r["soc_assumed"] == 1
    con = store.get_connection()
    assert con.execute("SELECT COUNT(*) FROM advisories").fetchone()[0] == 0
    con.close()


def test_run_daily_continues_after_failed_step_and_logs_history(monkeypatch, one_supporter):
    from src.pipeline import source
    monkeypatch.setattr(source, "load_scenario", lambda cfg=None: (_ev(), _ch(), _grid("2026-11-24", 24 * 10), False))

    def boom(day_dir):
        raise RuntimeError("model missing")
    monkeypatch.setattr(daily_batch, "refresh_forecast", boom)
    out = daily_batch.run_daily(now=datetime(2026, 12, 1, 20, 0))
    steps = {s["step"]: s["ok"] for s in out["steps"]}
    assert steps["forecast"] is False and steps["dataset"] and steps["recommend"] and out["ok"] is False
    assert out["data_source"] == "synthetic" and out["acryl"]["ev_charging_demand_kw"] == {"received": False}
    assert out["recommend"]["issued"] == 1
    from src.automation import issues
    assert any("forecast" in i["message"] for i in issues.list_issues(stage="forecast"))
    hist = daily_batch.history()
    assert len(hist) == 1 and hist[0]["day"] == "2026-12-01"


def test_daily_batch_api_admin_only():
    assert client.get("/kpi/daily-batch").status_code in (401, 403)
    auth_store.create_user("admin-db", "admin-pass-1", "admin")
    assert client.post("/auth/login", json={"username": "admin-db", "password": "admin-pass-1"}).status_code == 200
    daily_batch.HISTORY.write_text(json.dumps({"day": "2026-12-01", "ok": True, "steps": []}) + "\n", encoding="utf-8")
    r = client.get("/kpi/daily-batch")
    assert r.status_code == 200 and r.json()["count"] == 1 and "04:30" in r.json()["schedule"]
