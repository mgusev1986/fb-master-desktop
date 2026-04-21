"""Секретная страница с паролем перед веб-входом OAuth (/auth/login и т.д.). /auth/unlock — публично для клиентов с ключом."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request

# Ключ сессии после успешного ввода пароля на /gate/<slug>
WEB_CABINET_GATE_SESSION_KEY = "web_cabinet_gate_ok"


def allow_unlock_path_without_gate_session(_request: Request, path: str, method: str) -> bool:
    """
    /auth/unlock и POST /auth/access-key/activate — всегда без /gate (любой браузер).
    Клиенты с оплаченным ключом открывают публичный URL; gate скрывает только /auth/login и OAuth.
    """
    m = (method or "").upper()
    if path == "/auth/access-key/activate" and m != "POST":
        return False
    return path in ("/auth/unlock", "/auth/access-key/activate")


def cabinet_auth_entry_requires_gate_pass(path: str, method: str) -> bool:
    """Пути, для которых без отметки в сессии отдаём 404 (скрываем факт наличия входа)."""
    m = (method or "").upper()
    if path == "/auth/login":
        return True
    if path == "/auth/google":
        return True
    if path == "/auth/dev-login":
        return True
    if path.startswith("/auth/supabase/oauth-page/"):
        return True
    if path == "/auth/supabase/password" and m == "POST":
        return True
    if path == "/auth/supabase/session" and m == "POST":
        return True
    return False


def user_agent_desktop_cabinet_bypass(request: Request) -> bool:
    """Десктоп Electron помечает User-Agent — не требуем веб-пароль (локальный бэкенд / особый режим)."""
    ua = (request.headers.get("user-agent") or "").lower().replace(" ", "")
    return "fbmasterdesktop" in ua


def verify_cabinet_gate_password(given: str, expected: str) -> bool:
    """Сравнение паролей без утечки по длине (через SHA-256)."""
    g = (given or "").encode("utf-8")
    e = (expected or "").encode("utf-8")
    return hmac.compare_digest(
        hashlib.sha256(g).digest(),
        hashlib.sha256(e).digest(),
    )
