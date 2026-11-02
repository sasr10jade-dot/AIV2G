"""가명 ID 생성 — 번호판·차량ID·사용자ID를 외부 공유·LLM 입력용 식별자로 바꾼다.

HMAC-SHA256(비밀키, 종류:값)의 앞 12자리를 쓴다. 같은 입력은 항상 같은 가명이라 데이터
연결(조인)은 유지되고, 비밀키 없이는 역산할 수 없다. 키를 바꾸면 가명 전체가 바뀐다
(재식별 위험이 생기면 키 교체로 기존 공유본과 단절할 수 있다).

키 우선순위: 환경변수 PSEUDO_KEY -> data/.pseudo_key(없으면 최초 호출 시 무작위 생성).
키 파일은 저장소에 커밋하지 않는다 — 운영에서는 환경변수로 주입을 권장한다.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from src.paths import DATA_DIR

KEY_FILE = DATA_DIR / ".pseudo_key"
_PREFIX = {"plate": "PV", "ev": "PE", "user": "PU"}


def _key() -> bytes:
    env = os.environ.get("PSEUDO_KEY")
    if env:
        return env.encode("utf-8")
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


def pseudo_id(kind: str, value: str) -> str:
    if kind not in _PREFIX:
        raise ValueError(f"알 수 없는 가명 종류: {kind}")
    if not value:
        raise ValueError("빈 값은 가명화할 수 없습니다")
    digest = hmac.new(_key(), f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_PREFIX[kind]}-{digest[:12]}"


def mask_plate(plate: str) -> str:
    """화면·로그 표시용 부분 마스킹: 43오3450 -> 43오**50. 앞 3자, 뒤 2자만 남긴다."""
    p = (plate or "").strip()
    if len(p) <= 5:
        return "*" * len(p)
    return p[:3] + "*" * (len(p) - 5) + p[-2:]
