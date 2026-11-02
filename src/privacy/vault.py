"""개인정보 필드 암호화(번호판 등) — 저장은 암호문, 중복 확인은 복호화 없이 블라인드 인덱스로.

- encrypt/decrypt: Fernet(AES-128-CBC + HMAC). 저장 값은 "v1:" 접두 + 토큰이라 키를 교체(rotation)할 때
  버전을 구분할 수 있다.
- blind_index: HMAC-SHA256(키, 값) — 같은 번호판이면 같은 값이라 유니크 인덱스·중복 확인에 쓰지만, 값을
  알아내려면 키가 필요하다(키 없이 번호판 후보를 대입해 비교할 수 없다).
- 키: 환경변수 PII_KEY(운영 권장, 32바이트 urlsafe base64) 또는 data/.pii_key(없으면 최초 1회 자동 생성, git 제외).
  키를 잃으면 암호화된 값을 복구할 수 없으므로 운영에서는 키를 별도로 백업·보관해야 한다.

DB 파일이 유출돼도 키가 없으면 번호판을 읽을 수 없게 하려는 안전조치다. 키는 DB와 다른 곳에 둘 것.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from src.paths import DATA_DIR

KEY_FILE = DATA_DIR / ".pii_key"
_PREFIX = "v1:"


class VaultError(ValueError):
    pass


def _raw_key() -> bytes:
    env = os.environ.get("PII_KEY")
    if env:
        return env.encode("utf-8")
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"), encoding="utf-8")
    return KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


def _fernet() -> Fernet:
    try:
        return Fernet(_raw_key())
    except (ValueError, TypeError) as exc:
        raise VaultError("PII_KEY 형식이 올바르지 않습니다(32바이트 urlsafe base64)") from exc


def encrypt(plaintext: str) -> str:
    if plaintext is None or plaintext == "":
        raise VaultError("빈 값은 암호화할 수 없습니다")
    return _PREFIX + _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(stored: str) -> str:
    if not stored or not stored.startswith(_PREFIX):
        raise VaultError("알 수 없는 암호문 형식입니다")
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise VaultError("복호화에 실패했습니다(키가 다르거나 값이 손상됨)") from exc


def blind_index(value: str, purpose: str = "plate") -> str:
    """값 자체를 드러내지 않는 결정적 식별자. purpose가 다르면 같은 값이라도 다른 해시가 된다."""
    if not value:
        raise VaultError("빈 값은 색인할 수 없습니다")
    digest = hmac.new(_raw_key(), f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    return digest
