"""어드바이저리(EV별 추천 충전 시간) 발행·응답 테스트 (curated.db는 임시 파일로 격리)."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.advisory import service as adv
from src.api import app
from src.auth import store as auth_store
from src.pipeline import store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CURATED_DB", tmp_path / "curated.db")
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")


def test_advisory_issue_respond_once_and_stats():
    plan = {"net_krw": 880, "assumptions": {"verified": False}}
    a1, a2 = adv.issue("EV1", plan, "dashboard"), adv.issue("EV1", plan, "app")
    adv.respond(a1, "accepted")
    adv.respond(a2, "declined")
    with pytest.raises(adv.AdvisoryError, match="이미"):
        adv.respond(a1, "declined")
    with pytest.raises(adv.AdvisoryError):
        adv.respond("ADV-none", "accepted")
    with pytest.raises(adv.AdvisoryError):
        adv.issue("EV1", plan, "kakao")            # 메신저 채널은 계획서 범위 밖
    a3 = adv.issue("EV2", plan, "app")
    with pytest.raises(adv.AdvisoryError, match="없는"):
        adv.respond(a3, "accepted", ev_id="EV1")   # 다른 차량의 추천에는 응답할 수 없다
    s = adv.acceptance_stats()
    assert s["acceptance_rate"] == 0.5 and s["counts"] == {"accepted": 1, "declined": 1, "issued": 1}
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
