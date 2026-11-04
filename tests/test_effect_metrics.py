"""AI 적용 전·후 효용성 검증 지표(계획서 4.1 다.) — 피크 비중·목표 배터리 도달·예측률·조건별 반복 분석."""
import numpy as np
import pandas as pd
import pytest

from src.datagen.config import SimConfig
from src.optimizer import effect_metrics as em
from src.optimizer.economics import EconomicsParams
from src.optimizer.poc_compare import aggregate_comparison, run_v2g_optimization
from src.pipeline.source import load_scenario


def test_prediction_rate_is_one_minus_wape():
    assert em.prediction_rate([10, 10], [10, 10]) == 100.0
    assert em.prediction_rate([10, 10], [9, 11]) == 90.0
    assert em.prediction_rate([1, 1], [10, 10]) == 0.0          # 0 미만은 0으로 자른다
    assert em.prediction_rate([0, 0], [1, 1]) is None


def test_peak_hours_are_top_quarter_of_each_day():
    ts = pd.date_range("2027-01-04", periods=48, freq="h")
    grid = pd.DataFrame({"timestamp": ts, "power_demand_mw": np.tile(np.arange(24), 2)})
    hours = em.peak_hours(grid)
    assert len(hours) == 12 and {t.hour for t in hours} == set(range(18, 24))


def test_peak_share_counts_charging_only():
    ts = pd.date_range("2027-01-04", periods=24, freq="h")
    grid = pd.DataFrame({"timestamp": ts, "power_demand_mw": np.arange(24)})
    comp = pd.DataFrame({"timestamp": ts, "baseline_kw": [0.0] * 18 + [10.0] * 6,
                         "optimized_kw": [10.0] * 6 + [0.0] * 12 + [-5.0] * 6, "discharge_kw": [0.0] * 18 + [5.0] * 6})
    p = em.peak_metrics(comp, grid)
    assert p["baseline_peak_charge_share_pct"] == 100.0 and p["optimized_peak_charge_share_pct"] == 0.0


def test_target_reach_uses_observed_end_soc_as_floor():
    rows = [{"baseline_end_soc_pct": 92, "optimized_end_soc_pct": 92},
            {"baseline_end_soc_pct": 70, "optimized_end_soc_pct": 90},
            {"baseline_end_soc_pct": 95, "optimized_end_soc_pct": 90}]   # 이용자보다 덜 채웠으면 미도달
    r = em.target_reach(rows, 0.9)
    assert r["baseline_reach_pct"] == round(2 / 3 * 100, 1) and r["optimized_reach_pct"] == round(2 / 3 * 100, 1)


@pytest.fixture(scope="module")
def small_run():
    cfg = SimConfig(n_ev=8, weeks=4, n_chargers=8, charger_days=28)
    ev, _ch, grid, _real = load_scenario(cfg)
    eco = EconomicsParams.load()
    run = run_v2g_optimization(ev, grid, cfg, economics=eco)
    return run, aggregate_comparison(ev, run["schedule"]), ev, grid, eco


def test_effect_metrics_on_scenario(small_run):
    run, comp, ev, grid, eco = small_run
    m = em.compute_effect_metrics(run, comp, ev, grid, 0.9, eco, v2g_forecast=([5, 5], [4, 6]))
    assert m["generation"] == "inferred" and m["v2g_prediction_rate_pct"] == 80.0
    assert m["cost"]["sessions"] == len(run["sessions"]) == m["target_reach"]["sessions"]
    # AI 최적화는 피크 시간대 충전 비중과 최대 부하를 줄이고, 목표 배터리 도달률을 떨어뜨리지 않는다
    assert m["peak"]["optimized_peak_charge_share_pct"] < m["peak"]["baseline_peak_charge_share_pct"]
    assert m["peak"]["optimized_max_load_kw"] <= m["peak"]["baseline_max_load_kw"]
    assert m["target_reach"]["optimized_reach_pct"] >= m["target_reach"]["baseline_reach_pct"]


def test_condition_breakdown_partitions_sessions(small_run):
    run, comp, ev, grid, eco = small_run
    c = em.compute_effect_metrics(run, comp, ev, grid, 0.9, eco)["conditions"]
    n = len(run["sessions"])
    for key in ("time_band", "day_type", "start_soc"):
        assert sum(g["sessions"] for g in c[key]) == n, key
    assert [g["label"] for g in c["power_demand"]] == ["전력수요 낮음", "전력수요 보통", "전력수요 높음"]
    assert sum(g["baseline_charge_share_pct"] for g in c["power_demand"]) == pytest.approx(100, abs=0.5)
