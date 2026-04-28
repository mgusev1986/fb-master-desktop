"""Ed25519 подписи для системы лицензирования (v3.0+).

Архитектура:
- Приватный ключ (Ed25519, 32 байта) — ТОЛЬКО на VPS, загружается через
  env `FB_MASTER_LICENSE_PRIVATE_KEY_PATH`. Подписывает license_state и
  daily probes в эндпоинте /api/public/desktop-license/check.
- Публичный ключ (Ed25519, 32 байта) — встроен в Electron-bundle
  (`desktop/fb-master-desktop/license-public.pem`). Клиент проверяет
  подпись локально без сети.

Защита от подмены:
- Подделать новую подпись без приватного ключа криптографически невозможно.
- Локальная подмена license_state в БД → подпись не сходится → блок.
- Mock-VPS (через hosts) подписать тоже не может → ConnectError +
  fallback на встроенные probes.
"""

from __future__ import annotations

import base64
import logging
import os
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _private_key_path() -> Path:
    """Путь к приватному ключу. Только VPS должен его иметь."""
    env = os.environ.get("FB_MASTER_LICENSE_PRIVATE_KEY_PATH", "").strip()
    if env:
        return Path(env)
    # Локальная разработка / dev-среда:
    return _project_root() / "secrets" / "license-private.pem"


def _public_key_path() -> Path:
    """Путь к публичному ключу. Должен быть везде где проверяем подпись."""
    env = os.environ.get("FB_MASTER_LICENSE_PUBLIC_KEY_PATH", "").strip()
    if env:
        return Path(env)
    # Bundled с Electron-десктопом:
    candidates = [
        _project_root() / "desktop" / "fb-master-desktop" / "license-public.pem",
        # В Electron-bundle (resources-fb-master-backend/) ключ копируется
        # рядом с Python-кодом скриптом prepare-desktop-backend-bundle.sh:
        _project_root() / "license-public.pem",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # вернём первый, чтобы ошибка была явной


@lru_cache(maxsize=1)
def _load_private_key() -> Ed25519PrivateKey | None:
    """Загружает приватный ключ из PEM. None если файла нет (на клиенте)."""
    path = _private_key_path()
    if not path.exists():
        logger.debug("license private key not found at %s (ok on client)", path)
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
        key = serialization.load_pem_private_key(data, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError(f"expected Ed25519, got {type(key).__name__}")
        return key
    except Exception:  # noqa: BLE001
        logger.exception("license private key load failed (path=%s)", path)
        return None


@lru_cache(maxsize=1)
def _load_public_key() -> Ed25519PublicKey | None:
    """Загружает публичный ключ из PEM. Должен быть всегда."""
    path = _public_key_path()
    if not path.exists():
        logger.warning("license public key not found at %s", path)
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
        key = serialization.load_pem_public_key(data)
        if not isinstance(key, Ed25519PublicKey):
            raise TypeError(f"expected Ed25519, got {type(key).__name__}")
        return key
    except Exception:  # noqa: BLE001
        logger.exception("license public key load failed (path=%s)", path)
        return None


def has_private_key() -> bool:
    """True если на этой машине есть приватный ключ (= VPS)."""
    return _load_private_key() is not None


def has_public_key() -> bool:
    return _load_public_key() is not None


def sign_b64(payload: bytes) -> str | None:
    """Подписать байты приватным ключом → base64-строка. None если ключа нет."""
    priv = _load_private_key()
    if priv is None:
        return None
    sig = priv.sign(payload)
    return base64.b64encode(sig).decode("ascii")


def verify_b64(payload: bytes, signature_b64: str) -> bool:
    """Проверить base64-подпись публичным ключом."""
    pub = _load_public_key()
    if pub is None:
        return False
    try:
        sig = base64.b64decode(signature_b64.encode("ascii"))
    except Exception:  # noqa: BLE001
        return False
    try:
        pub.verify(sig, payload)
        return True
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001
        logger.exception("verify_b64: unexpected error")
        return False


__all__ = [
    "has_private_key",
    "has_public_key",
    "sign_b64",
    "verify_b64",
]
