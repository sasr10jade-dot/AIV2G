"""태양광 연계 충전소 에뮬레이션 — 발전 모델의 물리적 타당성, 수요·스케줄 제약, 에너지 보존, API."""
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.solar import dispatch, pv_model
from src.solar.demand import MAX_DWELL_H, Session, generate_sessions, horizon_hours
from src.solar.emulator import EmulationError, run_emulation
from src.solar.site import SiteError, get_site, load_sites

client = TestClient(app)
SITE = get_site("naju_solar_ev")


# ------------------------------------------------------------------ 사이트 --

def test_default_site_loads_and_is_flagged_unverified():
    s = load_sites()["naju_solar_ev"]
    assert s.verified is False and s.kwp == 100.0 and s.n_chargers == 10 and s.max_ev_load_kw == 70.0
    assert 34 < s.lat < 36 and 126 < s.lon < 127          # 나주 부근


@pytest.mark.parametrize("patch", [
    {"lat": 95}, {"lon": 200}, {"pv": {"kwp": 0, "performance_ratio": 0.8}},
    {"pv": {"kwp": 10, "performance_ratio": 1.5}}, {"pv": {"kwp": 10, "performance_ratio": 0.8, "temp_coeff_per_c": 0.01}},
    {"chargers": {"count": 0, "kw": 7}}, {"chargers": {"count": 3, "kw": -1}}, {"grid_import_limit_kw": 0},
    {"demand": {"min_kwh": 50, "max_kwh": 10}},
])
def test_invalid_site_config_rejected(tmp_path, patch):
    base = json.loads(json.dumps(
        json.load(open(load_sites.__globals__["SOLAR_SITES_CONFIG"], encoding="utf-8"))["sites"]["naju_solar_ev"]))
    base.update(patch)
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"sites": {"x": base}}), encoding="utf-8")
    with pytest.raises(SiteError):
        load_sites(p)


def test_unknown_site():
    with pytest.raises(SiteError):
        get_site("nope")


# --------------------------------------------------------------- 발전 모델 --

def test_no_generation_at_night_and_peak_near_noon():
    idx = pd.date_range("2026-06-21", periods=24, freq="h")
    p = pv_model.pv_power_kw(idx, SITE, sky_factor=0.95)
    assert (p.iloc[[0, 1, 2, 3, 22, 23]] == 0).all()
    assert p.idxmax().hour in (11, 12, 13) and p.max() > 0


def test_summer_produces_more_than_winter_and_never_exceeds_kwp():
    def day_kwh(day):
        return pv_model.pv_power_kw(pd.date_range(day, periods=24, freq="h"), SITE, sky_factor=0.95).sum()
    assert day_kwh("2026-06-21") > 1.8 * day_kwh("2026-12-21")
    idx = pd.date_range("2026-01-01", periods=24 * 365, freq="h")
    assert pv_model.pv_power_kw(idx, SITE, seed=1).max() <= SITE.kwp + 1e-9


def test_solstice_irradiance_is_physically_plausible():
    g = pv_model.clear_sky_ghi(pd.date_range("2026-06-21", periods=24, freq="h"), SITE)
    assert 900 < g.max() < 1100                                   # 맑은 날 정오 일사량 ~1,000 W/m²
    w = pv_model.clear_sky_ghi(pd.date_range("2026-12-21", periods=24, freq="h"), SITE)
    assert 400 < w.max() < 650


def test_annual_yield_in_plausible_korean_range():
    idx = pd.date_range("2026-01-01", periods=24 * 365, freq="h")
    per_kwp_day = pv_model.pv_power_kw(idx, SITE, seed=11).sum() / SITE.kwp / 365
    assert 2.8 < per_kwp_day < 4.0                                # 연 약 1,000~1,450 kWh/kWp


def test_hot_cells_produce_less():
    idx = pd.date_range("2026-06-21", periods=24, freq="h")
    hot = pv_model.pv_power_kw(idx, SITE, sky_factor=0.9, ambient_c=pd.Series(35.0, index=idx))
    cool = pv_model.pv_power_kw(idx, SITE, sky_factor=0.9, ambient_c=pd.Series(5.0, index=idx))
    assert hot.sum() < cool.sum()


def test_weather_is_reproducible_and_independent_of_the_requested_range():
    a = pv_model.pv_power_kw(pd.date_range("2026-07-01", periods=24 * 10, freq="h"), SITE, seed=5)
    b = pv_model.pv_power_kw(pd.date_range("2026-07-04", periods=24 * 3, freq="h"), SITE, seed=5)
    assert np.allclose(a["2026-07-04":"2026-07-06"].to_numpy(), b.to_numpy())
    c = pv_model.pv_power_kw(pd.date_range("2026-07-01", periods=24 * 10, freq="h"), SITE, seed=6)
    assert not np.allclose(a.to_numpy(), c.to_numpy())             # 시드가 다르면 날씨가 다르다


def test_observed_irradiance_can_replace_the_weather_model():
    idx = pd.date_range("2026-06-21", periods=24, freq="h")
    clear = pv_model.clear_sky_ghi(idx, SITE).to_numpy()
    factor = pv_model.sky_factor_from_ghi(clear * 0.5, idx, SITE)
    assert factor[clear > 100].round(2).eq(0.5).all()
    with pytest.raises(ValueError):
        pv_model.sky_factor_from_ghi(clear[:5], idx, SITE)
    assert pv_model.MJ_PER_M2_HOUR_TO_WM2 == pytest.approx(277.78, abs=0.01)


def test_timezone_aware_index_rejected():
    with pytest.raises(ValueError):
        pv_model.clear_sky_ghi(pd.date_range("2026-06-21", periods=3, freq="h", tz="Asia/Seoul"), SITE)


def test_expected_day_uses_climatology():
    p = pv_model.expected_pv_day("2026-06-21", SITE)
    assert len(p) == 24 and p.max() > 0 and p.iloc[0] == 0


# ------------------------------------------------------------------- 수요 --

def test_sessions_are_deterministic_and_respect_chargers_and_windows():
    s1, un1, unmet1 = generate_sessions(SITE, "2026-06-15", 7, seed=4)
    s2, un2, unmet2 = generate_sessions(SITE, "2026-06-15", 7, seed=4)
    assert s1 == s2 and (un1, unmet1) == (un2, unmet2)
    assert generate_sessions(SITE, "2026-06-15", 7, seed=5)[0] != s1
    horizon = horizon_hours(7)
    occupied = np.zeros(horizon, dtype=int)
    for s in s1:
        assert 0 <= s.arrival < s.departure <= s.arrival + MAX_DWELL_H
        assert s.need_kwh <= (s.departure - s.arrival) * SITE.charger_kw + 1e-9
        assert 0 <= s.charger < SITE.n_chargers
        occupied[s.arrival:s.departure] += 1
    assert occupied.max() <= SITE.n_chargers                      # 동시 충전 수가 충전기 수를 넘지 않는다


def test_weekdays_have_more_sessions_than_weekends():
    wd = len(generate_sessions(SITE, "2026-06-15", 5, seed=1)[0])    # 월~금
    we = len(generate_sessions(SITE, "2026-06-20", 2, seed=1)[0])    # 토·일
    assert wd / 5 > we / 2


def test_invalid_days():
    with pytest.raises(ValueError):
        generate_sessions(SITE, "2026-06-15", 0)


# ------------------------------------------------------------------ 스케줄 --

@pytest.fixture(scope="module")
def scenario():
    start, days, seed = "2026-06-15", 5, 2
    hours = horizon_hours(days)
    idx = pd.date_range(start, periods=hours, freq="h")
    pv = pv_model.pv_power_kw(idx, SITE, seed=seed).to_numpy()
    price = np.where((idx.hour >= 9) & (idx.hour < 23), 180.0, 100.0)
    sessions, _un, _unmet = generate_sessions(SITE, start, days, seed)
    return sessions, pv, price, hours


def test_baseline_meets_energy_within_windows_and_power_limit(scenario):
    sessions, pv, price, hours = scenario
    sched = dispatch.baseline_schedule(sessions, SITE.charger_kw, hours)
    for s in sessions:
        row = sched[s.session_id]
        assert row.sum() == pytest.approx(s.need_kwh, abs=1e-6)
        assert row.max() <= SITE.charger_kw + 1e-9
        assert row[:s.arrival].sum() == 0 and row[s.departure:].sum() == 0


def test_optimized_meets_energy_within_windows_and_power_limit(scenario):
    sessions, pv, price, hours = scenario
    sched, status = dispatch.optimize_schedule(sessions, SITE.charger_kw, pv, price)
    assert status == "optimal"
    for s in sessions:
        row = sched[s.session_id]
        assert row.sum() == pytest.approx(s.need_kwh, abs=1e-5)
        assert row.max() <= SITE.charger_kw + 1e-6 and row.min() >= -1e-9
        assert row[:s.arrival].sum() == 0 and row[s.departure:].sum() == 0


def test_optimization_raises_self_consumption_and_lowers_cost(scenario):
    sessions, pv, price, hours = scenario
    args = (pv, price, SITE.pv_price_krw_per_kwh, SITE.grid_import_limit_kw)
    base = dispatch.evaluate(sessions, dispatch.baseline_schedule(sessions, SITE.charger_kw, hours), *args)["summary"]
    sched, _ = dispatch.optimize_schedule(sessions, SITE.charger_kw, pv, price)
    opt = dispatch.evaluate(sessions, sched, *args)["summary"]
    assert opt["ev_energy_kwh"] == pytest.approx(base["ev_energy_kwh"], abs=0.5)      # 같은 에너지를 충전
    assert opt["self_consumption_pct"] > base["self_consumption_pct"]
    assert opt["grid_cost_krw"] <= base["grid_cost_krw"]
    assert opt["driver_cost_krw"] < base["driver_cost_krw"]


def test_evaluate_conserves_energy_and_cost(scenario):
    sessions, pv, price, hours = scenario
    sched, _ = dispatch.optimize_schedule(sessions, SITE.charger_kw, pv, price)
    ev = dispatch.evaluate(sessions, sched, pv, price, SITE.pv_price_krw_per_kwh, SITE.grid_import_limit_kw)
    h, s, per = ev["hourly"], ev["summary"], ev["per_session"]
    assert np.allclose(h["pv_used_kw"] + h["grid_kw"], h["load_kw"])                  # 사용 = 태양광 + 계통
    assert np.allclose(h["pv_used_kw"] + h["curtailed_kw"], pv)                      # 발전 = 사용 + 잉여
    assert sum(p["pv_kwh"] for p in per) == pytest.approx(h["pv_used_kw"].sum(), abs=0.5)
    assert sum(p["grid_kwh"] for p in per) == pytest.approx(h["grid_kw"].sum(), abs=0.5)
    assert sum(p["cost_krw"] for p in per) == pytest.approx(s["driver_cost_krw"], rel=0.01, abs=len(per))
    assert all(0 <= p["solar_share_pct"] <= 100 for p in per)


def test_infeasible_plan_falls_back_to_baseline():
    impossible = [Session(0, 0, 2, need_kwh=50.0, charger=0, requested_kwh=50.0, bucket="x")]   # 2시간×7kW로 50kWh 불가
    sched, status = dispatch.optimize_schedule(impossible, 7.0, np.zeros(6), np.full(6, 100.0))
    assert status.startswith("fallback") and sched.shape == (1, 6)


def test_empty_sessions():
    sched, status = dispatch.optimize_schedule([], 7.0, np.zeros(10), np.zeros(10))
    assert status == "empty" and sched.shape == (0, 10)


# ---------------------------------------------------------------- 에뮬레이터 --

def test_emulation_end_to_end_and_flags_assumptions():
    r = run_emulation(SITE, "2026-06-15", days=3, seed=2)
    assert r["verified"] is False and "가정값" in r["assumptions"]["note"]
    assert r["optimizer_status"] == "optimal" and r["period"]["days"] == 3
    assert r["optimized"]["self_consumption_pct"] > r["baseline"]["self_consumption_pct"]
    assert r["comparison"]["driver_cost_saving_krw"] > 0
    assert len(r["hourly"]) == r["period"]["horizon_hours"] and len(r["daily"]) == 3
    assert r["demand"]["sessions_served"] == len(r["per_session"]["baseline"])
    assert r["baseline"]["ev_energy_kwh"] == pytest.approx(r["optimized"]["ev_energy_kwh"], abs=0.5)


def test_emulation_is_reproducible():
    a = run_emulation(SITE, "2026-06-15", days=2, seed=9)
    b = run_emulation(SITE, "2026-06-15", days=2, seed=9)
    assert a["optimized"] == b["optimized"] and a["hourly"] == b["hourly"]


def test_forecast_error_costs_something():
    perfect = run_emulation(SITE, "2026-06-15", days=7, seed=3, plan_with="perfect")
    clim = run_emulation(SITE, "2026-06-15", days=7, seed=3, plan_with="climatology")
    assert clim["baseline"] == perfect["baseline"]                        # 기준안은 계획 방식과 무관
    assert clim["optimized"]["self_consumption_pct"] <= perfect["optimized"]["self_consumption_pct"] + 1e-6
    assert clim["comparison"]["driver_cost_saving_krw"] > 0               # 평균 날씨만 알아도 이득은 남는다


@pytest.mark.parametrize("kwargs", [{"days": 0}, {"days": 32}, {"plan_with": "psychic"}])
def test_emulation_input_validation(kwargs):
    with pytest.raises(EmulationError):
        run_emulation(SITE, "2026-06-15", **{"days": 3, **kwargs})


# ---------------------------------------------------------------------- API --

@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("opuser", "op-password-1", "admin")
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    yield
    client.post("/auth/logout")


def test_solar_api_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    assert client.get("/solar/sites").status_code == 401
    auth_store.create_user("driver1", "driver-pass-1", "owner", owned_ev_id="EV-001")
    client.post("/auth/login", json={"username": "driver1", "password": "driver-pass-1"})
    assert client.get("/solar/sites").status_code == 403
    assert client.post("/solar/emulate", json={}).status_code == 403
    client.post("/auth/logout")


def test_solar_api_sites_emulate_and_pv_day(admin):
    sites = client.get("/solar/sites").json()["sites"]
    assert sites[0]["site_id"] == "naju_solar_ev" and sites[0]["verified"] is False
    r = client.post("/solar/emulate", json={"start_date": "2026-06-15", "days": 2, "seed": 1})
    assert r.status_code == 200 and r.json()["verified"] is False
    assert client.post("/solar/emulate", json={"days": 99}).status_code == 422
    assert client.post("/solar/emulate", json={"plan_with": "x"}).status_code == 422
    assert client.post("/solar/emulate", json={"site_id": "nope"}).status_code == 404
    d = client.get("/solar/pv-day", params={"day": "2026-06-21"}).json()
    assert len(d["clear_pv_kw"]) == 24 and max(d["clear_pv_kw"]) > 50 and d["clear_pv_kw"][0] == 0
    assert d["verified"] is False and client.get("/solar/pv-day", params={"site_id": "nope"}).status_code == 404
