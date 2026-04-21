"""Вход в кабинет через Supabase Auth (email/password, OAuth через PKCE на клиенте)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy.orm import Session

from backend.services.cabinet_settings import effective_supabase_anon_key, effective_supabase_url

logger = logging.getLogger(__name__)


def supabase_auth_configured(db: Session) -> bool:
    u = effective_supabase_url(db).strip()
    k = effective_supabase_anon_key(db).strip()
    return bool(u and k)


def _base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    return u


async def sign_in_with_password(db: Session, *, email: str, password: str) -> tuple[str | None, str | None]:
    """
    POST /auth/v1/token grant_type=password.
    Возвращает (access_token, error_message).
    """
    base = _base(effective_supabase_url(db))
    anon = effective_supabase_anon_key(db).strip()
    if not base or not anon:
        return None, "Supabase не настроен (URL и anon key)."

    email = (email or "").strip().lower()
    if not email or not (password or "").strip():
        return None, "Укажите email и пароль."

    url = urljoin(base + "/", "auth/v1/token?grant_type=password")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url,
                headers={
                    "apikey": anon,
                    "Content-Type": "application/json",
                },
                json={"email": email, "password": password},
            )
    except httpx.RequestError as e:
        logger.warning("Supabase password sign-in network error: %s", e)
        return None, "Сеть: не удалось связаться с Supabase."

    if r.status_code != 200:
        try:
            body = r.json()
            msg = (body.get("error_description") or body.get("msg") or body.get("error") or r.text)[:500]
        except Exception:
            msg = r.text[:500] if r.text else r.reason_phrase
        return None, str(msg) or "Ошибка входа Supabase."

    data = r.json()
    token = (data.get("access_token") or "").strip()
    if not token:
        return None, "Supabase не вернул access_token."
    return token, None


async def fetch_supabase_user(db: Session, access_token: str) -> dict[str, Any] | None:
    """GET /auth/v1/user — проверка токена и данные пользователя."""
    base = _base(effective_supabase_url(db))
    anon = effective_supabase_anon_key(db).strip()
    token = (access_token or "").strip()
    if not base or not anon or not token:
        return None

    url = urljoin(base + "/", "auth/v1/user")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                url,
                headers={
                    "apikey": anon,
                    "Authorization": f"Bearer {token}",
                },
            )
    except httpx.RequestError as e:
        logger.warning("Supabase /user error: %s", e)
        return None

    if r.status_code != 200:
        try:
            snippet = (r.text or "")[:200]
        except Exception:
            snippet = ""
        logger.warning("Supabase GET /auth/v1/user → %s %s", r.status_code, snippet)
        return None
    try:
        return dict(r.json())
    except Exception:
        return None


def supabase_oauth_redirect_url(request_base_url: str, db: Session) -> str:
    """Абсолютный URL callback после OAuth (должен быть в Redirect URLs в Supabase)."""
    from backend.services.cabinet_settings import effective_oauth_redirect_base

    base = effective_oauth_redirect_base(db).strip()
    if base:
        return base.rstrip("/") + "/auth/supabase/callback"
    return str(request_base_url).rstrip("/") + "/auth/supabase/callback"


