import pandas as pd

from src.pipeline.store import append_dashboard_run_history, read_dashboard_run_history


def test_read_history_returns_empty_when_no_table_yet(tmp_path):
    db_path = tmp_path / "empty.db"
    df = read_dashboard_run_history(db_path=db_path)
    assert df.empty


def test_append_then_read_round_trip(tmp_path):
    db_path = tmp_path / "history.db"
    record = {
        "generated_at": "2026-09-01T00:00:00", "n_ev": 40, "weeks": 16,
        "savings_krw": 1000.0, "savings_pct": 20.0, "forecast_mae": 1.5,
        "closed_loop_drift_pct": 5.0, "closed_loop_improve_pct": 10.0,
    }
    append_dashboard_run_history(record, db_path=db_path)
    df = read_dashboard_run_history(db_path=db_path)
    assert len(df) == 1
    assert df.iloc[0]["n_ev"] == 40
    assert df.iloc[0]["savings_pct"] == 20.0


def test_read_history_orders_oldest_first_and_respects_limit(tmp_path):
    db_path = tmp_path / "history.db"
    for i in range(5):
        append_dashboard_run_history({
            "generated_at": f"2026-09-0{i + 1}T00:00:00", "n_ev": 40, "weeks": 16,
            "savings_krw": 1000.0 + i, "savings_pct": 20.0 + i, "forecast_mae": 1.5,
            "closed_loop_drift_pct": 5.0, "closed_loop_improve_pct": 10.0,
        }, db_path=db_path)

    df = read_dashboard_run_history(limit=3, db_path=db_path)
    assert len(df) == 3
    # 가장 최근 3건(3,4,5일)이되, 오래된 순으로 정렬돼야 함
    assert list(df["generated_at"]) == [
        "2026-09-03T00:00:00", "2026-09-04T00:00:00", "2026-09-05T00:00:00",
    ]
