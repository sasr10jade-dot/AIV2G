"""OCPP 접속 보안 — 충전기 인증(Security Profile 1: HTTP Basic)과 접속 한도.

운영 전환 시 OCPP_AUTH_REQUIRED=1을 켜면, 충전기는 WebSocket 접속 때 `Authorization: Basic
base64(<충전기ID>:<비밀번호>)` 헤더를 보내야 한다. 비밀번호는 bcrypt 해시로만 저장한다
(data/ocpp_credentials.json: {"CP-001": "<bcrypt 해시>"}). 등록되지 않은 충전기, 틀린 비밀번호,
헤더 없음은 모두 접속 수락 전에 거절한다(어떤 프레임도 처리하지 않는다).

이것은 Security Profile 1(전송 구간 평문 + 기본 인증)이다 — 운영 구간에서는 반드시 TLS(wss://)를
리버스 프록시에서 종단해야 하며(Profile 2), 클라이언트 인증서(Profile 3)는 충전사업자와 협의 후
적용한다. 자세한 내용은 docs/ocpp_security.md.
"""
from __future__ import annotations

import base64
import binascii
import json
import os

import bcrypt

from src.paths import DATA_DIR

CREDENTIALS_FILE = DATA_DIR / "ocpp_credentials.json"
DEFAULT_MAX_CONNECTIONS = 400  # 목표 150기 + 에뮬레이터·재접속 여유


def auth_required() -> bool:
    return os.environ.get("OCPP_AUTH_REQUIRED") == "1"


def max_connections() -> int:
    try:
        return int(os.environ.get("OCPP_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS))
    except ValueError:
        return DEFAULT_MAX_CONNECTIONS


def _load() -> dict:
    if not CREDENTIALS_FILE.exists():
        return {}
    return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))


def set_credential(cp_id: str, password: str) -> None:
    """충전기 비밀번호를 (재)설정한다. 평문은 저장하지 않는다."""
    if len(password) < 16:
        raise ValueError("충전기 비밀번호는 16자 이상이어야 합니다(OCPP 보안 권고)")
    creds = _load()
    creds[cp_id] = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")


def revoke_credential(cp_id: str) -> bool:
    creds = _load()
    if cp_id not in creds:
        return False
    del creds[cp_id]
    CREDENTIALS_FILE.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def verify_basic(authorization: str | None, cp_id: str) -> bool:
    """Authorization 헤더가 해당 충전기의 등록 자격과 일치하는지. 사용자명은 URL의 충전기 ID와
    같아야 한다(다른 충전기 이름으로 접속 불가)."""
    if not authorization or not authorization.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    if not sep or user != cp_id:
        return False
    stored = _load().get(cp_id)
    # 미등록 충전기도 같은 비용으로 비교해 응답 시간 차이로 등록 여부가 드러나지 않게 한다
    hashed = (stored or bcrypt.hashpw(b"dummy", bcrypt.gensalt()).decode("utf-8")).encode("utf-8")
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), hashed)
    except ValueError:
        return False
    return bool(stored) and ok
