"""Режим «просмотр как у клиента» для владельца платформы (сессия)."""

from __future__ import annotations

from starlette.requests import Request

SESSION_KEY = "fb_preview_as_user"


def preview_as_user_active(request: Request) -> bool:
    try:
        return request.session.get(SESSION_KEY) == "1"
    except Exception:
        return False


def set_preview_as_user(request: Request, enabled: bool) -> None:
    if enabled:
        request.session[SESSION_KEY] = "1"
    else:
        request.session.pop(SESSION_KEY, None)
