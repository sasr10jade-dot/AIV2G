"""서포터즈 '내 충전 리포트'(AIV2GAPP F.O 리포트 이식) — 산식과 엔드포인트."""
import pandas as pd

from src.advisory.owner_report import build_report


def _ev(ev_id, hours_on, start="2026-11-01"):
    ts = pd.date_range(start, periods=24 * 28, freq="h")
    kw = [7.0 if (t.hour in hours_on) else 0.0 for t in ts]
    return pd.DataFrame({"timestamp": ts, "ev_id": ev_id, "charging_kw": kw, "soc_pct": 60.0})


def _grid():
    ts = pd.date_range("2026-11-01", periods=24 * 28, freq="h")
    demand = [100.0 if t.hour in (18, 19, 20, 21, 22, 23) else 50.0 for t in ts]     # 날짜별 상위 25% = 18~23시
    return pd.DataFrame({"timestamp": ts, "power_demand_mw": demand, "price_krw_per_kwh": 100.0})


def test_report_counts_sessions_peak_share_and_compares_with_peers():
    ev = pd.concat([_ev("ME", {1, 2}), _ev("P1", {19, 20}), _ev("P2", {19, 20})])
    r = build_report("ME", ev, _grid(), 60.0, min_soc_pct=30, target_soc_pct=80)
    m = r["metrics"]
    assert r["has_data"] and m["sessions"] == 28 and m["per_week"] == 7.0        # 창(28일) 안의 매일 1회
    assert m["avg_kwh"] == 14.0 and m["peak_share_pct"] == 0.0 and m["won_per_kwh"] == 100
    assert r["compare"]["peak_share_pct"]["median"] == 100.0 and r["peer_count"] == 2
    titles = [i["title"] for i in r["insights"]]
    assert "전력 피크 시간 충전" in titles and "최소 보장 배터리 제안" in titles and "주행거리 기록" in titles
    assert r["min_soc_suggestion_pct"] == 45     # 하루 14kWh × 1.5 ÷ 60kWh + 10%p = 45%(5% 단위, 하한 30%)


def test_report_uses_odometer_entries_for_driving_metrics():
    ev = _ev("ME", {1})
    odo = [{"recorded_at": "2026-11-10T08:00:00", "odometer_km": 1000.0},
           {"recorded_at": "2026-11-17T08:00:00", "odometer_km": 1210.0}]
    d = build_report("ME", ev, _grid(), 60.0, odometer=odo)["driving"]
    assert d["entries"] == 2 and d["km_per_week"] == 210 and d["km_per_kwh"] == 4.3 and d["won_per_km"] == 23.3   # 7일 × 7kWh = 49kWh, 4,900원


def test_report_without_vehicle_data():
    r = build_report("EV-U0001", _ev("ME", {1}), _grid(), 60.0)
    assert r["has_data"] is False and "기록" in r["message"]


def test_report_and_odometer_endpoints_are_owner_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import app
    from src.api.routers import signup as signup_router
    from src.auth import store as auth_store
    from src.advisory import owner_guide as og
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("opuser2", "op-password-1", "admin")
    signup_router.reset_rate_limit()
    og.reset_caches()
    c = TestClient(app)
    payload = {"username": "odo_driver", "password": "driver-pass-1", "vehicle_model": "v2g_demo", "plate": "12가3456",
               "occupation": "office", "sido": "광주광역시", "sigungu": "북구", "consent": True, "odometer_km": 1000}
    assert c.post("/signup", json=payload).status_code == 200
    c.post("/auth/login", json={"username": "opuser2", "password": "op-password-1"})
    assert c.get("/owner/report").status_code in (401, 403)                     # 관리자는 볼 수 없다(본인 차량 전용)
    reg = [r for r in c.get("/registrations").json()["registrations"] if r["username"] == "odo_driver"][0]["registration_id"]
    assert c.post(f"/registrations/{reg}/approve", json={}).status_code == 200
    c.post("/auth/login", json={"username": "odo_driver", "password": "driver-pass-1"})
    assert c.post("/owner/odometer", json={"odometer_km": 900}).status_code == 422      # 줄어든 값은 거부
    r = c.post("/owner/odometer", json={"odometer_km": 1250})
    assert r.status_code == 200 and [h["odometer_km"] for h in r.json()["history"]] == [1000.0, 1250.0]
    rep = c.get("/owner/report").json()
    assert rep["has_data"] is False and [o["odometer_km"] for o in rep["odometer"]] == [1000.0, 1250.0]   # 앱 가입 차량은 아직 기록 없음
    assert c.get("/owner/me").json()                                                       # 세션 유지
    c.post("/auth/logout")


def test_report_text_in_english():
    ev = pd.concat([_ev("ME", {1, 2}), _ev("P1", {19, 20})])
    ko = build_report("ME", ev, _grid(), 60.0, min_soc_pct=30, target_soc_pct=80)
    en = build_report("ME", ev, _grid(), 60.0, min_soc_pct=30, target_soc_pct=80, lang="en")
    assert [i["title"] for i in en["insights"]][:2] == ["Charging habit", "Charging at peak hours"]
    assert len(en["insights"]) == len(ko["insights"]) and en["metrics"] == ko["metrics"]
    hangul = lambda s: any("\uac00" <= ch <= "\ud7a3" for ch in s)
    assert not any(hangul(i["title"] + i["text"]) for i in en["insights"])      # 영어 문장에 한글이 섞이지 않는다
    assert "simulated" in en["note"] and build_report("X", ev, _grid(), 60.0, lang="en")["message"].startswith("There are no")


def test_report_includes_recent_history_and_inferred_effect():
    ev = pd.concat([_ev("ME", {19, 20}), _ev("P1", {1, 2})])
    ev["connected"] = ev["charging_kw"] > 0
    ev["driving"] = False
    r = build_report("ME", ev, _grid(), 60.0, target_soc_pct=80)
    assert len(r["recent"]) == 3 and r["recent"][0]["kwh"] == 14.0 and r["recent"][0]["won_per_kwh"] == 100
    assert r["recent"][0]["start"] > r["recent"][1]["start"]                      # 최근 것부터
    assert len(r["daily"]) == 28 and r["daily"][0] == {"date": "2026-11-01", "sessions": 1, "kwh": 14.0, "cost_krw": 1400}
    assert r["soc_now_pct"] == 60.0 and r["as_of"] == "2026-11-28"
    eff = r["effect"]
    if eff is not None:                                                            # 최적화 결과는 추론값으로 표시된다
        assert eff["generation"] == "inferred" and eff["share"]["simulated"] is True
        assert eff["share"]["owner_krw"] == round(eff["savings_krw"] * 0.5)
        assert eff["peak_share_pct"]["before"] == 100.0                            # 19~20시 = 날짜별 수요 상위 25%
