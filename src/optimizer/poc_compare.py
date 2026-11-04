"""일반 충전(baseline) vs AI-V2G 최적화(optimized) 비교 (WS4 현장 PoC).

관측된 EV 텔레메트리에서 연결(connected) 구간을 세션으로 추출해 optimize_fleet에
넣고, 동일 기간에 대해 baseline(관측된 그대로) 대비 optimized(최적화 적용시) 집계
부하곡선을 산출한다.
"""
import math
from collections import defaultdict
from dataclasses import replace

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from src.datagen.config import SimConfig
from src.datagen.grid import generate_grid_signal
from src.timeutil import to_kst_naive
from src.grid.pricing import DEFAULT_COMPENSATION_ID, DEFAULT_TARIFF_ID, PriceModel, build_price_model
from src.model.plugin_pattern import UnknownVehicleError, forecast_plugin_schedule
from .economics import EconomicsParams, breakeven_sell_price
from .soc_planner import plan_single_vehicle
from scipy.optimize import Bounds, LinearConstraint, milp

from .decision import DISCHARGE_CYCLE_PENALTY, SOC_BAND_MAX_PCT, SOC_BAND_MIN_PCT, optimize_fleet

MIN_SESSION_HOURS = 2  # 1시간짜리 접속은 최적화 여지가 없어 세션에서 제외
NEXT_SESSION_HORIZON_HOURS = 72  # 이 시간 안에 예측 출차가 없으면 "다음 세션 계획"으로 보기엔 너무 멀다고 판단


def _extract_sessions(
    df: pd.DataFrame, cfg: SimConfig, start_ts: pd.Timestamp, end_ts: pd.Timestamp
) -> tuple[dict, dict]:
    """EV별 연결(connected) 연속 구간을 세션 단위로 추출한다.

    자정 등 날짜 경계에서 세션을 자르지 않기 위해, 하루 단위로 슬라이싱하지 않고
    EV별 전체 구간을 순서대로 훑으며 connected가 유지되는 연속 블록을 그대로
    하나의 세션으로 묶는다.
    """
    window = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)]

    sessions: dict[str, dict] = {}
    session_ev_map: dict[str, str] = {}
    session_seq = 1

    for ev_id, ev_df in window.groupby("ev_id"):
        ev_df = ev_df.sort_values("timestamp").reset_index(drop=True)
        block_id = (ev_df["connected"] != ev_df["connected"].shift()).cumsum()
        for _, block in ev_df.groupby(block_id):
            if not bool(block["connected"].iloc[0]) or len(block) < MIN_SESSION_HOURS:
                continue
            session_id = f"S{session_seq:05d}"
            session_seq += 1
            times = list(block["timestamp"])
            # 차종별 배터리 용량/V2G 지원여부(있으면) — 없으면(구 스키마·실증 업로드) 전역값으로 대체
            capacity_kwh = float(block["battery_capacity_kwh"].iloc[0]) if "battery_capacity_kwh" in block.columns else cfg.battery_capacity_kwh
            v2g_capable = bool(block["v2g_capable"].iloc[0]) if "v2g_capable" in block.columns else True
            sessions[session_id] = {
                "times": times,
                "soc_baseline_pct": dict(zip(block["timestamp"], block["soc_pct"])),
                "capacity_kwh": capacity_kwh,
                "v2g_capable": v2g_capable,
            }
            session_ev_map[session_id] = ev_id

    return sessions, session_ev_map


def run_v2g_optimization(
    ev_df: pd.DataFrame,
    grid_df: pd.DataFrame,
    cfg: SimConfig,
    max_charge_kw: float = 7.0,
    max_discharge_kw: float = 7.0,
    reserve_soc: float = 0.20,
    target_soc: float = 0.90,
    economics: EconomicsParams | None = None,
    price_model: PriceModel | None = None,
) -> dict:
    """세션 추출→LP 최적화까지 실행하고 중간 산출물을 전부 반환한다.

    `compare_baseline_vs_optimized`(집계 비교용)와 세션별 설명(`summarize_session_plan`)이
    이 함수 하나를 공유해, 대시보드 차트와 분석이 항상 같은 최적화 결과를 근거로 삼는다.

    price_model이 있으면 충전 비용은 요금표 단가(buy), 방전 보상은 보상 시나리오(sell)로 따로
    계산한다(FR-P01·P02). 없으면 기존처럼 grid_signal 가격 하나를 둘 다에 쓴다."""
    start_ts, end_ts = ev_df["timestamp"].min(), ev_df["timestamp"].max()
    sessions, session_ev_map = _extract_sessions(ev_df, cfg, start_ts, end_ts)
    if price_model is not None:
        price_by_time = price_model.buy_by_time
        sell_price_by_time = price_model.sell_by_time
    else:
        price_by_time = dict(zip(grid_df["timestamp"], grid_df["price_krw_per_kwh"]))
        sell_price_by_time = price_by_time
    schedule = optimize_fleet(
        sessions,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        reserve_soc=reserve_soc,
        target_soc=target_soc,
        price_by_time=price_by_time,
        economics=economics,
    )
    return {
        "sessions": sessions,
        "session_ev_map": session_ev_map,
        "schedule": schedule,
        "price_by_time": price_by_time,
        "sell_price_by_time": sell_price_by_time,
        "price_model": price_model.describe() if price_model is not None else None,
    }


def compare_baseline_vs_optimized(
    ev_df: pd.DataFrame,
    grid_df: pd.DataFrame,
    cfg: SimConfig,
    max_charge_kw: float = 7.0,
    max_discharge_kw: float = 7.0,
    reserve_soc: float = 0.20,
    target_soc: float = 0.90,
    economics: EconomicsParams | None = None,
    price_model: PriceModel | None = None,
) -> pd.DataFrame:
    """일반충전 vs AI-V2G 최적화 집계 부하곡선 비교 (viz.plot_poc_comparison 입력)."""
    result = run_v2g_optimization(
        ev_df, grid_df, cfg,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        reserve_soc=reserve_soc, target_soc=target_soc, economics=economics,
        price_model=price_model,
    )
    return aggregate_comparison(ev_df, result["schedule"])


def aggregate_comparison(ev_df: pd.DataFrame, schedule: dict) -> pd.DataFrame:
    """관측 충전(baseline)과 최적화 스케줄을 시간대별 선단 합계로 묶는다 — 최적화 결과(run)를 이미 가진 호출자가
    다시 풀지 않고 비교표만 만들 때 쓴다."""
    baseline = ev_df.groupby("timestamp")["charging_kw"].sum().rename("baseline_kw")

    optimized_by_time: dict[pd.Timestamp, float] = defaultdict(float)
    discharge_by_time: dict[pd.Timestamp, float] = defaultdict(float)
    for sched in schedule.values():
        for t, (charge, discharge) in sched.items():
            optimized_by_time[t] += charge - discharge
            discharge_by_time[t] += discharge
    optimized = pd.Series(optimized_by_time, name="optimized_kw")
    discharge_kw = pd.Series(discharge_by_time, name="discharge_kw")

    comparison = (
        pd.concat([baseline, optimized, discharge_kw], axis=1)
        .fillna(0.0)
        .rename_axis("timestamp")
        .reset_index()
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    return comparison


def compute_fleet_revenue_comparison(
    comparison: pd.DataFrame, grid_df: pd.DataFrame, economics: EconomicsParams | None = None
) -> dict:
    """일반 충전(규칙기반·무제어)과 AI-V2G 최적화의 전력비용을 원화로 비교한다.

    `comparison`은 `compare_baseline_vs_optimized`의 출력(시간대별 `baseline_kw`/
    `optimized_kw`, 둘 다 fleet 합계, `optimized_kw`는 충전-방전 순부하라 방전이
    우세하면 음수)이다. `baseline_kw`는 항상 0 이상(방전이 없는 "규칙기반: 그냥
    완속충전"에 해당)이므로 비용만 발생하고, `optimized_kw`는 음수 구간에서 V2G
    방전 수익이 비용을 상쇄한다 — 그 차이가 AI 최적화의 순수 금전적 가치다."""
    merged = comparison.merge(grid_df[["timestamp", "price_krw_per_kwh"]], on="timestamp", how="left")
    price = merged["price_krw_per_kwh"].fillna(0.0)

    baseline_cost_krw = float((merged["baseline_kw"] * price).sum())
    optimized_cost_krw = float((merged["optimized_kw"] * price).sum())
    savings_krw = baseline_cost_krw - optimized_cost_krw
    extra: dict = {}
    if economics is not None and "discharge_kw" in comparison.columns:
        # 열화비용: 배터리에서 꺼낸 kWh(= 방전/효율) x 열화단가 — 절감액에서 차감한다
        degradation = float(comparison["discharge_kw"].sum()) / economics.discharge_efficiency * economics.degradation_krw_per_kwh
        extra = {
            "savings_before_degradation_krw": round(savings_krw, 0),
            "degradation_cost_krw": round(degradation, 0),
            "assumptions": economics.as_dict(),
        }
        savings_krw -= degradation
    savings_pct = (savings_krw / baseline_cost_krw * 100) if baseline_cost_krw else 0.0

    return {
        "baseline_cost_krw": round(baseline_cost_krw, 0),
        "optimized_cost_krw": round(optimized_cost_krw, 0),
        "savings_krw": round(savings_krw, 0),
        "savings_pct": round(savings_pct, 1),
        **extra,
    }


def compute_owner_cost_comparison(
    comparison: pd.DataFrame, price_model: PriceModel, economics: EconomicsParams | None = None
) -> dict:
    """차주 관점 비용 비교(FR-P01·P02) — 충전은 요금표 단가(buy)로 사고 방전은 보상단가(sell)로
    파는 것으로 계산한다. `compute_fleet_revenue_comparison`은 순부하에 한 가격을 곱해 방전을
    충전 단가로 되파는 것처럼 계산하므로, 충전 단가가 보상단가보다 높은 실제 조건에서는 편익이
    과대평가된다.

    optimized 충전량 = optimized_kw + discharge_kw (optimized_kw는 충전-방전 순부하).
    """
    buy = comparison["timestamp"].map(price_model.buy_by_time).fillna(0.0)
    sell = comparison["timestamp"].map(price_model.sell_by_time).fillna(0.0)
    discharge = comparison["discharge_kw"] if "discharge_kw" in comparison.columns else pd.Series(0.0, index=comparison.index)
    charge = (comparison["optimized_kw"] + discharge).clip(lower=0.0)

    baseline_cost = float((comparison["baseline_kw"] * buy).sum())
    charge_cost = float((charge * buy).sum())
    discharge_revenue = float((discharge * sell).sum())
    degradation = 0.0
    if economics is not None:
        degradation = float(discharge.sum()) / economics.discharge_efficiency * economics.degradation_krw_per_kwh
    optimized_net_cost = charge_cost - discharge_revenue + degradation
    savings = baseline_cost - optimized_net_cost

    result = {
        "baseline_cost_krw": round(baseline_cost, 0),
        "optimized_charge_cost_krw": round(charge_cost, 0),
        "discharge_revenue_krw": round(discharge_revenue, 0),
        "degradation_cost_krw": round(degradation, 0),
        "optimized_net_cost_krw": round(optimized_net_cost, 0),
        "savings_krw": round(savings, 0),
        "savings_pct": round(savings / baseline_cost * 100, 1) if baseline_cost else 0.0,
        "price_model": price_model.describe(),
        "verified": price_model.verified and (economics is None or economics.verified),
    }
    if economics is not None:
        result["assumptions"] = economics.as_dict()
    return result


def _single_vehicle_arbitrage_schedule(
    times: list[pd.Timestamp],
    price_by_time: dict,
    needed_kwh: float,
    max_net_kwh: float,
    headroom_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    discharge_allowed: bool,
    economics: EconomicsParams | None = None,
    sell_price_by_time: dict | None = None,
    band_kwh: tuple[float, float] | None = None,
) -> dict[pd.Timestamp, tuple[float, float]]:
    """단일 차량의 충·방전 계획 — 시간대별 배터리 잔량을 상태변수로 둔 `soc_planner`에 위임한다.

    예전 구현은 "최종 순에너지"만 맞추고 시간대별 잔량은 제한하지 않아, 일찍 많이 충전했다가 나중에 방전하는 계획에서
    잔량이 용량을 넘거나(최대 109.5%) 방전 허용 범위(35~80%)를 벗어났고, 방전 모드를 켜 둔 채 쉬는 방식으로 하루 방전 횟수
    상한도 우회할 수 있었다. 기존 입력을 같은 의미로 넘기기 위해 에너지 축을 예비 잔량 기준으로 옮긴다:
      e' = e − 예비 → 시작 e'₀ = headroom_kwh, 용량' = headroom_kwh + max_net_kwh, 목표 = e'₀ + needed_kwh.

    price_by_time은 충전 단가, sell_price_by_time은 방전 보상단가(없으면 같은 가격). economics가 없으면 효율 1·열화 0·횟수 무제한.
    band_kwh는 같은 축에서의 방전 허용 범위(없으면 제한 없음). 조건을 만족하는 계획이 없으면 PlannerError(ValueError)를 올린다
    (예전에는 조용히 전부 0을 돌려줘 "충전 계획 없음"이 정상처럼 보였다).
    """
    n = len(times)
    if n == 0:
        return {}
    sell_price_by_time = price_by_time if sell_price_by_time is None else sell_price_by_time
    eco = economics if economics is not None else EconomicsParams()
    e0 = max(float(headroom_kwh), 0.0)
    cap = e0 + max(float(max_net_kwh), 0.0)
    target = min(e0 + max(float(needed_kwh), 0.0), cap)
    plan = plan_single_vehicle(
        [float(price_by_time.get(t, 0.0)) for t in times], [float(sell_price_by_time.get(t, 0.0)) for t in times],
        e0, cap, target, eco.charge_efficiency, eco.discharge_efficiency, eco.degradation_krw_per_kwh,
        max_charge_kw, max_discharge_kw, 0.0, band_kwh if band_kwh is not None else (0.0, cap),
        bool(discharge_allowed), eco.max_discharge_events_per_day, [pd.Timestamp(t).date() for t in times])
    return {t: (round(float(plan.charge[i]), 4), round(float(plan.discharge[i]), 4)) for i, t in enumerate(times)}


def plan_next_session(
    ev_df: pd.DataFrame,
    grid_df: pd.DataFrame,
    cfg: SimConfig,
    ev_id: str,
    max_charge_kw: float = 7.0,
    max_discharge_kw: float = 7.0,
    reserve_soc: float = 0.20,
    target_soc: float = 0.90,
    economics: EconomicsParams | None = None,
    departure_time: str | None = None,
    current_soc_pct: float | None = None,
    need_soc_pct: float | None = None,
    tariff_id: str | None = None,
    compensation_id: str | None = None,
) -> dict:
    """사용자 입력(departure_time, current_soc_pct, need_soc_pct)이 있으면 예측·관측값보다
    우선한다 — OBD 데이터는 D+1로 도착하므로 "지금 SOC"와 "내일 출발시간"은 사용자 입력이
    가장 정확한 근거다. economics가 있으면 효율·열화비용·방전 횟수 상한을 반영한다.

    tariff_id·compensation_id 중 하나라도 주면 차주 가격 모델(충전=요금표, 방전=보상 시나리오,
    빠진 쪽은 기본값)로 계획하고, 방전 손익분기 단가를 함께 싣는다. 둘 다 없으면 기존 동작.

    예측된 다음 출차 시각까지를 가상의 "다음 세션"으로 구성해 그 구간만 LP로
    최적화한다 — `run_v2g_optimization`이 다루는 기존(이미 관측된) 세션과 달리,
    아직 일어나지 않은 미래 구간에 대한 예측 기반 추천이다. "내일 몇 시에
    나가면 언제 충전/방전해서 얼마 번다"를 답할 때 이 함수를 쓴다.

    SOC 궤적을 알 수 없는 미래이므로, 현재(마지막 관측) SOC를 창 전체에 그대로
    반복해 SOC 밴드 방전 게이트(`decision.py`)의 대용으로 쓴다 — 실제로는 충전이
    진행되며 SOC가 오르지만, LP 자체가 SOC를 상태변수로 갖지 않는 기존 설계와
    같은 근사 방식이다.
    """
    if departure_time is not None:
        pattern = {"expected_next_departure": pd.Timestamp(departure_time).isoformat(), "n_weeks_history": None}
        if ev_df[ev_df["ev_id"] == ev_id].empty:
            raise UnknownVehicleError(f"차량 이력이 없습니다: {ev_id}")
    else:
        pattern = forecast_plugin_schedule(ev_df, ev_id, horizon_hours=NEXT_SESSION_HORIZON_HOURS)
    departure_iso = pattern["expected_next_departure"]
    if departure_iso is None:
        raise ValueError(
            f"{ev_id}의 다음 출차 시각을 예측할 수 없습니다(향후 {NEXT_SESSION_HORIZON_HOURS}시간 안에 "
            "연결→주행 전환 패턴이 뚜렷하지 않음)."
        )
    departure_ts = pd.Timestamp(departure_iso)

    hist = ev_df[ev_df["ev_id"] == ev_id].sort_values("timestamp")
    last_row = hist.iloc[-1]
    start_ts = last_row["timestamp"] + pd.Timedelta(hours=1)
    if departure_ts <= start_ts:
        raise ValueError(f"{ev_id}의 예측 출차 시각({departure_ts})이 현재 시점 이전이라 계획을 만들 수 없습니다.")

    times = list(pd.date_range(start_ts, departure_ts, freq="h"))
    current_soc = float(current_soc_pct) if current_soc_pct is not None else float(last_row["soc_pct"])
    capacity_kwh = float(last_row["battery_capacity_kwh"]) if "battery_capacity_kwh" in hist.columns else cfg.battery_capacity_kwh
    v2g_capable = bool(last_row["v2g_capable"]) if "v2g_capable" in hist.columns else True
    if need_soc_pct is not None:
        target_soc = need_soc_pct / 100.0

    session_id = "NEXT"
    sessions = {
        session_id: {
            "times": times,
            "soc_baseline_pct": {t: current_soc for t in times},
            "capacity_kwh": capacity_kwh,
            "v2g_capable": v2g_capable,
        }
    }
    session_ev_map = {session_id: ev_id}

    # 미래 구간 가격 신호 — 실제 미래 가격은 알 수 없으므로(forecast_service.py의
    # 재귀예측 확장과 같은 이유) 같은 계절·요일 패턴이 이어진다고 가정하고 합성
    # 생성기를 연장해 채운다(기후값 근사).
    last_grid_ts = grid_df["timestamp"].max()
    extra_weeks = math.ceil((len(times) + 24) / (7 * 24)) + 1
    ext_cfg = replace(cfg, weeks=cfg.weeks + extra_weeks)
    ext_grid = generate_grid_signal(ext_cfg, np.random.default_rng(cfg.random_seed))
    future_grid = ext_grid[ext_grid["timestamp"] > last_grid_ts]

    grid_price_by_time = dict(zip(grid_df["timestamp"], grid_df["price_krw_per_kwh"]))
    grid_price_by_time.update(dict(zip(future_grid["timestamp"], future_grid["price_krw_per_kwh"])))
    price_model = None
    if tariff_id is not None or compensation_id is not None:
        price_model = build_price_model(
            times, grid_price_by_time,
            tariff_id or DEFAULT_TARIFF_ID, compensation_id or DEFAULT_COMPENSATION_ID,
        )
        price_by_time, sell_price_by_time = price_model.buy_by_time, price_model.sell_by_time
    else:
        price_by_time = grid_price_by_time
        sell_price_by_time = price_by_time

    target_pct = target_soc * 100
    needed_kwh = max((target_pct - current_soc) / 100 * capacity_kwh, 0.0)
    max_net_kwh = max((100.0 - current_soc) / 100 * capacity_kwh, needed_kwh)
    headroom_kwh = max((current_soc - reserve_soc * 100) / 100 * capacity_kwh, 0.0)
    reserve_kwh = reserve_soc * capacity_kwh
    # 방전 허용 범위(35~80%)는 시간대별 잔량에 적용한다(예비 잔량 기준 축으로 옮겨서 전달)
    band_kwh = (max(SOC_BAND_MIN_PCT / 100 * capacity_kwh - reserve_kwh, 0.0),
                max(SOC_BAND_MAX_PCT / 100 * capacity_kwh - reserve_kwh, 0.0))
    discharge_allowed = v2g_capable

    sched = _single_vehicle_arbitrage_schedule(
        times, price_by_time, needed_kwh, max_net_kwh, headroom_kwh,
        max_charge_kw, max_discharge_kw, discharge_allowed, economics, sell_price_by_time, band_kwh,
    )
    schedule = {session_id: sched}
    plan = summarize_session_plan(
        session_id, sessions, schedule, session_ev_map, price_by_time, economics, sell_price_by_time,
    )
    if price_model is not None:
        plan["price_model"] = price_model.describe()
        if economics is not None:
            # 창 안에서 가장 싸게 충전했을 때의 손익분기 — 이보다 높은 보상 시간이 없으면 방전은 손해다
            breakeven = breakeven_sell_price(min(price_by_time[t] for t in times), economics)
            max_sell = max(sell_price_by_time[t] for t in times)
            plan["discharge_breakeven"] = {
                "breakeven_sell_krw_per_kwh": round(breakeven, 1),
                "max_sell_krw_per_kwh": round(max_sell, 1),
                "worthwhile": max_sell > breakeven,
            }
    plan["is_predicted"] = departure_time is None
    plan["inputs_from_user"] = {
        "departure_time": departure_time is not None,
        "current_soc": current_soc_pct is not None,
        "need_soc": need_soc_pct is not None,
    }
    plan["n_weeks_history"] = pattern["n_weeks_history"]
    return plan


def select_session_for_explanation(
    sessions: dict, schedule: dict, session_ev_map: dict, ev_id: str | None = None
) -> str | None:
    """설명할 세션 하나를 고른다 — ev_id가 주어지면 그 차량의 세션 중, 아니면 전체 중에서
    총 방전량이 가장 큰(=V2G 스토리가 가장 뚜렷한) 세션을 고른다."""
    candidates = [sid for sid in schedule if sid in sessions]
    if ev_id is not None:
        candidates = [sid for sid in candidates if session_ev_map.get(sid) == ev_id]
    if not candidates:
        return None

    def total_discharge(sid: str) -> float:
        return sum(d for _c, d in schedule[sid].values())

    return max(candidates, key=total_discharge)


def summarize_session_plan(
    session_id: str,
    sessions: dict,
    schedule: dict,
    session_ev_map: dict,
    price_by_time: dict,
    economics: EconomicsParams | None = None,
    sell_price_by_time: dict | None = None,
) -> dict:
    """economics가 있으면 열화비용을 빼고(net_krw), 가정값 정보를 함께 싣는다.
    sell_price_by_time이 있으면 방전 수익은 그 보상단가로 계산한다(없으면 price_by_time).

    세션 하나의 최적 충/방전 계획을 사람이 읽을 구조로 요약한다 — 화면·보고서가 자연어
    설명을 만들 때 근거로 쓰는 "사실"이며, 전부 LP 최적화 결과에서 그대로 계산한다
    (추측·가상수치 없음)."""
    session = sessions[session_id]
    sched = schedule[session_id]
    times = sorted(session["times"])

    charge_intervals: list[dict] = []
    discharge_intervals: list[dict] = []
    total_charge_kwh = 0.0
    total_discharge_kwh = 0.0
    charge_cost_krw = 0.0
    discharge_revenue_krw = 0.0
    if sell_price_by_time is None:
        sell_price_by_time = price_by_time

    for t in times:
        charge_kw, discharge_kw = sched.get(t, (0.0, 0.0))
        price = float(price_by_time.get(t, 0.0))
        sell = float(sell_price_by_time.get(t, 0.0))
        if charge_kw > 0.01:
            charge_intervals.append({"time": t.isoformat(), "kw": round(charge_kw, 2), "price_krw_per_kwh": price})
            total_charge_kwh += charge_kw
            charge_cost_krw += charge_kw * price
        if discharge_kw > 0.01:
            discharge_intervals.append({"time": t.isoformat(), "kw": round(discharge_kw, 2), "price_krw_per_kwh": sell})
            total_discharge_kwh += discharge_kw
            discharge_revenue_krw += discharge_kw * sell

    start_soc = session["soc_baseline_pct"][times[0]]
    end_soc = session["soc_baseline_pct"][times[-1]]

    degradation_cost_krw = 0.0
    if economics is not None:
        degradation_cost_krw = (
            total_discharge_kwh / economics.discharge_efficiency * economics.degradation_krw_per_kwh
        )

    plan = {
        "session_id": session_id,
        "ev_id": session_ev_map.get(session_id),
        "session_start": times[0].isoformat(),
        "session_end": times[-1].isoformat(),
        "departure_time": times[-1].isoformat(),
        "observed_start_soc_pct": round(float(start_soc), 1),
        "observed_end_soc_pct": round(float(end_soc), 1),
        "battery_capacity_kwh": session["capacity_kwh"],
        "charge_intervals": charge_intervals,
        "discharge_intervals": discharge_intervals,
        "total_charge_kwh": round(total_charge_kwh, 2),
        "total_discharge_kwh": round(total_discharge_kwh, 2),
        "charge_cost_krw": round(charge_cost_krw, 0),
        "discharge_revenue_krw": round(discharge_revenue_krw, 0),
        "net_krw": round(discharge_revenue_krw - charge_cost_krw - degradation_cost_krw, 0),
    }
    if economics is not None:
        plan["degradation_cost_krw"] = round(degradation_cost_krw, 0)
        plan["net_before_degradation_krw"] = round(discharge_revenue_krw - charge_cost_krw, 0)
        plan["assumptions"] = economics.as_dict()  # verified=False면 '가정값'으로 표기해야 한다
    return plan
