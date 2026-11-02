"""충전 스케줄 — 즉시 충전(기준)과 태양광 우선 최적 충전을 만들고 같은 기준으로 평가한다.

- baseline_schedule: 도착하자마자 충전기 최대 출력으로 채운다(현행 운영).
- optimize_schedule: 세션별로 출발 전까지 필요 에너지를 채우되, 계통에서 사 오는 전력의 요금을 최소화한다.
  태양광이 있는 시간은 계통 구입이 0이라 자연스럽게 그 시간으로 충전이 이동한다. 계통 구입 피크는
  날짜별로 작은 벌점을 줘 한 시간에 몰리지 않게 한다.
- evaluate: 시간별 PV 사용·계통 구입·잉여(버려지는 발전)를 계산하고, 그 시간의 PV 비중만큼 세션에 나눠 세션별
  태양광 비중과 요금을 낸다.

스케줄은 [세션, 시간] 행렬이며 값은 그 시간에 충전한 kWh(= 평균 kW)다.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from src.solar.demand import Session

EARLY_TIEBREAK = 1e-3        # 비용이 같을 때 일찍 채우는 쪽을 선호(출발 지연 위험 완화)
PEAK_WEIGHT_KRW_PER_KW = 5.0  # 날짜별 계통 구입 피크 벌점(원/kW)


def baseline_schedule(sessions: list[Session], charger_kw: float, hours: int) -> np.ndarray:
    sched = np.zeros((len(sessions), hours))
    for s in sessions:
        remaining = s.need_kwh
        for t in s.window:
            if remaining <= 1e-9 or t >= hours:
                break
            put = min(charger_kw, remaining)
            sched[s.session_id, t] = put
            remaining -= put
    return sched


def optimize_schedule(sessions: list[Session], charger_kw: float, pv_kw: np.ndarray, price: np.ndarray,
                      peak_weight: float = PEAK_WEIGHT_KRW_PER_KW) -> tuple[np.ndarray, str]:
    """선형계획으로 계통 구입 요금을 최소화한다. 실패하면 즉시 충전으로 되돌리고 상태를 알려 준다."""
    hours = len(pv_kw)
    if not sessions:
        return np.zeros((0, hours)), "empty"
    n_days = (hours + 23) // 24
    # 변수: 세션·시간별 충전량 c, 시간별 계통 구입 g, 날짜별 피크 p
    index: dict[tuple[int, int], int] = {}
    for s in sessions:
        for t in s.window:
            if t < hours:
                index[(s.session_id, t)] = len(index)
    n_c = len(index)
    g0, p0 = n_c, n_c + hours
    n_vars = p0 + n_days

    cost = np.zeros(n_vars)
    for (sid, t), j in index.items():
        cost[j] = EARLY_TIEBREAK * (t - sessions[sid].arrival)
    cost[g0:g0 + hours] = price
    cost[p0:] = peak_weight

    # 부등식: 시간별 (충전 합 - g <= PV),  g_t - p_day <= 0
    rows, cols, vals, b = [], [], [], []
    by_hour: dict[int, list[int]] = {}
    for (sid, t), j in index.items():
        by_hour.setdefault(t, []).append(j)
    r = 0
    for t in range(hours):
        for j in by_hour.get(t, []):
            rows.append(r); cols.append(j); vals.append(1.0)
        rows.append(r); cols.append(g0 + t); vals.append(-1.0)
        b.append(float(pv_kw[t]))
        r += 1
    for t in range(hours):
        rows += [r, r]; cols += [g0 + t, p0 + t // 24]; vals += [1.0, -1.0]
        b.append(0.0)
        r += 1
    a_ub = coo_matrix((vals, (rows, cols)), shape=(r, n_vars))

    # 등식: 세션별 충전량 합 = 필요 에너지
    er, ec, ev, eb = [], [], [], []
    for k, s in enumerate(sessions):
        for t in s.window:
            if t < hours:
                er.append(k); ec.append(index[(s.session_id, t)]); ev.append(1.0)
        eb.append(s.need_kwh)
    a_eq = coo_matrix((ev, (er, ec)), shape=(len(sessions), n_vars))

    bounds = [(0.0, charger_kw)] * n_c + [(0.0, None)] * (hours + n_days)
    res = linprog(cost, A_ub=a_ub, b_ub=np.array(b), A_eq=a_eq, b_eq=np.array(eb), bounds=bounds, method="highs")
    if not res.success:
        return baseline_schedule(sessions, charger_kw, hours), f"fallback:{res.status}"
    sched = np.zeros((len(sessions), hours))
    for (sid, t), j in index.items():
        sched[sid, t] = max(res.x[j], 0.0)
    return sched, "optimal"


def evaluate(sessions: list[Session], sched: np.ndarray, pv_kw: np.ndarray, price: np.ndarray,
             pv_price: float, grid_limit_kw: float) -> dict:
    load = sched.sum(axis=0)
    pv_used = np.minimum(pv_kw, load)
    grid = load - pv_used
    curtailed = pv_kw - pv_used
    share = np.divide(pv_used, load, out=np.zeros_like(load), where=load > 1e-9)   # 그 시간 충전 중 PV 비중

    pv_total = float(pv_kw.sum())
    ev_total = float(load.sum())
    grid_cost = float((grid * price).sum())
    pv_cost = float(pv_used.sum() * pv_price)

    per_session = []
    for s in sessions:
        c = sched[s.session_id]
        kwh = float(c.sum())
        pv_kwh = float((c * share).sum())
        grid_kwh = kwh - pv_kwh
        paid = pv_kwh * pv_price + float((c * (1.0 - share) * price).sum())
        per_session.append({
            "session_id": s.session_id, "kwh": round(kwh, 2), "pv_kwh": round(pv_kwh, 2), "grid_kwh": round(grid_kwh, 2),
            "solar_share_pct": round(pv_kwh / kwh * 100, 1) if kwh > 1e-9 else 0.0, "cost_krw": round(paid, 0),
        })
    return {
        "hourly": {"load_kw": load, "pv_used_kw": pv_used, "grid_kw": grid, "curtailed_kw": curtailed},
        "summary": {
            "ev_energy_kwh": round(ev_total, 1),
            "pv_generation_kwh": round(pv_total, 1),
            "pv_used_kwh": round(float(pv_used.sum()), 1),
            "pv_curtailed_kwh": round(float(curtailed.sum()), 1),
            "self_consumption_pct": round(float(pv_used.sum()) / pv_total * 100, 1) if pv_total > 1e-9 else 0.0,
            "solar_share_pct": round(float(pv_used.sum()) / ev_total * 100, 1) if ev_total > 1e-9 else 0.0,
            "grid_import_kwh": round(float(grid.sum()), 1),
            "grid_peak_kw": round(float(grid.max()) if len(grid) else 0.0, 1),
            "grid_limit_exceeded_hours": int((grid > grid_limit_kw + 1e-9).sum()),
            "grid_cost_krw": round(grid_cost, 0),
            "driver_cost_krw": round(grid_cost + pv_cost, 0),
            "driver_avg_krw_per_kwh": round((grid_cost + pv_cost) / ev_total, 1) if ev_total > 1e-9 else 0.0,
        },
        "per_session": per_session,
    }
