"""Хеширование ключей доступа для проверки. Plaintext дополнительно хранится в БД в зашифрованном виде (Fernet, см. AccessKey.key_plain_enc) для показа владельцу платформы."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from backend.config import SECRET_KEY


def _salt() -> bytes:
    raw = (SECRET_KEY or "dev") + "fbm_access_key_v1"
    return hashlib.sha256(raw.encode("utf-8")).digest()[:16]


def hash_access_key(plaintext: str) -> str:
    k = (plaintext or "").strip()
    if not k:
        return ""
    dk = hashlib.pbkdf2_hmac("sha256", k.encode("utf-8"), _salt(), 120_000)
    return dk.hex()


def verify_access_key(plaintext: str, stored_hex: str) -> bool:
    if not plaintext or not stored_hex:
        return False
    calc = hash_access_key(plaintext)
    return hmac.compare_digest(calc, stored_hex)


def generate_plaintext_key() -> str:
    """Человекочитаемый ключ для выдачи клиенту (показывается один раз)."""
    return "FBM-" + secrets.token_urlsafe(18).replace("-", "")[:24]
