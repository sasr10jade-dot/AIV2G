"""실증 결과 요약 보고서 생성 (Monitoring/Reporting 레이어).

데이터 품질 검증 → KPI 집계 → PoC 비교(피크저감) → Closed-loop 재학습 검증 결과를
하나의 마크다운 보고서로 정리해 output/에 저장한다.
"""
import numpy as np

from src.closed_loop import run_closed_loop_cycle
from src.datagen.charger import generate_charger_sessions
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.optimizer.poc_compare import compare_baseline_vs_optimized
from src.paths import OUTPUT_DIR
from src.pipeline.quality import validate, summarize
from src.pipeline.schema import CHARGER_SESSION_SCHEMA, EV_TELEMETRY_SCHEMA, GRID_SIGNAL_SCHEMA
from src.pipeline.store import build_ev_weekly_report
from src.viz import plot_poc_comparison


def build_report(cfg: SimConfig | None = None) -> str:
    cfg = cfg or SimConfig()
    rng = np.random.default_rng(cfg.random_seed)

    ev_df = generate_ev_telemetry(cfg, rng)
    charger_df = generate_charger_sessions(cfg, rng)
    grid_df = generate_grid_signal(cfg, rng)

    quality_md = summarize(
        [
            validate(ev_df, "ev_telemetry", EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"]),
            validate(charger_df, "charger_sessions", CHARGER_SESSION_SCHEMA, dedup_keys=["session_id"]),
            validate(grid_df, "grid_signal", GRID_SIGNAL_SCHEMA, dedup_keys=["timestamp"]),
        ]
    )

    weekly_report = build_ev_weekly_report(ev_df, cfg)
    from src.kpi import summarize_kpi
    kpi = summarize_kpi(ev_df, charger_df, grid_df)
    kpi_data_records = kpi.internal_records

    comparison = compare_baseline_vs_optimized(ev_df, grid_df, cfg)
    chart_path = plot_poc_comparison(comparison)
    baseline_peak = float(comparison["baseline_kw"].max())
    optimized_peak = float(comparison["optimized_kw"].max())
    peak_reduction_pct = (baseline_peak - optimized_peak) / baseline_peak * 100 if baseline_peak else 0.0

    closed_loop = run_closed_loop_cycle(cfg)

    lines = [
        "# AI-V2G 실증 결과 요약 보고서",
        "",
        f"- 실증 규모: EV {cfg.n_ev}대, 충전기 {cfg.n_chargers}기, 관측기간 {cfg.weeks}주",
        "",
        "## 1. 데이터 품질",
        quality_md,
        "",
        f"## 2. KPI: 데이터셋 구축 — {kpi_data_records:,}건 (목표 {kpi.internal_target:,}건 이상: A 세션 {kpi.a_sessions:,} + B 차량-일 {kpi.b_vehicle_days:,} + 방전 {kpi.b_discharge_events:,})",
        "",
        "## 3. AI-V2G 최적화 vs 일반충전 (PoC 비교)",
        f"- 무제어(baseline) 피크: {baseline_peak:.1f} kW",
        f"- AI-V2G 최적화 피크: {optimized_peak:.1f} kW",
        f"- 피크 저감률: {peak_reduction_pct:.1f}%",
        f"- 차트: {chart_path}",
        "",
        "## 4. Closed-loop 재학습 검증",
        f"- 최초 모델 MAE: {closed_loop['initial_mae']}",
        f"- 방치 모델(재학습 없음) MAE: {closed_loop['stale_model_mae_on_new_week']} (드리프트 {closed_loop['drift_pct']}%)",
        f"- 재학습 모델 MAE: {closed_loop['retrained_mae_on_new_week']} (개선 {closed_loop['improve_pct']}%)",
        f"- 재학습 필요 판정: {'예' if closed_loop['retrain_recommended'] else '아니오'}",
    ]
    report_md = "\n".join(lines)

    out_path = OUTPUT_DIR / "실증_결과_요약.md"
    out_path.write_text(report_md, encoding="utf-8")
    return str(out_path)


if __name__ == "__main__":
    path = build_report()
    print(f"보고서 생성 완료: {path}")
