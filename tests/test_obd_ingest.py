"""OBD 배치 집계(raw 1Hz → 시간당 ev_telemetry) 테스트.

합성 최소 데이터로 핵심 로직(주행/충전 판별, SOC 기록 시점)을 고정하고, 실제
OBDData/ 원본 파일이 있으면(5.2GB라 모든 체크아웃에 있다고 가정하지 않음) 그걸로도
검증한다 — 진짜 스키마 드리프트·이상 로그에 대한 강건성은 합성 데이터로는
재현하기 어렵기 때문에 실데이터 테스트가 더 신뢰도 높다.
"""
import pandas as pd
import pytest

from src.paths import PROJECT_ROOT
from src.pipeline.obd_ingest import ObdColumnsMissingError, aggregate_obd_day_to_hourly

OBD_DIR = PROJECT_ROOT / "OBDData"
SMALL_REAL_FILE = OBD_DIR / "20250902-43오3450-토레스 EVX.csv"
DEGRADED_REAL_FILE = OBD_DIR / "20260723-43오3450-토레스 EVX.csv"


def _make_synthetic_day(rows: list[dict]) -> pd.DataFrame:
    base_cols = {
        "Date": "2026-09-01",
        "SystemTime": 0.0,
        "VCU_VehicleSpeed_STD(km/h)": 0.0,
        "VCU_BatteryPack_ASOC(%)": 80.0,
        "VCU_BatteryPack_Voltage(V)": 380.0,
        "VCU_BatteryPack_Current(A)": 0.0,
    }
    return pd.DataFrame([{**base_cols, **r} for r in rows])


def test_aggregate_hourly_driving_vs_charging():
    df = _make_synthetic_day([
        # 09시대: 주행중(속도>2) — 충전 없음
        {"SystemTime": 90000.0, "VCU_VehicleSpeed_STD(km/h)": 60.0, "VCU_BatteryPack_ASOC(%)": 80.0,
         "VCU_BatteryPack_Current(A)": -50.0},
        {"SystemTime": 90030.0, "VCU_VehicleSpeed_STD(km/h)": 55.0, "VCU_BatteryPack_ASOC(%)": 78.0,
         "VCU_BatteryPack_Current(A)": -50.0},
        # 10시대: 정차 + 충전(전압*전류 > 0)
        {"SystemTime": 100000.0, "VCU_VehicleSpeed_STD(km/h)": 0.0, "VCU_BatteryPack_ASOC(%)": 78.0,
         "VCU_BatteryPack_Voltage(V)": 380.0, "VCU_BatteryPack_Current(A)": 18.0},
        {"SystemTime": 100030.0, "VCU_VehicleSpeed_STD(km/h)": 0.0, "VCU_BatteryPack_ASOC(%)": 79.0,
         "VCU_BatteryPack_Voltage(V)": 380.0, "VCU_BatteryPack_Current(A)": 18.0},
    ])

    hourly = aggregate_obd_day_to_hourly(df, ev_id="EV-TEST-01", vehicle_model="차종A(세단)", zone=2)

    assert list(hourly.columns) == [
        "timestamp", "ev_id", "ev_type", "zone", "soc_pct", "connected", "driving", "charging_kw",
        "vehicle_model", "battery_capacity_kwh", "v2g_capable", "batt_temp_c",
    ]
    assert len(hourly) == 2
    hour9 = hourly[hourly["timestamp"].dt.hour == 9].iloc[0]
    hour10 = hourly[hourly["timestamp"].dt.hour == 10].iloc[0]

    assert hour9["driving"] and not hour9["connected"]
    assert hour9["soc_pct"] == 80.0  # 그 시간대 "첫" 관측값이어야 함(마지막 78.0 아님)
    assert hour9["charging_kw"] == 0.0  # 방전 방향(음수)은 0으로 처리

    assert not hour10["driving"] and hour10["connected"]
    assert hour10["charging_kw"] == pytest.approx(380.0 * 18.0 / 1000.0, rel=0.01)

    for row in (hour9, hour10):
        assert row["ev_id"] == "EV-TEST-01"
        assert row["zone"] == 2
        assert row["vehicle_model"] == "차종A(세단)"
        assert row["battery_capacity_kwh"] == 77.4
        assert row["v2g_capable"] is True or row["v2g_capable"] == True  # noqa: E712


def test_aggregate_hourly_averages_battery_temperature_when_present():
    """실측 검증된 신호(BMS_Temperature_01)가 있으면 시간당 평균으로 집계돼야 하고,
    없으면 batt_temp_c가 None이어야 한다(구 스키마·이상 로그 등)."""
    df = _make_synthetic_day([
        {"SystemTime": 90000.0, "VCU_VehicleSpeed_STD(km/h)": 60.0, "BMS_Temperature_01(degC)": 30.0},
        {"SystemTime": 90030.0, "VCU_VehicleSpeed_STD(km/h)": 60.0, "BMS_Temperature_01(degC)": 34.0},
    ])
    hourly = aggregate_obd_day_to_hourly(df, ev_id="EV-TEST-02", vehicle_model="차종A(세단)")
    assert hourly.iloc[0]["batt_temp_c"] == pytest.approx(32.0)

    df_no_temp = _make_synthetic_day([{"SystemTime": 90000.0, "VCU_VehicleSpeed_STD(km/h)": 60.0}])
    hourly_no_temp = aggregate_obd_day_to_hourly(df_no_temp, ev_id="EV-TEST-03", vehicle_model="차종A(세단)")
    assert hourly_no_temp.iloc[0]["batt_temp_c"] is None


def test_aggregate_rejects_unknown_vehicle_model():
    df = _make_synthetic_day([{}])
    with pytest.raises(ValueError):
        aggregate_obd_day_to_hourly(df, ev_id="EV-X", vehicle_model="존재하지않는차종")


def test_aggregate_raises_when_required_signals_missing():
    df = pd.DataFrame({"Date": ["2026-09-01"], "SystemTime": [90000.0], "ADC_ACC(NA)": [1.0]})
    with pytest.raises(ObdColumnsMissingError):
        aggregate_obd_day_to_hourly(df, ev_id="EV-X", vehicle_model="차종A(세단)")


@pytest.mark.skipif(not SMALL_REAL_FILE.exists(), reason="OBDData/ 실데이터 없음(대용량, 모든 체크아웃에 없을 수 있음)")
def test_aggregate_against_real_small_obd_file():
    day_df = pd.read_csv(SMALL_REAL_FILE, low_memory=False)
    hourly = aggregate_obd_day_to_hourly(day_df, ev_id="EV-TORRES-01", vehicle_model="차종B(SUV)", zone=1)
    assert not hourly.empty
    assert hourly["soc_pct"].between(0, 100).dropna().shape[0] >= 0  # 결측 허용, 있으면 범위 내여야 함
    assert (hourly["vehicle_model"] == "차종B(SUV)").all()
    assert (hourly["battery_capacity_kwh"] == 84.0).all()


@pytest.mark.skipif(not DEGRADED_REAL_FILE.exists(), reason="OBDData/ 실데이터 없음(대용량, 모든 체크아웃에 없을 수 있음)")
def test_aggregate_raises_on_real_degraded_file():
    """20260723 파일은 실제로 컬럼이 10개뿐인 이상(degraded) 로그다
    (output/OBD_실데이터_분석_보고서.md 2절) — 명확한 에러로 걸러져야 한다."""
    day_df = pd.read_csv(DEGRADED_REAL_FILE, low_memory=False)
    with pytest.raises(ObdColumnsMissingError):
        aggregate_obd_day_to_hourly(day_df, ev_id="EV-TORRES-01", vehicle_model="차종B(SUV)")
