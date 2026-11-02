"""실증 EV 차량의 OBD 원본 로그(1Hz, 스키마 드리프트 있음)를 `ev_telemetry`
(시간당 집계) 스키마로 변환한다 (WS1 "이종 데이터 표준화").

실증차량은 OBD 데이터를 실시간이 아니라 배치(예: 일 단위)로 전송한다 — 이 모듈은
그 배치 파일 하나(며칠/하루치 원본 CSV)를 받아 시간당 `ev_telemetry` 행으로
집계하는 순수 함수를 제공하고, 실제 업로드 처리는
`src/api/routers/ingest.py::upload_obd_batch`가 담당한다.

컬럼 해석 방식은 `scripts/analyze_obd_data.py`(실제 토레스 EVX 279일치 데이터로
검증됨)와 동일하다 — 특히 SOC/팩전압/전류는 `BMS_*`가 아니라 `VCU_BatteryPack_*`를
우선한다: 실측 검증 결과(`output/OBD_실데이터_분석_보고서.md` 2.1절) `BMS_SOC(%)`/
`BMS_HVBattery_*`는 CAN 디코딩 스케일 오류로 물리적으로 불가능한 값만 나오고,
`VCU_BatteryPack_ASOC/RSOC/Voltage/Current`가 실제 EV 스펙과 일치함이 확인됐다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.datagen.ev import VEHICLE_MODELS
from src.pipeline import schema as schema_module
from src.pipeline import source
from src.pipeline.quality import summarize, validate

PLAUSIBLE = {
    "speed_kmh": (0, 250),
    "soc_pct": (0, 100),
    "hv_voltage": (100, 500),
    "hv_current": (-400, 400),
    "batt_temp_c": (-30, 80),
}


class ObdColumnsMissingError(ValueError):
    """속도·SOC 신호가 헤더에 전혀 없어 집계할 수 없을 때(예: CAN 버스 장애로 인한
    이상(degraded) 로그 — `output/OBD_실데이터_분석_보고서.md` 2절에서 실제로 7건 발견됨)."""


def _find_col(
    columns: list[str], *, startswith: str | None = None, contains: list[str] | None = None,
    equals_ci: str | None = None,
) -> str | None:
    for c in columns:
        if equals_ci is not None and c.lower() == equals_ci.lower():
            return c
        if startswith is not None and c.startswith(startswith):
            if contains is None or all(k in c for k in contains):
                return c
    return None


def resolve_obd_columns(header: list[str]) -> dict:
    """스키마 드리프트에 안전하게 대응 — 그 배치 파일 헤더에서 신호별 실제 컬럼명을 찾는다."""
    return {
        "speed": _find_col(header, startswith="VCU_VehicleSpeed") or _find_col(header, startswith="EBU_VehicleSpeed"),
        "soc": _find_col(header, startswith="VCU_BatteryPack", contains=["ASOC"])
        or _find_col(header, startswith="VCU_BatteryPack", contains=["RSOC"])
        or _find_col(header, startswith="BMS_SOC"),
        "hv_voltage": _find_col(header, startswith="VCU_BatteryPack", contains=["Voltage"])
        or _find_col(header, startswith="BMS_HVBattery", contains=["Voltage"]),
        "hv_current": _find_col(header, startswith="VCU_BatteryPack", contains=["Current"])
        or _find_col(header, startswith="BMS_HVBattery", contains=["Current"]),
        # 실측 검증됨(output/OBD_실데이터_분석_보고서.md) — SOC/전압과 달리 스케일 오류 없이 정상.
        "batt_temp": _find_col(header, startswith="BMS_Temperature")
        or _find_col(header, startswith="ModuleTemperature", contains=["Max"]),
    }


def _clean(series: pd.Series, key: str) -> pd.Series:
    lo, hi = PLAUSIBLE[key]
    return series.where(series.between(lo, hi))


def aggregate_obd_day_to_hourly(
    df: pd.DataFrame,
    ev_id: str,
    vehicle_model: str,
    zone: int = 0,
    ev_type: str = "실증차량",
) -> pd.DataFrame:
    """OBD 원본 1Hz 데이터프레임(배치 파일 하나, 하루 이상도 가능) 하나를 시간당
    `ev_telemetry` 스키마 행들로 집계한다.

    - `Date`+`SystemTime`(HHMMSS.ffffff)을 실제 datetime으로 변환
    - `soc_pct`: 그 시간대 첫 관측값 (합성 생성기의 "행동 이전 값 기록" 관례와 동일 —
      `src/datagen/ev.py` 모듈 docstring 참고)
    - `driving`: 그 시간대 샘플의 과반이 속도 2km/h 초과
    - `charging_kw`: HV 전압×전류가 양(+, 충전 방향)인 구간의 평균 kW. 방전(음수) 구간은
      배치 집계에서는 0으로 처리한다 — 실제 V2G 방전 실적은 OCPP 계량값(central_system.py)으로
      별도 집계하는 편이 더 정확하고, 이 함수의 책임은 "일반 충전 패턴 파악"으로 한정한다.
    - `connected`: 명확한 "플러그인 여부" 신호가 없어 `charging_kw > 0`으로 근사한다 —
      꽂혀 있지만 완충이라 충전 안 하는 시간은 "연결 안 됨"으로 과소추정될 수 있다는
      한계가 있다(알려진 근사, GPS처럼 신뢰 가능한 별도 신호가 없는 한 개선 어려움).
    """
    if vehicle_model not in VEHICLE_MODELS:
        raise ValueError(f"알 수 없는 차종입니다: {vehicle_model} (선택지: {', '.join(VEHICLE_MODELS)})")
    model_spec = VEHICLE_MODELS[vehicle_model]

    header = list(df.columns)
    resolved = resolve_obd_columns(header)
    if resolved["speed"] is None and resolved["soc"] is None:
        raise ObdColumnsMissingError(
            "속도(VCU_VehicleSpeed_STD/EBU_VehicleSpeed)와 SOC(VCU_BatteryPack_ASOC/RSOC 또는 BMS_SOC) "
            "신호가 모두 없어 집계할 수 없습니다 — CAN 버스 장애 등으로 인한 이상(degraded) 로그로 보입니다."
        )
    if "Date" not in df.columns or "SystemTime" not in df.columns:
        raise ObdColumnsMissingError("Date/SystemTime 컬럼이 없어 시간대를 알 수 없습니다.")

    t = pd.to_numeric(df["SystemTime"], errors="coerce")
    hh = (t // 10000).clip(0, 23)
    mm = ((t // 100) % 100).clip(0, 59)
    ss = (t % 100).clip(0, 59)
    seconds_of_day = hh * 3600 + mm * 60 + ss
    dates = pd.to_datetime(df["Date"], errors="coerce")
    ts = dates + pd.to_timedelta(seconds_of_day, unit="s")

    work = pd.DataFrame({"_ts": ts})
    work["_speed"] = (
        _clean(pd.to_numeric(df[resolved["speed"]], errors="coerce"), "speed_kmh")
        if resolved["speed"] else np.nan
    )
    work["_soc"] = (
        _clean(pd.to_numeric(df[resolved["soc"]], errors="coerce"), "soc_pct")
        if resolved["soc"] else np.nan
    )

    if resolved["hv_voltage"] and resolved["hv_current"]:
        v = _clean(pd.to_numeric(df[resolved["hv_voltage"]], errors="coerce"), "hv_voltage")
        i = _clean(pd.to_numeric(df[resolved["hv_current"]], errors="coerce"), "hv_current")
        power_kw = (v * i) / 1000.0
        work["_charge_kw"] = power_kw.where(power_kw > 0, 0.0)
    else:
        work["_charge_kw"] = np.nan

    work["_batt_temp"] = (
        _clean(pd.to_numeric(df[resolved["batt_temp"]], errors="coerce"), "batt_temp_c")
        if resolved["batt_temp"] else np.nan
    )

    work = work.dropna(subset=["_ts"]).sort_values("_ts")
    if work.empty:
        raise ObdColumnsMissingError("Date/SystemTime을 실제 시각으로 변환한 결과가 전부 결측입니다.")
    work["_hour"] = work["_ts"].dt.floor("h")

    rows = []
    for hour, grp in work.groupby("_hour"):
        soc_val = grp["_soc"].dropna()
        soc_pct = round(float(soc_val.iloc[0]), 2) if not soc_val.empty else None

        speed_val = grp["_speed"].dropna()
        driving = bool((speed_val > 2.0).mean() > 0.5) if not speed_val.empty else False

        charge_val = grp["_charge_kw"].dropna()
        charging_kw = round(max(float(charge_val.mean()), 0.0), 2) if not charge_val.empty else 0.0
        connected = (not driving) and charging_kw > 0.05

        temp_val = grp["_batt_temp"].dropna()
        batt_temp_c = round(float(temp_val.mean()), 1) if not temp_val.empty else None

        rows.append((
            hour, ev_id, ev_type, zone, soc_pct, connected, driving, charging_kw,
            vehicle_model, model_spec["battery_capacity_kwh"], model_spec["v2g_capable"], batt_temp_c,
        ))

    return pd.DataFrame(
        rows,
        columns=[
            "timestamp", "ev_id", "ev_type", "zone", "soc_pct", "connected", "driving", "charging_kw",
            "vehicle_model", "battery_capacity_kwh", "v2g_capable", "batt_temp_c",
        ],
    )


def merge_hourly_rows_into_source(new_rows: pd.DataFrame) -> dict:
    """집계된 시간당 EV 행(실제 배치 업로드든, OBD 에뮬레이터 합성이든)을 기존
    `source.EV_CSV`에 병합하고 품질검증 결과와 함께 반환한다. `/ingest/upload-obd`와
    OBD 에뮬레이터 엔드포인트가 이 함수 하나를 공유해 병합 로직이 갈라지지 않는다."""
    if source.EV_CSV.exists():
        existing = pd.read_csv(source.EV_CSV, parse_dates=["timestamp"])
        combined = pd.concat([existing, new_rows], ignore_index=True)
        combined = combined.drop_duplicates(subset=["ev_id", "timestamp"], keep="last")
    else:
        combined = new_rows
    combined = combined.sort_values(["ev_id", "timestamp"]).reset_index(drop=True)

    quality_report = validate(
        combined, "ev_telemetry", schema_module.EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"]
    )
    combined.to_csv(source.EV_CSV, index=False)
    source.mark_source("uploaded")

    return {
        "new_rows": len(new_rows),
        "total_ev_rows": len(combined),
        "quality_report_md": summarize([quality_report]),
    }
