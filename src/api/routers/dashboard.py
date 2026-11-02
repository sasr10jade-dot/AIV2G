"""Monitoring/Reporting 레이어 — 웹 대시보드 (KPI, PoC 비교, 예측 적합도, Closed-loop).

report.py(마크다운 보고서)와 동일한 데이터를 사람이 브라우저에서 바로 볼 수 있는
형태로 재구성한다. 실증 환경 연계 전 단계이므로 report.py와 마찬가지로 SimConfig로
시나리오를 재생성해 보여준다(데이터 소스만 교체하면 실증 연계로 전환 가능).
"""
import os
import sys
import threading
import time
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from src.timeutil import kst_now
from src.auth.deps import require_admin
from src.automation.model_registry import ModelRegistry
from src.closed_loop import run_closed_loop_cycle
from src.datagen.config import SimConfig
from src.grid import dr_events
from src import kpi as kpi_targets
from src.kpi import summarize_kpi
from src.model.charging_efficiency import NoLinkedSessionsError, compute_charging_efficiency
from src.model.demand_forecast import train_forecast_model
from src.optimizer.carbon import compute_carbon_reduction
from src.optimizer.economics import EconomicsParams
from src.optimizer.poc_compare import (
    compare_baseline_vs_optimized,
    compute_dr_event_performance,
    compute_fleet_revenue_comparison,
)
from src.optimizer.profit_sharing import InvalidShareError, compute_profit_sharing
from src.pipeline import source
from src.pipeline.quality import validate
from src.pipeline.schema import CHARGER_SESSION_SCHEMA, EV_TELEMETRY_SCHEMA, GRID_SIGNAL_SCHEMA
from src.pipeline.source import load_scenario
from src.pipeline.store import (
    append_dashboard_run_history,
    build_charger_utilization_hourly,
    build_city_hourly,
    build_ev_weekly_report,
    build_renewable_summary,
    build_vehicle_status_table,
    get_connection,
)

router = APIRouter(tags=["dashboard"])

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
KNOWN_TARGETS = ["ev_charging_demand_kw", "ev_available_v2g_kwh"]
DEFAULT_TARGET = "ev_charging_demand_kw"
CHART_WINDOW_DAYS = 5  # viz.py의 정적 차트와 동일한 관례(최근/앞 5일만 표시)
POC_DAILY_WINDOW_DAYS = 56  # 주별 뷰: 최근 8주를 일 합계(kWh)로

# LP 최적화(compare_baseline_vs_optimized)와 Closed-loop 재학습 검증은 각각
# 4초·3초 가까이 걸리는데, 결과가 "지금" 시각과 무관하게 데이터+파라미터로만
# 결정된다 — 그런데도 화면을 열 때마다(특히 "라이브 데모" 자동새로고침·OCPP 상태
# 폴링이 겹칠 때) 매번 처음부터 다시 계산해서 동기 워커 스레드를 오래 점유했고,
# 이게 실제로 서버 전체가 응답 불능(hang)처럼 보이는 원인이 됐다(라이브 서버에서
# 재현 확인됨). 같은 조건(데이터·시나리오·최적화 파라미터 불변)이면 캐시를 쓴다.
_HEAVY_CACHE_TTL_SEC = 300  # 전자정부 웹표준 감리 기준(복잡한 리포트 5~10초) 대응 — 아래 백그라운드 워밍과 짝을 이룸
_HEAVY_CACHE_MAX_ENTRIES = 50
_heavy_compute_cache: dict[tuple, tuple[float, tuple]] = {}

# 대시보드 화면(dashboard.html)의 프리셋 버튼(기본/스케일/단기/보수)과 동일한 조합 —
# 사용자가 가장 자주 쓰는 조합을 백그라운드에서 미리 계산해둬 캐시미스를 피한다.
_CACHE_WARM_PRESETS = [
    {"n_ev": 30, "n_chargers": 150, "weeks": 21, "max_charge_kw": 7.0, "max_discharge_kw": 7.0, "reserve_soc": 0.20, "target_soc": 0.90},
    {"n_ev": 100, "n_chargers": 200, "weeks": 16, "max_charge_kw": 7.0, "max_discharge_kw": 7.0, "reserve_soc": 0.20, "target_soc": 0.90},
    {"n_ev": 30, "n_chargers": 150, "weeks": 4, "max_charge_kw": 7.0, "max_discharge_kw": 7.0, "reserve_soc": 0.20, "target_soc": 0.90},
    {"n_ev": 30, "n_chargers": 150, "weeks": 21, "max_charge_kw": 7.0, "max_discharge_kw": 7.0, "reserve_soc": 0.20, "target_soc": 0.60},
]
_CACHE_WARM_INTERVAL_SEC = 240  # TTL(300s) 만료 전에 미리 재계산해 캐시가 끊기지 않게 함


def _raw_data_fingerprint() -> tuple:
    """실증 데이터가 업로드/에뮬레이터로 바뀌면 파일 수정시각이 바뀌므로, 캐시
    키에 이걸 포함시키면 데이터가 바뀐 순간 자동으로 캐시가 무효화된다."""
    return tuple(
        p.stat().st_mtime if p.exists() else None
        for p in (source.EV_CSV, source.CHARGER_CSV, source.GRID_CSV)
    )


def _warm_one_preset(preset: dict) -> None:
    cfg = SimConfig(
        n_ev=preset["n_ev"], n_chargers=preset["n_chargers"], weeks=preset["weeks"],
        charger_days=preset["weeks"] * 7,
    )
    ev_df, _charger_df, grid_df, _is_real = load_scenario(cfg)
    _cached_heavy_compute(
        cfg, DEFAULT_TARGET, ev_df, grid_df,
        preset["max_charge_kw"], preset["max_discharge_kw"], preset["reserve_soc"], preset["target_soc"],
    )


def _cache_warmer_loop() -> None:
    while True:
        for preset in _CACHE_WARM_PRESETS:
            try:
                _warm_one_preset(preset)
            except Exception:
                pass  # 워밍 실패로 서버 프로세스가 죽으면 안 되므로 조용히 다음 프리셋으로 넘어감
        time.sleep(_CACHE_WARM_INTERVAL_SEC)


def start_cache_warmer() -> None:
    """서버 기동 시 1번 호출해 백그라운드 데몬 스레드로 프리셋을 순차(단일 스레드) 워밍한다.
    LP 최적화·병렬 실행 검증 때 "동시에 여러 무거운 계산이 겹치면 CPU 과다구독으로
    서버가 응답 불능이 된다"는 걸 실측으로 확인했기 때문에, 워밍은 절대 동시성을
    추가하지 않고 한 스레드에서 프리셋을 하나씩 순서대로 계산한다(실사용자 요청 1건과
    동일한 부하 프로파일). pytest 실행 중에는 비활성화한다 — 테스트 프로세스마다
    무거운 연산이 배경에서 계속 도는 걸 막기 위함."""
    if "pytest" in sys.modules or os.environ.get("DISABLE_CACHE_WARMER") == "1":
        return
    threading.Thread(target=_cache_warmer_loop, daemon=True).start()


def _cached_heavy_compute(
    cfg: SimConfig, target_col: str, ev_df: pd.DataFrame, grid_df: pd.DataFrame,
    max_charge_kw: float, max_discharge_kw: float, reserve_soc: float, target_soc: float,
):
    key = (
        cfg.n_ev, cfg.n_chargers, cfg.weeks, cfg.n_zones, cfg.random_seed, cfg.start_date,
        cfg.battery_capacity_kwh, cfg.charger_avg_sessions_per_day,
        target_col, max_charge_kw, max_discharge_kw, reserve_soc, target_soc,
        _raw_data_fingerprint(),
    )
    cached = _heavy_compute_cache.get(key)
    if cached is not None and time.time() - cached[0] < _HEAVY_CACHE_TTL_SEC:
        return cached[1]

    # LP 최적화(scipy HiGHS)와 예측모델 학습(LightGBM)을 스레드로 동시 실행하는 것도
    # 시도했으나(격리 벤치마크로는 확실히 빨라짐), 실서비스처럼 대시보드 새로고침/OCPP/DR
    # 폴링이 겹쳐 캐시미스 요청이 동시에 여러 개 들어오면 요청마다 CPU를 2배로 요구해
    # 코어를 과다구독(oversubscribe)하게 되고, 서버 전체가 응답 불능(hang)에 빠지는 걸
    # 실측으로 확인함(동시 요청 3개로 재현) — 우리가 애초에 고치려던 바로 그 장애를
    # 더 악화시키므로 되돌림. 순차 실행 유지.
    economics = EconomicsParams.load()  # config/economics.json — 효율·열화비용(가정값은 verified=false)
    comparison = compare_baseline_vs_optimized(
        ev_df, grid_df, cfg,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        reserve_soc=reserve_soc, target_soc=target_soc, economics=economics,
    )
    revenue = compute_fleet_revenue_comparison(comparison, grid_df, economics)
    city_hourly = build_city_hourly(ev_df, grid_df, cfg)
    forecast_result = train_forecast_model(city_hourly, target_col)
    # run_closed_loop_cycle의 "재학습 전(before)" 모델은 바로 위 forecast_result와
    # 사실상 같은 것을 다시 만드는 것이라(같은 cfg·target_col), 재사용해 중복 학습을 없앤다.
    closed_loop = run_closed_loop_cycle(cfg, target_col=target_col, before=forecast_result)

    # EV Bot의 "통계 요약본" 근거 데이터 — 실제 새로 계산했을 때만 한 줄 남긴다
    # (캐시 히트·워머의 재확인 호출은 여기 도달하지 않으므로 라이브 데모 자동새로고침
    # 으로 이력이 부풀지 않는다). pytest 실행 중에는 테스트용 소규모 시나리오가
    # 실사용자에게 보여줄 실제 curated.db 이력에 섞여 들어가지 않도록 건너뛴다
    # (캐시 워머와 동일한 가드).
    if "pytest" not in sys.modules:
        append_dashboard_run_history({
            "generated_at": kst_now().isoformat(),
            "n_ev": cfg.n_ev,
            "weeks": cfg.weeks,
            "savings_krw": revenue["savings_krw"],
            "savings_pct": revenue["savings_pct"],
            "forecast_mae": forecast_result.mae,
            "closed_loop_drift_pct": closed_loop["drift_pct"],
            "closed_loop_improve_pct": closed_loop["improve_pct"],
        })

    result = (comparison, revenue, forecast_result, closed_loop)
    if len(_heavy_compute_cache) >= _HEAVY_CACHE_MAX_ENTRIES:
        _heavy_compute_cache.clear()  # 단순 무제한 성장 방지(LRU 아님, PoC 규모엔 충분)
    _heavy_compute_cache[key] = (time.time(), result)
    return result


def _bucket_poc(comparison: pd.DataFrame, bucket: str) -> pd.DataFrame:
    """PoC 비교 시계열(baseline_kw/optimized_kw, 시간당 kW)을 조회 단위로 재집계한다.
    미래 예측이 아니라 이미 계산된 과거 시뮬레이션 결과를 다시 묶는 것뿐이라, 재귀
    예측(forecast_horizon)과 달리 추가 연산 비용이 거의 없다."""
    if bucket == "hour":
        cutoff = comparison["timestamp"].max() - pd.Timedelta(days=CHART_WINDOW_DAYS)
        return comparison[comparison["timestamp"] >= cutoff].reset_index(drop=True)

    freq = {"day": "1D", "week": "1W"}[bucket]
    bucketed = (
        comparison.set_index("timestamp")[["baseline_kw", "optimized_kw"]]
        .resample(freq)
        .sum()
        .reset_index()
    )
    if bucket == "day":
        bucketed = bucketed.tail(POC_DAILY_WINDOW_DAYS)
    return bucketed.reset_index(drop=True)


def build_dashboard_payload(
    cfg: SimConfig | None = None,
    target_col: str = DEFAULT_TARGET,
    max_charge_kw: float = 7.0,
    max_discharge_kw: float = 7.0,
    reserve_soc: float = 0.20,
    target_soc: float = 0.90,
) -> dict:
    cfg = cfg or SimConfig()
    ev_df, charger_df, grid_df, is_real = load_scenario(cfg)

    quality_reports = [
        validate(ev_df, "ev_telemetry", EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"]),
        validate(charger_df, "charger_sessions", CHARGER_SESSION_SCHEMA, dedup_keys=["session_id"]),
        validate(grid_df, "grid_signal", GRID_SIGNAL_SCHEMA, dedup_keys=["timestamp"]),
    ]
    weekly_report = build_ev_weekly_report(ev_df, cfg)
    _con = get_connection()
    try:
        real_discharges = kpi_targets.count_discharge_events(_con)
        emulated_discharges = kpi_targets.count_discharge_events(_con, emulated=True)
    finally:
        _con.close()
    kpi_summary = summarize_kpi(ev_df, charger_df, grid_df, grid_is_real=is_real, discharge_events=real_discharges)
    kpi_data_records = kpi_summary.internal_records

    charger_utilization = build_charger_utilization_hourly(charger_df)
    fleet_hourly_util = (
        charger_utilization.groupby("timestamp")[["occupied_chargers", "total_chargers"]].sum()
    )
    fleet_hourly_util["utilization_pct"] = (
        fleet_hourly_util["occupied_chargers"] / fleet_hourly_util["total_chargers"] * 100
    ).round(1)
    avg_utilization_pct = round(float(fleet_hourly_util["utilization_pct"].mean()), 1) if not fleet_hourly_util.empty else 0.0
    peak_utilization_pct = round(float(fleet_hourly_util["utilization_pct"].max()), 1) if not fleet_hourly_util.empty else 0.0
    peak_utilization_at = (
        fleet_hourly_util["utilization_pct"].idxmax().isoformat() if not fleet_hourly_util.empty else None
    )

    comparison, revenue, forecast_result, closed_loop = _cached_heavy_compute(
        cfg, target_col, ev_df, grid_df,
        max_charge_kw, max_discharge_kw, reserve_soc, target_soc,
    )
    poc_views = {gran: _bucket_poc(comparison, bucket) for gran, bucket in (("hourly", "hour"), ("daily", "day"), ("weekly", "week"))}
    fit_df = forecast_result.test_df.sort_values("timestamp")
    fit_window = fit_df[fit_df["timestamp"] < fit_df["timestamp"].min() + pd.Timedelta(days=CHART_WINDOW_DAYS)]

    models = {}
    for tc in KNOWN_TARGETS:
        registry = ModelRegistry(tc)
        version = registry.active_version()
        info = registry.load()[2] if version is not None else None
        models[tc] = {
            "active_version": version,
            "holdout_mae": info["holdout_mae"] if info else None,
        }

    return {
        "generated_at": kst_now().isoformat(),
        "is_real_data": is_real,
        "scenario": {
            "n_ev": cfg.n_ev,
            "n_chargers": cfg.n_chargers,
            "weeks": cfg.weeks,
            "n_zones": cfg.n_zones,
            "max_charge_kw": max_charge_kw,
            "max_discharge_kw": max_discharge_kw,
            "reserve_soc": reserve_soc,
            "target_soc": target_soc,
        },
        "kpi": {
            # cfg.n_ev 등은 "다음 시나리오 실행에 쓸 설정값"일 뿐이라 실증(특히 부분 실증 —
            # EV 데이터만 실측이고 충전기/계통은 아직 합성인 상태) 데이터의 실제 규모와
            # 다를 수 있다. KPI 타일("실증 EV"/"실증 충전기")은 실제 로드된 데이터 기준이어야 한다.
            "ev_count": int(ev_df["ev_id"].nunique()),
            "charger_count": int(charger_df["charger_id"].nunique()) if "charger_id" in charger_df.columns else cfg.n_chargers,
            "weeks": cfg.weeks,
            "data_records": kpi_data_records,
            "data_target": kpi_summary.internal_target,
            "data_breakdown": {"a_sessions": kpi_summary.a_sessions, "b_vehicle_days": kpi_summary.b_vehicle_days,
                               "b_discharge_events": kpi_summary.b_discharge_events},
            "external_records": kpi_summary.external_records,
            "emulated_discharge_events": emulated_discharges,  # 에뮬레이터 기록 — 공식 건수에 합산하지 않음
            "external_is_real": kpi_summary.external_is_real,
            "operator_count": kpi_summary.operator_count,
            "ev_model_count": kpi_summary.ev_model_count,
            "targets": {
                "ev": kpi_targets.TARGET_EV, "ev_models": kpi_targets.TARGET_EV_MODELS,
                "chargers": kpi_targets.TARGET_CHARGERS, "operators": kpi_targets.TARGET_OPERATORS,
                "discharge_events": kpi_targets.TARGET_DISCHARGE_EVENTS,
            },
            "external_target": kpi_summary.external_target,
            "charger_utilization_avg_pct": avg_utilization_pct,
            "charger_utilization_peak_pct": peak_utilization_pct,
            "charger_utilization_peak_at": peak_utilization_at,
            "quality": [
                {"table": r.table, "n_rows": r.n_rows, "passed": r.passed}
                for r in quality_reports
            ],
        },
        "poc_comparison": {
            gran: {
                "timestamps": [t.isoformat() for t in df["timestamp"]],
                "baseline_kw": [round(float(v), 2) for v in df["baseline_kw"]],
                "optimized_kw": [round(float(v), 2) for v in df["optimized_kw"]],
            }
            for gran, df in poc_views.items()
        },
        "revenue_comparison": revenue,
        "forecast_fit": {
            "target_col": target_col,
            "mae": forecast_result.mae,
            "timestamps": [t.isoformat() for t in fit_window["timestamp"]],
            "y_true": [round(float(v), 2) for v in fit_window["y_true"]],
            "y_pred": [round(float(v), 2) for v in fit_window["y_pred"]],
        },
        "closed_loop": closed_loop,
        "models": models,
        "vehicle_status": build_vehicle_status_table(ev_df),
        "renewable": build_renewable_summary(grid_df, window_days=CHART_WINDOW_DAYS),
        "carbon": compute_carbon_reduction(comparison, grid_df),
    }


@router.get("/dashboard", response_class=FileResponse, include_in_schema=False)
def dashboard_page() -> FileResponse:
    """실시간 웹 대시보드 HTML 페이지 (Swagger에는 노출하지 않음 — API가 아니라 화면)."""
    return FileResponse(STATIC_DIR / "dashboard.html")


@router.get(
    "/dashboard/data",
    summary="대시보드 전체 데이터 — KPI/PoC비교/예측적합도/Closed-loop",
    dependencies=[Depends(require_admin)],
)
def dashboard_data(
    n_ev: int = Query(30, ge=1, le=500, description="시나리오 EV 대수 (데모 모드에서만 적용)"),
    n_chargers: int = Query(150, ge=1, le=2000, description="시나리오 충전기 대수 (데모 모드에서만 적용)"),
    weeks: int = Query(21, ge=4, le=52, description="시나리오 관측기간(주) (데모 모드에서만 적용)"),
    n_zones: int = Query(4, ge=1, le=20, description="시나리오 구역 수 (데모 모드에서만 적용)"),
    max_charge_kw: float = Query(7.0, gt=0, le=50, description="V2G 최적화 최대 충전 출력(kW)"),
    max_discharge_kw: float = Query(7.0, ge=0, le=50, description="V2G 최적화 최대 방전 출력(kW)"),
    reserve_soc: float = Query(0.20, ge=0, le=0.5, description="방전 하한 예비 SOC 비율"),
    target_soc: float = Query(0.90, ge=0.5, le=0.99, description="세션 종료 목표 SOC 비율"),
    owner_share_pct: float = Query(50.0, ge=0, le=100, description="이익공유 배분 — 차주 몫(%)"),
    operator_share_pct: float = Query(30.0, ge=0, le=100, description="이익공유 배분 — 충전인프라 운영자 몫(%)"),
    platform_share_pct: float = Query(20.0, ge=0, le=100, description="이익공유 배분 — 플랫폼 사업자 몫(%)"),
) -> dict:
    """대시보드 화면 하나를 그리는 데 필요한 전체 데이터를 한 번에 반환한다 —
    KPI 요약, 데이터 품질, PoC 비교(일/주/월), 예측 적합도, Closed-loop 재학습 결과,
    모델 레지스트리 현황, 이익공유 배분 시뮬레이션을 포함한다.

    시나리오 파라미터(n_ev 등)는 데모(합성) 모드에서만 규모에 영향을 준다 — 실제
    업로드된 데이터가 있으면 규모는 데이터 자체가 결정하고, 최적화 파라미터
    (max_charge_kw 등)만 두 모드 모두에 적용된다.

    **실제 데이터 연동 시 유의**: `profit_sharing`의 배분 금액은 `revenue_comparison.
    savings_krw`(AI 최적화가 실제로 만든 절감액)를 그대로 나눈 값이다 — 배분
    비율은 고정이지만, 실제 V2G 참여율·가격 변동폭이 지금 데모(40대, 규칙적 요금
    곡선)와 다르면 절감액 자체가 커지거나 작아져 배분 금액도 그만큼 달라진다.
    """
    cfg = SimConfig(n_ev=n_ev, n_chargers=n_chargers, weeks=weeks, charger_days=weeks * 7, n_zones=n_zones)
    payload = build_dashboard_payload(
        cfg,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        reserve_soc=reserve_soc, target_soc=target_soc,
    )
    try:
        payload["profit_sharing"] = compute_profit_sharing(
            payload["revenue_comparison"]["savings_krw"], owner_share_pct, operator_share_pct, platform_share_pct
        )
    except InvalidShareError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return payload


@router.get(
    "/dashboard/dr-performance",
    summary="등록된 DR 이벤트별 방전/부하감축 실적(AI 추론·모의 포함)",
    dependencies=[Depends(require_admin)],
)
def dashboard_dr_performance(
    n_ev: int = Query(30, ge=1, le=500),
    n_chargers: int = Query(150, ge=1, le=2000),
    weeks: int = Query(21, ge=4, le=52),
    n_zones: int = Query(4, ge=1, le=20),
    max_charge_kw: float = Query(7.0, gt=0, le=50),
    max_discharge_kw: float = Query(7.0, ge=0, le=50),
    reserve_soc: float = Query(0.20, ge=0, le=0.5),
    target_soc: float = Query(0.90, ge=0.5, le=0.99),
) -> list[dict]:
    """`/grid/dr-events`가 "이벤트가 등록됐다"까지만 보여주는 것과 달리, 그 이벤트
    시간대에 선단이 실제로 방전/부하감축으로 얼마나 반응했는지(실제 LP 최적화 결과
    기준)를 계산한다. `/dashboard/data`와 동일한 시나리오 파라미터를 주면 같은 캐시를
    재사용해 추가 연산 없이 즉시 응답한다.

    이벤트 시각이 현재 시나리오의 관측기간과 겹치지 않으면(시나리오를 새로 실행해
    기간이 바뀐 경우 등) `overlapped: false`로 표시한다 — 지어낸 값을 채우지 않는다."""
    cfg = SimConfig(n_ev=n_ev, n_chargers=n_chargers, weeks=weeks, charger_days=weeks * 7, n_zones=n_zones)
    ev_df, _charger_df, grid_df, _is_real = load_scenario(cfg)
    comparison, _revenue, _forecast_result, _closed_loop = _cached_heavy_compute(
        cfg, DEFAULT_TARGET, ev_df, grid_df, max_charge_kw, max_discharge_kw, reserve_soc, target_soc,
    )
    events = dr_events.list_dr_events()
    return compute_dr_event_performance(comparison, grid_df, events)


@router.get(
    "/dashboard/charging-efficiency",
    summary="차량(OBD) 충전량 vs 충전기 계측 충전량 비교 — 충전 효율(손실률) 추정",
    dependencies=[Depends(require_admin)],
)
def get_charging_efficiency(
    n_ev: int = Query(30, ge=1, le=500),
    n_chargers: int = Query(150, ge=1, le=2000),
    weeks: int = Query(21, ge=4, le=52),
    n_zones: int = Query(4, ge=1, le=20),
) -> dict:
    """`charger_sessions`에 `ev_id`가 연결된 세션만 대상으로, 그 차량의 OBD 충전량
    추정치와 충전기 계측값을 비교해 효율(%)을 추정한다.

    연결된 세션이 하나도 없으면(실증 데이터에 아직 OCPP id_tag-ev_id 매핑이 없는
    경우 등) 404를 반환한다 — 이 분석은 지금은 합성 데이터에서만 의미가 있다."""
    cfg = SimConfig(n_ev=n_ev, n_chargers=n_chargers, weeks=weeks, charger_days=weeks * 7, n_zones=n_zones)
    ev_df, charger_df, _grid_df, is_real = load_scenario(cfg)
    try:
        result = compute_charging_efficiency(ev_df, charger_df)
    except NoLinkedSessionsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result["is_real_data"] = is_real
    return result
