"""optimizer 회귀테스트 — 개발 중 실제로 발생했던 두 가지 버그를 고정한다."""
import pandas as pd
import pytest

from src.optimizer.decision import optimize_fleet
from src.optimizer.poc_compare import (
    _extract_sessions,
    _single_vehicle_arbitrage_schedule,
    compute_dr_event_performance,
    compute_fleet_revenue_comparison,
    plan_next_session,
    run_v2g_optimization,
    select_session_for_explanation,
    summarize_session_plan,
)
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.model.plugin_pattern import UnknownVehicleError
import numpy as np


def _hours(start: str, n: int) -> list[pd.Timestamp]:
    return list(pd.date_range(start, periods=n, freq="h"))


def test_no_avalanche_synchronization():
    """회귀테스트: 여러 EV가 동일 최저가 시간에 몰려 새 피크를 만들지 않아야 한다.

    5대가 각각 6시간짜리 세션을 갖고, 그중 1개 시간대(idx=2)가 나머지보다 훨씬
    저렴하다면, '가격만' 보는 독립 최적화는 5대 모두 그 한 시간에 몰아 충전하여
    (5 * max_charge_kw)에 달하는 새로운 피크를 만든다. valley-filling 최적화는
    이를 여러 시간대로 분산시켜야 한다.
    """
    times = _hours("2026-09-01 18:00", 6)
    max_charge_kw = 11.0
    n_ev = 5

    sessions = {}
    for i in range(n_ev):
        soc_baseline = {t: 60.0 for t in times}  # 세션 내내 정적 baseline(사용되는건 시작/끝만)
        sessions[f"S{i}"] = {"times": times, "soc_baseline_pct": soc_baseline, "capacity_kwh": 64.0}

    price_by_time = {t: 200.0 for t in times}
    price_by_time[times[2]] = 10.0  # 한 시간만 압도적으로 저렴

    schedule = optimize_fleet(
        sessions, max_charge_kw=max_charge_kw, max_discharge_kw=7.0,
        reserve_soc=0.20, target_soc=0.80, price_by_time=price_by_time,
    )

    naive_avalanche_peak = n_ev * max_charge_kw  # 모두 한 시간에 몰렸을 때의 피크
    hourly_totals = {
        t: sum(schedule[sid].get(t, (0.0, 0.0))[0] - schedule[sid].get(t, (0.0, 0.0))[1] for sid in schedule)
        for t in times
    }
    actual_peak = max(hourly_totals.values())

    assert actual_peak < naive_avalanche_peak * 0.9, (
        f"peak={actual_peak:.1f}kW — 최저가 시간대로 세션이 동기화되어 몰린 것으로 보임 "
        f"(naive avalanche 기준 {naive_avalanche_peak:.1f}kW)"
    )


def test_session_not_truncated_at_midnight():
    """회귀테스트: 자정을 가로지르는 연결구간이 하나의 세션으로 유지돼야 한다.

    과거 버그: 하루(0~23시) 단위로 슬라이싱해 저녁~다음날 새벽 세션이 둘로
    잘렸다. 여기서는 18시~다음날 04시(10시간) 연속 연결 데이터를 만들어
    하나의 세션으로 추출되는지 검증한다.
    """
    ts = pd.date_range("2026-09-01 12:00", periods=24, freq="h")
    connected = [False] * len(ts)
    # 18시(인덱스6)부터 다음날 04시(인덱스16)까지 연결 상태로 설정
    for i in range(6, 17):
        connected[i] = True

    df = pd.DataFrame(
        {
            "timestamp": ts,
            "ev_id": "EV-TEST",
            "ev_type": "승용형(출퇴근)",
            "connected": connected,
            "soc_pct": [70.0] * len(ts),
            "charging_kw": [0.0] * len(ts),
        }
    )

    cfg = SimConfig()
    sessions, _ = _extract_sessions(df, cfg, ts[0], ts[-1])

    lengths = [len(s["times"]) for s in sessions.values()]
    assert 11 in lengths, f"연속 11시간 세션이 하나로 추출돼야 하는데 세션 길이들={lengths}"


def test_baseline_and_optimized_energy_conserved():
    """회귀테스트: 동일 세션 기준 baseline·optimized 총 충전에너지가 크게 달라지면 안 된다."""
    times = _hours("2026-09-01 20:00", 10)
    soc_baseline = {t: 65.0 for t in times}
    soc_baseline[times[-1]] = 92.0  # baseline이 세션 종료 시 도달한 SOC

    sessions = {"S0": {"times": times, "soc_baseline_pct": soc_baseline, "capacity_kwh": 64.0}}
    price_by_time = {t: 100.0 for t in times}

    schedule = optimize_fleet(
        sessions, max_charge_kw=11.0, max_discharge_kw=7.0,
        reserve_soc=0.20, target_soc=0.80, price_by_time=price_by_time,
    )
    total_charge = sum(c for c, d in schedule["S0"].values())
    baseline_need_kwh = (92.0 - 65.0) / 100 * 64.0

    assert total_charge == pytest.approx(baseline_need_kwh, rel=0.15)


def test_discharge_blocked_outside_soc_band():
    """SoH 보호: SOC가 안정구간(35~80%) 밖이면 방전을 허용하면 안 된다."""
    times = _hours("2026-09-01 20:00", 4)
    # 시작 SOC 90%(과충전 인접), 이후 안정구간 진입
    soc_baseline = {times[0]: 90.0, times[1]: 60.0, times[2]: 55.0, times[3]: 20.0}
    sessions = {"S0": {"times": times, "soc_baseline_pct": soc_baseline, "capacity_kwh": 64.0}}
    price_by_time = {t: 100.0 for t in times}
    price_by_time[times[0]] = 500.0  # 방전 유인을 극대화해도(가격 높음) 밴드 밖이면 막혀야 함
    price_by_time[times[3]] = 500.0

    schedule = optimize_fleet(
        sessions, max_charge_kw=7.0, max_discharge_kw=7.0,
        reserve_soc=0.0, target_soc=0.5, price_by_time=price_by_time,
    )
    discharge_at = {t: schedule["S0"][t][1] for t in times}
    assert discharge_at[times[0]] == 0.0, "SOC 90%(밴드 밖)에서 방전이 발생하면 안 됨"
    assert discharge_at[times[3]] == 0.0, "SOC 20%(밴드 밖)에서 방전이 발생하면 안 됨"
    # times[1](60%), times[2](55%)는 밴드 안이라 가격 유인이 있으면 방전 가능해야 함(막히지 않음 확인용 상한 체크)
    assert discharge_at[times[1]] <= 7.0 and discharge_at[times[2]] <= 7.0


def test_discharge_blocked_entirely_when_not_v2g_capable():
    """V2G 미지원 차종(v2g_capable=False)은 SOC 밴드 안이어도 방전이 전혀 없어야 한다."""
    times = _hours("2026-09-01 20:00", 3)
    soc_baseline = {t: 60.0 for t in times}  # 전부 안정 밴드 안
    sessions = {
        "S0": {"times": times, "soc_baseline_pct": soc_baseline, "capacity_kwh": 64.0, "v2g_capable": False}
    }
    price_by_time = {t: 500.0 for t in times}  # 방전 유인 극대화

    schedule = optimize_fleet(
        sessions, max_charge_kw=7.0, max_discharge_kw=7.0,
        reserve_soc=0.0, target_soc=0.5, price_by_time=price_by_time,
    )
    assert all(d == 0.0 for _c, d in schedule["S0"].values())


def test_compute_fleet_revenue_comparison_v2g_beats_rule_based_baseline():
    """방전(음수 optimized_kw)이 있으면 AI가 규칙기반(무제어, 항상 충전만)보다
    저렴해야(비용 절감이 양수여야) 한다 — Ai-V2g-Cems-Agent.html 목업의
    "₩12,400 (↑23% vs 규칙기반)" 표시와 같은 지표."""
    times = _hours("2026-09-01 18:00", 3)
    comparison = pd.DataFrame({
        "timestamp": times,
        "baseline_kw": [10.0, 10.0, 10.0],   # 규칙기반: 항상 10kW 충전(방전 없음)
        "optimized_kw": [10.0, 0.0, -5.0],    # AI: 마지막 시간대에 5kW 순방전
    })
    grid_df = pd.DataFrame({"timestamp": times, "price_krw_per_kwh": [100.0, 100.0, 200.0]})

    result = compute_fleet_revenue_comparison(comparison, grid_df)

    assert result["baseline_cost_krw"] == pytest.approx(10 * 100 + 10 * 100 + 10 * 200)  # 4000
    assert result["optimized_cost_krw"] == pytest.approx(10 * 100 + 0 * 100 + (-5) * 200)  # 0
    assert result["savings_krw"] == pytest.approx(4000.0)
    assert result["savings_pct"] == pytest.approx(100.0)


def test_compute_fleet_revenue_comparison_handles_zero_baseline_cost():
    times = _hours("2026-09-01 18:00", 1)
    comparison = pd.DataFrame({"timestamp": times, "baseline_kw": [0.0], "optimized_kw": [0.0]})
    grid_df = pd.DataFrame({"timestamp": times, "price_krw_per_kwh": [100.0]})
    result = compute_fleet_revenue_comparison(comparison, grid_df)
    assert result["savings_pct"] == 0.0


def test_select_session_for_explanation_picks_largest_discharge():
    sessions = {"S1": {}, "S2": {}, "S3": {}}
    schedule = {
        "S1": {0: (5.0, 0.0)},
        "S2": {0: (0.0, 3.0), 1: (0.0, 2.0)},  # 총 방전 5.0 — 가장 큼
        "S3": {0: (0.0, 1.0)},
    }
    session_ev_map = {"S1": "EV-A", "S2": "EV-B", "S3": "EV-A"}

    assert select_session_for_explanation(sessions, schedule, session_ev_map) == "S2"
    # ev_id로 필터하면 그 차량 세션 중에서만 골라야 함(EV-A는 S1과 S3, 방전 큰 쪽은 S3)
    assert select_session_for_explanation(sessions, schedule, session_ev_map, ev_id="EV-A") == "S3"
    assert select_session_for_explanation(sessions, schedule, session_ev_map, ev_id="EV-none") is None


def test_summarize_session_plan_computes_real_revenue_from_schedule():
    times = _hours("2026-09-01 13:00", 4)
    sessions = {
        "S0": {
            "times": times,
            "soc_baseline_pct": {times[0]: 40.0, times[-1]: 90.0},
            "capacity_kwh": 64.0,
        }
    }
    schedule = {
        "S0": {
            times[0]: (7.0, 0.0),   # 13시: 충전 7kW @ 100원
            times[1]: (7.0, 0.0),   # 14시: 충전 7kW @ 100원
            times[2]: (0.0, 5.0),   # 15시: 방전 5kW @ 300원(피크가)
            times[3]: (0.0, 0.0),
        }
    }
    price_by_time = {times[0]: 100.0, times[1]: 100.0, times[2]: 300.0, times[3]: 300.0}
    session_ev_map = {"S0": "EV-007"}

    plan = summarize_session_plan("S0", sessions, schedule, session_ev_map, price_by_time)

    assert plan["ev_id"] == "EV-007"
    assert plan["total_charge_kwh"] == pytest.approx(14.0)
    assert plan["total_discharge_kwh"] == pytest.approx(5.0)
    assert plan["charge_cost_krw"] == pytest.approx(1400.0)      # 14kWh * 100원
    assert plan["discharge_revenue_krw"] == pytest.approx(1500.0)  # 5kWh * 300원
    assert plan["net_krw"] == pytest.approx(100.0)
    assert len(plan["charge_intervals"]) == 2
    assert len(plan["discharge_intervals"]) == 1


def test_run_v2g_optimization_end_to_end_produces_explainable_session():
    """실제 시나리오 파이프라인으로 최적화까지 돌려서, 설명 가능한 세션이 최소 하나는
    나와야 한다 — /copilot/explain-schedule이 의존하는 전체 경로의 통합 테스트."""
    cfg = SimConfig(n_ev=5, weeks=2, n_chargers=5, charger_days=14, n_zones=2)
    rng = np.random.default_rng(cfg.random_seed)
    ev_df = generate_ev_telemetry(cfg, rng)
    grid_df = generate_grid_signal(cfg, rng)

    result = run_v2g_optimization(ev_df, grid_df, cfg, max_discharge_kw=7.0)
    assert result["sessions"]
    assert result["schedule"]

    session_id = select_session_for_explanation(result["sessions"], result["schedule"], result["session_ev_map"])
    assert session_id is not None

    plan = summarize_session_plan(
        session_id, result["sessions"], result["schedule"], result["session_ev_map"], result["price_by_time"]
    )
    assert plan["ev_id"] in result["session_ev_map"].values()
    assert plan["total_discharge_kwh"] >= 0.0


def test_plan_next_session_produces_future_predicted_plan():
    """EV Bot이 '내일 몇 시에 나가면 언제 충전/방전해서 얼마 번다'를 답할 때 쓰는
    예측 기반 다음 세션 계획 — 이력 마지막 시각 이후, 예측 출차 시각까지의 구간을
    가상 세션으로 만들어 LP 최적화한 결과가 나와야 한다."""
    cfg = SimConfig(n_ev=2, weeks=8, n_chargers=2, charger_days=56, n_zones=1)
    rng = np.random.default_rng(cfg.random_seed)
    ev_df = generate_ev_telemetry(cfg, rng)
    grid_df = generate_grid_signal(cfg, rng)
    ev_id = ev_df["ev_id"].iloc[0]

    plan = plan_next_session(ev_df, grid_df, cfg, ev_id)

    assert plan["ev_id"] == ev_id
    assert plan["is_predicted"] is True
    assert plan["n_weeks_history"] >= 2.0
    last_hist_ts = ev_df[ev_df["ev_id"] == ev_id]["timestamp"].max()
    assert pd.Timestamp(plan["session_start"]) > last_hist_ts
    assert pd.Timestamp(plan["session_end"]) > pd.Timestamp(plan["session_start"])
    assert plan["total_charge_kwh"] >= 0.0
    assert plan["total_discharge_kwh"] >= 0.0


def test_single_vehicle_arbitrage_charges_cheap_and_discharges_expensive():
    """회귀테스트: fleet용 optimize_fleet을 세션 하나에 그대로 쓰면 방전이 항상
    0으로 나오는 버그가 있었다(피크=이 세션 하나의 순부하라, 방전을 위한 추가
    충전이 그대로 페널티가 됨) — 전용 단일 차량 차익거래 LP는 싼 시간에 충전,
    비싼 시간에 방전해야 한다."""
    times = _hours("2026-09-01 12:00", 4)
    price_by_time = {times[0]: 50.0, times[1]: 50.0, times[2]: 500.0, times[3]: 500.0}

    sched = _single_vehicle_arbitrage_schedule(
        times, price_by_time,
        needed_kwh=0.0, max_net_kwh=20.0, headroom_kwh=20.0,
        max_charge_kw=7.0, max_discharge_kw=7.0, discharge_allowed=True,
    )

    assert sched[times[0]][0] > 0.0, "싼 시간대에 충전이 있어야 함"
    assert sched[times[2]][1] > 0.0 or sched[times[3]][1] > 0.0, "비싼 시간대에 방전이 있어야 함"


def test_single_vehicle_arbitrage_no_discharge_when_not_allowed():
    times = _hours("2026-09-01 12:00", 2)
    price_by_time = {times[0]: 50.0, times[1]: 500.0}
    sched = _single_vehicle_arbitrage_schedule(
        times, price_by_time,
        needed_kwh=0.0, max_net_kwh=20.0, headroom_kwh=20.0,
        max_charge_kw=7.0, max_discharge_kw=7.0, discharge_allowed=False,
    )
    assert all(d == 0.0 for _c, d in sched.values())


def test_plan_next_session_unknown_vehicle_raises():
    cfg = SimConfig(n_ev=2, weeks=8, n_chargers=2, charger_days=56, n_zones=1)
    rng = np.random.default_rng(cfg.random_seed)
    ev_df = generate_ev_telemetry(cfg, rng)
    grid_df = generate_grid_signal(cfg, rng)

    with pytest.raises(UnknownVehicleError):
        plan_next_session(ev_df, grid_df, cfg, "NO-SUCH-EV")


def _dr_comparison_and_grid():
    ts = pd.date_range("2026-09-01 00:00", periods=6, freq="h")
    comparison = pd.DataFrame({
        "timestamp": ts,
        "baseline_kw": [10, 10, 10, 10, 10, 10],
        "optimized_kw": [10, 10, -5, -5, 10, 10],  # 09:01~03시에 방전
    })
    grid_df = pd.DataFrame({"timestamp": ts, "price_krw_per_kwh": [100, 100, 200, 200, 100, 100]})
    return comparison, grid_df


def test_compute_dr_event_performance_overlapping_event_measures_actual_response():
    comparison, grid_df = _dr_comparison_and_grid()
    events = [{
        "event_id": "DR-1", "severity": "high", "reason": "test",
        "start_ts": "2026-09-01T01:00:00", "end_ts": "2026-09-01T03:00:00",
    }]
    result = compute_dr_event_performance(comparison, grid_df, events)
    assert len(result) == 1
    r = result[0]
    # 창은 [01:00, 03:00) -> 01:00(optimized=10), 02:00(optimized=-5) 두 시간
    assert r["overlapped"] is True
    assert r["baseline_kwh"] == 20.0
    assert r["optimized_kwh"] == 5.0
    assert r["reduced_kwh"] == 15.0
    assert r["cost_savings_krw"] > 0


def test_compute_dr_event_performance_non_overlapping_event_returns_zeros():
    comparison, grid_df = _dr_comparison_and_grid()
    events = [{
        "event_id": "DR-2", "severity": "low", "reason": "",
        "start_ts": "2020-01-01T00:00:00", "end_ts": "2020-01-01T02:00:00",
    }]
    result = compute_dr_event_performance(comparison, grid_df, events)
    assert result[0]["overlapped"] is False
    assert result[0]["baseline_kwh"] == 0.0
    assert result[0]["cost_savings_krw"] == 0.0
