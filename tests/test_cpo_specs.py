"""CPO 3사 연계 명세 및 API 테스트 (계획서 13장 산출물)."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store

client = TestClient(app)


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("admin-test", "admin-pass-1", "admin")
    r = client.post("/auth/login", json={"username": "admin-test", "password": "admin-pass-1"})
    assert r.status_code == 200


def test_cpo_specs(admin_env):
    r = client.get("/cpo/specs")
    assert r.status_code == 200
    data = r.json()
    assert "data_dictionary" in data
    assert "api_specification" in data
    assert data["total_target_chargers"] == 150
    fields = [item["field"] for item in data["data_dictionary"]]
    for req in ("session_id", "charger_id", "start_ts", "energy_kwh", "stop_reason", "error_code"):
        assert req in fields


def test_cpo_status(admin_env):
    r = client.get("/cpo/status")
    assert r.status_code == 200
    data = r.json()
    assert data["total_operators"] == 3
    assert data["total_target_chargers"] == 150
    op_ids = [op["operator_id"] for op in data["operators"]]
    assert "niceinfra" in op_ids
    assert "pluglink" in op_ids
    assert "evsis" in op_ids


def test_cpo_ingest_success():
    payload = {
        "batch_date": "2026-11-05",
        "sessions": [
            {
                "session_id": "TEST-TXN-01",
                "charger_id": "CHG-TEST-01",
                "start_ts": "2026-11-05T01:00:00",
                "end_ts": "2026-11-05T03:00:00",
                "energy_kwh": 35.5,
                "connector_type": "DC_COMBO1",
                "stop_reason": "NormalStop"
            }
        ]
    }
    r = client.post("/cpo/ingest/niceinfra", json=payload)
    assert r.status_code == 200
    res = r.json()
    assert res["status"] == "ok"
    assert res["cpo_id"] == "niceinfra"
    assert res["received"] == 1
    assert res["valid"] == 1


def test_cpo_ingest_unknown_operator():
    payload = {"batch_date": "2026-11-05", "sessions": []}
    r = client.post("/cpo/ingest/unknown_cpo", json=payload)
    assert r.status_code == 404
