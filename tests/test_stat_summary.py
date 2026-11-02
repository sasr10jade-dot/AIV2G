import pandas as pd

from src.model.stat_summary import build_stat_summary


def test_build_stat_summary_returns_none_for_empty_history():
    assert build_stat_summary(pd.DataFrame()) is None


def test_build_stat_summary_single_run_notes_insufficient_history():
    history = pd.DataFrame([
        {"generated_at": "2026-09-01T00:00:00", "n_ev": 40, "weeks": 16, "savings_krw": 1000,
         "savings_pct": 20.0, "forecast_mae": 1.5, "closed_loop_drift_pct": 5.0, "closed_loop_improve_pct": 10.0},
    ])
    summary = build_stat_summary(history)
    assert "1건" in summary
    assert "20.0%" in summary


def test_build_stat_summary_two_runs_reports_trend():
    history = pd.DataFrame([
        {"generated_at": "2026-09-01T00:00:00", "n_ev": 40, "weeks": 16, "savings_krw": 1000,
         "savings_pct": 20.0, "forecast_mae": 2.0, "closed_loop_drift_pct": 5.0, "closed_loop_improve_pct": 10.0},
        {"generated_at": "2026-09-02T00:00:00", "n_ev": 40, "weeks": 16, "savings_krw": 1200,
         "savings_pct": 24.0, "forecast_mae": 1.5, "closed_loop_drift_pct": 4.0, "closed_loop_improve_pct": 12.0},
    ])
    summary = build_stat_summary(history)
    assert "개선" in summary  # 절감률도 오르고 MAE도 내려갔으니 둘 다 개선
    assert "24.0%" in summary


def test_build_stat_summary_reports_degradation_when_mae_worsens():
    history = pd.DataFrame([
        {"generated_at": "2026-09-01T00:00:00", "n_ev": 40, "weeks": 16, "savings_krw": 1000,
         "savings_pct": 20.0, "forecast_mae": 1.0, "closed_loop_drift_pct": 5.0, "closed_loop_improve_pct": 10.0},
        {"generated_at": "2026-09-02T00:00:00", "n_ev": 40, "weeks": 16, "savings_krw": 900,
         "savings_pct": 18.0, "forecast_mae": 2.0, "closed_loop_drift_pct": 6.0, "closed_loop_improve_pct": 8.0},
    ])
    summary = build_stat_summary(history)
    assert "악화" in summary
