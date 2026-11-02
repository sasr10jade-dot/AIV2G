"""SOC 상태변수 최적화기(FR-O03) 검증 — 제약 준수, 독립 계산과의 대조(탐욕법·손익분기), 무작위 조건."""
import numpy as np
import pytest

from src.optimizer.economics import EconomicsParams, breakeven_sell_price
from src.optimizer.soc_planner import PlannerError, plan_single_vehicle

ETA_C = ETA_D = 0.92
DEG = 140000 / 3000
CAP, KW = 80.0, 7.0
LO, HI, RESERVE = 0.35 * CAP, 0.80 * CAP, 0.20 * CAP


def solve(buy, sell, soc0_pct=50.0, target_pct=80.0, capable=True, events=2, n_per_day=24, deg=DEG, **over):
    n = len(buy)
    kwargs = dict(
        soc0_kwh=soc0_pct / 100 * CAP, capacity_kwh=CAP, target_kwh=target_pct / 100 * CAP, eta_c=ETA_C, eta_d=ETA_D,
        degradation_krw_per_kwh=deg, charge_kw=KW, discharge_kw=KW, reserve_kwh=RESERVE, band_kwh=(LO, HI),
        discharge_capable=capable, max_events_per_day=events, day_ids=[i // n_per_day for i in range(n)])
    kwargs.update(over)
    return plan_single_vehicle(buy, sell, **kwargs)


def check_invariants(r, buy, soc0_pct, target_pct, capable=True, events=2, n_per_day=24, tol=1e-6):
    n = len(buy)
    e = r.energy
    assert len(e) == n + 1 and len(r.charge) == n and len(r.discharge) == n
    assert e[0] == pytest.approx(soc0_pct / 100 * CAP)
    floor = min(RESERVE, e[0])
    assert e.min() >= floor - 1e-5 and e.max() <= CAP + 1e-5                   # 예비 잔량 이상, 만충 이하
    assert e[-1] >= min(target_pct / 100 * CAP, CAP) - 1e-5                    # 출발 시 목표 잔량
    assert (r.charge >= -tol).all() and (r.discharge >= -tol).all()
    assert r.charge.max() <= KW + 1e-6 and r.discharge.max() <= KW + 1e-6      # 출력 한도
    assert not ((r.charge > 1e-6) & (r.discharge > 1e-6)).any()                # 충전·방전 동시 금지
    for t in range(n):                                                          # 에너지 수지
        assert e[t + 1] == pytest.approx(e[t] + ETA_C * r.charge[t] - r.discharge[t] / ETA_D, abs=1e-5)
        if r.discharge[t] > 1e-6:
            assert LO - 1e-5 <= e[t] <= HI + 1e-5                               # 방전은 35~80% 구간에서만
    if not capable:
        assert r.discharge.sum() < 1e-9
    starts = [t for t in range(n) if r.discharge[t] > 1e-6 and (t == 0 or r.discharge[t - 1] <= 1e-6)]
    for day in {t // n_per_day for t in starts}:
        assert sum(1 for t in starts if t // n_per_day == day) <= events         # 하루 방전 시작 횟수


# ------------------------------------------------------- 독립 계산과의 대조 --

def greedy_v1g_cost(buy, soc0_pct, target_pct):
    """방전이 없으면 가장 싼 시간부터 충전기 출력만큼 채우는 것이 최적이다(독립 구현)."""
    need_battery = max((target_pct - soc0_pct) / 100 * CAP, 0.0)
    grid_needed = need_battery / ETA_C
    cost = 0.0
    for price in sorted(buy):
        put = min(KW, grid_needed)
        cost += put * price
        grid_needed -= put
        if grid_needed <= 1e-12:
            break
    return cost


@pytest.mark.parametrize("seed", range(8))
def test_v1g_cost_equals_the_greedy_cheapest_hours_cost(seed):
    rng = np.random.default_rng(seed)
    buy = rng.uniform(80, 300, size=int(rng.integers(8, 40)))
    soc0, target = float(rng.uniform(20, 70)), float(rng.uniform(60, 95))
    need = max((target - soc0) / 100 * CAP / ETA_C, 0)
    if need > len(buy) * KW:                       # 시간이 모자라는 조건은 건너뛴다
        pytest.skip("infeasible by construction")
    r = solve(buy, np.zeros_like(buy), soc0, target, capable=False)
    assert float((r.charge * buy).sum()) == pytest.approx(greedy_v1g_cost(buy, soc0, target), abs=0.05)
    check_invariants(r, buy, soc0, target, capable=False)


def test_discharge_flips_exactly_at_the_theoretical_breakeven():
    """목표 잔량 = 현재 잔량이면 방전한 만큼 다시 충전해야 하므로, 손익분기 = b/(ηcηd) + 열화/ηd 에서 판단이 갈린다."""
    b = 100.0
    eco = EconomicsParams(charge_efficiency=ETA_C, discharge_efficiency=ETA_D, degradation_krw_per_kwh=DEG)
    s_star = breakeven_sell_price(b, eco)
    assert s_star == pytest.approx(b / (ETA_C * ETA_D) + DEG / ETA_D)             # 산식 자체
    buy = np.full(24, b)
    below = solve(buy, np.full(24, s_star * 0.98), 60.0, 60.0)
    above = solve(buy, np.full(24, s_star * 1.05), 60.0, 60.0)
    assert below.discharge.sum() < 1e-6                                             # 손익분기 미만이면 방전하지 않는다
    assert above.discharge.sum() > 1.0                                              # 초과하면 방전한다
    check_invariants(below, buy, 60.0, 60.0)
    check_invariants(above, buy, 60.0, 60.0)


def test_optimized_cost_never_exceeds_the_charge_only_plan():
    rng = np.random.default_rng(3)
    for _ in range(20):
        n = 24
        buy = rng.uniform(80, 250, n)
        sell = rng.uniform(0, 400, n)
        base = solve(buy, sell, 50.0, 80.0, capable=False)
        v2g = solve(buy, sell, 50.0, 80.0, capable=True)
        c_base = float((base.charge * buy).sum())
        c_v2g = float((v2g.charge * buy).sum() - (v2g.discharge * sell).sum()
                      + v2g.discharge.sum() / ETA_D * DEG)
        assert c_v2g <= c_base + 1e-3                                              # 방전을 쓸 수 있으면 손해를 보지 않는다


def test_no_arbitrage_when_compensation_is_zero_even_if_capable():
    buy = np.random.default_rng(1).uniform(80, 250, 24)
    r = solve(buy, np.zeros(24), 60.0, 70.0, capable=True)
    assert r.discharge.sum() < 1e-6


# ----------------------------------------------------------------- 제약 경계 --

def test_soc_never_exceeds_100_percent_regression():
    """이전 구현은 현재 80% → 목표 100%에서 중간 잔량이 101.4%까지 올라갔다."""
    buy = np.where(np.arange(13) < 6, 100.0, 180.0)        # 앞쪽이 싸서 일찍 많이 충전하고 싶어지는 조건
    r = solve(buy, np.full(13, 300.0), 80.0, 100.0)
    check_invariants(r, buy, 80.0, 100.0)
    assert r.energy.max() <= CAP + 1e-5


def test_discharge_only_inside_the_band():
    buy = np.full(24, 100.0)
    for soc0 in (30.0, 90.0):                               # 방전 허용 범위(35~80%) 밖에서 시작
        r = solve(buy, np.full(24, 400.0), soc0, soc0)
        # 밖에서 시작하면 잔량이 범위 안으로 들어오지 않는 한 방전하지 않는다
        check_invariants(r, buy, soc0, soc0)


def test_events_per_day_limit_is_enforced_across_days():
    buy = np.tile(np.r_[np.full(6, 100.0), np.full(18, 300.0)], 2)
    sell = np.tile(np.r_[np.zeros(6), np.full(18, 500.0) * (np.arange(18) % 2 == 0) + 0.0], 2)   # 격시간 고가 → 여러 번 방전 유인
    r = solve(buy, sell, 70.0, 70.0, events=1)
    check_invariants(r, buy, 70.0, 70.0, events=1)
    r2 = solve(buy, sell, 70.0, 70.0, events=3)
    check_invariants(r2, buy, 70.0, 70.0, events=3)
    assert r2.discharge.sum() >= r.discharge.sum() - 1e-6   # 횟수 상한을 풀면 방전이 줄지 않는다


def test_reserve_is_respected_when_target_is_below_current():
    buy = np.full(12, 100.0)
    r = solve(buy, np.full(12, 600.0), 60.0, 20.0, events=4)     # 목표가 예비 잔량이어도 20% 밑으로 내려가지 않는다
    assert r.energy.min() >= RESERVE - 1e-5


def test_starting_below_reserve_never_goes_lower():
    buy = np.full(10, 100.0)
    r = solve(buy, np.full(10, 600.0), 10.0, 30.0)
    assert r.energy.min() >= 10.0 / 100 * CAP - 1e-5          # 시작 잔량(10%) 아래로는 내려가지 않는다
    # 충전해서 방전 허용 범위(35~80%)에 들어온 뒤에는 방전할 수 있다 — 그때도 모든 제약이 지켜져야 한다
    check_invariants(r, buy, 10.0, 30.0)


def test_infeasible_when_not_enough_time():
    with pytest.raises(PlannerError):
        solve(np.full(2, 100.0), np.zeros(2), 20.0, 100.0)         # 2시간×7kW로 64kWh 불가


def test_input_validation():
    with pytest.raises(PlannerError):
        solve(np.array([]), np.array([]))
    with pytest.raises(PlannerError):
        solve(np.full(4, 100.0), np.full(3, 0.0))
    with pytest.raises(PlannerError):
        solve(np.full(4, 100.0), np.zeros(4), eta_c=1.2)
    with pytest.raises(PlannerError):
        solve(np.full(4, 100.0), np.zeros(4), soc0_kwh=CAP + 1)


# -------------------------------------------------------------- 무작위 조건 --

@pytest.mark.parametrize("seed", range(40))
def test_random_scenarios_always_satisfy_every_constraint(seed):
    rng = np.random.default_rng(1000 + seed)
    n = int(rng.integers(4, 49))
    buy = rng.uniform(60, 320, n)
    sell = rng.uniform(0, 450, n) * rng.integers(0, 2)
    soc0, target = float(rng.uniform(5, 98)), float(rng.uniform(20, 100))
    capable = bool(rng.integers(0, 2))
    events = int(rng.integers(1, 4))
    try:
        r = solve(buy, sell, soc0, target, capable=capable, events=events)
    except PlannerError:
        return                                   # 시간·잔량 때문에 불가능한 조건은 오류로 알리는 것이 정상
    check_invariants(r, buy, soc0, target, capable=capable, events=events)
