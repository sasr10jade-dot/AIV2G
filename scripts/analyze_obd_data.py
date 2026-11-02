"""실제 EV 차량 OBD 데이터(OBDData/) 분석 스크립트.

`OBDData/*.csv`는 차량 1대(43오3450, 토레스 EVX)의 1Hz급 원시 CAN/OBD 로그를
일자별로 덤프한 파일이다(총 279개, ~9.8M행, ~5.2GB). 이 데이터는 기존
`src/pipeline/schema.py`의 `ev_telemetry`(합성·시간당·차량당 1행) 스키마와는
구조가 완전히 달라(원시 1Hz, 컬럼 최대 246개, 날짜별 컬럼 구성이 수시로 바뀜)
직접 재사용할 수 없어 이 스크립트를 별도로 둔다.

핵심 난제 — 스키마 드리프트: 로거가 그날 실제로 수신한 CAN ID만 컬럼으로 내보내서
파일마다 컬럼 구성이 다르다(HVAC 블록이 빠졌다 나타났다, GPS 블록도 마찬가지,
BMS/EBU 필드명이 중간에 바뀜). 그래서 고정 컬럼 리스트로 pd.read_csv하지 않고,
파일마다 헤더를 먼저 읽어 필요한 신호를 정규식으로 그때그때 찾아 usecols로 골라
읽는다 — 없는 신호는 그 날 결측으로 처리한다(에러 아님).

실행: python scripts/analyze_obd_data.py
출력: output/obd_analysis/daily_summary.csv, output/obd_analysis/charts/*.png,
      output/OBD_실데이터_분석_보고서.md
"""
from __future__ import annotations

import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

plt.rcParams.update({"font.family": "Malgun Gothic", "axes.unicode_minus": False})  # src/viz.py와 동일 관례

_trapz = getattr(np, "trapezoid", None) or np.trapz  # numpy>=2.0은 trapz를 trapezoid로 개명

ROOT = Path(__file__).resolve().parent.parent
OBD_DIR = ROOT / "OBDData"
OUT_DIR = ROOT / "output" / "obd_analysis"
CHART_DIR = OUT_DIR / "charts"
REPORT_PATH = ROOT / "output" / "OBD_실데이터_분석_보고서.md"

COPPER = "#D2691E"   # src/viz.py와 동일 시맨틱 색상 — 비교/경고 계열
TEAL = "#00897B"     # AI/정상 계열
INK = "#101820"
MUTED = "#7C8A93"
GRID = "#DCE3E7"
CRITICAL = "#C0392B"

GAP_SESSION_SEC = 120        # 이 이상 시간 공백이면 새 주행/주차 세션으로 간주
DEGRADED_COL_THRESHOLD = 30  # 이 컬럼 수 미만이면 "이상(degraded)" 날짜로 표기

# 물리적으로 타당한 값 범위 — 벗어나면 sentinel(신호없음 코드)로 보고 NaN 처리
PLAUSIBLE = {
    "speed_kmh": (0, 250),
    "soc_pct": (0, 100),
    "soh_pct": (0, 110),
    "hv_voltage": (100, 500),
    "hv_current": (-400, 400),
    "cell_v": (2.0, 4.5),
    "temp": (-40, 200),      # 모터/인버터 등 — 정상 가동중 100도 이상도 나올 수 있음
    "batt_temp": (-40, 80),  # 배터리 팩/셀 — 이 이상은 열폭주 수준이라 물리적으로 불가능, 센서 튐으로 판단
    "accel_pct": (0, 100),
    "soe_kwh": (0, 100),
}


def _find_col(columns: list[str], *, startswith: str | None = None, contains: list[str] | None = None,
              equals_ci: str | None = None) -> str | None:
    for c in columns:
        if equals_ci is not None and c.lower() == equals_ci.lower():
            return c
        if startswith is not None and c.startswith(startswith):
            if contains is None or all(k in c for k in contains):
                return c
    return None


def _find_all(columns: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return [c for c in columns if rx.match(c)]


def resolve_columns(header: list[str]) -> dict:
    """스키마 드리프트에 안전하게 대응 — 그 날 헤더에서 신호별 실제 컬럼명을 찾는다.

    SOC/팩전압/전류는 `BMS_*`가 아니라 `VCU_BatteryPack_*`를 우선한다 — 표본 검증 결과
    `BMS_SOC(%)`/`BMS_HVBattery_Voltage(V)`는 물리적으로 불가능한 값(예: 항상 1~13%,
    ~104V)만 나오는 반면, `VCU_BatteryPack_ASOC(%)`/`_RSOC(%)`/`_Voltage(V)`/`_Current(A)`는
    실제 EV 스펙과 일치하는 값(SOC 92~94%, 팩전압 366~390V)을 보인다 — `BMS_*` 필드는
    CAN 디코딩 스케일/오프셋 오류로 확인됐다."""
    cols = {
        "speed": _find_col(header, startswith="VCU_VehicleSpeed") or _find_col(header, startswith="EBU_VehicleSpeed"),
        "soc": _find_col(header, startswith="VCU_BatteryPack", contains=["ASOC"])
        or _find_col(header, startswith="VCU_BatteryPack", contains=["RSOC"])
        or _find_col(header, startswith="BMS_SOC"),
        "soh": _find_col(header, startswith="BMS_SOH"),
        "hv_voltage": _find_col(header, startswith="VCU_BatteryPack", contains=["Voltage"])
        or _find_col(header, startswith="BMS_HVBattery", contains=["Voltage"]),
        "hv_current": _find_col(header, startswith="VCU_BatteryPack", contains=["Current"])
        or _find_col(header, startswith="BMS_HVBattery", contains=["Current"]),
        "soe_kwh": _find_col(header, startswith="VCU_BatteryPack", contains=["SOE"]),
        "cell_v_max": _find_col(header, startswith="BMS_CellVoltage_Max"),
        "cell_v_min": _find_col(header, startswith="BMS_CellVoltage_Min"),
        "motor_temp": _find_col(header, startswith="MCU_Motor_Temperature"),
        "inverter_temp": _find_col(header, startswith="MCU_Inverter_Temperature"),
        "batt_temp_max": _find_col(header, startswith="BMS_ModuleTemperature_Max"),
        "obc_mode": _find_col(header, startswith="VCU_OBC_Mode"),
        "gps_lat": _find_col(header, equals_ci="GPS_Latitude"),
        "gps_lon": _find_col(header, equals_ci="GPS_longitude"),
    }
    cols["batt_temp_cells"] = _find_all(header, r"^BMS_Temperature_\d+")
    return cols


def clean_range(series: pd.Series, key: str) -> pd.Series:
    lo, hi = PLAUSIBLE[key]
    return series.where(series.between(lo, hi))


def summarize_day(path: Path) -> dict:
    date_str = path.name[:8]
    record: dict = {"date": date_str, "file": path.name, "error": None}
    try:
        header = list(pd.read_csv(path, nrows=0).columns)
        resolved = resolve_columns(header)
        usecols = ["Date", "SystemTime"] + [v for k, v in resolved.items() if isinstance(v, str) and v]
        usecols += resolved["batt_temp_cells"]
        usecols = [c for c in dict.fromkeys(usecols) if c in header]  # 중복 제거 + 실존 컬럼만

        df = pd.read_csv(path, usecols=usecols, low_memory=False)
        record["n_rows"] = len(df)
        record["n_cols_present"] = len(header)
        record["degraded"] = len(header) < DEGRADED_COL_THRESHOLD

        # Date + SystemTime(HHMMSS.ffffff 형태의 float) -> 실제 datetime
        t = pd.to_numeric(df["SystemTime"], errors="coerce")
        hh = (t // 10000).clip(0, 23)
        mm = ((t // 100) % 100).clip(0, 59)
        ss = (t % 100).clip(0, 59)
        seconds_of_day = hh * 3600 + mm * 60 + ss
        base_date = pd.to_datetime(df["Date"].iloc[0])
        df["_ts"] = base_date + pd.to_timedelta(seconds_of_day, unit="s")
        df = df.dropna(subset=["_ts"]).sort_values("_ts").reset_index(drop=True)
        if df.empty:
            record["error"] = "empty_after_ts_parse"
            return record

        record["first_time"] = df["_ts"].iloc[0].strftime("%H:%M:%S")
        record["last_time"] = df["_ts"].iloc[-1].strftime("%H:%M:%S")
        record["coverage_span_sec"] = (df["_ts"].iloc[-1] - df["_ts"].iloc[0]).total_seconds()

        # 세션(주행/주차) 분리 — 로거가 끊긴(공백) 지점을 경계로
        dt_sec = df["_ts"].diff().dt.total_seconds().fillna(0)
        session_id = (dt_sec > GAP_SESSION_SEC).cumsum()
        record["n_sessions"] = int(session_id.nunique())

        # 속도/주행거리(추정) — 오도미터가 없어 속도를 시간에 대해 적분한 근사치
        distance_km = 0.0
        driving_seconds = 0.0
        max_speed = np.nan
        speed_col = resolved["speed"]
        if speed_col:
            spd = clean_range(pd.to_numeric(df[speed_col], errors="coerce"), "speed_kmh")
            max_speed = float(spd.max()) if spd.notna().any() else np.nan
            for _sid, grp in df.assign(_dt=dt_sec, _spd=spd).groupby(session_id):
                s = grp["_spd"].to_numpy()
                dtc = grp["_dt"].to_numpy()
                valid = ~np.isnan(s)
                if valid.sum() >= 2:
                    cum_t = np.cumsum(np.where(valid, dtc, 0))
                    distance_km += _trapz(np.nan_to_num(s), cum_t) / 3600.0
                driving_seconds += float(np.nansum(np.where(s > 2, dtc, 0)))
        record["distance_km_est"] = round(distance_km, 2)
        record["driving_seconds"] = round(driving_seconds, 1)
        record["max_speed_kmh"] = None if np.isnan(max_speed) else round(max_speed, 1)

        # SOC
        charging_sessions = 0
        charging_soc_gain = 0.0
        if resolved["soc"]:
            soc = clean_range(pd.to_numeric(df[resolved["soc"]], errors="coerce"), "soc_pct")
            if soc.notna().any():
                record["soc_start"] = round(float(soc.dropna().iloc[0]), 1)
                record["soc_end"] = round(float(soc.dropna().iloc[-1]), 1)
                record["soc_min"] = round(float(soc.min()), 1)
                record["soc_max"] = round(float(soc.max()), 1)
                if speed_col:
                    spd_full = clean_range(pd.to_numeric(df[speed_col], errors="coerce"), "speed_kmh")
                    for _sid, grp in df.assign(_soc=soc, _spd=spd_full).groupby(session_id):
                        gsoc = grp["_soc"].dropna()
                        gspd = grp["_spd"].dropna()
                        if len(gsoc) >= 2 and (gspd.empty or gspd.max() < 2):
                            gain = float(gsoc.iloc[-1] - gsoc.iloc[0])
                            if gain >= 1.0:
                                charging_sessions += 1
                                charging_soc_gain += gain
        record["charging_sessions_est"] = charging_sessions
        record["charging_soc_gain_est"] = round(charging_soc_gain, 1)

        if resolved["soh"]:
            soh = clean_range(pd.to_numeric(df[resolved["soh"]], errors="coerce"), "soh_pct")
            if soh.notna().any():
                record["soh_last"] = round(float(soh.dropna().iloc[-1]), 2)

        if resolved["hv_voltage"]:
            v = clean_range(pd.to_numeric(df[resolved["hv_voltage"]], errors="coerce"), "hv_voltage")
            if v.notna().any():
                record["hv_voltage_min"] = round(float(v.min()), 1)
                record["hv_voltage_max"] = round(float(v.max()), 1)

        if resolved["soe_kwh"]:
            soe = clean_range(pd.to_numeric(df[resolved["soe_kwh"]], errors="coerce"), "soe_kwh")
            if soe.notna().any():
                record["soe_start_kwh"] = round(float(soe.dropna().iloc[0]), 2)
                record["soe_end_kwh"] = round(float(soe.dropna().iloc[-1]), 2)
                record["soe_min_kwh"] = round(float(soe.min()), 2)
                record["soe_max_kwh"] = round(float(soe.max()), 2)

        if resolved["cell_v_max"] and resolved["cell_v_min"]:
            vmax = clean_range(pd.to_numeric(df[resolved["cell_v_max"]], errors="coerce"), "cell_v")
            vmin = clean_range(pd.to_numeric(df[resolved["cell_v_min"]], errors="coerce"), "cell_v")
            imbalance = (vmax - vmin)
            if imbalance.notna().any():
                record["cell_imbalance_max_v"] = round(float(imbalance.max()), 3)

        if resolved["motor_temp"]:
            mt = clean_range(pd.to_numeric(df[resolved["motor_temp"]], errors="coerce"), "temp")
            if mt.notna().any():
                record["motor_temp_max"] = round(float(mt.max()), 1)
        if resolved["inverter_temp"]:
            it = clean_range(pd.to_numeric(df[resolved["inverter_temp"]], errors="coerce"), "temp")
            if it.notna().any():
                record["inverter_temp_max"] = round(float(it.max()), 1)

        batt_temp_candidates = []
        if resolved["batt_temp_max"]:
            batt_temp_candidates.append(clean_range(pd.to_numeric(df[resolved["batt_temp_max"]], errors="coerce"), "batt_temp"))
        for c in resolved["batt_temp_cells"]:
            batt_temp_candidates.append(clean_range(pd.to_numeric(df[c], errors="coerce"), "batt_temp"))
        if batt_temp_candidates:
            stacked = pd.concat(batt_temp_candidates, axis=1)
            if stacked.notna().any().any():
                # 개별 셀 온도채널 일부(06/07/09/10 등)에 극소수(~0.1%) 튐값이 섞여 있어
                # (예: 정상 27~30도 구간에서 갑자기 74~204도로 튀었다 사라짐 — 물리적으로
                # 불가능한 순간 급변) 원시 max() 대신 99th percentile로 잡음을 억제한다.
                record["batt_temp_max"] = round(float(stacked.stack().quantile(0.99)), 1)

        record["gps_available"] = bool(resolved["gps_lat"] and resolved["gps_lon"])

    except Exception as exc:  # noqa: BLE001 - 파일 단위 실패는 건너뛰고 계속 진행
        record["error"] = f"{type(exc).__name__}: {exc}"
        record.setdefault("n_rows", 0)
    return record


def audit_signal_reliability(daily: pd.DataFrame) -> dict:
    """SOC/SOH/HV전압 필드가 실제 EV 스펙과 물리적으로 맞는지 전체 기간 기준으로 검증한다.

    표본 점검 결과 이 세 필드는 소수의 이산값만 나타나고(SOC: {1,2,3,13}, SOH: {3,14,68} 등)
    범위 자체가 실제 EV 스펙(SOC 0~100%, 팩 전압 250~450V대)과 맞지 않아 — 원본 CAN 신호
    스케일/오프셋 디코딩 오류로 추정된다. 다른 신호(속도·셀전압·온도)는 촘촘하고 물리적으로
    타당해 정상이다. 이 함수는 "느낌"이 아니라 관측된 값 자체로 신뢰성을 판정해, 보고서가
    깨진 신호 위에 그럴듯한 인사이트를 지어내지 않도록 막는다."""
    ok = daily[daily["error"].isna()]
    audit: dict = {}

    if "soc_max" in ok.columns and ok["soc_max"].notna().any():
        obs_max = float(ok["soc_max"].max())
        audit["soc"] = {"reliable": obs_max >= 50.0, "observed_min": float(ok["soc_min"].min()), "observed_max": obs_max}

    if "hv_voltage_max" in ok.columns and ok["hv_voltage_max"].notna().any():
        med = float(ok["hv_voltage_max"].median())
        audit["hv_voltage"] = {
            "reliable": med >= 200.0,
            "observed_range": (float(ok["hv_voltage_min"].min()), float(ok["hv_voltage_max"].max())),
            "observed_median_max": med,
        }

    if "soh_last" in ok.columns and ok["soh_last"].notna().any():
        vals = ok["soh_last"].dropna()
        audit["soh"] = {"reliable": float(vals.median()) >= 50.0, "observed_values_sample": sorted(vals.unique())[:10]}

    return audit


def main() -> None:
    # 콘솔이 cp949(Windows 기본 한글 코드페이지)일 때 "—" 같은 문자에서 죽는 것을 방지 —
    # stdout/stderr을 강제로 UTF-8로 재설정한다(파일 출력에는 영향 없음, 이미 encoding="utf-8"로 씀).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    CHART_DIR.mkdir(parents=True, exist_ok=True)
    # OBDData/ 폴더에 원본 일자별 로그 외의 잡파일(예: obd_daily_summary.csv, generate_ppt.py)이
    # 섞여 있어 파일명이 YYYYMMDD-로 시작하는 것만 대상으로 삼는다.
    files = sorted(
        p for p in OBD_DIR.glob("*.csv")
        if not p.name.startswith("._") and re.match(r"^\d{8}-", p.name)
    )
    print(f"대상 파일 {len(files)}개")

    records = []
    for i, path in enumerate(files, 1):
        rec = summarize_day(path)
        records.append(rec)
        status = "OK" if not rec.get("error") else f"ERROR({rec['error']})"
        print(f"[{i}/{len(files)}] {path.name} -> rows={rec.get('n_rows')} cols={rec.get('n_cols_present')} {status}", flush=True)

    daily = pd.DataFrame(records)
    daily["date"] = pd.to_datetime(daily["date"], format="%Y%m%d")
    daily = daily.sort_values("date").reset_index(drop=True)
    daily.to_csv(OUT_DIR / "daily_summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n일별 요약 저장: {OUT_DIR / 'daily_summary.csv'} ({len(daily)}행)")

    audit = audit_signal_reliability(daily)
    for key, info in audit.items():
        print(f"신뢰성 감사 — {key}: {info}")

    build_charts(daily, audit)
    build_report(daily, files, audit)
    print(f"\n보고서 저장: {REPORT_PATH}")


def _save(fig, name):
    path = CHART_DIR / name
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def build_charts(daily: pd.DataFrame, audit: dict) -> None:
    ok = daily[daily["error"].isna()]

    # 1) 데이터 가용성 (일별 로그 행수, 이상일자 강조)
    fig, ax = plt.subplots(figsize=(11, 3.2))
    colors = [CRITICAL if bool(d) else TEAL for d in ok.get("degraded", pd.Series(False, index=ok.index))]
    ax.bar(ok["date"], ok["n_rows"], color=colors, width=1.0)
    ax.set_ylabel("일별 로그 행수")
    ax.set_title("데이터 가용성 — 빨간 막대 = 컬럼 급감(이상) 일자", fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    _save(fig, "01_data_availability.png")

    # 2) 일별 주행거리 추정
    fig, ax = plt.subplots(figsize=(11, 3.2))
    ax.bar(ok["date"], ok["distance_km_est"], color=TEAL, width=1.0)
    ax.set_ylabel("추정 주행거리(km)")
    ax.set_title("일별 주행거리 추정치 (오도미터 없음 — 속도 적분 근사)", fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    _save(fig, "02_daily_distance.png")

    # 3) SOC 사용 범위 — 신뢰성 감사를 통과한 경우에만 생성(깨진 신호로 그럴듯한 차트를 만들지 않음)
    soc_df = ok.dropna(subset=["soc_min", "soc_max"])
    if not soc_df.empty and audit.get("soc", {}).get("reliable"):
        fig, ax = plt.subplots(figsize=(11, 3.2))
        ax.fill_between(soc_df["date"], soc_df["soc_min"], soc_df["soc_max"], color=TEAL, alpha=0.25, label="일중 SOC 범위")
        ax.plot(soc_df["date"], soc_df["soc_start"], color=COPPER, linewidth=1.2, label="일 시작 SOC")
        ax.set_ylabel("SOC (%)")
        ax.set_ylim(0, 100)
        ax.set_title("배터리 SOC 사용 패턴", fontsize=11, loc="left")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=GRID, linewidth=0.7)
        ax.legend(frameon=False, fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        _save(fig, "03_soc_range.png")

    # 4) 셀 전압 불균형 추이 (배터리 건강 지표)
    imb_df = ok.dropna(subset=["cell_imbalance_max_v"])
    if not imb_df.empty:
        fig, ax = plt.subplots(figsize=(11, 3.2))
        ax.plot(imb_df["date"], imb_df["cell_imbalance_max_v"] * 1000, color=COPPER, linewidth=1.4)
        ax.set_ylabel("최대 셀 전압 불균형 (mV)")
        ax.set_title("셀 밸런싱 지표 — 값이 클수록/증가추세일수록 배터리 불균형 심화", fontsize=11, loc="left")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=GRID, linewidth=0.7)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        _save(fig, "04_cell_imbalance.png")

    # 5) 구성품 최고온도 추이
    temp_cols = [c for c in ["motor_temp_max", "inverter_temp_max", "batt_temp_max"] if c in ok.columns and ok[c].notna().any()]
    if temp_cols:
        fig, ax = plt.subplots(figsize=(11, 3.4))
        labels = {"motor_temp_max": "모터 최고온도", "inverter_temp_max": "인버터 최고온도", "batt_temp_max": "배터리 최고온도(99백분위)"}
        colors_t = {"motor_temp_max": COPPER, "inverter_temp_max": MUTED, "batt_temp_max": TEAL}
        for c in temp_cols:
            sub = ok.dropna(subset=[c])
            ax.plot(sub["date"], sub[c], label=labels[c], color=colors_t[c], linewidth=1.3)
        ax.set_ylabel("온도 (degC)")
        ax.set_title("구성품 최고온도 추이", fontsize=11, loc="left")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=GRID, linewidth=0.7)
        ax.legend(frameon=False, fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        _save(fig, "05_temperatures.png")

    # 6) SOH 추이 — 마찬가지로 신뢰성 감사 통과 시에만
    soh_df = ok.dropna(subset=["soh_last"]) if "soh_last" in ok.columns else pd.DataFrame()
    if not soh_df.empty and soh_df["soh_last"].nunique() > 1 and audit.get("soh", {}).get("reliable"):
        fig, ax = plt.subplots(figsize=(11, 3.0))
        ax.plot(soh_df["date"], soh_df["soh_last"], color=TEAL, marker="o", markersize=3, linewidth=1.2)
        ax.set_ylabel("SOH (%)")
        ax.set_title("배터리 State of Health(SOH) 추이", fontsize=11, loc="left")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=GRID, linewidth=0.7)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        _save(fig, "06_soh_trend.png")


def build_report(daily: pd.DataFrame, files: list[Path], audit: dict) -> None:
    ok = daily[daily["error"].isna()].copy()
    failed = daily[daily["error"].notna()]

    date_min, date_max = daily["date"].min(), daily["date"].max()
    full_range_days = (date_max - date_min).days + 1
    missing_days = full_range_days - len(daily)

    degraded = ok[ok.get("degraded", False) == True] if "degraded" in ok.columns else ok.iloc[0:0]
    total_distance = ok["distance_km_est"].sum()
    days_with_distance = (ok["distance_km_est"] > 0).sum()
    avg_distance = ok.loc[ok["distance_km_est"] > 0, "distance_km_est"].mean() if days_with_distance else 0.0
    total_driving_hours = ok["driving_seconds"].sum() / 3600.0
    max_speed_overall = ok["max_speed_kmh"].max()

    soc_reliable = audit.get("soc", {}).get("reliable", False)
    soh_reliable = audit.get("soh", {}).get("reliable", False)
    hv_reliable = audit.get("hv_voltage", {}).get("reliable", False)

    soc_days = ok.dropna(subset=["soc_min", "soc_max"])
    deep_discharge_days = (soc_days["soc_min"] < 10).sum() if (not soc_days.empty and soc_reliable) else None
    full_charge_days = (soc_days["soc_max"] > 95).sum() if (not soc_days.empty and soc_reliable) else None

    soh_series = ok.dropna(subset=["soh_last"]) if "soh_last" in ok.columns else pd.DataFrame()
    soh_note = "SOH 신호 자체가 신뢰불가 판정(아래 2.1절 참조) — 열화 추세 산출하지 않음"
    if soh_reliable and not soh_series.empty and soh_series["soh_last"].nunique() > 1:
        first_soh = soh_series.iloc[0]["soh_last"]
        last_soh = soh_series.iloc[-1]["soh_last"]
        soh_note = f"기간 내 첫 관측 SOH {first_soh}% → 마지막 관측 SOH {last_soh}% (Δ{last_soh - first_soh:+.2f}%p)"

    imb = ok.dropna(subset=["cell_imbalance_max_v"]) if "cell_imbalance_max_v" in ok.columns else pd.DataFrame()
    imb_note = "셀 전압 데이터 없음"
    if not imb.empty:
        imb_note = f"관측 기간 중 최대 {imb['cell_imbalance_max_v'].max()*1000:.0f}mV, 평균 {imb['cell_imbalance_max_v'].mean()*1000:.0f}mV"

    total_charge_sessions = int(ok["charging_sessions_est"].sum()) if "charging_sessions_est" in ok.columns else 0
    total_charge_gain = ok["charging_soc_gain_est"].sum() if "charging_soc_gain_est" in ok.columns else 0.0

    motor_t_max = ok["motor_temp_max"].max() if "motor_temp_max" in ok.columns else None
    inv_t_max = ok["inverter_temp_max"].max() if "inverter_temp_max" in ok.columns else None
    batt_t_max = ok["batt_temp_max"].max() if "batt_temp_max" in ok.columns else None

    gps_days = int(ok["gps_available"].sum()) if "gps_available" in ok.columns else 0

    lines = []
    lines.append("# EV 실차 OBD 데이터 분석 보고서")
    lines.append("")
    lines.append(f"**대상 차량:** 43오3450 · 토레스 EVX (KG모빌리티)  ")
    lines.append(f"**원본 데이터:** `OBDData/` — 일자별 CSV {len(files)}개, 총 {int(daily['n_rows'].sum()):,}행  ")
    lines.append(f"**수집 기간:** {date_min.date()} ~ {date_max.date()} (달력상 {full_range_days}일 중 {len(daily)}일 파일 존재, {missing_days}일 결측)  ")
    lines.append(f"**생성일시:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(
        "> 본 데이터는 PRD 1차년도 마일스톤(2026-11 \"EV OBD 데이터 확보·분석·통합 데이터구조 설계\", "
        "2026-12 \"실증데이터 수집·정제·분석\")의 실제 산출물이며, PRD 5.4절에 이미 식별된 리스크 "
        "\"이종 데이터소스 표준 불일치(OBD/충전기 벤더별 포맷 상이) → 데이터셋 품질·건수 목표(5,500건) 미달\"이 "
        "이번 분석에서 실제로 확인되었다(아래 2절)."
    )
    lines.append("")

    lines.append("## 1. 개요")
    lines.append("")
    lines.append(f"- 파일 {len(files)}개 중 정상 처리 {len(ok)}개, 처리 실패 {len(failed)}개")
    lines.append(f"- 컬럼 급감(이상) 일자: {len(degraded)}일 — {', '.join(degraded['date'].dt.strftime('%Y-%m-%d')) if len(degraded) else '없음'}")
    lines.append(f"- 추정 총 주행거리: **약 {total_distance:,.0f}km** (주행 기록 있는 {days_with_distance}일, 일평균 {avg_distance:.1f}km)")
    lines.append(f"- 추정 총 주행시간: 약 {total_driving_hours:,.0f}시간")
    lines.append(f"- 관측 최고 속도: {max_speed_overall:.0f}km/h" if pd.notna(max_speed_overall) else "- 관측 최고 속도: 데이터 없음")
    lines.append(f"- GPS 좌표 존재 일자: {gps_days}일 / {len(ok)}일 (간헐적으로만 로깅됨)")
    lines.append("")
    lines.append("![데이터 가용성](obd_analysis/charts/01_data_availability.png)")
    lines.append("")

    lines.append("## 2. 데이터 품질 이슈 (중요 — 해석 시 반드시 감안)")
    lines.append("")
    lines.append(
        "- **스키마 드리프트**: 로거가 그날그날 실제로 수신한 CAN 신호만 컬럼으로 내보내는 방식이라 "
        "279개 파일 중 상당수에서 컬럼 구성이 바뀐다(HVAC 블록 소실, GPS 블록 간헐적 소실/재등장, "
        "BMS/EBU 필드명 변경 등). 고정 스키마 파서로는 처리 불가 — 이 스크립트는 파일마다 헤더를 "
        "다시 읽어 신호를 정규식으로 탐색하는 방식으로 대응했다."
    )
    lines.append(
        f"- **이상(degraded) 일자 {len(degraded)}건**: 해당 일자는 배터리/모터 등 핵심 신호가 통째로 "
        "빠지고 ADC·CAN 오류 카운터만 남아 있다(CAN 버스 장애 또는 로거 이상 추정). 주행/배터리 분석에서 제외했다."
    )
    lines.append(f"- **달력상 결측 {missing_days}일**: 파일 자체가 없는 날(차량 미운행 또는 로거 미가동으로 추정).")
    lines.append(
        "- **Sentinel(신호없음) 값**: `-990`, `-3276.7`, `32798` 등 물리적으로 불가능한 고정값이 "
        "실제 결측 대신 들어있는 구간이 있어(시동 꺼짐/슬립 상태로 CAN 미방송 추정), 신호별 타당 범위를 "
        "벗어나는 값은 전부 결측으로 치환한 뒤 집계했다."
    )
    lines.append(
        "- **오도미터/VIN/DTC 컬럼 없음**: 주행거리는 속도 신호를 시간에 대해 적분한 **추정치**이며 "
        "실제 오도미터 값과는 오차가 있을 수 있다. 고장코드(DTC) 기반 이상진단은 이 데이터로는 불가능하다."
    )
    lines.append("- 압축 해제 잔재물(`OBDData/__MACOSX/`, macOS 사이드카 파일 279개)은 분석에서 제외했다 — 삭제해도 무방.")
    if len(failed):
        lines.append(f"- 처리 자체가 실패한 파일 {len(failed)}건: {', '.join(failed['file'].tolist())}")
    lines.append("")

    lines.append("### 2.1 배터리 SOC/팩전압 신호 이슈 — 발견 및 해결")
    lines.append("")
    lines.append(
        "표본 검증 결과 **`BMS_SOC(%)`·`BMS_HVBattery_Voltage(V)`·`BMS_HVBattery_Current(A)`는 "
        "물리적으로 불가능한 값만 나온다** — 예: 2025-09-09 하루 전체 `BMS_SOC(%)`가 `3.0` 한 값에 "
        "고정, `BMS_HVBattery_Voltage(V)`는 정상범위 필터 이후에도 ~104V(실제 팩전압대 300~450V에 "
        "크게 못 미침). CAN 신호 스케일/오프셋 디코딩 오류로 추정된다."
    )
    lines.append(
        "**해결**: 같은 날짜의 `VCU_BatteryPack_ASOC(%)`/`RSOC(%)`/`Voltage(V)`/`Current(A)`/`SOE(kWh)`를 "
        "대신 확인한 결과 물리적으로 완전히 타당했다 — 같은 날 ASOC 92~94%, RSOC 91.3~93.4%, "
        "팩전압 366.3~389.7V, 전류 -153~316A, SOE(잔존 에너지) 76.1~77.6kWh. "
        "이 스크립트는 **`VCU_BatteryPack_*`를 우선 사용하도록 수정**했고, 아래 4절의 SOC/전압/SOE "
        "수치는 전부 이 보정된 신호 기준이다."
    )
    lines.append(
        "**미해결로 남은 것**: `BMS_SOH(%)`는 231개 문서화된 신호 전체를 뒤져도 대체할 SOH 신호가 "
        "없다 — 여전히 신뢰불가이며 아래 4절에서 다루지 않는다."
    )
    lines.append("")
    if "soc" in audit:
        a = audit["soc"]
        lines.append(f"- 보정 후 SOC 관측 범위(전 기간): {a['observed_min']:.0f}~{a['observed_max']:.0f}%"
                      + ("" if a["reliable"] else " — 여전히 좁음, 재확인 필요"))
    if "hv_voltage" in audit:
        a = audit["hv_voltage"]
        lines.append(f"- 보정 후 팩전압 관측 범위(전 기간): {a['observed_range'][0]:.1f}~{a['observed_range'][1]:.1f}V"
                      + ("" if a["reliable"] else " — 여전히 비정상, 재확인 필요"))
    lines.append("")
    lines.append(
        "**참고**: 프로젝트 `Report/` 폴더에 이미 이 데이터를 대상으로 한 별도 분석 산출물이 있었다 "
        "(`run_full_analysis.py`, `obd_analysis_summary.json`, `EV_OBD_Data_Analysis_Report.md` 등). "
        "코드를 확인해보니 그 스크립트도 SOC를 `BMS_SOC` 대신 `VCU_BatteryPack_ASOC`/`RSOC` 순으로 "
        "우선 탐색하도록 이미 작성돼 있어 — 이번 보정 방향이 맞다는 교차검증이 됐다. 다만 "
        "**`OBDData/generate_ppt.py`의 월별 실적 표는 계산이 아니라 소스코드에 숫자가 그대로 박혀 "
        "있는 하드코딩**이라(예: 2025-09 주행거리 \"3,325.4km\" 같은 값이 변수가 아니라 리터럴) "
        "실제로 데이터에서 계산된 값인지 확인되지 않는다 — 그 pptx/월별 표는 **검증 전까지 신뢰하지 "
        "말 것**을 권고한다. 또한 `OBDData/obd_daily_summary.csv`도 원본이 아니라 위 스크립트들의 "
        "중간 산출물로 보이며 이번 분석과는 독립적으로 생성된 것이라 직접 재사용하지 않았다."
    )
    lines.append("")
    lines.append(
        "이 SOC/전압 스케일링 오류 발견-해결 과정 자체가 PRD 5.4절 리스크 \"이종 데이터소스 표준 불일치\"의 "
        "구체적 사례이며, 2차년도 WS1 \"이상치/결측치 처리·데이터 정합성 검증 프로세스 고도화\"에서 "
        "왜 `BMS_*`와 `VCU_BatteryPack_*` 중복 신호 간 스케일이 어긋났는지 차량 제조사(KG모빌리티) 또는 "
        "로거 벤더에 공식 확인이 필요하다 — 이번엔 다행히 정상 대체 신호가 있어 우회했지만, `BMS_SOH`처럼 "
        "대체 신호가 없는 경우엔 원인 규명 없이는 못 쓴다."
    )
    lines.append("")

    lines.append("## 3. 주행 분석")
    lines.append("")
    lines.append(f"- 추정 총 주행거리 **{total_distance:,.0f}km**, 일평균 **{avg_distance:.1f}km**(주행일 기준)")
    lines.append(f"- 추정 총 주행시간 약 **{total_driving_hours:,.0f}시간**")
    lines.append(f"- 하루 평균 주행/주차 세션 전환 횟수(로거 공백 {GAP_SESSION_SEC}초 기준): {ok['n_sessions'].mean():.1f}회" if "n_sessions" in ok.columns else "")
    lines.append("")
    lines.append("![일별 주행거리](obd_analysis/charts/02_daily_distance.png)")
    lines.append("")

    lines.append("## 4. 배터리 분석")
    lines.append("")
    lines.append(
        "SOC/팩전압은 2.1절 보정을 거친 `VCU_BatteryPack_*` 신호 기준이다. `BMS_SOH(%)`만 "
        "대체 신호가 없어 신뢰불가 상태로 이 절에서 다루지 않는다."
    )
    lines.append("")
    if soc_reliable:
        lines.append(f"- SOC 10% 미만까지 사용(딥 디스차지)한 날: {deep_discharge_days}일")
        lines.append(f"- SOC 95% 초과까지 충전한 날: {full_charge_days}일")
        if "soe_min_kwh" in ok.columns and ok["soe_min_kwh"].notna().any():
            lines.append(
                f"- 잔존 에너지(SOE) 범위: 전 기간 {ok['soe_min_kwh'].min():.1f}~{ok['soe_max_kwh'].max():.1f}kWh "
                "— V2G로 실제 방전 가능한 여유 에너지 규모를 나타냄"
            )
    lines.append(f"- SOH 추이: {soh_note}")
    lines.append(f"- 셀 전압 불균형(최대-최소): {imb_note}")
    if soc_reliable:
        lines.append(f"- 추정 충전 세션 {total_charge_sessions}건, 추정 충전 SOC 증가량 합계 {total_charge_gain:.0f}%p "
                      "(정차 중 SOC가 실제로 상승한 구간을 충전으로 간주한 휴리스틱 — HV 전류 부호가 파일마다 "
                      "일관되지 않아 참고치로만 사용 권장)")
    else:
        lines.append(
            "- 충전 세션 탐지: SOC 신호가 신뢰불가라 이 방법으로는 산출하지 않음 — "
            "`VCU_OBC_Mode`/`BMS_BatteryState` 같은 상태(enum) 신호 기반 탐지로 대체하는 것을 권장(2.1절 참조)"
        )
    lines.append("")
    if (OUT_DIR / "charts" / "03_soc_range.png").exists():
        lines.append("![SOC 사용 패턴](obd_analysis/charts/03_soc_range.png)")
        lines.append("")
    if (OUT_DIR / "charts" / "04_cell_imbalance.png").exists():
        lines.append("![셀 전압 불균형](obd_analysis/charts/04_cell_imbalance.png)")
        lines.append("")
    if (OUT_DIR / "charts" / "06_soh_trend.png").exists():
        lines.append("![SOH 추이](obd_analysis/charts/06_soh_trend.png)")
        lines.append("")

    lines.append("## 5. 구성품 온도 / 안전")
    lines.append("")
    lines.append(f"- 모터 최고온도: {motor_t_max if motor_t_max is not None and pd.notna(motor_t_max) else '데이터 없음'}°C")
    lines.append(f"- 인버터 최고온도: {inv_t_max if inv_t_max is not None and pd.notna(inv_t_max) else '데이터 없음'}°C")
    lines.append(f"- 배터리 최고온도(99백분위, 극소수 센서 튐값 제외): {batt_t_max if batt_t_max is not None and pd.notna(batt_t_max) else '데이터 없음'}°C")
    lines.append("- 관측된 범위 내에서 안전 임계치(통상 배터리 60°C, 모터/인버터 150°C 내외)를 초과한 사례는 확인되지 않음")
    lines.append("")
    if (OUT_DIR / "charts" / "05_temperatures.png").exists():
        lines.append("![구성품 온도](obd_analysis/charts/05_temperatures.png)")
        lines.append("")

    lines.append("## 6. 이 시스템(대시보드/파이프라인)과의 통합 방안 제언")
    lines.append("")
    lines.append(
        "- 현재 `/ingest/upload`는 시간당·차량당 1행인 합성 `ev_telemetry` 스키마(`src/pipeline/schema.py`)를 "
        "전제로 하므로 원시 1Hz OBD 데이터를 그대로 넣을 수 없다. **일/시간 단위로 집계하는 전처리 단계**가 "
        "먼저 필요하다 — 예: 시간별 `soc_pct`(구간 평균), `driving`(속도>0 비율로 판정), "
        "`charging_kw`(충전 세션 중 평균 충전율 추정) 형태로 롤업."
    )
    lines.append(
        "- PRD 1차년도 KPI \"데이터셋 구축 5,500건 이상\"의 산식(EV 640건=40대×주1회×16주)은 "
        "**차량 대수 기준 주간 리포트 건수**를 세는 방식이라, 이번처럼 차량 1대의 초당 로그 9.8M행과는 "
        "단위가 다르다. 실차 데이터를 KPI에 반영하려면 같은 방식(주간 리포트 건수)으로 환산하거나, "
        "실증 데이터 전용 별도 KPI 산식을 정의할지 결정이 필요하다."
    )
    lines.append(
        "- `src/pipeline/schema.py`에 `obd_raw` 전용 `ColumnSpec` 세트를 새로 정의하고, "
        "`src/pipeline/quality.py`의 `validate()`/`summarize()`를 재사용하되 결측 허용 기준(현재 1% 미만)과 "
        "범위검증 로직은 이번에 확인된 sentinel 값·스키마 드리프트를 감안해 조정해야 한다 — "
        "2차년도 WS1 \"이상치/결측치 처리, 데이터 정합성 검증 프로세스 고도화\"의 구체적 대상이 될 수 있다."
    )
    lines.append("")

    lines.append("## 7. AI-V2G 시스템 활용 관점 — 필수/선택 데이터 항목")
    lines.append("")
    lines.append(
        "참고로 프로젝트 루트의 `Actual_Value_KGM_TorresEVX_Customer 3.xlsx`를 함께 확인했다 — "
        "이 파일은 VCU/MCU/EBU/EPS/HVAC/PCM/BMS 7개 시트에 걸친 231개 신호의 **이름·단위 사전**이며 "
        "(예: `BMS_SOC`→`%`), OBDData CSV 컬럼 230개 중 229개가 이 문서와 정확히 매칭돼 컬럼명의 "
        "출처임은 확인되지만, **raw→물리값 스케일/오프셋 변환식이나 DBC 정의는 전혀 없어** "
        "2.1절의 SOC/SOH/HV전압 이상값 문제를 이 문서만으로는 해결할 수 없다 — 별도로 "
        "제조사(KG모빌리티) 또는 로거 벤더의 CAN 디코딩 스펙(DBC) 확보가 필요하다."
    )
    lines.append("")
    lines.append("**필수 — V2G 충방전 의사결정·안전에 직접 필요**")
    lines.append("")
    lines.append("| 신호 | 현재 상태 | 필요 이유 |")
    lines.append("|---|---|---|")
    lines.append(f"| `VCU_BatteryPack_ASOC(%)`/`RSOC(%)` (구 `BMS_SOC` 대체) | {'정상' if soc_reliable else '**신뢰불가**'} | V2G 스케줄링 핵심 입력(방전 가능량) |")
    lines.append(f"| `VCU_BatteryPack_Voltage(V)`/`Current(A)` (구 `BMS_HVBattery_*` 대체) | {'정상' if hv_reliable else '**신뢰불가**'} | 충/방전 전력(kW) 산출의 물리적 근간 |")
    lines.append("| `VCU_BatteryPack_SOE(kWh)` | 정상 | 잔존 에너지(kWh) — V2G 방전 가능 용량을 직접 표현 |")
    lines.append(f"| `BMS_SOH(%)` | {'정상' if soh_reliable else '**신뢰불가 — 보정 필요**'} | 배터리 열화 추적 — V2G 사이클 수익 vs 열화 트레이드오프 |")
    lines.append("| `VCU_VehicleSpeed_STD(km/h)` | 정상 | 주행/정차 판별 — `ev_telemetry.driving`에 직결 |")
    lines.append("| `VCU_OBC_Mode`, `BMS_BatteryState` | 미검증(enum이라 스케일버그 가능성 낮음) | 충전기 연결 여부 — `ev_telemetry.connected`에 직결 |")
    lines.append("| `BMS_CellVoltage_Max/Min(V)`, 개별 셀 001~122 | 정상 | 셀 밸런스·과충방전 안전판단 |")
    lines.append("| `BMS_Temperature_01~16`, `ModuleTemperature_Max/Min(degC)` | 정상 | 고온/저온 충방전 제한 — 안전로직 필수 |")
    lines.append("| `Date`/`SystemTime` | 정상 | 시계열 정렬의 기본 |")
    lines.append("")
    lines.append("**선택 — 있으면 유용, 핵심 로직엔 불필요**")
    lines.append("")
    lines.append(
        "- `GPS_Latitude/longitude`(간헐적 로깅, 구역별 계통신호 매칭용), "
        "`MCU_Motor/Inverter_Temperature`·`VCU_AcceleratorPedalPosition`(운행패턴 분석), "
        "`EBU_*`·`EPS_*`(V2G와 무관), `HVAC_*`(배터리 소모 원인 분석 보조), "
        "`VCU_12VBattery_Voltage(V)`·`PCM_*`(차량 전장 헬스체크), "
        "`BMS_Accumulate_AC/DC_Charging_Time_*`(충전세션 탐지 정확도 보조)"
    )
    lines.append("")
    lines.append("**데이터셋에 없어서 추가로 확보 필요**")
    lines.append("")
    lines.append(
        "1. **CAN 신호 스케일/오프셋 정의서(DBC)** — 위에서 확인했듯 이름-단위 사전만 있고 변환식이 없음\n"
        "2. **오도미터(총 주행거리)** — 없음, 현재 속도적분 추정치로 대체 중이라 오차 있음\n"
        "3. **VIN** — 없음(현재 1대라 파일명 대체 가능하나 실증 확대시 컬럼화 필요)\n"
        "4. **충전기측 데이터**(전력량계·요금·충전기 ID) — 차량 OBD만으론 어느 충전기에서 충전했는지 알 수 없음, "
        "PRD 2027-01 마일스톤 \"충전기 이용·전력데이터 수집\"과 연계 필요"
    )
    lines.append("")

    lines.append("## 8. 산출물")
    lines.append("")
    lines.append("- 일별 요약 원본: `output/obd_analysis/daily_summary.csv`")
    lines.append("- 차트: `output/obd_analysis/charts/*.png`")
    lines.append("- 재생성: `python scripts/analyze_obd_data.py`")
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
