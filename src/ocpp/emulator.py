"""가상 충전기(OCPP 클라이언트) 에뮬레이터 — 1.6과 2.0.1을 모두 지원한다.

실제 EV/충전기 실증 착수 전, 이 앱 자신의 OCPP Central System(`central_system.py`)에
WebSocket으로 접속해 부팅→대기→충전→종료 사이클을 반복하며 진짜 OCPP 메시지를
주고받는다. 세션 파라미터(출력 범위)는 `src/datagen/charger.py`의 완속충전기 가정과
동일하게 맞추되, 실시간 데모를 위해 세션 길이를 시간 단위가 아닌 초 단위로 압축한다.

OCPP 1.6 에뮬레이터는 충전(Import)만 수행한다 — 1.6은 구조적으로 방전을 표현할 수
없다. V2G(방전) 사이클을 보려면 protocol="2.0.1"로 시작해야 하며, 이 경우 한 세션
안에서 충전 구간 이후 Energy.Active.Export.Register로 그리드 역송전(방전) 구간을
이어서 시뮬레이션한다.
"""
import asyncio
import json
import os
import random
from datetime import datetime, timezone

import websockets

DEFAULT_WS_BASE = os.environ.get("OCPP_WS_URL", "ws://127.0.0.1:8000/ocpp")
MAX_CHARGE_KW_DEFAULT = 7.0

RUNNING_TASKS: dict[str, asyncio.Task] = {}
_msg_seq = 0


def _next_message_id() -> str:
    global _msg_seq
    _msg_seq += 1
    return f"m{_msg_seq}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _call(ws, action: str, payload: dict) -> dict:
    unique_id = _next_message_id()
    await ws.send(json.dumps([2, unique_id, action, payload]))
    raw = await ws.recv()
    frame = json.loads(raw)
    return frame[2] if len(frame) > 2 else {}


# ---------------------------------------------------------------- OCPP 1.6 --

async def _run_charge_point_v16(cp_id: str, ws_base: str, max_charge_kw: float) -> None:
    url = f"{ws_base}/{cp_id}?protocol=1.6"
    async with websockets.connect(url, subprotocols=["ocpp1.6"]) as ws:
        await _call(ws, "BootNotification", {"chargePointVendor": "AI-V2G-Sim", "chargePointModel": "Emu-1.6"})
        await _call(ws, "StatusNotification", {"connectorId": 1, "status": "Available", "errorCode": "NoError"})

        meter_wh = random.uniform(0, 5000)
        while True:
            await asyncio.sleep(random.uniform(3, 8))  # 세션 사이 대기(수요 없는 시간)

            await _call(ws, "StatusNotification", {"connectorId": 1, "status": "Preparing", "errorCode": "NoError"})
            id_tag = f"EV-{random.randint(1, 40):03d}"
            auth_result = await _call(ws, "Authorize", {"idTag": id_tag})
            ai_cmd = auth_result.get("aiCommand") if isinstance(auth_result, dict) else None

            start_result = await _call(
                ws, "StartTransaction",
                {"connectorId": 1, "idTag": id_tag, "meterStart": int(meter_wh), "timestamp": _now_iso()},
            )
            tx_id = start_result.get("transactionId")
            await _call(ws, "StatusNotification", {"connectorId": 1, "status": "Charging", "errorCode": "NoError"})

            # CEMS(AI 옵티마이저)가 명령을 내려둔 상태면(central_system.set_command) 그 충전율을
            # 그대로 따른다 — 명령이 없으면 기존처럼 임의 값으로 자율 시뮬레이션한다.
            session_kw = (
                float(ai_cmd["charge_kw"]) if ai_cmd and ai_cmd.get("charge_kw") is not None
                else random.uniform(3.0, max_charge_kw)
            )
            duration_s = random.uniform(10, 25)
            tick_s = 2.0
            elapsed = 0.0
            while elapsed < duration_s:
                await asyncio.sleep(tick_s)
                elapsed += tick_s
                meter_wh += session_kw * (tick_s / 3600) * 1000
                await _call(
                    ws, "MeterValues",
                    {
                        "connectorId": 1,
                        "transactionId": tx_id,
                        "meterValue": [
                            {
                                "timestamp": _now_iso(),
                                "sampledValue": [
                                    {"value": f"{meter_wh:.0f}", "measurand": "Energy.Active.Import.Register", "unit": "Wh"}
                                ],
                            }
                        ],
                    },
                )

            await _call(ws, "StatusNotification", {"connectorId": 1, "status": "Finishing", "errorCode": "NoError"})
            await _call(
                ws, "StopTransaction",
                {"transactionId": tx_id, "meterStop": int(meter_wh), "timestamp": _now_iso()},
            )
            await _call(ws, "StatusNotification", {"connectorId": 1, "status": "Available", "errorCode": "NoError"})


# -------------------------------------------------------------- OCPP 2.0.1 --

def _meter_value_v201(measurand: str, value_wh: float) -> dict:
    return {
        "timestamp": _now_iso(),
        "sampledValue": [{"value": round(value_wh), "measurand": measurand, "unitOfMeasure": {"unit": "Wh"}}],
    }


async def _run_charge_point_v201(cp_id: str, ws_base: str, max_charge_kw: float) -> None:
    """충전(Import) 구간 이후 V2G 방전(Export) 구간을 이어서 수행 — OCPP 1.6으로는
    표현 불가능한 양방향 전력흐름을 실제 프로토콜 메시지로 검증한다."""
    url = f"{ws_base}/{cp_id}?protocol=2.0.1"
    async with websockets.connect(url, subprotocols=["ocpp2.0.1"]) as ws:
        await _call(ws, "BootNotification", {
            "reason": "PowerUp",
            "chargingStation": {"vendorName": "AI-V2G-Sim", "model": "Emu-V2G-2.0.1"},
        })
        await _call(ws, "StatusNotification", {
            "timestamp": _now_iso(), "connectorStatus": "Available", "evseId": 1, "connectorId": 1,
        })

        energy_import_wh = random.uniform(0, 5000)
        energy_export_wh = random.uniform(0, 500)
        tx_counter = 0

        while True:
            await asyncio.sleep(random.uniform(3, 8))

            id_tag = f"EV-{random.randint(1, 40):03d}"
            auth_result = await _call(ws, "Authorize", {"idToken": {"idToken": id_tag, "type": "ISO14443"}})
            ai_cmd = auth_result.get("aiCommand") if isinstance(auth_result, dict) else None

            tx_counter += 1
            tx_id = f"tx-{cp_id}-{tx_counter}"
            seq = 0

            await _call(ws, "TransactionEvent", {
                "eventType": "Started", "timestamp": _now_iso(), "triggerReason": "Authorized",
                "seqNo": seq, "transactionInfo": {"transactionId": tx_id, "chargingState": "Charging"},
                "evse": {"id": 1, "connectorId": 1}, "idToken": {"idToken": id_tag, "type": "ISO14443"},
                "meterValue": [_meter_value_v201("Energy.Active.Import.Register", energy_import_wh)],
            })

            # 1단계 — 일반 충전(Import). CEMS가 명령을 내려뒀으면(central_system.set_command)
            # 그 충전율을 그대로 따르고, charge_kw<=0으로 명령됐으면 이 단계 자체를 건너뛴다.
            has_command = ai_cmd is not None
            charge_kw = (
                float(ai_cmd["charge_kw"]) if has_command and ai_cmd.get("charge_kw") is not None
                else random.uniform(3.0, max_charge_kw)
            )
            charge_ticks = 0 if (has_command and charge_kw <= 0) else random.randint(4, 7)
            for _ in range(charge_ticks):
                await asyncio.sleep(2.0)
                seq += 1
                energy_import_wh += charge_kw * (2.0 / 3600) * 1000
                await _call(ws, "TransactionEvent", {
                    "eventType": "Updated", "timestamp": _now_iso(), "triggerReason": "MeterValuePeriodic",
                    "seqNo": seq, "transactionInfo": {"transactionId": tx_id, "chargingState": "Charging"},
                    "evse": {"id": 1, "connectorId": 1},
                    "meterValue": [_meter_value_v201("Energy.Active.Import.Register", energy_import_wh)],
                })

            # 2단계 — V2G 방전(Export): 그리드로 역송전. OCPP 1.6은 이 방향을 표현할 수 없다.
            # CEMS 명령의 discharge_kw<=0/None이면(LP가 이 세션은 방전 부적합으로 판단한 경우,
            # 예: SOC 밴드 밖) 이 단계를 건너뛴다.
            discharge_kw = (
                float(ai_cmd["discharge_kw"]) if has_command and ai_cmd.get("discharge_kw") is not None
                else random.uniform(2.0, max_charge_kw * 0.7)
            )
            discharge_ticks = 0 if (has_command and discharge_kw <= 0) else random.randint(3, 6)
            for _ in range(discharge_ticks):
                await asyncio.sleep(2.0)
                seq += 1
                energy_export_wh += discharge_kw * (2.0 / 3600) * 1000
                await _call(ws, "TransactionEvent", {
                    "eventType": "Updated", "timestamp": _now_iso(), "triggerReason": "ChargingStateChanged",
                    "seqNo": seq, "transactionInfo": {"transactionId": tx_id, "chargingState": "Charging"},
                    "evse": {"id": 1, "connectorId": 1},
                    "meterValue": [_meter_value_v201("Energy.Active.Export.Register", energy_export_wh)],
                })

            seq += 1
            await _call(ws, "TransactionEvent", {
                "eventType": "Ended", "timestamp": _now_iso(), "triggerReason": "EVDeparted",
                "seqNo": seq, "transactionInfo": {"transactionId": tx_id, "chargingState": "Idle"},
                "evse": {"id": 1, "connectorId": 1},
                "meterValue": [
                    _meter_value_v201("Energy.Active.Import.Register", energy_import_wh),
                    _meter_value_v201("Energy.Active.Export.Register", energy_export_wh),
                ],
            })
            await _call(ws, "StatusNotification", {
                "timestamp": _now_iso(), "connectorStatus": "Available", "evseId": 1, "connectorId": 1,
            })


_RUNNERS = {"1.6": _run_charge_point_v16, "2.0.1": _run_charge_point_v201}


def start_fleet(
    n_chargers: int, max_charge_kw: float = MAX_CHARGE_KW_DEFAULT, protocol: str = "1.6", ws_base: str = DEFAULT_WS_BASE
) -> list[str]:
    runner = _RUNNERS.get(protocol, _run_charge_point_v16)
    started = []
    next_index = len(RUNNING_TASKS) + 1
    for i in range(n_chargers):
        cp_id = f"EMU-CP-{next_index + i:03d}"
        task = asyncio.create_task(runner(cp_id, ws_base, max_charge_kw))
        RUNNING_TASKS[cp_id] = task
        started.append(cp_id)
    return started


def stop_fleet() -> list[str]:
    stopped = list(RUNNING_TASKS.keys())
    for task in RUNNING_TASKS.values():
        task.cancel()
    RUNNING_TASKS.clear()
    return stopped


def running_ids() -> list[str]:
    return list(RUNNING_TASKS.keys())
