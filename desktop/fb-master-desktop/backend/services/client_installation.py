"""Уникальный ID экземпляра программы (один файл в DATA_DIR на установку)."""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from backend.config import DATA_DIR

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHED: str | None = None

_INSTALL_FILENAME = "client_installation_id"


def get_client_installation_id() -> str:
    """
    UUID v4, создаётся при первом обращении и сохраняется в DATA_DIR.
    Один и тот же на всех запросах этого экземпляра приложения.
    """
    global _CACHED
    if _CACHED:
        return _CACHED
    with _LOCK:
        if _CACHED:
            return _CACHED
        path: Path = DATA_DIR / _INSTALL_FILENAME
        try:
            if path.is_file():
                raw = path.read_text(encoding="utf-8").strip()
                if len(raw) >= 32:
                    _CACHED = raw[:40]
                    return _CACHED
        except OSError as e:
            logger.warning("Не удалось прочитать %s: %s", path, e)
        new_id = str(uuid.uuid4())
        try:
            path.write_text(new_id + "\n", encoding="utf-8")
        except OSError as e:
            logger.error("Не удалось сохранить installation id в %s: %s", path, e)
        _CACHED = new_id
        return _CACHED
