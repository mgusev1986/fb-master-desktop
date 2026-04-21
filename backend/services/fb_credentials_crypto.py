"""Шифрование пароля и секрета TOTP для FB-аккаунтов (Fernet, ключ из SECRET_KEY)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from backend.config import SECRET_KEY


def _fernet() -> Fernet:
    raw = (SECRET_KEY or "dev-secret-change-me").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_secret(plain: str | None) -> str | None:
    if plain is None or not str(plain).strip():
        return None
    return _fernet().encrypt(str(plain).strip().encode("utf-8")).decode("ascii")


def decrypt_secret(token: str | None) -> str | None:
    if not token or not str(token).strip():
        return None
    try:
        return _fernet().decrypt(str(token).strip().encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None
