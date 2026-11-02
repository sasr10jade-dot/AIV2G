"""사업수행계획서 v1.0 성과지표(KPI) 산식 — 데이터 건수·인프라 규모의 단일 정의.

대시보드·보고서가 각자 다른 산식을 쓰지 않도록 이 모듈 하나에서 계산한다.

v1.0 정의 (EV 30대 · 충전기 150기 · 3종 데이터)
- 내부 수집 건수 = A 충전세션(참여 차량 연결 세션) + B 차량-일 배터리 프로파일 + B 방전 이벤트
    · 설계값: A 30대 × 주 4회 × 21주 = 2,520 / B 30대 × 100일 = 3,000 / 방전 100 → 5,620 (목표 5,500건 이상)
- 외부 연계 건수 = 전력·기상 등 외부 데이터의 시간단위 실데이터 레코드(합성 데이터는 제외).
    목표 14,000건 이상 — 5,500건 산정에는 포함하지 않는다.
- 이전 산식(EV 주간리포트 건수 + 충전기 세션 건수)은 폐기한다.

"차량-일 프로파일"은 그 날짜에 주행·충전·연결 중 하나라도 관측된 (ev_id, 날짜)이다.
방전 이벤트(V2G/V2L)는 `discharge_events` 테이블(WP2)에 기록되며, 호출 측이
`count_discharge_events(con)` 결과를 `discharge_events` 인자로 넘긴다(TEST 종류 제외).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

# ---- 계획서 v1.0 목표치 --------------------------------------------------------
TARGET_EV = 30
TARGET_EV_MODELS = 2          # 차종 2종 이상
TARGET_EV_PER_MODEL = 10      # 차종당 10대 이상
TARGET_CHARGERS = 150
TARGET_OPERATORS = 2          # 충전사업자 2개사 이상
TARGET_INTERNAL_RECORDS = 5500
TARGET_EXTERNAL_RECORDS = 14000
TARGET_DISCHARGE_EVENTS = 100

DESIGN_A_SESSIONS = TARGET_EV * 4 * 21       # 2,520
DESIGN_B_VEHICLE_DAYS = TARGET_EV * 100      # 3,000
DESIGN_INTERNAL_RECORDS = DESIGN_A_SESSIONS + DESIGN_B_VEHICLE_DAYS + TARGET_DISCHARGE_EVENTS  # 5,620


@dataclass(frozen=True)
class KpiSummary:
    ev_count: int
    ev_model_count: int
    charger_count: int
    operator_count: int | None        # 사업자 컬럼이 없으면 None(미집계)
    a_sessions: int
    b_vehicle_days: int
    b_discharge_events: int
    internal_records: int
    internal_target: int
    internal_design: int
    external_records: int
    external_target: int
    external_is_real: bool

    def as_dict(self) -> dict:
        return asdict(self)


def count_a_sessions(charger_df: pd.DataFrame) -> int:
    """A 충전세션: 참여 차량(ev_id)과 연결된 세션만. ev_id가 아직 없는 데이터는 0."""
    if "ev_id" not in charger_df.columns:
        return 0
    return int(charger_df["ev_id"].notna().sum())


def count_b_vehicle_days(ev_df: pd.DataFrame) -> int:
    """B 차량-일: 주행·충전·연결 중 하나라도 관측된 (ev_id, 날짜) 수."""
    if ev_df.empty:
        return 0
    active = ev_df[
        ev_df["driving"].astype(bool) | ev_df["connected"].astype(bool) | (ev_df["charging_kw"] > 0)
    ]
    if active.empty:
        return 0
    days = active.assign(_day=pd.to_datetime(active["timestamp"]).dt.date)
    return int(days.groupby(["ev_id", "_day"]).ngroups)


EMULATOR_SOURCE = "emulator"


def count_discharge_events(con, emulated: bool = False) -> int:
    """discharge_events 테이블의 방전(V2G/V2L) 기록 수. TEST 종류는 실적에서 제외한다.
    emulated=False(기본)는 실측 기록만, True는 에뮬레이터(source='emulator')가 만든 기록만 센다 —
    에뮬레이션 값이 공식 실적(5,500건 산정)에 섞이지 않도록 항상 분리해서 센다."""
    op = "=" if emulated else "<>"
    try:
        return int(con.execute(
            "SELECT COUNT(*) FROM discharge_events WHERE kind IN ('V2G','V2L') "
            f"AND COALESCE(source,'') {op} ?", (EMULATOR_SOURCE,)).fetchone()[0])
    except Exception:
        return 0


def summarize_kpi(
    ev_df: pd.DataFrame,
    charger_df: pd.DataFrame,
    grid_df: pd.DataFrame | None = None,
    grid_is_real: bool = False,
    discharge_events: int = 0,
) -> KpiSummary:
    a = count_a_sessions(charger_df)
    b = count_b_vehicle_days(ev_df)
    internal = a + b + int(discharge_events)

    # 외부 연계: 합성 데이터는 "수집 실적"이 아니므로 실데이터일 때만 센다.
    # 전력수요·재생E·SMP·기상 4종 × 행 수(시간단위)
    external = (len(grid_df) * 4) if (grid_df is not None and grid_is_real) else 0

    return KpiSummary(
        ev_count=int(ev_df["ev_id"].nunique()) if not ev_df.empty else 0,
        ev_model_count=int(ev_df["vehicle_model"].nunique()) if "vehicle_model" in ev_df.columns else 0,
        charger_count=int(charger_df["charger_id"].nunique()) if "charger_id" in charger_df.columns else 0,
        operator_count=int(charger_df["operator_id"].nunique()) if "operator_id" in charger_df.columns else None,
        a_sessions=a,
        b_vehicle_days=b,
        b_discharge_events=int(discharge_events),
        internal_records=internal,
        internal_target=TARGET_INTERNAL_RECORDS,
        internal_design=DESIGN_INTERNAL_RECORDS,
        external_records=external,
        external_target=TARGET_EXTERNAL_RECORDS,
        external_is_real=bool(grid_is_real),
    )
