"""OBD 에뮬레이터(가상 실증차량 배치 로그 생성기) 테스트 — 생성한 데이터가 실제
집계 파이프라인(`aggregate_obd_day_to_hourly`)을 그대로 통과하는지 확인한다."""
import numpy as np
import pandas as pd
import pytest

from src.datagen.ev import VEHICLE_MODELS
from src.datagen.obd_emulator import DEGRADED_DAY_PROB, generate_synthetic_obd_day
from src.pipeline.obd_ingest import ObdColumnsMissingError, aggregate_obd_day_to_hourly


def test_generate_synthetic_obd_day_round_trips_through_aggregation():
    rng = np.random.default_rng(1)
    day_df = generate_synthetic_obd_day("차종A(세단)", pd.Timestamp("2026-09-01"), rng, inject_messiness=False)
    hourly = aggregate_obd_day_to_hourly(day_df, ev_id="EV-EMU-001", vehicle_model="차종A(세단)", zone=2)

    assert len(hourly) == 24
    assert (hourly["vehicle_model"] == "차종A(세단)").all()
    assert (hourly["battery_capacity_kwh"] == VEHICLE_MODELS["차종A(세단)"]["battery_capacity_kwh"]).all()
    assert hourly["soc_pct"].between(0, 100).all()
    assert (hourly["charging_kw"] >= 0).all()


def test_generate_synthetic_obd_day_rejects_unknown_model():
    rng = np.random.default_rng(1)
    with pytest.raises(ValueError):
        generate_synthetic_obd_day("존재하지않는차종", pd.Timestamp("2026-09-01"), rng)


def test_generate_synthetic_obd_day_can_produce_degraded_log():
    """DEGRADED_DAY_PROB 확률로 이상(degraded) 로그가 나오고, 그건
    aggregate_obd_day_to_hourly가 명확히 거부해야 한다(실측과 동일한 강건성 검증)."""
    found_degraded = False
    for seed in range(200):
        rng = np.random.default_rng(seed)
        day_df = generate_synthetic_obd_day("차종B(SUV)", pd.Timestamp("2026-09-01"), rng, inject_messiness=True)
        if "VCU_VehicleSpeed_STD(km/h)" not in day_df.columns:
            found_degraded = True
            with pytest.raises(ObdColumnsMissingError):
                aggregate_obd_day_to_hourly(day_df, ev_id="EV-EMU-002", vehicle_model="차종B(SUV)")
            break
    assert found_degraded, f"200번 시도 중 이상 로그가 한 번도 안 나옴(기대 확률 {DEGRADED_DAY_PROB})"


def test_generate_synthetic_obd_day_schema_drift_columns_are_optional():
    """GPS/HVAC 블록은 있어도 없어도 집계가 정상 동작해야 한다(스키마 드리프트 대응)."""
    for seed in range(20):
        rng = np.random.default_rng(seed)
        day_df = generate_synthetic_obd_day("차종C(준중형)", pd.Timestamp("2026-09-01"), rng, inject_messiness=True)
        if "VCU_VehicleSpeed_STD(km/h)" not in day_df.columns:
            continue  # 이상 로그는 다른 테스트에서 다룸
        hourly = aggregate_obd_day_to_hourly(day_df, ev_id="EV-EMU-003", vehicle_model="차종C(준중형)")
        assert len(hourly) == 24
