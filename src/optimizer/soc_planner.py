"""배터리 잔량(SOC)을 상태변수로 두는 단일 차량 충·방전 계획 (MILP) — PRD FR-O03.

기존 `poc_compare._arbitrage_with_economics`는 "최종 순에너지"만 맞추고 시간대별 잔량은 제한하지 않는다. 그래서 일찍 많이
충전한 뒤 나중에 방전하는 계획에서 잔량이 100%를 넘거나, 잔량이 80%를 넘은 상태에서 방전하는 계획이 나올 수 있었다.
이 모듈은 시간대별 잔량 e_t를 변수로 두고 다음을 모두 제약으로 건다.

  e_{t+1} = e_t + η_c·c_t − d_t/η_d            (c: 계통에서 산 kWh, d: 계통으로 내보낸 kWh)
  e_0 = 현재 잔량,  e_n ≥ 목표 잔량
  reserve ≤ e_t ≤ 용량                          (예비 잔량 아래·만충 초과 금지)
  c_t ≤ P_c·(1−u_t),  d_t ≤ P_d·u_t            (같은 시간에 충전과 방전을 동시에 하지 않음)
  u_t=1 이면 방전 허용 범위(lo~hi) 안에서만 방전   (고전압·저잔량 구간 방전 금지)
  d_t ≥ d_min·u_t                              (모드가 켜지면 실제로 방전해야 함 — 쉬었다 다시 하는 방전을 한 번으로 세지 않게)
  하루 방전 시작 횟수 ≤ 상한                      (s_t ≥ u_t − u_{t−1})

목적: 최소화 Σ [ buy_t·c_t − sell_t·d_t + (열화단가/η_d)·d_t ] (+ 사이클 미세 벌점, 일찍 충전을 선호하는 미세 벌점)
열화비용은 배터리에서 꺼낸 kWh(= d/η_d)에 붙는다. 방전이 손해인 조건(보상이 손익분기 미만)에서는 최적해가 방전하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

CYCLE_PENALTY = 0.01          # 의미 없는 충·방전 왕복을 막는 미세 벌점(원/kWh)
MIN_DISCHARGE_KWH = 0.5       # 방전 모드가 켜진 시간의 최소 방전량 — 모드만 켜 두고 쉬는 편법으로 횟수 제한을 피하지 못하게 한다
EARLY_CHARGE_TIEBREAK = 1e-4  # 비용이 같을 때 일찍 충전하는 쪽을 선호


class PlannerError(ValueError):
    pass


@dataclass(frozen=True)
class PlanResult:
    charge: np.ndarray        # 시간별 계통 구입 kWh
    discharge: np.ndarray     # 시간별 계통 판매 kWh
    energy: np.ndarray        # 시간 경계별 배터리 에너지(kWh), 길이 n+1
    status: str               # optimal | fallback_v1g
    objective: float


def _solve(buy, sell, e0, cap, target, eta_c, eta_d, deg, kw_c, kw_d, reserve, band_lo, band_hi,
           allow_discharge, max_events, day_ids):
    n = len(buy)
    nv = 5 * n + 1                       # c, d, u, s (각 n) + e (n+1)
    C, D, U, S, E = 0, n, 2 * n, 3 * n, 4 * n
    cost = np.zeros(nv)
    for t in range(n):
        cost[C + t] = buy[t] + EARLY_CHARGE_TIEBREAK * t
        cost[D + t] = -sell[t] + deg / eta_d + CYCLE_PENALTY

    lb, ub = np.zeros(nv), np.zeros(nv)
    ub[C:C + n] = kw_c
    ub[D:D + n] = kw_d if allow_discharge else 0.0
    ub[U:U + n] = 1.0 if allow_discharge else 0.0
    ub[S:S + n] = 1.0 if allow_discharge else 0.0
    floor = min(reserve, e0)             # 이미 예비 잔량 아래에서 시작하면 그 아래로 더 내려가지 않게만 한다
    lb[E:E + n + 1], ub[E:E + n + 1] = floor, cap
    lb[E] = ub[E] = e0
    lb[E + n] = max(floor, min(target, cap))
    integrality = np.zeros(nv)
    integrality[U:S + n] = 1

    rows, lo_b, hi_b = [], [], []
    A = lil_matrix((n * 7 + 2 * len(set(day_ids)), nv))
    min_d = min(MIN_DISCHARGE_KWH, 0.5 * kw_d)
    r = 0

    def add(coefs: dict, low: float, high: float):
        nonlocal r
        for k, v in coefs.items():
            A[r, k] = v
        lo_b.append(low); hi_b.append(high)
        r += 1

    for t in range(n):
        add({E + t + 1: 1.0, E + t: -1.0, C + t: -eta_c, D + t: 1.0 / eta_d}, 0.0, 0.0)           # 에너지 수지
        add({C + t: 1.0, U + t: kw_c}, -np.inf, kw_c)                                              # 충전·방전 동시 금지
        add({D + t: 1.0, U + t: -kw_d}, -np.inf, 0.0)
        add({D + t: 1.0, U + t: -min_d}, 0.0, np.inf)                                              # 모드 on이면 실제 방전
        add({E + t: 1.0, U + t: cap - band_hi}, -np.inf, cap)                                      # 방전 시 잔량 ≤ hi
        add({E + t: 1.0, U + t: -band_lo}, 0.0, np.inf)                                            # 방전 시 잔량 ≥ lo
        add({S + t: 1.0, U + t: -1.0, **({U + t - 1: 1.0} if t > 0 else {})}, 0.0, np.inf)         # 방전 시작 표시
    if max_events is not None:
        for day in sorted(set(day_ids)):
            add({S + t: 1.0 for t in range(n) if day_ids[t] == day}, 0.0, float(max_events))

    A = A[:r].tocsr()
    res = milp(cost, constraints=LinearConstraint(A, lo_b, hi_b), integrality=integrality, bounds=Bounds(lb, ub))
    return res, (C, D, E, n)


def plan_single_vehicle(buy, sell, soc0_kwh: float, capacity_kwh: float, target_kwh: float, eta_c: float, eta_d: float,
                        degradation_krw_per_kwh: float, charge_kw: float, discharge_kw: float, reserve_kwh: float,
                        band_kwh: tuple[float, float], discharge_capable: bool, max_events_per_day: int | None,
                        day_ids: list) -> PlanResult:
    """buy/sell: 시간별 원/kWh. day_ids: 시간별 날짜 식별자(하루 방전 횟수 제한용). 에너지 단위는 모두 kWh."""
    buy, sell = np.asarray(buy, dtype=float), np.asarray(sell, dtype=float)
    n = len(buy)
    if n == 0 or len(sell) != n or len(day_ids) != n:
        raise PlannerError("가격·날짜 길이가 서로 달라요")
    if not 0 < eta_c <= 1 or not 0 < eta_d <= 1:
        raise PlannerError("효율은 0 초과 1 이하여야 해요")
    if not 0 <= soc0_kwh <= capacity_kwh:
        raise PlannerError("현재 잔량이 배터리 용량 범위를 벗어났어요")
    lo, hi = band_kwh
    for allow in ((True, False) if discharge_capable else (False,)):
        res, (C, D, E, _n) = _solve(buy, sell, soc0_kwh, capacity_kwh, target_kwh, eta_c, eta_d, degradation_krw_per_kwh,
                                    charge_kw, discharge_kw, reserve_kwh, lo, hi, allow, max_events_per_day, day_ids)
        if res.success:
            x = res.x
            charge = np.clip(x[C:C + n], 0, None)
            discharge = np.clip(x[D:D + n], 0, None)
            return PlanResult(charge, discharge, x[E:E + n + 1], "optimal" if allow or not discharge_capable else "fallback_v1g",
                              float(res.fun))
    raise PlannerError("조건을 만족하는 계획이 없어요(출발까지 시간이 부족하거나 목표가 너무 높을 수 있어요)")
