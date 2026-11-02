"""Curated Zone 저장소 — 정제된 데이터셋을 집계 리포트/시계열 테이블로 만들고
로컬 SQLite(curated.db)에 적재한다 (WS1 "AI 학습용 표준화 데이터셋").
"""
import sqlite3

import numpy as np
import pandas as pd

from src.datagen.config import SimConfig
from src.optimizer.decision import SOC_BAND_MAX_PCT, SOC_BAND_MIN_PCT
from src.paths import CURATED_DB

RESERVE_SOC_PCT = 20.0  # 이 이하로는 V2G 방전 불가(배터리 보호 최소 SOC)


def build_ev_weekly_report(ev_df: pd.DataFrame, cfg: SimConfig) -> pd.DataFrame:
    """EV별 주간 운행·충전 요약 (KPI "데이터셋 구축" 산출근거: EV대수 x 주1회 x 주차).

    주(week) 인덱스는 `cfg.start_date`가 아니라 데이터 자체의 최소 timestamp를 기준으로
    계산한다 — 합성 데이터는 항상 `cfg.start_date`에서 시작해 정확히 `cfg.weeks`주만큼
    생성되므로(`SimConfig.n_days = weeks*7`) 결과가 그대로지만, 실증 업로드 데이터는
    합성 시나리오와 무관한 임의의 날짜 범위를 갖는다 — 예전처럼 `cfg.start_date` 기준
    윈도우로 자르면 그 범위 밖 실측 데이터가 전부 조용히 버려진다(실제로 2025년 실증
    OBD 데이터를 업로드했을 때 주간리포트가 통째로 비어버리는 것으로 확인된 버그)."""
    df = ev_df.copy()
    ts_start = df["timestamp"].min()
    df["week"] = (df["timestamp"] - ts_start).dt.days // 7

    weekly = (
        df.groupby(["ev_id", "week"])
        .agg(
            ev_type=("ev_type", "first"),
            zone=("zone", "first"),
            avg_soc_pct=("soc_pct", "mean"),
            min_soc_pct=("soc_pct", "min"),
            total_charging_kwh=("charging_kw", "sum"),
            driving_hours=("driving", "sum"),
            connected_hours=("connected", "sum"),
        )
        .reset_index()
    )
    weekly["avg_soc_pct"] = weekly["avg_soc_pct"].round(2)
    return weekly


def build_charger_utilization_hourly(charger_df: pd.DataFrame) -> pd.DataFrame:
    """시간대별·구역별 충전기 혼잡도(가동률) = 그 시간에 세션이 걸쳐 있는 충전기 수 /
    해당 구역 전체 충전기 수.

    PRD 데이터소스 요구항목("전기차 충전사업자: 시간대별 충전사용자 수, 충전기
    이용·전력 데이터")의 코드화 — 원천 데이터(`charger_sessions`: charger_id/
    start_ts/duration_h)는 이미 있어 새 수집 없이 계산만 추가한다. 이 지표는
    (1) V2G 스케줄 배차 시 "지금 어느 충전기가 비어있는지" 판단 근거, (2) 예측
    수요 대비 실제 가용 충전기 부족 시간대 식별(인프라 확충 근거)에 쓰인다."""
    df = charger_df.copy()
    df["end_ts"] = df["start_ts"] + pd.to_timedelta(df["duration_h"], unit="h")

    occupied_rows = []
    for row in df.itertuples(index=False):
        hours = pd.date_range(row.start_ts.floor("h"), row.end_ts, freq="h", inclusive="left")
        if len(hours) == 0:
            hours = pd.DatetimeIndex([row.start_ts.floor("h")])
        for h in hours:
            occupied_rows.append((h, row.zone, row.charger_id))

    occ = pd.DataFrame(occupied_rows, columns=["timestamp", "zone", "charger_id"]).drop_duplicates()
    occupied = occ.groupby(["timestamp", "zone"])["charger_id"].nunique().rename("occupied_chargers").reset_index()

    chargers_per_zone = charger_df.groupby("zone")["charger_id"].nunique().rename("total_chargers")

    result = occupied.merge(chargers_per_zone, on="zone", how="left")
    result["utilization_pct"] = (result["occupied_chargers"] / result["total_chargers"] * 100).round(1)
    return result.sort_values(["timestamp", "zone"]).reset_index(drop=True)


def build_city_hourly(ev_df: pd.DataFrame, grid_df: pd.DataFrame, cfg: SimConfig) -> pd.DataFrame:
    """도시 단위 시간별 통합 뷰 — AI 수요·공급 예측모델(WS2)의 학습 원천.

    `ev_available_v2g_kwh`는 V2G 미지원 차종(`v2g_capable=False`)을 제외하고 계산한다 —
    실제로 방전이 불가능한 차량의 배터리를 "가용 자원"으로 잘못 집계하면 예측모델이
    실현 불가능한 V2G 잠재량을 학습하게 된다. 두 컬럼이 없는(구 스키마) 입력은
    기존과 동일하게(전체 대수, 전역 배터리 용량) 동작해 실증 업로드 데이터와 호환된다."""
    if "battery_capacity_kwh" in ev_df.columns:
        battery_capacity_arr = ev_df["battery_capacity_kwh"].to_numpy()
    else:
        battery_capacity_arr = np.full(len(ev_df), cfg.battery_capacity_kwh)

    if "v2g_capable" in ev_df.columns:
        v2g_capable_arr = ev_df["v2g_capable"].to_numpy()
    else:
        v2g_capable_arr = np.full(len(ev_df), True)

    dischargeable_kwh = np.where(
        (~ev_df["driving"].to_numpy()) & (ev_df["soc_pct"].to_numpy() > RESERVE_SOC_PCT) & v2g_capable_arr,
        (ev_df["soc_pct"].to_numpy() - RESERVE_SOC_PCT) / 100.0 * battery_capacity_arr,
        0.0,
    )
    ev = ev_df.assign(dischargeable_kwh=dischargeable_kwh)

    agg = (
        ev.groupby("timestamp")
        .agg(
            ev_charging_demand_kw=("charging_kw", "sum"),
            ev_available_v2g_kwh=("dischargeable_kwh", "sum"),
            ev_connected_count=("connected", "sum"),
        )
        .reset_index()
    )

    city = grid_df.merge(agg, on="timestamp", how="left").sort_values("timestamp").reset_index(drop=True)
    fill_cols = ["ev_charging_demand_kw", "ev_available_v2g_kwh", "ev_connected_count"]
    city[fill_cols] = city[fill_cols].fillna(0.0)
    return city


def _renewable_kpis(window: pd.DataFrame) -> dict:
    total_demand = float(window["power_demand_mw"].sum())
    total_renewable = float(window["renewable_gen_mw"].sum())
    renewable_share_pct = round(total_renewable / total_demand * 100, 1) if total_demand else 0.0
    peak_idx = window["renewable_gen_mw"].idxmax() if not window.empty else None

    return {
        "timestamps": [t.isoformat() for t in window["timestamp"]],
        "power_demand_mw": [round(float(v), 2) for v in window["power_demand_mw"]],
        "renewable_gen_mw": [round(float(v), 2) for v in window["renewable_gen_mw"]],
        "avg_renewable_mw": round(float(window["renewable_gen_mw"].mean()), 1) if not window.empty else 0.0,
        "avg_demand_mw": round(float(window["power_demand_mw"].mean()), 1) if not window.empty else 0.0,
        "renewable_share_pct": renewable_share_pct,
        "peak_renewable_mw": round(float(window["renewable_gen_mw"].max()), 1) if not window.empty else 0.0,
        "peak_renewable_at": window.loc[peak_idx, "timestamp"].isoformat() if peak_idx is not None else None,
    }


def build_renewable_summary(
    grid_df: pd.DataFrame, window_days: int = 5, daily_window_days: int = 56
) -> dict:
    """도시 전력수요 대비 재생에너지(태양광 위주) 발전 현황 — `generate_grid_signal`이
    이미 만들어 예측모델 피처·가격신호에는 쓰고 있었지만 화면에 따로 보여주지 않던
    `renewable_gen_mw`를 대시보드에 노출한다. 일반충전 vs AI-V2G 차트(`_bucket_poc`)와
    동일한 조회조건 관례로 일별(최근 N일, 시간 단위)/주별(최근 8주, 일 합계)/
    월별(전체 기간, 주 합계) 세 뷰를 한 번에 계산해, 프런트엔드가 그래뉼래러티를
    바꿀 때 재조회 없이 즉시 전환할 수 있게 한다.

    **실제 데이터 연동 시 유의**: `renewable_gen_mw`는 지금 항상 합성값이다 — KPX SMP
    연동(`sync-kpx-smp`)을 쓰더라도 이 컬럼은 갱신되지 않으므로, 실제 재생에너지
    발전량 공공데이터를 별도로 연계하기 전까지는 이 카드의 발전량 수치를 실측치로
    오인하지 않아야 한다."""
    cutoff = grid_df["timestamp"].max() - pd.Timedelta(days=window_days)
    hourly = grid_df[grid_df["timestamp"] >= cutoff].sort_values("timestamp").reset_index(drop=True)

    bucketed = (
        grid_df.set_index("timestamp")[["power_demand_mw", "renewable_gen_mw"]]
        .resample("1D")
        .sum()
        .reset_index()
    )
    daily = bucketed.tail(daily_window_days).reset_index(drop=True)

    weekly = (
        grid_df.set_index("timestamp")[["power_demand_mw", "renewable_gen_mw"]]
        .resample("1W")
        .sum()
        .reset_index()
    )

    return {
        "hourly": _renewable_kpis(hourly),
        "daily": _renewable_kpis(daily),
        "weekly": _renewable_kpis(weekly),
    }


def _v2g_dischargeable_now(r: pd.Series) -> float:
    """"달리는 ESS"로서 이 차량이 지금 당장 V2G로 내줄 수 있는 에너지(kWh) —
    `decision.optimize_fleet`이 실제 방전을 허용하는 조건(SOC 밴드 35~80%,
    v2g_capable, 연결·비주행)과 동일한 기준을 쓴다. 도시 전체 집계용
    `build_city_hourly`의 `dischargeable_kwh`는 더 느슨한 기준(SOC>20%)을 쓰는데,
    이 표는 "지금 실제로 디스패치될 수 있는 양"을 보여주는 게 목적이라 실제 LP
    게이트와 맞춘다."""
    if bool(r["driving"]) or not bool(r["connected"]):
        return 0.0
    if not bool(r.get("v2g_capable", True)):
        return 0.0
    soc = r.get("soc_pct")
    capacity = r.get("battery_capacity_kwh")
    if pd.isna(soc) or pd.isna(capacity) or not (SOC_BAND_MIN_PCT <= soc <= SOC_BAND_MAX_PCT):
        return 0.0
    return round((float(soc) - SOC_BAND_MIN_PCT) / 100.0 * float(capacity), 2)


def build_vehicle_status_table(ev_df: pd.DataFrame) -> list[dict]:
    """차량별 최신 상태(배터리 SOC·충전 여부·순간 충전량) 스냅샷 — 대시보드의
    "차량별 배터리·충전 현황" 표가 이 함수 하나만 근거로 삼는다.

    단순히 각 차량의 마지막 행(항상 그날 23시)을 쓰면 배치 데이터 특성상 항상
    심야 스냅샷만 보여 "주행중"이 늘 0으로 보이는 문제가 있었다 — 그래서 각
    차량의 마지막 관측일 중에서 "지금(서울 시간) 시각과 같은 시(hour)"을 찾아
    그 행을 쓴다. 그 시각 데이터가 없으면(실측 데이터의 결측 등) 그 차량의
    마지막 행으로 대체한다.

    `vehicle_model`/`battery_capacity_kwh`/`v2g_capable`는 구 스키마(실증 업로드
    초기 등)엔 없을 수 있어 각각 안전한 기본값으로 대체한다."""
    if ev_df.empty:
        return []

    now_hour = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).hour
    df = ev_df.copy()
    last_day_per_ev = df.groupby("ev_id")["timestamp"].transform("max").dt.normalize()
    is_target_hour = (df["timestamp"].dt.normalize() == last_day_per_ev) & (df["timestamp"].dt.hour == now_hour)
    snapshot = df[is_target_hour]

    missing_ids = set(df["ev_id"]) - set(snapshot["ev_id"])
    if missing_ids:
        fallback = (
            df[df["ev_id"].isin(missing_ids)]
            .sort_values("timestamp")
            .groupby("ev_id", as_index=False)
            .tail(1)
        )
        snapshot = pd.concat([snapshot, fallback], ignore_index=True)

    latest = snapshot.sort_values("ev_id")

    rows = []
    for _, r in latest.iterrows():
        rows.append({
            "ev_id": r["ev_id"],
            "vehicle_model": r.get("vehicle_model", "미상"),
            "zone": int(r["zone"]) if pd.notna(r.get("zone")) else None,
            "timestamp": r["timestamp"].isoformat(),
            "soc_pct": round(float(r["soc_pct"]), 1) if pd.notna(r["soc_pct"]) else None,
            "connected": bool(r["connected"]),
            "driving": bool(r["driving"]),
            "charging_kw": round(float(r["charging_kw"]), 2),
            "battery_capacity_kwh": float(r["battery_capacity_kwh"]) if pd.notna(r.get("battery_capacity_kwh")) else None,
            "v2g_capable": bool(r["v2g_capable"]) if pd.notna(r.get("v2g_capable")) else True,
            "batt_temp_c": round(float(r["batt_temp_c"]), 1) if pd.notna(r.get("batt_temp_c")) else None,
            "v2g_dischargeable_kwh_now": _v2g_dischargeable_now(r),
        })
    return rows


def get_connection() -> sqlite3.Connection:
    from src.pipeline.migrations import migrate

    con = sqlite3.connect(CURATED_DB)
    migrate(con)
    return con


def write_table(df: pd.DataFrame, name: str, con: sqlite3.Connection) -> None:
    df.to_sql(name, con, if_exists="replace", index=False)


DASHBOARD_RUN_HISTORY_TABLE = "dashboard_run_history"


def append_dashboard_run_history(record: dict, db_path=CURATED_DB) -> None:
    """대시보드가 시나리오를 실제로 새로 계산할 때마다(캐시 히트 시에는 호출되지
    않음) 핵심 지표 한 줄을 누적 기록한다 — EV Bot이 "지난번보다 나아졌어?" 같은
    추세 질문에 답할 근거 데이터가 된다. `write_table`과 달리 append이므로 매번
    테이블을 교체하지 않고 이력이 쌓인다."""
    con = sqlite3.connect(db_path)
    try:
        pd.DataFrame([record]).to_sql(DASHBOARD_RUN_HISTORY_TABLE, con, if_exists="append", index=False)
    finally:
        con.close()


def read_dashboard_run_history(limit: int = 20, db_path=CURATED_DB) -> pd.DataFrame:
    """오래된 순으로 정렬된 최근 실행 이력. 테이블이 아직 없으면(최초 실행 전)
    빈 DataFrame을 반환한다."""
    con = sqlite3.connect(db_path)
    try:
        query = f"SELECT * FROM {DASHBOARD_RUN_HISTORY_TABLE} ORDER BY generated_at DESC LIMIT ?"
        try:
            df = pd.read_sql(query, con, params=(limit,))
        except pd.errors.DatabaseError:
            return pd.DataFrame()
    finally:
        con.close()
    return df.iloc[::-1].reset_index(drop=True)
