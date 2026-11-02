"""차주 가격 모델(FR-P01~P04) — 충전 단가와 방전 보상단가 분리, 손익분기, 차주 비용 비교."""
import json

import pandas as pd
import pytest

from src.grid import dr_events
from src.grid import pricing as pr
from src.grid import tariff as t
from src.optimizer.economics import EconomicsParams, breakeven_sell_price
from src.optimizer.poc_compare import _single_vehicle_arbitrage_schedule, compute_owner_cost_comparison

TIMES = list(pd.date_range("2026-11-02 00:00", periods=24, freq="h"))
ECO = EconomicsParams(charge_efficiency=0.92, discharge_efficiency=0.92,
                      degradation_krw_per_kwh=140000 / 3000, max_discharge_events_per_day=2)


def test_public_tariffs_registered_unverified_and_mapped_by_charger_type():
    tariffs = t.load_tariffs()
    assert tariffs["public_slow_2026_08"].price_at(TIMES[3]) == 295.0
    assert tariffs["public_ultra_2026_08"].price_at(TIMES[3]) == 393.1
    assert not tariffs["public_slow_2026_08"].verified
    assert t.tariff_for_charger_type("public_slow").tariff_id == "public_slow_2026_08"
    assert t.tariff_for_charger_type("home").tariff_id == "kepco_ev_example"
    with pytest.raises(t.TariffError):
        t.tariff_for_charger_type("unknown_type")


def test_compensation_scenarios_load_and_validate(tmp_path):
    comps = pr.load_compensations()
    assert comps["none"].kind == "zero" and comps["none"].verified
    assert comps["smp"].kind == "grid_signal" and not comps["smp"].verified
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"compensations": {"x": {"kind": "flat"}}}), encoding="utf-8")
    with pytest.raises(t.TariffError):
        pr.load_compensations(p)
    p.write_text(json.dumps({"compensations": {"x": {"kind": "bogus"}}}), encoding="utf-8")
    with pytest.raises(t.TariffError):
        pr.load_compensations(p)


def test_price_model_separates_buy_and_sell():
    grid = {ts: 120.0 for ts in TIMES}
    pm = pr.build_price_model(TIMES, grid, "public_slow_2026_08", "smp")
    assert pm.buy_by_time[TIMES[0]] == 295.0
    assert pm.sell_by_time[TIMES[0]] == 120.0
    assert not pm.verified
    zero = pr.build_price_model(TIMES, grid, "public_slow_2026_08", "none")
    assert set(zero.sell_by_time.values()) == {0.0}
    flat = pr.build_price_model(TIMES, grid, "kepco_ev_example", "contract_example")
    assert set(flat.sell_by_time.values()) == {300.0}
    assert flat.buy_by_time[TIMES[3]] == 100 and flat.buy_by_time[TIMES[12]] == 180


def test_dr_premium_applies_to_sell_only(monkeypatch):
    monkeypatch.setitem(dr_events.DR_EVENTS, "e1", {
        "start_ts": TIMES[18], "end_ts": TIMES[20], "severity": "high",
    })
    grid = {ts: 100.0 for ts in TIMES}
    pm = pr.build_price_model(TIMES, grid, "public_slow_2026_08", "smp")
    assert pm.sell_by_time[TIMES[18]] > 100.0
    assert pm.sell_by_time[TIMES[10]] == 100.0
    assert pm.buy_by_time[TIMES[18]] == 295.0


def test_breakeven_matches_prd_examples():
    assert breakeven_sell_price(295.0, ECO) == pytest.approx(399.3, abs=0.1)
    assert breakeven_sell_price(100.0, ECO) == pytest.approx(168.9, abs=0.1)
    assert breakeven_sell_price(393.1, ECO) == pytest.approx(515.2, abs=0.1)


def _plan(buy, sell):
    return _single_vehicle_arbitrage_schedule(
        TIMES, buy, needed_kwh=10.0, max_net_kwh=30.0, headroom_kwh=20.0,
        max_charge_kw=7.0, max_discharge_kw=7.0, discharge_allowed=True,
        economics=ECO, sell_price_by_time=sell,
    )


def test_no_discharge_when_compensation_below_breakeven():
    buy = {ts: 295.0 for ts in TIMES}
    sell = {ts: (380.0 if 18 <= ts.hour <= 20 else 120.0) for ts in TIMES}  # 손익분기 399원 미만
    sched = _plan(buy, sell)
    assert sum(d for _c, d in sched.values()) == pytest.approx(0.0, abs=1e-6)


def test_discharge_when_compensation_above_breakeven():
    buy = {ts: (100.0 if ts.hour < 9 else 180.0) for ts in TIMES}
    sell = {ts: (400.0 if 18 <= ts.hour <= 20 else 0.0) for ts in TIMES}  # 경부하 손익분기 169원 초과
    sched = _plan(buy, sell)
    discharged = {ts.hour: d for ts, (_c, d) in sched.items() if d > 1e-6}
    assert discharged and set(discharged) <= {18, 19, 20}


def test_legacy_single_price_unchanged_without_sell():
    price = {ts: (100.0 if ts.hour < 9 else 400.0) for ts in TIMES}
    legacy = _single_vehicle_arbitrage_schedule(
        TIMES, price, 10.0, 30.0, 20.0, 7.0, 7.0, True, ECO)
    explicit = _plan(price, price)
    assert legacy == explicit


def test_owner_cost_comparison_uses_buy_for_charge_and_sell_for_discharge():
    ts = TIMES[:3]
    comparison = pd.DataFrame({
        "timestamp": ts,
        "baseline_kw": [10.0, 0.0, 0.0],
        "optimized_kw": [12.0, 0.0, -2.0],  # 12 충전, 2 방전
        "discharge_kw": [0.0, 0.0, 2.0],
    })
    pm = pr.build_price_model(ts, {x: 50.0 for x in ts}, "public_slow_2026_08", "smp")
    out = compute_owner_cost_comparison(comparison, pm, ECO)
    assert out["baseline_cost_krw"] == 2950
    assert out["optimized_charge_cost_krw"] == 3540
    assert out["discharge_revenue_krw"] == 100
    assert out["degradation_cost_krw"] == round(2 / 0.92 * 140000 / 3000)
    assert out["savings_krw"] < 0  # 공공 단가로 사서 SMP로 되팔면 손해
    assert out["verified"] is False and out["price_model"]["compensation_id"] == "smp"
