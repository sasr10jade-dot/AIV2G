"""개인정보 필드 암호화 — 왕복·변조 탐지·블라인드 인덱스·키 관리."""
import base64

import pytest
from cryptography.fernet import Fernet

from src.privacy import vault


def test_encrypt_decrypt_roundtrip_and_not_plaintext():
    token = vault.encrypt("43오3450")
    assert token.startswith("v1:") and "43오3450" not in token
    assert vault.decrypt(token) == "43오3450"


def test_encryption_is_randomized_but_blind_index_is_deterministic():
    assert vault.encrypt("43오3450") != vault.encrypt("43오3450")       # 같은 값이어도 암호문은 매번 다르다
    assert vault.blind_index("43오3450") == vault.blind_index("43오3450")
    assert vault.blind_index("43오3450") != vault.blind_index("43오3451")
    assert vault.blind_index("43오3450", "plate") != vault.blind_index("43오3450", "other")
    assert "43오3450" not in vault.blind_index("43오3450")


def test_tampered_or_foreign_values_rejected():
    token = vault.encrypt("43오3450")
    with pytest.raises(vault.VaultError):
        vault.decrypt(token[:-4] + "AAAA")
    with pytest.raises(vault.VaultError):
        vault.decrypt("not-a-token")
    with pytest.raises(vault.VaultError):
        vault.decrypt("")


def test_empty_values_rejected():
    for fn in (vault.encrypt, vault.blind_index):
        with pytest.raises(vault.VaultError):
            fn("")


def test_different_key_cannot_decrypt(monkeypatch):
    token = vault.encrypt("43오3450")
    monkeypatch.setenv("PII_KEY", Fernet.generate_key().decode())
    with pytest.raises(vault.VaultError):
        vault.decrypt(token)
    assert vault.blind_index("43오3450") != ""   # 새 키로는 다른 해시가 나온다(키 없이 대조 불가)


def test_env_key_takes_precedence_and_bad_key_is_reported(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PII_KEY", key)
    token = vault.encrypt("abc")
    assert Fernet(key.encode()).decrypt(token[3:].encode()).decode() == "abc"
    monkeypatch.setenv("PII_KEY", "too-short")
    with pytest.raises(vault.VaultError):
        vault.encrypt("abc")


def test_key_file_autocreated_once_and_stable():
    assert not vault.KEY_FILE.exists()
    first = vault.encrypt("x")
    assert vault.KEY_FILE.exists()
    raw = vault.KEY_FILE.read_text(encoding="utf-8").strip()
    assert len(base64.urlsafe_b64decode(raw)) == 32
    assert vault.decrypt(first) == "x"
    vault.encrypt("y")
    assert vault.KEY_FILE.read_text(encoding="utf-8").strip() == raw   # 재생성되지 않는다
