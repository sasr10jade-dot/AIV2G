"""Raw → Curated 파이프라인 오케스트레이션 (WS1 "표준화·정제하여 통합 데이터셋 구축")."""
import pandas as pd

from src.paths import RAW_DIR
from src.datagen.config import SimConfig
from . import schema, store
from .quality import validate, summarize


def run(cfg: SimConfig | None = None) -> dict:
    cfg = cfg or SimConfig()

    ev_df = pd.read_csv(RAW_DIR / "ev_telemetry.csv", parse_dates=["timestamp"])
    charger_df = pd.read_csv(RAW_DIR / "charger_sessions.csv", parse_dates=["start_ts"])
    grid_df = pd.read_csv(RAW_DIR / "grid_signal.csv", parse_dates=["timestamp"])

    reports = [
        validate(ev_df, "ev_telemetry", schema.EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"]),
        validate(charger_df, "charger_sessions", schema.CHARGER_SESSION_SCHEMA, dedup_keys=["session_id"]),
        validate(grid_df, "grid_signal", schema.GRID_SIGNAL_SCHEMA, dedup_keys=["timestamp"]),
    ]
    quality_md = summarize(reports)
    if not all(r.passed for r in reports):
        failed = [r.table for r in reports if not r.passed]
        raise ValueError(f"데이터 품질 검증 실패: {failed}\n{quality_md}")

    weekly_report = store.build_ev_weekly_report(ev_df, cfg)
    city_hourly = store.build_city_hourly(ev_df, grid_df, cfg)

    con = store.get_connection()
    try:
        store.write_table(ev_df, "ev_telemetry", con)
        store.write_table(charger_df, "charger_sessions", con)
        store.write_table(grid_df, "grid_signal", con)
        store.write_table(weekly_report, "ev_weekly_report", con)
        store.write_table(city_hourly, "city_hourly", con)
        con.commit()
    finally:
        con.close()

    from src.kpi import summarize_kpi
    kpi = summarize_kpi(ev_df, charger_df, grid_df)
    return {
        "quality_report_md": quality_md,
        "ev_weekly_report_rows": len(weekly_report),
        "charger_sessions_rows": len(charger_df),
        "kpi_by_year": [y.__dict__ for y in kpi.by_year],
        "kpi_scenario_records": kpi.scenario_records,
        "city_hourly_rows": len(city_hourly),
    }


if __name__ == "__main__":
    result = run()
    print(result["quality_report_md"])
    for y in result["kpi_by_year"]:
        print(f"\nKPI {y['label']} 데이터: {y['records']:,} / 목표 {y['target']:,}건 이상 "
              f"(충전 세션 {y['charger_sessions']:,} + 차량-일 {y['vehicle_days']:,})")
    print(f"사업기간 구분 없이 센 전체 유효 건수(참고): {result['kpi_scenario_records']:,}")
