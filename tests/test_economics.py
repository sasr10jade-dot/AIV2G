"""경제성 반영 최적화(열화·효율·방전횟수·사용자 입력)와 WAPE·수익 백테스트 테스트."""
import numpy as np
import pandas as pd
import pytest

from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.model.backtest import revenue_backtest
from src.model.demand_forecast import wape
from src.optimizer.economics import EconomicsParams, derive_degradation_krw_per_kwh
from src.optimizer.poc_compare import _single_vehicle_arbitrage_schedule, plan_next_session, summarize_session_plan


def _hours(start, n):
    return list(pd.date_range(start, periods=n, freq="h"))


def _arb(times, prices, eco, needed=0.0):
    return _single_vehicle_arbitrage_schedule(
        times, dict(zip(times, prices)), needed, 30.0, 30.0, 7.0, 7.0, True, eco)


def _discharged(s):
    return sum(d for _c, d in s.values())


def test_derive_degradation_and_param_validation():
    assert derive_degradation_krw_per_kwh(135000, 3000) == 45.0
    assert derive_degradation_krw_per_kwh(135000, 3000, 0.5) == 90.0
    with pytest.raises(ValueError):
        derive_degradation_krw_per_kwh(1, 0)
    with pytest.raises(ValueError):
        EconomicsParams(charge_efficiency=1.2)


def test_default_config_loads_with_derived_degradation_and_unverified():
    eco = EconomicsParams.load()
    assert eco.degradation_krw_per_kwh == pytest.approx(140000 / 3000) and not eco.verified
    assert eco.max_discharge_events_per_day == 2


def test_small_spread_not_worth_discharging_after_losses_and_degradation():
    times = _hours("2026-09-01 00:00", 6)
    prices = [100, 100, 100, 110, 110, 110]  # 스프레드 10원 < 효율손실 + 열화
    free = _arb(times, prices, EconomicsParams())
    real = _arb(times, prices, EconomicsParams(0.92, 0.92, 45.0))
    assert _discharged(free) > 0 and _discharged(real) == 0


def test_large_spread_still_discharges_with_degradation():
    times = _hours("2026-09-01 00:00", 6)
    s = _arb(times, [50, 50, 50, 400, 400, 400], EconomicsParams(0.92, 0.92, 45.0))
    assert _discharged(s) > 0


def test_daily_discharge_event_cap_is_respected():
    times = _hours("2026-09-01 00:00", 12)
    prices = [50, 400] * 6  # 싼/비싼 시간이 번갈아 → 상한이 없으면 방전 구간이 여러 번
    uncapped = _arb(times, prices, EconomicsParams(1.0, 1.0, 0.0, None))
    capped = _arb(times, prices, EconomicsParams(1.0, 1.0, 0.0, 1))

    def blocks(s):
        on = [s[t][1] > 1e-6 for t in times]
        return sum(1 for i, v in enumerate(on) if v and (i == 0 or not on[i - 1]))

    assert blocks(uncapped) > 1 and blocks(capped) == 1


def test_target_soc_is_still_met_with_efficiency():
    times = _hours("2026-09-01 00:00", 8)
    s = _arb(times, [100] * 8, EconomicsParams(0.9, 0.9, 45.0), needed=10.0)
    stored = sum(c for c, _d in s.values()) * 0.9 - _discharged(s) / 0.9
    assert stored >= 10.0 - 1e-3  # 4자리 반올림·솔버 허용오차


def test_summary_deducts_degradation_and_carries_assumptions():
    eco = EconomicsParams(0.92, 0.92, 45.0, 2, False)
    times = _hours("2026-09-01 00:00", 6)
    prices = dict(zip(times, [50, 50, 50, 400, 400, 400]))
    sched = {"S": _single_vehicle_arbitrage_schedule(times, prices, 0.0, 30.0, 30.0, 7.0, 7.0, True, eco)}
    sessions = {"S": {"times": times, "soc_baseline_pct": {t: 50.0 for t in times}, "capacity_kwh": 64.0}}
    plan = summarize_session_plan("S", sessions, sched, {"S": "EV"}, prices, eco)
    assert plan["degradation_cost_krw"] > 0
    assert plan["net_krw"] == pytest.approx(plan["net_before_degradation_krw"] - plan["degradation_cost_krw"], abs=1)
    assert plan["assumptions"]["verified"] is False
    legacy = summarize_session_plan("S", sessions, sched, {"S": "EV"}, prices)
    assert "assumptions" not in legacy


def test_plan_next_session_uses_user_inputs():
    cfg = SimConfig(n_ev=2, weeks=8, n_chargers=2, charger_days=56, n_zones=1)
    rng = np.random.default_rng(cfg.random_seed)
    ev_df = generate_ev_telemetry(cfg, rng)
    grid_df = generate_grid_signal(cfg, rng)
    ev_id = ev_df["ev_id"].iloc[0]
    last = ev_df[ev_df["ev_id"] == ev_id]["timestamp"].max()
    dep = (last + pd.Timedelta(hours=14)).isoformat()
    plan = plan_next_session(ev_df, grid_df, cfg, ev_id, economics=EconomicsParams(0.92, 0.92, 45.0, 2),
                             departure_time=dep, current_soc_pct=45.0, need_soc_pct=80.0)
    assert plan["departure_time"].startswith(dep[:13])
    assert plan["observed_start_soc_pct"] == 45.0
    assert plan["inputs_from_user"] == {"departure_time": True, "current_soc": True, "need_soc": True}
    assert plan["is_predicted"] is False


def test_wape():
    assert wape(np.array([100, 100]), np.array([90, 120])) == pytest.approx(15.0)
    assert wape(np.array([0, 0]), np.array([1, 1])) == 0.0


def test_revenue_backtest_oracle_is_upper_bound_and_perfect_forecast_matches():
    eco = EconomicsParams(0.92, 0.92, 45.0, None)
    times = _hours("2026-09-02 00:00", 8)
    hist = _hours("2026-09-01 00:00", 8)
    shape = [60, 60, 60, 60, 300, 300, 300, 300]
    actual = dict(zip(hist, shape))
    actual.update(dict(zip(times, shape)))
    perfect = revenue_backtest(times, actual, dict(actual), eco)
    assert perfect["capture_ratio"] == pytest.approx(1.0) and perfect["oracle_net_krw"] > 0
    wrong = dict(zip(times, [300, 300, 300, 300, 60, 60, 60, 60]))  # 반대로 예측
    bad = revenue_backtest(times, actual, wrong, eco)
    assert bad["forecast_net_krw"] <= bad["oracle_net_krw"]
    assert bad["capture_ratio"] is None or bad["capture_ratio"] < perfect["capture_ratio"]


def test_fleet_optimizer_with_economics_discharges_less_and_stays_feasible():
    from src.optimizer.decision import optimize_fleet
    from src.optimizer.poc_compare import compute_fleet_revenue_comparison

    times = _hours("2026-09-01 00:00", 8)
    sessions = {"S": {"times": times, "soc_baseline_pct": {t: 60.0 for t in times}, "capacity_kwh": 64.0, "v2g_capable": True}}
    price = dict(zip(times, [60, 60, 60, 60, 300, 300, 300, 300]))
    kw = dict(max_charge_kw=7.0, max_discharge_kw=7.0, reserve_soc=0.2, target_soc=0.9, price_by_time=price)
    free = optimize_fleet(sessions, **kw)
    real = optimize_fleet(sessions, economics=EconomicsParams(0.92, 0.92, 200.0), **kw)  # 열화비용이 큰 경우
    d = lambda s: sum(x[1] for x in s["S"].values())
    assert d(real) <= d(free) + 1e-6
    # 목표 SOC(필요 에너지)는 효율을 감안해도 충족
    need = (90 - 60) / 100 * 64.0
    stored = sum(c for c, _ in real["S"].values()) * 0.92 - d(real) / 0.92
    assert stored >= min(need, 7.0 * 8 * 0.92) - 1e-2

    comp = pd.DataFrame({"timestamp": times, "baseline_kw": [5.0] * 8, "optimized_kw": [4.0] * 8, "discharge_kw": [1.0] * 8})
    grid = pd.DataFrame({"timestamp": times, "price_krw_per_kwh": [100.0] * 8})
    base = compute_fleet_revenue_comparison(comp, grid)
    net = compute_fleet_revenue_comparison(comp, grid, EconomicsParams(0.92, 0.92, 46.0))
    assert "assumptions" not in base
    assert net["degradation_cost_krw"] == pytest.approx(8 / 0.92 * 46.0, abs=1)
    assert net["savings_krw"] == pytest.approx(net["savings_before_degradation_krw"] - net["degradation_cost_krw"], abs=1)
