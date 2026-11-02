"""OCPP Central System 프로토콜 로직 테스트 (1.6 및 2.0.1).

실제 WebSocket 연결(에뮬레이터 <-> 서버)은 TestClient의 인프로세스 ASGI 전송으로는
검증할 수 없어(실제 소켓 리스너가 아님) 이미 실행 중인 서버에 대고 수동으로
확인했다(부팅→충전→종료 사이클, 실시간 kW·누적 Wh 갱신, 2.0.1 V2G 충전→방전 사이클
확인). 여기서는 그 상태 전이 로직(`_handle_call_v16`/`_handle_call_v201`)만 순수
함수 단위로 고정한다.
"""
from src.ocpp import central_system


def setup_function(_):
    central_system.clear()
    central_system.clear_commands()


def test_boot_notification_registers_charge_point():
    result = central_system._handle_call_v16("CP-1", "BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"})
    assert result["status"] == "Accepted"
    assert central_system.CHARGE_POINTS["CP-1"]["status"] == "Available"
    assert central_system.CHARGE_POINTS["CP-1"]["protocol"] == "1.6"


def test_full_charging_cycle_updates_power_and_energy():
    cp_id = "CP-2"
    central_system._handle_call_v16(cp_id, "BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"})
    central_system._handle_call_v16(cp_id, "StatusNotification", {"connectorId": 1, "status": "Charging", "errorCode": "NoError"})

    start = central_system._handle_call_v16(
        cp_id, "StartTransaction", {"connectorId": 1, "idTag": "EV-001", "meterStart": 1000, "timestamp": "2026-01-01T00:00:00+00:00"}
    )
    tx_id = start["transactionId"]
    assert central_system.CHARGE_POINTS[cp_id]["meter_wh"] == 1000

    central_system._handle_call_v16(
        cp_id, "MeterValues",
        {
            "connectorId": 1, "transactionId": tx_id,
            "meterValue": [{"timestamp": "2026-01-01T00:00:02+00:00", "sampledValue": [{"value": "1050"}]}],
        },
    )
    cp = central_system.CHARGE_POINTS[cp_id]
    assert cp["meter_wh"] == 1050
    assert cp["power_kw"] > 0  # 50Wh 증가를 관측 시간차로 나눈 순간 전력

    stop = central_system._handle_call_v16(
        cp_id, "StopTransaction", {"transactionId": tx_id, "meterStop": 1200, "timestamp": "2026-01-01T00:05:00+00:00"}
    )
    assert stop["idTagInfo"]["status"] == "Accepted"
    cp = central_system.CHARGE_POINTS[cp_id]
    assert cp["meter_wh"] == 1200
    assert cp["transaction_id"] is None
    assert cp["power_kw"] == 0.0


def test_unknown_action_returns_empty_result():
    result = central_system._handle_call_v16("CP-3", "SomeUnsupportedAction", {})
    assert result == {}


def _meter_value(ts, measurand, value):
    return {"timestamp": ts, "sampledValue": [{"value": value, "measurand": measurand, "unitOfMeasure": {"unit": "Wh"}}]}


def test_v201_boot_notification_registers_charge_point():
    result = central_system._handle_call_v201(
        "CP-V1", "BootNotification", {"chargingStation": {"vendorName": "V", "model": "M"}}
    )
    assert result["status"] == "Accepted"
    cp = central_system.CHARGE_POINTS["CP-V1"]
    assert cp["status"] == "Available"
    assert cp["protocol"] == "2.0.1"


def test_v201_transaction_event_charge_then_v2g_discharge():
    """OCPP 1.6에는 없는 기능: 같은 트랜잭션 안에서 충전(Import) 후 V2G 방전(Export)까지 표현."""
    cp_id = "CP-V2"
    central_system._handle_call_v201(cp_id, "BootNotification", {"chargingStation": {"vendorName": "V", "model": "M"}})

    started = central_system._handle_call_v201(
        cp_id, "TransactionEvent",
        {
            "eventType": "Started", "timestamp": "2026-01-01T00:00:00+00:00", "seqNo": 0,
            "transactionInfo": {"transactionId": "tx-1", "chargingState": "Charging"},
            "idToken": {"idToken": "EV-001", "type": "ISO14443"},
            "meterValue": [_meter_value("2026-01-01T00:00:00+00:00", "Energy.Active.Import.Register", 1000)],
        },
    )
    assert started["idTokenInfo"]["status"] == "Accepted"
    cp = central_system.CHARGE_POINTS[cp_id]
    assert cp["transaction_id"] == "tx-1"
    assert cp["energy_import_wh"] == 1000

    # 충전 단계: Import 증가 -> 전력 양수
    central_system._handle_call_v201(
        cp_id, "TransactionEvent",
        {
            "eventType": "Updated", "timestamp": "2026-01-01T00:00:02+00:00", "seqNo": 1,
            "transactionInfo": {"transactionId": "tx-1", "chargingState": "Charging"},
            "meterValue": [_meter_value("2026-01-01T00:00:02+00:00", "Energy.Active.Import.Register", 1050)],
        },
    )
    cp = central_system.CHARGE_POINTS[cp_id]
    assert cp["energy_import_wh"] == 1050
    assert cp["power_kw"] > 0

    # V2G 방전 단계: Export 증가 -> 전력 음수(역송전 신호) — OCPP 1.6은 이 상태를 표현할 수 없음
    central_system._handle_call_v201(
        cp_id, "TransactionEvent",
        {
            "eventType": "Updated", "timestamp": "2026-01-01T00:00:04+00:00", "seqNo": 2,
            "transactionInfo": {"transactionId": "tx-1", "chargingState": "Charging"},
            "meterValue": [_meter_value("2026-01-01T00:00:04+00:00", "Energy.Active.Export.Register", 30)],
        },
    )
    cp = central_system.CHARGE_POINTS[cp_id]
    assert cp["energy_export_wh"] == 30
    assert cp["power_kw"] < 0

    ended = central_system._handle_call_v201(
        cp_id, "TransactionEvent",
        {
            "eventType": "Ended", "timestamp": "2026-01-01T00:05:00+00:00", "seqNo": 3,
            "transactionInfo": {"transactionId": "tx-1", "chargingState": "Idle"},
            "meterValue": [
                _meter_value("2026-01-01T00:05:00+00:00", "Energy.Active.Import.Register", 1050),
                _meter_value("2026-01-01T00:05:00+00:00", "Energy.Active.Export.Register", 30),
            ],
        },
    )
    assert ended == {}
    cp = central_system.CHARGE_POINTS[cp_id]
    assert cp["transaction_id"] is None
    assert cp["status"] == "Available"
    assert cp["power_kw"] == 0.0


def test_v201_unknown_action_returns_empty_result():
    result = central_system._handle_call_v201("CP-V3", "SomeUnsupportedAction", {})
    assert result == {}


# --------------------------------------------------- CEMS→CSMS 명령 전달 채널 --

def test_authorize_v16_includes_ai_command_when_set():
    central_system.set_command("CP-CMD1", charge_kw=3.5)
    result = central_system._handle_call_v16("CP-CMD1", "Authorize", {"idTag": "EV-001"})
    assert result["aiCommand"] == {"charge_kw": 3.5}
    assert central_system.CHARGE_POINTS["CP-CMD1"]["ai_commanded"] is True


def test_authorize_v16_omits_ai_command_when_not_set():
    result = central_system._handle_call_v16("CP-CMD2", "Authorize", {"idTag": "EV-001"})
    assert "aiCommand" not in result
    assert central_system.CHARGE_POINTS["CP-CMD2"]["ai_commanded"] is False


def test_authorize_v201_includes_charge_and_discharge_command():
    central_system.set_command("CP-CMD3", charge_kw=0.0, discharge_kw=4.2)
    result = central_system._handle_call_v201("CP-CMD3", "Authorize", {"idToken": {"idToken": "EV-001"}})
    assert result["aiCommand"] == {"charge_kw": 0.0, "discharge_kw": 4.2}


def test_clear_commands_removes_all_pending_commands():
    central_system.set_command("CP-CMD4", charge_kw=5.0)
    central_system.clear_commands()
    result = central_system._handle_call_v201("CP-CMD4", "Authorize", {"idToken": {"idToken": "EV-001"}})
    assert "aiCommand" not in result
