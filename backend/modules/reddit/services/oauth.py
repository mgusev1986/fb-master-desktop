"""Reddit OAuth2 authorization-code flow helpers.

Поток:
  1) Пользователь жмёт «Подключить» → `build_authorize_url(state, scope)`.
  2) Откроется Reddit OAuth страница; после approve — redirect на
     `redirect_uri` с `code` и `state`.
  3) `exchange_code(code)` возвращает access/refresh tokens + expires_in.
  4) Refresh через `refresh_access_token(refresh_token)`.

Refresh-токены Reddit живут долго (permanent при `duration=permanent`);
шифруются через `backend.services.fb_credentials_crypto` и сохраняются в
`RedditAccount.oauth_refresh_token_enc`.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx

from backend.modules.reddit.config import (
    get_oauth_client_id,
    get_oauth_client_secret,
    get_oauth_redirect_uri,
    get_user_agent,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


REDDIT_AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# Минимально необходимые scope'ы. Расширяются в M5.5/5.7 по мере надобности.
DEFAULT_SCOPES = (
    "identity",
    "read",
    "mysubreddits",
    "history",
    "privatemessages",
    "submit",
    "edit",
    "vote",
    "save",
)


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    token_type: str
    scope: str
    expires_at: datetime
    raw: dict


class RedditOAuthError(RuntimeError):
    """Ошибки OAuth flow (неверные ключи, отказ пользователя, сетевые)."""


def new_state() -> str:
    """CSRF-защита state-parameter."""
    return secrets.token_urlsafe(24)


def build_authorize_url(
    db: "Session",
    *,
    state: str,
    scope: tuple[str, ...] = DEFAULT_SCOPES,
    duration: str = "permanent",
) -> str:
    """Сформировать URL авторизации Reddit."""
    client_id = get_oauth_client_id(db)
    redirect_uri = get_oauth_redirect_uri(db)
    if not client_id:
        raise RedditOAuthError("OAuth client_id не задан — заполните настройки Reddit.")
    if not redirect_uri:
        raise RedditOAuthError("OAuth redirect_uri пуст — проверьте настройки.")

    params = {
        "client_id": client_id,
        "response_type": "code",
        "state": state,
        "redirect_uri": redirect_uri,
        "duration": duration,  # permanent | temporary
        "scope": " ".join(scope),
    }
    return f"{REDDIT_AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    b = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(b).decode("ascii")


def _parse_tokens(payload: dict) -> OAuthTokens:
    expires_in = int(payload.get("expires_in", 3600) or 3600)
    return OAuthTokens(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=payload.get("refresh_token"),
        token_type=str(payload.get("token_type") or "bearer"),
        scope=str(payload.get("scope") or ""),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in - 30),
        raw=dict(payload),
    )


def exchange_code(db: "Session", code: str) -> OAuthTokens:
    """Обменять authorization-code на access+refresh токены."""
    client_id = get_oauth_client_id(db)
    client_secret = get_oauth_client_secret(db)
    redirect_uri = get_oauth_redirect_uri(db)
    user_agent = get_user_agent(db)
    if not (client_id and client_secret):
        raise RedditOAuthError("OAuth creds не заданы")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {
        "Authorization": _basic_auth_header(client_id, client_secret),
        "User-Agent": user_agent,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = httpx.post(REDDIT_TOKEN_URL, data=data, headers=headers, timeout=20.0)
    except httpx.HTTPError as exc:
        raise RedditOAuthError(f"reddit token request failed: {exc!r}") from exc

    if resp.status_code != 200:
        raise RedditOAuthError(f"reddit token HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    if "access_token" not in payload:
        raise RedditOAuthError(f"reddit token response lacks access_token: {payload!r}")
    return _parse_tokens(payload)


def refresh_access_token(db: "Session", refresh_token: str) -> OAuthTokens:
    """Обновить access_token через refresh_token."""
    client_id = get_oauth_client_id(db)
    client_secret = get_oauth_client_secret(db)
    user_agent = get_user_agent(db)
    if not (client_id and client_secret):
        raise RedditOAuthError("OAuth creds не заданы")
    if not refresh_token:
        raise RedditOAuthError("refresh_token empty")

    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    headers = {
        "Authorization": _basic_auth_header(client_id, client_secret),
        "User-Agent": user_agent,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = httpx.post(REDDIT_TOKEN_URL, data=data, headers=headers, timeout=20.0)
    except httpx.HTTPError as exc:
        raise RedditOAuthError(f"reddit refresh failed: {exc!r}") from exc
    if resp.status_code != 200:
        raise RedditOAuthError(f"reddit refresh HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    if "access_token" not in payload:
        raise RedditOAuthError(f"reddit refresh response lacks access_token: {payload!r}")
    # refresh-ответ может не вернуть новый refresh_token — оставляем старый
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh_token
    return _parse_tokens(payload)


__all__ = [
    "DEFAULT_SCOPES",
    "OAuthTokens",
    "REDDIT_AUTHORIZE_URL",
    "REDDIT_TOKEN_URL",
    "RedditOAuthError",
    "build_authorize_url",
    "exchange_code",
    "new_state",
    "refresh_access_token",
]
