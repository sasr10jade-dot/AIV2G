import numpy as np

from src.closed_loop import run_closed_loop_cycle


def test_closed_loop_cycle_runs_and_returns_finite_metrics(medium_cfg):
    result = run_closed_loop_cycle(medium_cfg, fleet_growth=2)

    for key in ("initial_mae", "stale_model_mae_on_new_week", "retrained_mae_on_new_week"):
        assert np.isfinite(result[key])
        assert result[key] >= 0

    assert isinstance(result["retrain_recommended"], bool)
    assert result["retrain_recommended"] == (result["drift_pct"] > result["trigger_threshold_pct"])
