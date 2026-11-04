"""V2G 최적 충·방전 의사결정 (WS3) — 세션·가격 신호를 입력으로 각 EV 세션의
시간대별 충전/방전량을 결정한다.

두 가지 목표를 동시에 고려한다:
  1) valley-filling(1차 목표, 큰 가중치): 여러 EV가 같은 저가 시간대로 몰려
     새로운 피크를 만들지 않도록 선단(fleet) 전체의 시간대별 합계 부하를 평탄화한다.
  2) 경제성(2차 목표): 동일한 평탄화 수준 안에서는 더 저렴한 시간대를 우선 사용한다.

선형계획(LP)으로 정식화해 scipy.optimize.linprog(HiGHS)로 푼다. 실증 규모(EV 40대 x
16주)에서는 변수 수가 수만~수십만에 달할 수 있어, 제약행렬은 반드시 희소행렬(sparse)로
구성한다 — 밀집행렬(dense)로 만들면 메모리가 기가바이트 단위로 폭증해 사실상 풀 수 없다.
"""
import logging
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

logger = logging.getLogger(__name__)

PEAK_WEIGHT = 10_000.0  # valley-filling을 가격보다 압도적으로 우선시
DISCHARGE_CYCLE_PENALTY = 0.01  # 의미 없는 충-방전 사이클(라운드트립) 방지용 미세 페널티

# SoH 보호를 위한 SOC 밴드 방전 컷오프 — 만충 영역(고전압 스트레스)과 과방전 인접 영역
# (고전류 내부저항 증가 구간)에서는 V2G 방전을 금지하고, 화학적으로 가장 안정적인
# 구간에서만 방전을 허용한다. LP가 시간대별 SOC를 상태변수로 갖지 않으므로(집계 에너지
# 제약만 있음), 세션의 관측(baseline) SOC 궤적을 근사치로 사용해 그 시간대의 방전
# 상한을 0으로 낮추는 방식으로 근사 구현한다 — 실제 최적화된 궤적과는 다를 수 있는
# 근사이지만, 최소한 "관측상 이미 SOC가 위험 구간이었던 시간대"의 방전은 확실히 막는다.
SOC_BAND_MIN_PCT = 35.0
SOC_BAND_MAX_PCT = 80.0


def _session_needed_kwh(session: dict, target_soc: float) -> float:
    """세션이 최소 충전해야 할 에너지량.

    target_soc(정책 목표)를 기본 하한으로 하되, 실제 관측된 세션 종료 SOC가 그보다
    높았다면(사용자가 실제로 더 충전했었다면) 그 값을 하한으로 사용해 최적화가
    실제 필요보다 적게 충전하지 않도록 한다. baseline 궤적 중 시작/끝 값만 사용한다.
    """
    times = session["times"]
    start_soc = session["soc_baseline_pct"][times[0]]
    end_soc = session["soc_baseline_pct"][times[-1]]
    target_floor = max(target_soc * 100, end_soc)
    return max((target_floor - start_soc) / 100 * session["capacity_kwh"], 0.0)


def _immediate_schedule(sessions: dict, max_charge_kw: float, needed_by_sid: dict, eta_c: float) -> dict:
    """도착 즉시 최대 출력으로 필요한 만큼만 충전하는 계획(최적화가 실패했을 때의 보수적 대체안)."""
    schedule: dict[str, dict] = {}
    for sid, session in sessions.items():
        remaining = needed_by_sid.get(sid, 0.0) / eta_c        # 계통에서 사야 하는 kWh
        row = {}
        for t in session["times"]:
            put = min(max_charge_kw, max(remaining, 0.0))
            row[t] = (round(put, 4), 0.0)
            remaining -= put
        schedule[sid] = row
    return schedule


def optimize_fleet(
    sessions: dict,
    max_charge_kw: float,
    max_discharge_kw: float,
    reserve_soc: float,
    target_soc: float,
    price_by_time: dict,
    economics=None,
) -> dict:
    """economics(EconomicsParams)가 있으면 충·방전 효율과 열화비용을 반영한다 — 에너지 수지는
    eta_c*충전 - 방전/eta_d >= 필요량, 방전 비용에는 열화단가를 더한다. 없으면 기존 동작 그대로.

    방전이 가능한 차량은 시간대별 배터리 잔량(예비 잔량 이상, 용량 이하)을 상태변수로 둬 제한한다 — 일찍 많이 충전했다가
    나중에 방전하는 계획에서 잔량이 용량을 넘거나 예비 아래로 내려가는 것을 막는다."""
    if not sessions:
        return {}
    eta_c = economics.charge_efficiency if economics else 1.0
    inv_eta_d = 1.0 / economics.discharge_efficiency if economics else 1.0

    session_ids = list(sessions.keys())
    var_index: dict[tuple, int] = {}
    n_vars = 0
    for sid in session_ids:
        for t in sessions[sid]["times"]:
            var_index[(sid, t, "c")] = n_vars
            n_vars += 1
            var_index[(sid, t, "d")] = n_vars
            n_vars += 1
    peak_idx = n_vars
    n_vars += 1

    def _band_ok(session: dict, t) -> bool:
        soc_here = session["soc_baseline_pct"].get(t)
        return soc_here is not None and SOC_BAND_MIN_PCT <= soc_here <= SOC_BAND_MAX_PCT

    state_idx: dict[tuple, int] = {}     # (세션, k) → k번째 시간이 끝난 뒤의 배터리 에너지(kWh)
    stateful: list = []
    for sid in session_ids:
        sess = sessions[sid]
        if sess.get("v2g_capable", True) and any(_band_ok(sess, t) for t in sess["times"]):
            stateful.append(sid)
            for k in range(1, len(sess["times"]) + 1):
                state_idx[(sid, k)] = n_vars
                n_vars += 1

    c = np.zeros(n_vars)
    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)
    for sid in session_ids:
        session = sessions[sid]
        v2g_capable = session.get("v2g_capable", True)  # 차종 데이터가 없으면(구 스키마) 기본 허용
        soc_trajectory = session["soc_baseline_pct"]
        for t in session["times"]:
            ci, di = var_index[(sid, t, "c")], var_index[(sid, t, "d")]
            c[ci] = price_by_time.get(t, 0.0)
            c[di] = DISCHARGE_CYCLE_PENALTY + (
                economics.degradation_krw_per_kwh / economics.discharge_efficiency if economics else 0.0
            )
            ub[ci] = max_charge_kw
            soc_here = soc_trajectory.get(t)
            in_band = soc_here is not None and SOC_BAND_MIN_PCT <= soc_here <= SOC_BAND_MAX_PCT
            ub[di] = max_discharge_kw if (v2g_capable and in_band) else 0.0
    c[peak_idx] = PEAK_WEIGHT
    for sid in stateful:
        sess = sessions[sid]
        cap_kwh = float(sess["capacity_kwh"])
        e0 = sess["soc_baseline_pct"][sess["times"][0]] / 100.0 * cap_kwh
        floor = min(reserve_soc * cap_kwh, e0)        # 이미 예비 아래에서 시작하면 그 아래로 더 내려가지 않게만 한다
        for k in range(1, len(sess["times"]) + 1):
            lb[state_idx[(sid, k)]], ub[state_idx[(sid, k)]] = floor, cap_kwh

    by_time: dict = defaultdict(list)
    for sid in session_ids:
        for t in sessions[sid]["times"]:
            by_time[t].append((var_index[(sid, t, "c")], var_index[(sid, t, "d")]))

    row_idx: list[int] = []
    col_idx: list[int] = []
    data: list[float] = []
    b_ub: list[float] = []
    row = 0

    # 시간대별 fleet 합계(충전-방전) <= peak
    for _t, pairs in by_time.items():
        for ci, di in pairs:
            row_idx += [row, row]
            col_idx += [ci, di]
            data += [1.0, -1.0]
        row_idx.append(row)
        col_idx.append(peak_idx)
        data.append(-1.0)
        b_ub.append(0.0)
        row += 1

    # 세션별 최소 필요 에너지: charge - discharge >= needed_kwh  ->  -charge + discharge <= -needed_kwh
    # 세션 길이 x 최대충전출력을 넘어서는 요구량은 물리적으로 불가능(infeasible)하므로 상한을 둔다
    # — 짧은 세션에 목표 SOC를 강제하면 풀 수 없는 문제가 되는 것을 방지.
    needed_by_sid: dict = {}
    for sid in session_ids:
        session = sessions[sid]
        # 배터리에 실제로 들어가는 에너지는 충전량 × 충전 효율이다. 상한에 효율을 빼먹으면(예전 버그) 짧은 세션 하나만 있어도
        # 풀 수 없는 문제가 되어 선단 전체가 "충전 0"으로 처리됐다 — 상한도 효율을 반영한다.
        reachable = eta_c * max_charge_kw * len(session["times"]) * (1.0 - 1e-9)
        needed_kwh = min(_session_needed_kwh(session, target_soc), reachable)
        needed_by_sid[sid] = needed_kwh
        for t in session["times"]:
            row_idx += [row, row]
            col_idx += [var_index[(sid, t, "c")], var_index[(sid, t, "d")]]
            data += [-eta_c, inv_eta_d]
        b_ub.append(-needed_kwh)
        row += 1

    a_ub = coo_matrix((data, (row_idx, col_idx)), shape=(row, n_vars))

    # 잔량 상태: e_{k+1} = e_k + η충·c_k − d_k/η방  (e_0는 관측된 시작 잔량)
    eq_r, eq_c, eq_v, b_eq = [], [], [], []
    er = 0
    for sid in stateful:
        sess = sessions[sid]
        cap_kwh = float(sess["capacity_kwh"])
        e_prev_const = sess["soc_baseline_pct"][sess["times"][0]] / 100.0 * cap_kwh
        for k, t in enumerate(sess["times"], start=1):
            eq_r += [er, er, er]
            eq_c += [state_idx[(sid, k)], var_index[(sid, t, "c")], var_index[(sid, t, "d")]]
            eq_v += [1.0, -eta_c, inv_eta_d]
            if k == 1:
                b_eq.append(e_prev_const)
            else:
                eq_r.append(er)
                eq_c.append(state_idx[(sid, k - 1)])
                eq_v.append(-1.0)
                b_eq.append(0.0)
            er += 1
    a_eq = coo_matrix((eq_v, (eq_r, eq_c)), shape=(er, n_vars)) if er else None

    result = linprog(
        c, A_ub=a_ub, b_ub=np.array(b_ub), A_eq=a_eq, b_eq=np.array(b_eq) if er else None,
        bounds=list(zip(lb, ub)), method="highs",
    )
    if not result.success:
        # 해를 못 찾았을 때 모든 세션을 "충전 0"으로 두면 비용·피크가 0으로 계산돼 절감률이 100%로 부풀려진다(실제 있었던 오류).
        # 가장 보수적인 대안(도착 즉시 충전)으로 되돌리고 반드시 알린다.
        logger.error("선단 최적화 실패(status=%s): 즉시 충전 계획으로 대체합니다", result.status)
        return _immediate_schedule(sessions, max_charge_kw, needed_by_sid, eta_c)
    x = result.x

    schedule: dict[str, dict] = {sid: {} for sid in session_ids}
    for sid in session_ids:
        for t in sessions[sid]["times"]:
            charge = float(x[var_index[(sid, t, "c")]])
            discharge = float(x[var_index[(sid, t, "d")]])
            schedule[sid][t] = (round(charge, 4), round(discharge, 4))

    return schedule
