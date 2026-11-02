"""최소 OCPP Central System — 실증 충전기 통신 스택의 자체 검증용.

기술개발전략 3장(Build vs Buy)에 따르면 실제 사업에서는 OCPP 스택을 오픈소스(SteVe)나
충전사업자 API로 대체하는 게 맞다(자체 구현은 리소스 낭비). 이 모듈은 그 결정을 뒤집는
것이 아니라, 실제 충전기·SteVe 없이도 AI-V2G 파이프라인의 "충전기 연동" 구간을 눈으로
보여주기 위한 사전 검증/데모용 최소 구현이다.

두 프로토콜 버전을 함께 지원한다:
  - OCPP 1.6-JSON: BootNotification/StatusNotification/Authorize/StartTransaction/
    MeterValues/StopTransaction. 충전(단방향, Import)만 표현 가능하다.
  - OCPP 2.0.1-JSON: BootNotification/StatusNotification/Authorize/TransactionEvent.
    TransactionEvent의 meterValue가 Energy.Active.Import.Register(충전)와
    Energy.Active.Export.Register(방전=V2G 역송전)를 모두 표현할 수 있어, 실제 V2G
    양방향 전력흐름을 프로토콜 수준에서 검증하려면 2.0.1이 필요하다 — 1.6은 구조적으로
    방전을 표현할 방법이 없다.
"""
import json
from datetime import datetime, timezone

import logging

from fastapi import WebSocket, WebSocketDisconnect

from src.ocpp import security

logger = logging.getLogger(__name__)
MAX_FRAME_CHARS = 64 * 1024

CHARGE_POINTS: dict[str, dict] = {}
_tx_counter = 0

# CEMS(AI 최적화)→CSMS(이 모듈)→충전기로 이어지는 명령 전달 채널.
# 실제 OCPP CSMS는 서버가 먼저 SetChargingProfile CALL을 충전기에 push하지만, 이 PoC
# 에뮬레이터/서버는 단순 요청-응답 루프로 짜여 있어 서버발 push를 받으려면 양쪽 다
# "리더 루프 + 대기 중인 호출 상관관계" 구조로 다시 짜야 한다(범위 초과). 대신 충전기가
# 세션마다 한 번 보내는 Authorize 호출의 응답에 "지금 AI가 원하는 충전/방전율"을 실어
# 보내는 방식으로 근사한다 — 매 세션 시작 시점에 최신 명령이 반영되어(수 초~수십 초
# 지연) 폐루프를 이룬다. 실제 상용 CSMS로 갈 때는 이 부분을 SetChargingProfile 서버발
# CALL로 교체해야 한다.
COMMANDED_SCHEDULE: dict[str, dict] = {}


def set_command(cp_id: str, charge_kw: float | None = None, discharge_kw: float | None = None) -> None:
    COMMANDED_SCHEDULE[cp_id] = {"charge_kw": charge_kw, "discharge_kw": discharge_kw}


def clear_commands() -> None:
    COMMANDED_SCHEDULE.clear()


def _get_ai_command(cp_id: str) -> dict | None:
    """새 충전 프로파일(SetChargingProfile)처럼 명시적으로 교체·해제되기 전까지 유지된다
    (조회해도 소비되지 않음) — CEMS가 다시 dispatch할 때까지 같은 명령이 세션마다 계속 적용."""
    return COMMANDED_SCHEDULE.get(cp_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_transaction_id() -> int:
    global _tx_counter
    _tx_counter += 1
    return _tx_counter


# ---------------------------------------------------------------- OCPP 1.6 --

def _handle_meter_values(cp: dict, payload: dict) -> None:
    meter_value = payload.get("meterValue", [])
    if not meter_value:
        return
    sampled = meter_value[-1].get("sampledValue", [])
    if not sampled:
        return
    try:
        new_wh = float(sampled[0].get("value"))
    except (TypeError, ValueError):
        return

    now = datetime.now(timezone.utc)
    prev_wh = cp.get("meter_wh")
    prev_at = cp.get("last_meter_at")
    if prev_wh is not None and prev_at:
        dt_seconds = (now - datetime.fromisoformat(prev_at)).total_seconds()
        if dt_seconds > 0:
            cp["power_kw"] = round((new_wh - prev_wh) / 1000 / (dt_seconds / 3600), 2)

    cp["meter_wh"] = new_wh
    cp["last_meter_at"] = now.isoformat()


def _handle_call_v16(cp_id: str, action: str, payload: dict) -> dict:
    cp = CHARGE_POINTS.setdefault(cp_id, {})
    cp["protocol"] = "1.6"

    if action == "BootNotification":
        cp["vendor"] = payload.get("chargePointVendor")
        cp["model"] = payload.get("chargePointModel")
        cp["status"] = "Available"
        cp["connected_at"] = _now()
        return {"status": "Accepted", "currentTime": _now(), "interval": 30}

    if action == "Heartbeat":
        cp["last_heartbeat"] = _now()
        return {"currentTime": _now()}

    if action == "StatusNotification":
        cp["status"] = payload.get("status")
        cp["last_status_at"] = _now()
        if cp["status"] in ("Available", "Finishing"):
            cp["power_kw"] = 0.0
        return {}

    if action == "Authorize":
        resp = {"idTagInfo": {"status": "Accepted"}}
        cmd = _get_ai_command(cp_id)
        cp["ai_commanded"] = bool(cmd)
        if cmd and cmd.get("charge_kw") is not None:
            resp["aiCommand"] = {"charge_kw": cmd["charge_kw"]}  # 1.6은 방전 자체가 불가해 충전율만 전달
        return resp

    if action == "StartTransaction":
        tx_id = _next_transaction_id()
        cp["transaction_id"] = tx_id
        cp["id_tag"] = payload.get("idTag")
        cp["meter_wh"] = payload.get("meterStart", 0)
        cp["last_meter_at"] = _now()
        cp["session_started_at"] = _now()
        return {"transactionId": tx_id, "idTagInfo": {"status": "Accepted"}}

    if action == "MeterValues":
        _handle_meter_values(cp, payload)
        return {}

    if action == "StopTransaction":
        cp["meter_wh"] = payload.get("meterStop", cp.get("meter_wh", 0))
        cp["transaction_id"] = None
        cp["id_tag"] = None
        cp["session_started_at"] = None
        cp["power_kw"] = 0.0
        return {"idTagInfo": {"status": "Accepted"}}

    return {}


# -------------------------------------------------------------- OCPP 2.0.1 --

def _apply_meter_values_v201(cp: dict, meter_value: list) -> None:
    """meterValue의 sampledValue에서 Import/Export 두 measurand를 각각 누적하고,
    가장 최근 관측값의 방향으로 순간 전력(kW)을 계산한다 — 양수=충전(Import),
    음수=V2G 방전(Export)."""
    sampled: dict[str, float] = {}
    for mv in meter_value:
        for sv in mv.get("sampledValue", []):
            measurand = sv.get("measurand", "Energy.Active.Import.Register")
            try:
                sampled[measurand] = float(sv.get("value"))
            except (TypeError, ValueError):
                continue
    if not sampled:
        return

    now = datetime.now(timezone.utc)
    prev_at = cp.get("last_meter_at")
    dt_seconds = (now - datetime.fromisoformat(prev_at)).total_seconds() if prev_at else None

    if "Energy.Active.Import.Register" in sampled:
        new_val = sampled["Energy.Active.Import.Register"]
        prev = cp.get("energy_import_wh")
        if prev is not None and dt_seconds and dt_seconds > 0 and new_val > prev:
            cp["power_kw"] = round((new_val - prev) / 1000 / (dt_seconds / 3600), 2)
        cp["energy_import_wh"] = new_val

    if "Energy.Active.Export.Register" in sampled:
        new_val = sampled["Energy.Active.Export.Register"]
        prev = cp.get("energy_export_wh")
        if prev is not None and dt_seconds and dt_seconds > 0 and new_val > prev:
            cp["power_kw"] = -round((new_val - prev) / 1000 / (dt_seconds / 3600), 2)
        cp["energy_export_wh"] = new_val

    cp["last_meter_at"] = now.isoformat()


def _record_discharge_if_any(cp_id: str, cp: dict) -> None:
    """세션 중 Export(방전) 누적량이 있으면 discharge_events에 한 건 기록한다. 기록 실패가 OCPP
    통신을 막으면 안 되므로 어떤 오류도 삼킨다(로그만). 종류는 DISCHARGE_RECORD_KIND(기본 TEST)."""
    try:
        exported_kwh = (cp.get("energy_export_wh", 0.0) - cp.get("export_start_wh", 0.0)) / 1000.0
        started = cp.get("session_started_at")
        if exported_kwh <= 0 or not started:
            return
        from src.events import discharge

        duration_h = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 3600
        discharge.record_discharge_event(
            ev_id=cp.get("id_tag") or cp_id, start_ts=started, duration_h=duration_h, energy_kwh=exported_kwh,
            kind=discharge.default_kind(), charger_id=cp_id, source="ocpp",
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception("방전 이벤트 기록 실패(통신에는 영향 없음)")


def _handle_call_v201(cp_id: str, action: str, payload: dict) -> dict:
    cp = CHARGE_POINTS.setdefault(cp_id, {})
    cp["protocol"] = "2.0.1"

    if action == "BootNotification":
        station = payload.get("chargingStation", {})
        cp["vendor"] = station.get("vendorName")
        cp["model"] = station.get("model")
        cp["status"] = "Available"
        cp["connected_at"] = _now()
        return {"status": "Accepted", "currentTime": _now(), "interval": 30}

    if action == "Heartbeat":
        cp["last_heartbeat"] = _now()
        return {"currentTime": _now()}

    if action == "StatusNotification":
        cp["status"] = payload.get("connectorStatus")
        cp["last_status_at"] = _now()
        if cp["status"] == "Available":
            cp["power_kw"] = 0.0
        return {}

    if action == "Authorize":
        resp = {"idTokenInfo": {"status": "Accepted"}}
        cmd = _get_ai_command(cp_id)
        cp["ai_commanded"] = bool(cmd)
        if cmd:
            resp["aiCommand"] = {"charge_kw": cmd.get("charge_kw"), "discharge_kw": cmd.get("discharge_kw")}
        return resp

    if action == "TransactionEvent":
        event_type = payload.get("eventType")
        tx_info = payload.get("transactionInfo", {})
        cp["charging_state"] = tx_info.get("chargingState", cp.get("charging_state"))

        if event_type == "Started":
            cp["transaction_id"] = tx_info.get("transactionId")
            id_token = payload.get("idToken") or {}
            cp["id_tag"] = id_token.get("idToken")
            cp["session_started_at"] = _now()
            cp["energy_import_wh"] = cp.get("energy_import_wh", 0.0)
            cp["energy_export_wh"] = cp.get("energy_export_wh", 0.0)
            cp["export_start_wh"] = cp["energy_export_wh"]
            cp["status"] = "Occupied"

        _apply_meter_values_v201(cp, payload.get("meterValue", []))

        if event_type == "Ended":
            _record_discharge_if_any(cp_id, cp)
            cp["transaction_id"] = None
            cp["id_tag"] = None
            cp["session_started_at"] = None
            cp["charging_state"] = "Idle"
            cp["power_kw"] = 0.0
            cp["status"] = "Available"

        return {"idTokenInfo": {"status": "Accepted"}} if event_type == "Started" else {}

    return {}


# ------------------------------------------------------------- connection --

_HANDLERS = {"1.6": _handle_call_v16, "2.0.1": _handle_call_v201}
_SUBPROTOCOLS = {"1.6": "ocpp1.6", "2.0.1": "ocpp2.0.1"}


async def handle_connection(websocket: WebSocket, cp_id: str, protocol: str = "1.6") -> None:
    protocol = protocol if protocol in _HANDLERS else "1.6"

    # 접속 수락 전 검사 — 거절한 연결은 어떤 프레임도 처리하지 않는다.
    if security.auth_required() and not security.verify_basic(websocket.headers.get("authorization"), cp_id):
        logger.warning("OCPP 인증 실패로 접속 거절: %s", cp_id)
        await websocket.close(code=1008)
        return
    online = sum(1 for c in CHARGE_POINTS.values() if c.get("status") not in (None, "Offline"))
    if cp_id not in CHARGE_POINTS and online >= security.max_connections():
        logger.warning("OCPP 동시 접속 한도 초과로 거절: %s", cp_id)
        await websocket.close(code=1013)
        return

    await websocket.accept(subprotocol=_SUBPROTOCOLS[protocol])
    CHARGE_POINTS[cp_id] = {"status": "Connecting", "connected_at": _now(), "power_kw": 0.0, "protocol": protocol}
    handler = _HANDLERS[protocol]
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_FRAME_CHARS:  # 비정상적으로 큰 프레임은 연결을 끊는다
                await websocket.close(code=1009)
                break
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue  # 형식이 깨진 프레임은 무시(연결은 유지)
            if not isinstance(frame, list) or len(frame) != 4:
                continue
            if frame[0] == 2:  # CALL
                _, unique_id, action, payload = frame
                result = handler(cp_id, action, payload)
                await websocket.send_text(json.dumps([3, unique_id, result]))
    except WebSocketDisconnect:
        pass
    finally:
        if cp_id in CHARGE_POINTS:
            CHARGE_POINTS[cp_id]["status"] = "Offline"
            CHARGE_POINTS[cp_id]["power_kw"] = 0.0


def snapshot() -> dict:
    return CHARGE_POINTS


def clear() -> None:
    CHARGE_POINTS.clear()
