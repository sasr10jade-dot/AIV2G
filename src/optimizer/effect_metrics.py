"""AI 적용 전·후 효용성 검증 지표 (계획서 4.1 다. "EV 30대 기반 V2G 타당성 검증").

계획서의 비교 관점을 그대로 지표로 만든다.
  일반 충전 환경(적용 전) = 차량 연결 즉시 충전하는 현재 이용 방식(관측된 그대로)
  AI 최적화 적용 환경(적용 후) = 같은 데이터에 AI 예측·추론으로 충전 시점·자원량을 조정한 결과
  비교 관점 = 피크 시간대 충전 비중, 충전 비용, 목표 배터리 도달 여부, AI 추론 V2G 예측률

그리고 "운행조건·충전시간대·SOC·전력수요 등 다양한 조건에서 반복 분석"(조건별 반복 분석)을 같은 산식으로 나눠 계산한다.

모든 결과는 실측이 아니라 추론이다(generation="inferred") — 최적화 후 부하·방전량은 계산 결과이며 실제 방전이 아니다.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

PEAK_QUANTILE = 0.75   # 하루 중 전력수요 상위 25% 시간(하루 6시간)을 피크 시간대로 본다
TIME_BANDS = (("심야(00~06시)", 0, 6), ("오전(06~12시)", 6, 12), ("오후(12~18시)", 12, 18), ("저녁(18~24시)", 18, 24))
SOC_BANDS = (("저잔량(<35%)", 0.0, 35.0), ("중간(35~80%)", 35.0, 80.0), ("고잔량(≥80%)", 80.0, 100.01))


def peak_hours(grid_df: pd.DataFrame) -> set:
    """피크 시간대 — 날짜별 계통 전력수요가 그날 상위 25%인 시각. 요금표 구간이 아니라 계통 수요 기준이라
    실제 전력수요(KPX) 데이터로 바꿔도 같은 정의를 쓴다."""
    if grid_df.empty:
        return set()
    g = grid_df[["timestamp", "power_demand_mw"]].dropna()
    day = g["timestamp"].dt.normalize()
    cut = g.groupby(day)["power_demand_mw"].transform(lambda v: v.quantile(PEAK_QUANTILE))
    return set(g.loc[g["power_demand_mw"] >= cut, "timestamp"])


def _share(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 1) if whole > 1e-9 else None


def peak_metrics(comparison: pd.DataFrame, grid_df: pd.DataFrame) -> dict:
    """피크 시간대(계통 수요 상위 25%) 충전 비중과 선단 최대 부하.
    적용 후 충전량은 순부하가 아니라 충전 쪽(양수)만 센다 — 방전이 피크 비중을 가리지 않게."""
    hours = peak_hours(grid_df)
    peak = comparison["timestamp"].isin(hours).to_numpy()
    base = comparison["baseline_kw"].clip(lower=0).to_numpy()
    discharge = comparison["discharge_kw"].to_numpy() if "discharge_kw" in comparison.columns else np.zeros(len(comparison))
    opt_charge = (comparison["optimized_kw"].to_numpy() + discharge).clip(min=0)
    return {
        "peak_definition": "날짜별 계통 전력수요 상위 25% 시간",
        "baseline_peak_charge_share_pct": _share(float(base[peak].sum()), float(base.sum())),
        "optimized_peak_charge_share_pct": _share(float(opt_charge[peak].sum()), float(opt_charge.sum())),
        "baseline_max_load_kw": round(float(comparison["baseline_kw"].max()), 1) if len(comparison) else 0.0,
        "optimized_max_load_kw": round(float(comparison["optimized_kw"].max()), 1) if len(comparison) else 0.0,
    }


def _session_rows(run: dict, ev_df: pd.DataFrame, price_by_time: dict, economics) -> list[dict]:
    """세션별 적용 전·후 비교 원자료 — 시작/종료 SOC, 비용, 목표 배터리 도달 여부."""
    eta_c = economics.charge_efficiency if economics else 1.0
    eta_d = economics.discharge_efficiency if economics else 1.0
    observed = ev_df.set_index(["ev_id", "timestamp"])["charging_kw"]
    rows = []
    for sid, session in run["sessions"].items():
        ev_id = run["session_ev_map"][sid]
        times = session["times"]
        soc = session["soc_baseline_pct"]
        cap = session["capacity_kwh"]
        sched = run["schedule"].get(sid, {})
        charge = np.array([sched.get(t, (0.0, 0.0))[0] for t in times])
        discharge = np.array([sched.get(t, (0.0, 0.0))[1] for t in times])
        price = np.array([float(price_by_time.get(t, 0.0)) for t in times])
        base_kw = np.array([float(observed.get((ev_id, t), 0.0)) for t in times])
        start_soc, end_soc = float(soc[times[0]]), float(soc[times[-1]])
        opt_end_soc = start_soc + (float(charge.sum()) * eta_c - float(discharge.sum()) / eta_d) / cap * 100
        rows.append({
            "session_id": sid, "ev_id": ev_id, "start": times[0], "hour": times[0].hour,
            "weekend": times[0].dayofweek >= 5, "start_soc_pct": start_soc,
            "baseline_end_soc_pct": end_soc, "optimized_end_soc_pct": min(opt_end_soc, 100.0),
            "baseline_cost_krw": float((base_kw * price).sum()),
            "optimized_cost_krw": float(((charge - discharge) * price).sum()),
            "discharge_kwh": float(discharge.sum()),
        })
    return rows


def target_reach(rows: list[dict], target_soc: float) -> dict:
    """세션 종료 시 목표 배터리 도달 비율. 적용 후는 관측 종료 SOC가 목표보다 높았던 세션은 그 값을 목표로 본다
    (최적화가 실제 이용자보다 덜 채우지 않았는지 확인 — decision._session_needed_kwh와 같은 기준)."""
    if not rows:
        return {"sessions": 0, "baseline_reach_pct": None, "optimized_reach_pct": None}
    tgt = target_soc * 100
    base_ok = sum(r["baseline_end_soc_pct"] >= tgt - 0.5 for r in rows)
    opt_ok = sum(r["optimized_end_soc_pct"] >= max(tgt, r["baseline_end_soc_pct"]) - 0.5 or
                 r["optimized_end_soc_pct"] >= 99.5 for r in rows)
    return {"sessions": len(rows), "target_soc_pct": tgt,
            "baseline_reach_pct": _share(base_ok, len(rows)), "optimized_reach_pct": _share(opt_ok, len(rows))}


def prediction_rate(y_true, y_pred) -> float | None:
    """AI 추론 예측률(%) = (1 − WAPE) × 100, WAPE = Σ|실측−예측| / Σ|실측|. 0 미만은 0으로 자른다.
    실측과 예측을 같은 시각끼리 비교한 값이며, 실측 합이 0이면 계산하지 않는다."""
    y_true, y_pred = np.asarray(y_true, dtype=float), np.asarray(y_pred, dtype=float)
    denom = float(np.abs(y_true).sum())
    if denom <= 1e-9:
        return None
    return round(max(0.0, 1 - float(np.abs(y_true - y_pred).sum()) / denom) * 100, 1)


def _group_summary(rows: list[dict]) -> dict:
    base = sum(r["baseline_cost_krw"] for r in rows)
    opt = sum(r["optimized_cost_krw"] for r in rows)
    return {"sessions": len(rows), "baseline_cost_krw": round(base), "optimized_cost_krw": round(opt),
            "savings_krw": round(base - opt), "savings_pct": _share(base - opt, base),
            "discharge_kwh": round(sum(r["discharge_kwh"] for r in rows), 1)}


def condition_breakdown(rows: list[dict], comparison: pd.DataFrame, grid_df: pd.DataFrame) -> dict:
    """조건별 반복 분석 — 같은 산식을 충전 시작 시간대·평일/주말·시작 SOC·전력수요 수준별로 나눠 계산한다."""
    by_time: dict[str, list] = defaultdict(list)
    by_day: dict[str, list] = defaultdict(list)
    by_soc: dict[str, list] = defaultdict(list)
    for r in rows:
        by_time[next(lbl for lbl, a, b in TIME_BANDS if a <= r["hour"] < b)].append(r)
        by_day["주말" if r["weekend"] else "평일"].append(r)
        by_soc[next(lbl for lbl, a, b in SOC_BANDS if a <= r["start_soc_pct"] < b)].append(r)

    # 전력수요 수준: 시간대별 계통 수요를 3분위로 나눠 그 시간의 선단 충전량(적용 전·후)을 비교한다
    demand = comparison.merge(grid_df[["timestamp", "power_demand_mw"]], on="timestamp", how="left").dropna(subset=["power_demand_mw"])
    by_demand = []
    if len(demand) >= 3:
        q1, q2 = demand["power_demand_mw"].quantile([1 / 3, 2 / 3])
        level = np.where(demand["power_demand_mw"] <= q1, "낮음", np.where(demand["power_demand_mw"] <= q2, "보통", "높음"))
        discharge = demand["discharge_kw"] if "discharge_kw" in demand.columns else 0.0
        demand = demand.assign(_level=level, _opt_charge=(demand["optimized_kw"] + discharge).clip(lower=0))
        total_b, total_o = float(demand["baseline_kw"].sum()), float(demand["_opt_charge"].sum())
        for lvl in ("낮음", "보통", "높음"):
            part = demand[demand["_level"] == lvl]
            by_demand.append({"label": f"전력수요 {lvl}", "hours": int(len(part)),
                              "baseline_charge_share_pct": _share(float(part["baseline_kw"].sum()), total_b),
                              "optimized_charge_share_pct": _share(float(part["_opt_charge"].sum()), total_o)})

    def rows_of(groups: dict, order) -> list[dict]:
        return [{"label": lbl, **_group_summary(groups[lbl])} for lbl in order if groups.get(lbl)]

    return {
        "time_band": rows_of(by_time, [lbl for lbl, *_ in TIME_BANDS]),
        "day_type": rows_of(by_day, ["평일", "주말"]),
        "start_soc": rows_of(by_soc, [lbl for lbl, *_ in SOC_BANDS]),
        "power_demand": by_demand,
    }


def compute_effect_metrics(run: dict, comparison: pd.DataFrame, ev_df: pd.DataFrame, grid_df: pd.DataFrame,
                           target_soc: float, economics=None, v2g_forecast: tuple | None = None) -> dict:
    """적용 전·후 효용성 검증 결과 한 묶음. v2g_forecast=(실측, 예측) 시계열이 있으면 V2G 예측률을 함께 낸다."""
    rows = _session_rows(run, ev_df, run["price_by_time"], economics)
    total = _group_summary(rows)
    return {
        "peak": peak_metrics(comparison, grid_df),
        "cost": total,
        "target_reach": target_reach(rows, target_soc),
        "v2g_prediction_rate_pct": prediction_rate(*v2g_forecast) if v2g_forecast else None,
        "conditions": condition_breakdown(rows, comparison, grid_df),
        "generation": "inferred",
        "note": "적용 후 값은 같은 실측 데이터에 AI 최적화를 적용한 추론 결과이며 실제 방전이 아닙니다.",
    }
