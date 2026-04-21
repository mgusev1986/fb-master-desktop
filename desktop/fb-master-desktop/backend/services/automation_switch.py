"""Глобальный выключатель фоновой Playwright-автоматизации на хосте (например VPS)."""

from __future__ import annotations

import os


def playwright_workers_enabled() -> bool:
    """
    False, если задано FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS=1 — сценарии/прогрев/парсер
    на этом сервере не стартуют (норма для облака, когда работают только десктопы с локальным бэкендом).
    """
    return os.getenv("FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS", "").lower() not in ("1", "true", "yes")
