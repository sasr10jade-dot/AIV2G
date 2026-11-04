"""원천(raw) 데이터 스키마 정의 — WS1 "수집주기·항목·인터페이스·품질관리 기준"의 코드화."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dtype: str  # "float" | "int" | "bool" | "str" | "datetime"
    min_value: float | None = None
    max_value: float | None = None
    required: bool = True


EV_TELEMETRY_SCHEMA = [
    ColumnSpec("timestamp", "datetime"),
    ColumnSpec("ev_id", "str"),
    ColumnSpec("ev_type", "str"),
    ColumnSpec("zone", "int", min_value=0),
    ColumnSpec("soc_pct", "float", min_value=0, max_value=100),
    ColumnSpec("connected", "bool"),
    ColumnSpec("driving", "bool"),
    ColumnSpec("charging_kw", "float", min_value=0),
]

CHARGER_SESSION_SCHEMA = [
    ColumnSpec("session_id", "str"),
    ColumnSpec("charger_id", "str"),
    ColumnSpec("zone", "int", min_value=0),
    ColumnSpec("start_ts", "datetime"),
    ColumnSpec("duration_h", "float", min_value=0),
    ColumnSpec("energy_kwh", "float", min_value=0),
    # 어느 차량이 충전했는지 — 실증 초기엔 OCPP id_tag<->ev_id 매핑이 없어 대부분
    # 비어있을 수 있으므로 선택 컬럼이다(품질검증에서 결측으로 잡지 않음). 있는
    # 세션만 충전 효율 분석(src/model/charging_efficiency.py)의 대상이 된다.
    ColumnSpec("ev_id", "str", required=False),
    # 충전사업자(CPO) 식별자·수집 경로(cpo_api/ocpp/manual) — 사업자별 연계 실적 집계용(선택).
    ColumnSpec("operator_id", "str", required=False),
    ColumnSpec("source", "str", required=False),
    # 계획서 4.1 충전기 수집 항목(충전 시작·종료 시각, 충전량, 충전기·커넥터 상태, 오류·종료 사유) — 사업자 제공 범위가
    # 데이터 제공 용역으로 확정되기 전이라 선택 컬럼으로 둔다.
    ColumnSpec("end_ts", "datetime", required=False),
    ColumnSpec("charger_type", "str", required=False),      # AC_SLOW | DC_FAST 등 — "충전기 2종 이상" 집계
    ColumnSpec("connector_type", "str", required=False),
    ColumnSpec("avg_power_kw", "float", min_value=0, required=False),
    ColumnSpec("stop_reason", "str", required=False),
    ColumnSpec("error_code", "str", required=False),
    ColumnSpec("site_id", "str", required=False),           # 실증 참여 장소 — "참여 장소 3곳 이상" 집계
]

GRID_SIGNAL_SCHEMA = [
    ColumnSpec("timestamp", "datetime"),
    ColumnSpec("power_demand_mw", "float", min_value=0),
    ColumnSpec("renewable_gen_mw", "float", min_value=0),
    ColumnSpec("temperature_c", "float", min_value=-30, max_value=50),
    ColumnSpec("price_krw_per_kwh", "float", min_value=0),
    ColumnSpec("is_holiday", "int", min_value=0, max_value=1),
]

SCHEMAS = {
    "ev_telemetry": EV_TELEMETRY_SCHEMA,
    "charger_sessions": CHARGER_SESSION_SCHEMA,
    "grid_signal": GRID_SIGNAL_SCHEMA,
}
