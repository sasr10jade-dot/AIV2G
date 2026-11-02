import numpy as np
import pandas as pd
import pytest

from src.pipeline.quality import validate, QualityReport
from src.pipeline.schema import EV_TELEMETRY_SCHEMA
from src.pipeline.store import (
    build_ev_weekly_report,
    build_city_hourly,
    build_charger_utilization_hourly,
    build_renewable_summary,
    build_vehicle_status_table,
)
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal


def test_quality_validate_catches_out_of_range():
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-09-01", periods=3, freq="h"),
            "ev_id": ["EV-001"] * 3,
            "ev_type": ["승용형(출퇴근)"] * 3,
            "zone": [0, 0, 0],
            "soc_pct": [50.0, 150.0, -5.0],  # 2건 범위 위반
            "connected": [True, True, True],
            "driving": [False, False, False],
            "charging_kw": [1.0, 1.0, 1.0],
        }
    )
    report = validate(df, "ev_telemetry", EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"])
    assert not report.passed
    assert report.out_of_range.get("soc_pct") == 2


def test_quality_validate_passes_clean_data(small_cfg, rng):
    df = generate_ev_telemetry(small_cfg, rng)
    report = validate(df, "ev_telemetry", EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"])
    assert report.passed


def test_weekly_report_row_count_matches_kpi_formula(small_cfg, rng):
    ev_df = generate_ev_telemetry(small_cfg, rng)
    weekly = build_ev_weekly_report(ev_df, small_cfg)
    assert len(weekly) == small_cfg.n_ev * small_cfg.weeks


def test_weekly_report_includes_real_data_outside_cfg_start_date(small_cfg):
    """회귀테스트: 실증 업로드 데이터의 날짜가 cfg.start_date(합성 시나리오 기준일)와
    무관하면(예: 2025년 실측 OBD를 2026년 기본 시나리오에 업로드) 예전엔 주간리포트가
    통째로 비어버렸다 — build_ev_weekly_report가 cfg.start_date가 아니라 데이터 자체의
    최소 timestamp를 기준으로 주(week)를 계산해야 한다."""
    real_ev_df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=5, freq="h"),  # small_cfg.start_date와 무관한 과거 날짜
        "ev_id": ["EV-REAL-01"] * 5,
        "ev_type": ["실증차량"] * 5,
        "zone": [0] * 5,
        "soc_pct": [80.0, 79.0, 78.0, 77.0, 76.0],
        "connected": [False] * 5,
        "driving": [True] * 5,
        "charging_kw": [0.0] * 5,
    })
    weekly = build_ev_weekly_report(real_ev_df, small_cfg)
    assert len(weekly) > 0, "cfg.start_date와 무관한 실측 데이터가 주간리포트에서 전부 누락됨"
    assert weekly.iloc[0]["ev_id"] == "EV-REAL-01"


def test_charger_utilization_full_overlap_gives_100_percent():
    """충전기 2대짜리 구역에서 두 충전기가 동시에 같은 시간대에 점유되면
    가동률이 100%여야 한다."""
    charger_df = pd.DataFrame({
        "session_id": ["S1", "S2"],
        "charger_id": ["CHG-0001", "CHG-0002"],
        "zone": [0, 0],
        "start_ts": [pd.Timestamp("2026-09-01 10:00"), pd.Timestamp("2026-09-01 10:15")],
        "duration_h": [1.0, 1.0],
        "energy_kwh": [7.0, 7.0],
    })
    util = build_charger_utilization_hourly(charger_df)
    row = util[(util["timestamp"] == pd.Timestamp("2026-09-01 10:00")) & (util["zone"] == 0)].iloc[0]
    assert row["occupied_chargers"] == 2
    assert row["total_chargers"] == 2
    assert row["utilization_pct"] == 100.0


def test_charger_utilization_partial_overlap():
    """충전기 4대짜리 구역에서 1대만 점유되면 25%여야 한다."""
    charger_df = pd.DataFrame({
        "session_id": ["S1"],
        "charger_id": ["CHG-0001"],
        "zone": [1],
        "start_ts": [pd.Timestamp("2026-09-01 14:00")],
        "duration_h": [2.0],
        "energy_kwh": [14.0],
    })
    # 같은 구역에 미점유 충전기 3대가 더 있다는 걸 나타내려면 각 충전기가 최소 1건의
    # 세션 이력을 가져야 한다(현재 구현은 charger_df에 등장한 charger_id로 총 대수를 센다) —
    # 다른 시각에 짧게라도 세션을 하나씩 넣어준다.
    extra = pd.DataFrame({
        "session_id": ["S2", "S3", "S4"],
        "charger_id": ["CHG-0002", "CHG-0003", "CHG-0004"],
        "zone": [1, 1, 1],
        "start_ts": [pd.Timestamp("2026-09-02 03:00")] * 3,
        "duration_h": [0.5, 0.5, 0.5],
        "energy_kwh": [3.5, 3.5, 3.5],
    })
    charger_df = pd.concat([charger_df, extra], ignore_index=True)

    util = build_charger_utilization_hourly(charger_df)
    row = util[(util["timestamp"] == pd.Timestamp("2026-09-01 14:00")) & (util["zone"] == 1)].iloc[0]
    assert row["occupied_chargers"] == 1
    assert row["total_chargers"] == 4
    assert row["utilization_pct"] == 25.0


def test_city_hourly_has_no_missing_values(small_cfg, rng):
    ev_df = generate_ev_telemetry(small_cfg, rng)
    grid_df = generate_grid_signal(small_cfg, rng)
    city_hourly = build_city_hourly(ev_df, grid_df, small_cfg)
    assert not city_hourly.isna().any().any()
    assert len(city_hourly) == small_cfg.n_hours


def test_build_vehicle_status_table_one_row_per_vehicle_at_latest_timestamp(small_cfg, rng):
    """스냅샷은 각 차량의 마지막 관측일 중 "지금(서울 시간)과 같은 시(hour)"을
    골라야 한다 — 무조건 마지막 행(=23시)을 쓰면 배치 데이터 특성상 항상 심야만
    보여 "주행중"이 늘 0으로 보이는 문제가 있었다(실제로 발견됨)."""
    ev_df = generate_ev_telemetry(small_cfg, rng)
    rows = build_vehicle_status_table(ev_df)
    now_hour = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).hour

    assert len(rows) == ev_df["ev_id"].nunique()
    by_id = {r["ev_id"]: r for r in rows}
    for ev_id in ev_df["ev_id"].unique():
        expected_last_day = ev_df[ev_df["ev_id"] == ev_id]["timestamp"].max().normalize()
        picked_ts = pd.Timestamp(by_id[ev_id]["timestamp"])
        assert picked_ts.normalize() == expected_last_day
        assert picked_ts.hour == now_hour
    row = next(iter(by_id.values()))
    assert 0.0 <= row["soc_pct"] <= 100.0
    assert isinstance(row["connected"], bool)
    assert isinstance(row["v2g_capable"], bool)
    assert row["batt_temp_c"] is not None and -10.0 <= row["batt_temp_c"] <= 65.0


def test_build_vehicle_status_table_empty_input_returns_empty_list():
    assert build_vehicle_status_table(pd.DataFrame(columns=["ev_id", "timestamp"])) == []


def test_build_vehicle_status_table_falls_back_when_target_hour_missing():
    """실측 데이터처럼 그 차량의 마지막 날짜에 "지금 시각"과 같은 시간이 아예
    없으면(결측 등), 예외 없이 그 차량의 마지막 행으로 대체돼야 한다."""
    ev_df = pd.DataFrame([
        {
            "ev_id": "EV-SOLO", "ev_type": "승용형(출퇴근)", "zone": 0,
            "timestamp": pd.Timestamp("2026-01-01 05:00:00"),
            "soc_pct": 55.0, "connected": True, "driving": False, "charging_kw": 3.5,
            "vehicle_model": "차종A(세단)", "battery_capacity_kwh": 77.4, "v2g_capable": True,
        },
    ])
    rows = build_vehicle_status_table(ev_df)
    assert len(rows) == 1
    assert rows[0]["ev_id"] == "EV-SOLO"
    assert pd.Timestamp(rows[0]["timestamp"]) == pd.Timestamp("2026-01-01 05:00:00")


def test_v2g_dischargeable_now_matches_actual_dispatch_gate():
    """`_v2g_dischargeable_now`가 `decision.optimize_fleet`이 실제 방전을 허용하는
    조건(SOC 밴드 35~80%·v2g_capable·연결·비주행)과 같은 기준을 써야 한다 — "달리는
    ESS" 표시가 실제로 디스패치되지 않을 차량까지 방전 가능하다고 보여주면 안 된다."""
    base = {
        "ev_id": "EV-X", "vehicle_model": "차종A(세단)", "zone": 0,
        "timestamp": pd.Timestamp("2026-01-01"), "connected": True, "driving": False,
        "charging_kw": 0.0, "battery_capacity_kwh": 80.0, "v2g_capable": True, "batt_temp_c": None,
    }

    def _row(**overrides):
        return build_vehicle_status_table(pd.DataFrame([{**base, **overrides, "soc_pct": overrides.get("soc_pct", 60.0)}]))[0]

    in_band = _row(soc_pct=60.0)
    assert in_band["v2g_dischargeable_kwh_now"] == pytest.approx((60.0 - 35.0) / 100 * 80.0)

    below_band = _row(soc_pct=25.0)
    assert below_band["v2g_dischargeable_kwh_now"] == 0.0

    above_band = _row(soc_pct=90.0)
    assert above_band["v2g_dischargeable_kwh_now"] == 0.0

    not_v2g = _row(soc_pct=60.0, v2g_capable=False)
    assert not_v2g["v2g_dischargeable_kwh_now"] == 0.0

    driving_now = _row(soc_pct=60.0, driving=True)
    assert driving_now["v2g_dischargeable_kwh_now"] == 0.0

    disconnected = _row(soc_pct=60.0, connected=False)
    assert disconnected["v2g_dischargeable_kwh_now"] == 0.0


def test_build_renewable_summary_windows_to_recent_days_and_computes_kpis(small_cfg, rng):
    grid_df = generate_grid_signal(small_cfg, rng)
    result = build_renewable_summary(grid_df, window_days=5)
    summary = result["hourly"]

    # cutoff이 >= 비교라 max-5일 시각 자체도 포함돼 5*24+1개가 된다 — 다른 차트의
    # "최근 5일" 절단(comparison["timestamp"] >= cutoff)과 동일한 관례.
    expected_hours = 5 * 24 + 1
    assert len(summary["timestamps"]) == min(expected_hours, len(grid_df))
    assert len(summary["power_demand_mw"]) == len(summary["timestamps"])
    assert len(summary["renewable_gen_mw"]) == len(summary["timestamps"])
    assert summary["avg_demand_mw"] > 0
    assert summary["avg_renewable_mw"] >= 0
    assert 0 <= summary["renewable_share_pct"] <= 100
    assert summary["peak_renewable_mw"] >= summary["avg_renewable_mw"]
    assert summary["peak_renewable_at"] is not None


def test_build_renewable_summary_handles_window_longer_than_data(small_cfg, rng):
    """small_cfg는 2주(14일)치 데이터뿐이라 window_days=5로 잘라도 정상 동작해야
    하고, window_days가 전체 기간보다 길어도(예: 30일) 있는 만큼만 반환해야 한다."""
    grid_df = generate_grid_signal(small_cfg, rng)
    summary = build_renewable_summary(grid_df, window_days=30)["hourly"]
    assert len(summary["timestamps"]) == len(grid_df)


def test_build_renewable_summary_daily_and_weekly_views_are_aggregated():
    """일별(시간대별) 뷰는 시간 단위 그대로지만, 주별/월별 뷰는 각각 일/주 단위로
    합산돼야 한다 — 일반충전 vs AI-V2G 차트의 _bucket_poc와 동일한 조회조건 관례."""
    cfg = SimConfig(n_ev=2, weeks=10, n_chargers=2, charger_days=70, n_zones=1, random_seed=1)
    grid_df = generate_grid_signal(cfg, np.random.default_rng(1))
    result = build_renewable_summary(grid_df, window_days=5, daily_window_days=56)

    assert set(result.keys()) == {"hourly", "daily", "weekly"}
    assert len(result["daily"]["timestamps"]) <= 56
    assert len(result["daily"]["timestamps"]) < len(result["hourly"]["timestamps"]) or len(result["daily"]["timestamps"]) <= 10 * 7
    assert len(result["weekly"]["timestamps"]) <= 10 + 1  # 10주치 데이터라 주 합계는 11개 이하
