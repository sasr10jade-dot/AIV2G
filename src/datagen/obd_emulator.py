"""가상 실증차량 OBD 배치 로그 생성기.

실물 차량 40대·5개 차종이 다 모이기 전까지 배치 수집 파이프라인
(`src/pipeline/obd_ingest.py`)을 규모감 있게 검증하기 위한 것 — OCPP 에뮬레이터
(`src/ocpp/emulator.py`)를 만든 이유와 같다: 실제 하드웨어 없이 Data 레이어를 실증한다.

`OBDData/`(43오3450·토레스 EVX, 279일치)에서 실제로 확인된 특성을 의도적으로 재현한다:
  - 스키마 드리프트 — 파일마다 HVAC/GPS 블록이 나타났다 사라짐(`src/scripts/analyze_obd_data.py`
    분석에서 확인)
  - `BMS_SOC(%)`/`BMS_HVBattery_*` 스케일 오류 — `output/OBD_실데이터_분석_보고서.md` 2.1절
  - 드문 이상(degraded) 로그 — 컬럼이 통째로 빠지는 CAN 버스 장애 로그(7/279일, 약 2.5%)
이렇게 일부러 지저분하게 만들어야 `aggregate_obd_day_to_hourly`의 강건성이 실데이터
없이도 반복적으로 검증된다 — 깨끗한 데이터만 내면 에뮬레이터의 존재 의미가 없다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.datagen.ev import VEHICLE_MODELS

DEGRADED_DAY_PROB = 0.03  # 실측(279일 중 7일 ≈ 2.5%)과 비슷한 비율
SAMPLE_INTERVAL_SEC = 60  # 1분 간격 — 실제 1Hz보다 성기지만 시간당 집계 검증엔 충분하고 가볍다


def _cell_imbalance_mv(soc: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """실측 기준(`output/OBD_실데이터_분석_보고서.md` 4절 — 관측기간 중 평균 60mV,
    최대 703mV)을 재현하는 비선형 셀 전압 편차. SOC가 20% 미만이거나 80% 초과인
    극단 구간에 진입하면 편차가 급증한다(실제 배터리 팩의 셀 밸런싱 특성과 동일한
    방향) — 중간 SOC 구간에서는 낮게 유지된다."""
    base_mv = rng.uniform(5.0, 30.0, size=soc.shape)
    low_extremity = np.clip((20.0 - soc) / 20.0, 0.0, 1.0)
    high_extremity = np.clip((soc - 80.0) / 20.0, 0.0, 1.0)
    extremity = np.maximum(low_extremity, high_extremity)
    spike_mv = (extremity ** 2) * rng.uniform(100.0, 700.0, size=soc.shape)
    return base_mv + spike_mv


def _degraded_day(date: pd.Timestamp, rng: np.random.Generator, n_samples: int) -> pd.DataFrame:
    """CAN 버스 장애로 배터리/모터 신호가 통째로 빠지고 ADC/오류 카운터만 남은 로그
    (실측 `20260723-...csv`와 동일한 패턴 — `output/OBD_실데이터_분석_보고서.md` 2절)."""
    seconds = np.arange(n_samples) * SAMPLE_INTERVAL_SEC
    system_time = [
        f"{s // 3600:02d}{(s % 3600) // 60:02d}{s % 60:02d}.000000" for s in seconds
    ]
    return pd.DataFrame({
        "Date": [date.strftime("%Y-%m-%d")] * n_samples,
        "SystemTime": [float(t) for t in system_time],
        "ADC_ACC(NA)": rng.choice([0.0, 1.0], size=n_samples),
        "ADC_BATT(V)": rng.uniform(12.0, 13.5, size=n_samples),
        "ERR_CAN1_BusOff()": np.zeros(n_samples),
        "ERR_CAN1_RxCnt()": rng.integers(0, 5, size=n_samples),
    })


def generate_synthetic_obd_day(
    vehicle_model: str,
    date: pd.Timestamp,
    rng: np.random.Generator,
    inject_messiness: bool = True,
) -> pd.DataFrame:
    """차종 하나·날짜 하나에 대한 raw OBD 배치 파일(하루치)을 합성한다.

    `inject_messiness=True`(기본)면 스키마 드리프트(HVAC/GPS 블록 무작위 포함/누락)와
    `BMS_SOC`/`BMS_HVBattery_*` 스케일 오류를 실측과 같은 패턴으로 함께 재현해,
    파이프라인이 실제 배포 환경의 지저분함에 강건한지 계속 검증할 수 있게 한다.
    """
    if vehicle_model not in VEHICLE_MODELS:
        raise ValueError(f"알 수 없는 차종입니다: {vehicle_model}")
    spec = VEHICLE_MODELS[vehicle_model]
    capacity_kwh = spec["battery_capacity_kwh"]

    n_samples = (24 * 3600) // SAMPLE_INTERVAL_SEC

    if inject_messiness and rng.random() < DEGRADED_DAY_PROB:
        return _degraded_day(date, rng, n_samples)

    seconds = np.arange(n_samples) * SAMPLE_INTERVAL_SEC
    hours = seconds / 3600.0
    system_time = [
        f"{int(s // 3600):02d}{int((s % 3600) // 60):02d}{s % 60:02d}.000000" for s in seconds
    ]

    # 출퇴근형 패턴: 07-09시/18-20시 주행, 야간 충전
    driving = ((hours >= 7) & (hours < 9)) | ((hours >= 18) & (hours < 20))
    driving = driving & (rng.random(n_samples) < 0.9)
    charging = (~driving) & (((hours >= 22) | (hours < 6)) & (rng.random(n_samples) < 0.85))

    speed = np.where(driving, rng.uniform(20, 90, n_samples), 0.0)

    soc = np.empty(n_samples)
    soc[0] = rng.uniform(40, 90)
    hv_current = np.zeros(n_samples)
    for i in range(1, n_samples):
        if driving[i]:
            delta = -rng.uniform(0.05, 0.25)  # 주행 중 소모
            hv_current[i] = -rng.uniform(20, 80)
        elif charging[i]:
            delta = rng.uniform(0.05, 0.3)  # 충전 중 회복
            hv_current[i] = rng.uniform(10, 40)
        else:
            delta = 0.0
        soc[i] = float(np.clip(soc[i - 1] + delta, 5, 100))
    hv_voltage = rng.uniform(360, 400, n_samples)  # VCU_BatteryPack_Voltage — 정상 범위(실측 검증됨)
    soe_kwh = soc / 100.0 * capacity_kwh

    imbalance_mv = _cell_imbalance_mv(soc, rng)
    cell_mid_v = 3.0 + soc / 100.0 * 0.4
    half_v = imbalance_mv / 2000.0
    cell_max_v = cell_mid_v + half_v
    cell_min_v = cell_mid_v - half_v

    # 배터리 온도는 주행/충전 여부의 구간값이 아니라 전류 크기에 비례한 발열로
    # 계산한다 — 실측 4절의 "온도가 충방전 강도와 함께 오른다"는 관측을 반영.
    ambient_c = np.where(driving, rng.uniform(20.0, 28.0, n_samples), rng.uniform(18.0, 24.0, n_samples))
    current_heat_c = np.abs(hv_current) * 0.12
    batt_temp_c = np.clip(ambient_c + current_heat_c, 15.0, 65.0)

    df = pd.DataFrame({
        "Date": [date.strftime("%Y-%m-%d")] * n_samples,
        "SystemTime": [float(t) for t in system_time],
        "VCU_VehicleSpeed_STD(km/h)": np.round(speed, 1),
        "VCU_BatteryPack_ASOC(%)": np.round(soc, 1),
        "VCU_BatteryPack_RSOC(%)": np.round(soc + rng.uniform(-0.5, 0.5, n_samples), 1),
        "VCU_BatteryPack_Voltage(V)": np.round(hv_voltage, 1),
        "VCU_BatteryPack_Current(A)": np.round(hv_current, 1),
        "VCU_BatteryPack_SOE(kWh)": np.round(soe_kwh, 2),
        "MCU_Motor_Temperature(degC)": np.round(np.where(driving, rng.uniform(35, 65, n_samples), rng.uniform(20, 30, n_samples)), 1),
        "MCU_Inverter_Temperature(degC)": np.round(np.where(driving, rng.uniform(30, 60, n_samples), rng.uniform(20, 28, n_samples)), 1),
        "BMS_CellVoltage_Max(V)": np.round(cell_max_v, 3),
        "BMS_CellVoltage_Min(V)": np.round(cell_min_v, 3),
        "BMS_Temperature_01(degC)": np.round(batt_temp_c, 1),
        # 실측에서 확인된 스케일 오류를 그대로 재현 — 다운스트림(aggregate_obd_day_to_hourly)이
        # 이 필드들을 무시하고 VCU_BatteryPack_*를 쓰는지 계속 검증하기 위한 목적.
        "BMS_SOC(%)": rng.choice([1.0, 2.0, 3.0, 13.0], size=n_samples),
        "BMS_HVBattery_Voltage(V)": np.round(rng.uniform(100, 105, n_samples), 1),
    })

    if inject_messiness:
        # GPS 블록 — 실측처럼 간헐적으로만 포함(파일 전체 단위로 있거나 없거나)
        if rng.random() < 0.5:
            df["GPS_Latitude"] = np.round(37.5 + rng.uniform(-0.05, 0.05, n_samples), 6)
            df["GPS_longitude"] = np.round(127.0 + rng.uniform(-0.05, 0.05, n_samples), 6)
        # HVAC 블록 — 마찬가지로 간헐적
        if rng.random() < 0.5:
            df["HVAC_Cabin_Temperature(degC)"] = np.round(rng.uniform(18, 26, n_samples), 1)

    return df
