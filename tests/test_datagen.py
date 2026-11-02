import numpy as np
import pytest

from src.datagen.config import SimConfig
from src.datagen.ev import VEHICLE_MODELS, generate_ev_telemetry
from src.datagen.charger import generate_charger_sessions
from src.datagen.grid import generate_grid_signal


def test_ev_telemetry_row_count(small_cfg, rng):
    df = generate_ev_telemetry(small_cfg, rng)
    assert len(df) == small_cfg.n_ev * small_cfg.n_days * 24


def test_ev_soc_within_bounds(small_cfg, rng):
    df = generate_ev_telemetry(small_cfg, rng)
    assert df["soc_pct"].between(0, 100).all()


def test_ev_connected_and_driving_are_exclusive(small_cfg, rng):
    df = generate_ev_telemetry(small_cfg, rng)
    assert not (df["connected"] & df["driving"]).any()


def test_soc_recorded_before_action_not_after(small_cfg, rng):
    """회귀테스트: SOC는 '그 시간대 행동 이전' 값이어야 한다.

    과거 버그: 충전 이후 값을 기록해, 세션 시작 시점 SOC가 이미 목표치 근방으로
    부풀려져 있었다(최적화 여지가 거의 사라짐). 주행 직후 첫 연결 시간의 SOC는
    직전 시간의 SOC보다 낮아야(또는 같아야) 하고, 목표 SOC(0.95)에 바짝 붙어있으면
    안 된다.
    """
    df = generate_ev_telemetry(small_cfg, rng).sort_values(["ev_id", "timestamp"]).reset_index(drop=True)
    ev0 = df[df["ev_id"] == df["ev_id"].iloc[0]].reset_index(drop=True)

    driving_end_idx = ev0.index[ev0["driving"] & ~ev0["driving"].shift(-1, fill_value=False)]
    assert len(driving_end_idx) > 0, "테스트 데이터에 주행 구간이 없음 — 시드/설정 확인 필요"

    idx = driving_end_idx[0]
    first_reconnect_soc = ev0.loc[idx + 1, "soc_pct"]
    assert first_reconnect_soc < 90.0, (
        f"주행 직후 재접속 시점 SOC={first_reconnect_soc} — 이미 충전이 반영된 값으로 보임 "
        "(soc_hourly 기록 시점이 충전 이후로 되돌아갔을 가능성)"
    )


def test_ev_battery_temperature_within_plausible_range(small_cfg, rng):
    """실측(OBDData) 검증 결과 배터리 온도는 신뢰 가능한 신호였다 — 합성 데이터도
    이를 반영해 batt_temp_c 컬럼을 내야 하고, 물리적으로 타당한 범위여야 한다."""
    df = generate_ev_telemetry(small_cfg, rng)
    assert "batt_temp_c" in df.columns
    assert df["batt_temp_c"].between(-10, 65).all()


def test_ev_vehicle_model_columns_match_spec(small_cfg, rng):
    """차종별 battery_capacity_kwh/v2g_capable이 VEHICLE_MODELS 스펙과 항상 일치해야 한다."""
    df = generate_ev_telemetry(small_cfg, rng)
    assert set(df["vehicle_model"].unique()) <= set(VEHICLE_MODELS.keys())
    for model, spec in VEHICLE_MODELS.items():
        rows = df[df["vehicle_model"] == model]
        if rows.empty:
            continue
        assert (rows["battery_capacity_kwh"] == spec["battery_capacity_kwh"]).all()
        assert (rows["v2g_capable"] == spec["v2g_capable"]).all()


def test_ev_vehicle_model_diversity_at_full_scale():
    """PRD 기본 규모(40대)에서는 5개 차종이 섞여 나와야 하고, V2G 지원(2종)·미지원(3종)이
    모두 존재해야 한다 — "40대 중 5종, 그중 일부만 방전 가능" 설계의 회귀테스트."""
    cfg = SimConfig(n_ev=40, weeks=2, n_chargers=10, charger_days=14, n_zones=2, random_seed=7)
    rng = np.random.default_rng(7)
    df = generate_ev_telemetry(cfg, rng)

    models_seen = set(df.groupby("ev_id")["vehicle_model"].first())
    assert len(models_seen) >= 4, f"40대 규모에서 차종이 너무 적게 나옴: {models_seen}"

    v2g_flags = df.groupby("ev_id")["v2g_capable"].first()
    assert v2g_flags.any(), "V2G 지원 차량이 하나도 없음"
    assert (~v2g_flags).any(), "V2G 미지원 차량이 하나도 없음 — 전부 방전 가능하면 안 됨"


def test_charger_sessions_rate_matches_kpi_formula(small_cfg, rng):
    df = generate_charger_sessions(small_cfg, rng)
    expected = small_cfg.n_chargers * small_cfg.charger_days * small_cfg.charger_avg_sessions_per_day
    assert len(df) == pytest.approx(expected, rel=0.35)


def test_charger_sessions_without_ev_df_have_no_ev_id(small_cfg, rng):
    df = generate_charger_sessions(small_cfg, rng)
    assert "ev_id" in df.columns
    assert df["ev_id"].isna().all()


def test_charger_sessions_with_ev_df_link_some_sessions_and_keep_rate(small_cfg):
    """ev_df를 넘기면 일부 세션에 ev_id가 배정돼야 하고(차량-충전기 데이터 연결),
    세션 발생 건수(PRD 산출근거)는 매칭 여부와 무관하게 그대로 유지돼야 한다.

    ev 생성과 charger 생성에 서로 다른 rng를 써서, ev_telemetry 생성 로직이
    소비하는 난수 개수가 바뀌어도(예: 필드 추가) 이 세션 건수 검증이 흔들리지
    않게 한다 — 공유 rng를 순차로 쓰면 업스트림 난수 소비량 변화가 다운스트림
    Poisson 표본을 바꿔 이 테스트를 깨뜨릴 수 있다(실제로 한 번 그랬음)."""
    from src.datagen.ev import generate_ev_telemetry

    ev_df = generate_ev_telemetry(small_cfg, np.random.default_rng(1))
    df = generate_charger_sessions(small_cfg, np.random.default_rng(2), ev_df=ev_df)

    expected = small_cfg.n_chargers * small_cfg.charger_days * small_cfg.charger_avg_sessions_per_day
    assert len(df) == pytest.approx(expected, rel=0.35)
    assert df["ev_id"].notna().any(), "ev_id가 매칭된 세션이 하나도 없음"
    assert set(df.loc[df["ev_id"].notna(), "ev_id"]) <= set(ev_df["ev_id"].unique())


def test_grid_signal_no_negative_values(small_cfg, rng):
    df = generate_grid_signal(small_cfg, rng)
    assert (df["power_demand_mw"] >= 0).all()
    assert (df["renewable_gen_mw"] >= 0).all()
    assert (df["price_krw_per_kwh"] > 0).all()
