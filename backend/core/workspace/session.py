"""Session state helpers для текущего workspace.

Хранится в cookie-сессии как строка (module_id). Отсутствие ключа
допустимо — пользователь ещё не выбирал workspace либо работает
в legacy-режиме с выключенным multi_workspace_enabled().
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

WORKSPACE_SESSION_KEY = "fbm_current_workspace"


def get_current_workspace_id(request: "Request") -> str | None:
    """Прочитать выбранный workspace из сессии."""
    try:
        raw = request.session.get(WORKSPACE_SESSION_KEY)
    except Exception:
        return None
    if not raw or not isinstance(raw, str):
        return None
    return raw.strip() or None


def set_current_workspace_id(request: "Request", module_id: str) -> None:
    """Записать выбранный workspace в сессию."""
    if not module_id or not isinstance(module_id, str):
        raise ValueError("module_id must be non-empty str")
    request.session[WORKSPACE_SESSION_KEY] = module_id.strip()


def clear_current_workspace(request: "Request") -> None:
    """Сбросить выбранный workspace (например, на logout)."""
    try:
        request.session.pop(WORKSPACE_SESSION_KEY, None)
    except Exception:
        pass


__all__ = [
    "WORKSPACE_SESSION_KEY",
    "clear_current_workspace",
    "get_current_workspace_id",
    "set_current_workspace_id",
]
