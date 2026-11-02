"""EV 차량별 입출차 패턴 예측(src/model/plugin_pattern.py) 테스트."""
import numpy as np
import pytest

from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.model.plugin_pattern import (
    InsufficientHistoryError,
    UnknownVehicleError,
    forecast_plugin_schedule,
)


def _ev_df(weeks: int, n_ev: int = 2):
    cfg = SimConfig(n_ev=n_ev, weeks=weeks, n_chargers=2, charger_days=weeks * 7, n_zones=1)
    rng = np.random.default_rng(cfg.random_seed)
    return generate_ev_telemetry(cfg, rng)


def test_forecast_plugin_schedule_returns_probability_curve():
    df = _ev_df(weeks=8)
    ev_id = df["ev_id"].iloc[0]
    result = forecast_plugin_schedule(df, ev_id, horizon_hours=48)

    assert result["ev_id"] == ev_id
    assert result["n_weeks_history"] >= 2.0
    assert len(result["timestamps"]) == 48
    assert len(result["connection_probability"]) == 48
    assert all(0.0 <= p <= 1.0 for p in result["connection_probability"])


def test_forecast_plugin_schedule_unknown_vehicle_raises():
    df = _ev_df(weeks=8)
    with pytest.raises(UnknownVehicleError):
        forecast_plugin_schedule(df, "NO-SUCH-EV")


def test_forecast_plugin_schedule_insufficient_history_raises():
    df = _ev_df(weeks=1)
    ev_id = df["ev_id"].iloc[0]
    with pytest.raises(InsufficientHistoryError):
        forecast_plugin_schedule(df, ev_id)


def test_forecast_plugin_schedule_departure_soc_is_within_range_when_present():
    df = _ev_df(weeks=8)
    ev_id = df["ev_id"].iloc[0]
    result = forecast_plugin_schedule(df, ev_id)
    if result["expected_departure_soc_pct"] is not None:
        assert 0.0 <= result["expected_departure_soc_pct"] <= 100.0
