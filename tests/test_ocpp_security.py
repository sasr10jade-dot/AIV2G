"""OCPP 접속 보안 테스트 — 인증(Basic), 접속 한도, 비정상 프레임."""
import base64
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api import app
from src.ocpp import central_system as cs
from src.ocpp import security as sec


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sec, "CREDENTIALS_FILE", tmp_path / "ocpp_credentials.json")
    cs.clear()
    yield
    cs.clear()


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


PW = "a-long-charger-password-1"


def test_set_credential_requires_long_password_and_stores_only_hash():
    with pytest.raises(ValueError):
        sec.set_credential("CP1", "short")
    sec.set_credential("CP1", PW)
    stored = sec.CREDENTIALS_FILE.read_text(encoding="utf-8")
    assert PW not in stored and "$2" in stored  # bcrypt 해시


def test_verify_basic_cases():
    sec.set_credential("CP1", PW)
    assert sec.verify_basic(basic("CP1", PW)["Authorization"], "CP1")
    assert not sec.verify_basic(basic("CP1", "wrong-password-xxxxxxx")["Authorization"], "CP1")
    assert not sec.verify_basic(basic("CP2", PW)["Authorization"], "CP1")   # 다른 충전기 이름
    assert not sec.verify_basic(basic("CP9", PW)["Authorization"], "CP9")   # 미등록
    assert not sec.verify_basic(None, "CP1") and not sec.verify_basic("Basic !!!", "CP1") and not sec.verify_basic("Bearer x", "CP1")
    assert sec.revoke_credential("CP1") and not sec.verify_basic(basic("CP1", PW)["Authorization"], "CP1")


def test_auth_required_rejects_unauthenticated_and_accepts_valid(monkeypatch):
    monkeypatch.setenv("OCPP_AUTH_REQUIRED", "1")
    sec.set_credential("CP1", PW)
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ocpp/CP1?protocol=1.6", subprotocols=["ocpp1.6"]):
            pass
    assert "CP1" not in cs.CHARGE_POINTS  # 거절된 연결은 상태를 남기지 않는다
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ocpp/CP1?protocol=1.6", subprotocols=["ocpp1.6"], headers=basic("CP1", "nope-nope-nope-nope")):
            pass
    with c.websocket_connect("/ocpp/CP1?protocol=1.6", subprotocols=["ocpp1.6"], headers=basic("CP1", PW)) as ws:
        ws.send_text(json.dumps([2, "1", "Heartbeat", {}]))
        assert json.loads(ws.receive_text())[0] == 3


def test_auth_off_by_default_keeps_dev_and_emulator_working(monkeypatch):
    monkeypatch.delenv("OCPP_AUTH_REQUIRED", raising=False)
    with TestClient(app).websocket_connect("/ocpp/DEV1?protocol=1.6", subprotocols=["ocpp1.6"]) as ws:
        ws.send_text(json.dumps([2, "1", "Heartbeat", {}]))
        assert json.loads(ws.receive_text())[0] == 3


def test_connection_cap_rejects_new_charge_point_but_allows_known(monkeypatch):
    monkeypatch.setenv("OCPP_MAX_CONNECTIONS", "1")
    cs.CHARGE_POINTS["EXIST"] = {"status": "Available"}
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ocpp/NEW1?protocol=1.6", subprotocols=["ocpp1.6"]):
            pass
    with c.websocket_connect("/ocpp/EXIST?protocol=1.6", subprotocols=["ocpp1.6"]) as ws:  # 재접속은 허용
        ws.send_text(json.dumps([2, "1", "Heartbeat", {}]))
        assert json.loads(ws.receive_text())[0] == 3


def test_malformed_frames_do_not_kill_connection():
    with TestClient(app).websocket_connect("/ocpp/DEV2?protocol=1.6", subprotocols=["ocpp1.6"]) as ws:
        ws.send_text("not json")
        ws.send_text(json.dumps({"a": 1}))
        ws.send_text(json.dumps([2, "x"]))
        ws.send_text(json.dumps([2, "1", "Heartbeat", {}]))
        assert json.loads(ws.receive_text())[0] == 3


def test_oversized_frame_closes_connection():
    with TestClient(app).websocket_connect("/ocpp/DEV3?protocol=1.6", subprotocols=["ocpp1.6"]) as ws:
        ws.send_text("x" * (cs.MAX_FRAME_CHARS + 1))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
