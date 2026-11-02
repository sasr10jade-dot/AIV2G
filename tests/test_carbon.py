"""탄소배출 절감 효과 추정 테스트."""
import pandas as pd

from src.optimizer.carbon import GRID_EMISSION_FACTOR_KG_PER_KWH, compute_carbon_reduction


def _comparison(baseline, optimized):
    ts = pd.date_range("2026-09-01", periods=len(baseline), freq="h")
    return pd.DataFrame({"timestamp": ts, "baseline_kw": baseline, "optimized_kw": optimized})


def _grid(renewable_mw, n=None):
    n = n or len(renewable_mw)
    ts = pd.date_range("2026-09-01", periods=n, freq="h")
    return pd.DataFrame({"timestamp": ts, "renewable_gen_mw": renewable_mw})


def test_pure_retiming_with_no_discharge_yields_zero_optimization_co2():
    """총 에너지량은 그대로 두고 시점만 옮기는 밸리필링(방전 없음)은 고정
    배출계수 가정 하에서 최적화로 인한 탄소회피가 0이어야 한다."""
    comparison = _comparison(baseline=[10, 0, 10, 0], optimized=[5, 5, 5, 5])  # 합계 동일(20)
    grid_df = _grid([0, 0, 0, 0])
    result = compute_carbon_reduction(comparison, grid_df)
    assert result["net_energy_saved_kwh"] == 0.0
    assert result["optimization_co2_kg"] == 0.0


def test_v2g_discharge_reduces_net_grid_energy_and_yields_positive_co2():
    comparison = _comparison(baseline=[10, 10, 10, 10], optimized=[10, 10, -5, 10])  # 방전으로 순에너지 15kWh 감소(합계 40->25)
    grid_df = _grid([0, 0, 0, 0])
    result = compute_carbon_reduction(comparison, grid_df)
    assert result["net_energy_saved_kwh"] == 15.0
    assert result["optimization_co2_kg"] == round(15.0 * GRID_EMISSION_FACTOR_KG_PER_KWH, 1)


def test_renewable_generation_contributes_its_own_co2_avoidance():
    comparison = _comparison(baseline=[10, 10], optimized=[10, 10])
    grid_df = _grid([2.0, 3.0])  # 5 MWh 합계
    result = compute_carbon_reduction(comparison, grid_df)
    assert result["renewable_mwh"] == 5.0
    assert result["renewable_co2_kg"] == round(5.0 * 1000 * GRID_EMISSION_FACTOR_KG_PER_KWH, 1)
    assert result["total_co2_kg"] == result["optimization_co2_kg"] + result["renewable_co2_kg"]
    assert result["total_co2_ton"] == round(result["total_co2_kg"] / 1000, 2)


def test_negative_net_energy_change_does_not_produce_negative_co2():
    """어떤 이유로 optimized 총합이 baseline보다 커지는 극단적 케이스에서도
    회피 배출량이 음수로 나오면 안 된다(0으로 clamp)."""
    comparison = _comparison(baseline=[5, 5], optimized=[6, 6])
    grid_df = _grid([0, 0])
    result = compute_carbon_reduction(comparison, grid_df)
    assert result["net_energy_saved_kwh"] == 0.0
    assert result["optimization_co2_kg"] == 0.0
