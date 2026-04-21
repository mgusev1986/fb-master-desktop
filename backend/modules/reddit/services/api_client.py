"""Тонкий Reddit API клиент поверх httpx.

Один экземпляр на запрос: получает валидный access_token (при необходимости
обновляет через refresh) и делает API-вызовы. Все ошибки нормализуются
в `RedditApiError` с нужным reason_code.

Не используем PRAW (лишняя зависимость и меньше контроля над rate-limit
обработкой). Reddit API простое — json over https.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from backend.modules.reddit.config import get_user_agent
from backend.modules.reddit.services.oauth import RedditOAuthError, refresh_access_token

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.modules.reddit.models import RedditAccount


REDDIT_OAUTH_BASE = "https://oauth.reddit.com"


@dataclass(frozen=True)
class RedditApiResponse:
    status: int
    data: Any
    rate_limit_used: float | None
    rate_limit_remaining: float | None
    rate_limit_reset_at: datetime | None


class RedditApiError(RuntimeError):
    def __init__(self, message: str, reason_code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


def _decrypt_refresh(account: "RedditAccount") -> str | None:
    from backend.services.fb_credentials_crypto import decrypt_secret

    return decrypt_secret(account.oauth_refresh_token_enc) if account.oauth_refresh_token_enc else None


def _ensure_access_token(db: "Session", account: "RedditAccount") -> str:
    """Вернуть живой access_token; обновить через refresh при истечении."""
    from backend.services.fb_credentials_crypto import encrypt_secret

    now = datetime.now(timezone.utc)
    # access_token мы не храним в БД (только refresh) — каждый раз refresh при необходимости
    # Чтобы не рефрешить на каждый запрос, кладём последний access в атрибут in-memory runtime
    cached = getattr(account, "_runtime_access_token", None)
    cached_at: datetime | None = getattr(account, "_runtime_access_expires_at", None)
    if cached and cached_at and cached_at > now:
        return cached

    refresh = _decrypt_refresh(account)
    if not refresh:
        raise RedditApiError(
            "no refresh token stored for account",
            reason_code="oauth_missing_refresh",
            status=401,
        )
    try:
        tokens = refresh_access_token(db, refresh)
    except RedditOAuthError as exc:
        raise RedditApiError(str(exc), reason_code="oauth_refresh_failed", status=401) from exc

    # обновить refresh, если reddit вернул новый
    if tokens.refresh_token and tokens.refresh_token != refresh:
        account.oauth_refresh_token_enc = encrypt_secret(tokens.refresh_token)
    account.oauth_expires_at = tokens.expires_at
    account.oauth_scope = tokens.scope or account.oauth_scope

    # кэшируем в memory атрибутах (sqlalchemy transient-атрибут — не мешает ORM)
    object.__setattr__(account, "_runtime_access_token", tokens.access_token)
    object.__setattr__(account, "_runtime_access_expires_at", tokens.expires_at)
    return tokens.access_token


def _parse_rate_limit_headers(resp: httpx.Response) -> tuple[float | None, float | None, datetime | None]:
    try:
        used = float(resp.headers.get("x-ratelimit-used") or "") if resp.headers.get("x-ratelimit-used") else None
    except (TypeError, ValueError):
        used = None
    try:
        remaining = float(resp.headers.get("x-ratelimit-remaining") or "") if resp.headers.get("x-ratelimit-remaining") else None
    except (TypeError, ValueError):
        remaining = None
    reset_at: datetime | None = None
    try:
        reset_s = resp.headers.get("x-ratelimit-reset")
        if reset_s:
            secs = int(float(reset_s))
            reset_at = datetime.now(timezone.utc).replace(microsecond=0)
            # reddit отдаёт секунды до сброса
            from datetime import timedelta
            reset_at = reset_at + timedelta(seconds=secs)
    except (TypeError, ValueError):
        reset_at = None
    return used, remaining, reset_at


def api_call(
    db: "Session",
    account: "RedditAccount",
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> RedditApiResponse:
    """Сделать запрос к Reddit OAuth API."""
    token = _ensure_access_token(db, account)
    user_agent = get_user_agent(db)

    url = path if path.startswith("http") else REDDIT_OAUTH_BASE + (path if path.startswith("/") else "/" + path)
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent,
    }
    try:
        resp = httpx.request(
            method.upper(),
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise RedditApiError(f"http error: {exc!r}", reason_code="network_error") from exc

    used, remaining, reset_at = _parse_rate_limit_headers(resp)
    # Записать в RateLimitState (best-effort)
    try:
        _record_rate_limit(db, account, used, remaining, reset_at)
    except Exception:
        pass

    if resp.status_code == 401:
        raise RedditApiError("unauthorized", reason_code="oauth_unauthorized", status=401)
    if resp.status_code == 403:
        raise RedditApiError(f"forbidden: {resp.text[:200]}", reason_code="forbidden", status=403)
    if resp.status_code == 429:
        raise RedditApiError("rate limited", reason_code="rate_limited", status=429)
    if resp.status_code >= 400:
        raise RedditApiError(
            f"reddit API HTTP {resp.status_code}: {resp.text[:200]}",
            reason_code="api_error",
            status=resp.status_code,
        )

    try:
        payload: Any = resp.json() if resp.content else None
    except Exception:
        payload = resp.text

    return RedditApiResponse(
        status=resp.status_code,
        data=payload,
        rate_limit_used=used,
        rate_limit_remaining=remaining,
        rate_limit_reset_at=reset_at,
    )


def _record_rate_limit(
    db: "Session",
    account: "RedditAccount",
    used: float | None,
    remaining: float | None,
    reset_at: datetime | None,
) -> None:
    if remaining is None and used is None and reset_at is None:
        return
    from backend.modules.reddit.models import RedditRateLimitState

    row = (
        db.query(RedditRateLimitState)
        .filter(RedditRateLimitState.account_id == account.id, RedditRateLimitState.bucket == "api")
        .one_or_none()
    )
    if row is None:
        row = RedditRateLimitState(account_id=account.id, bucket="api")
        db.add(row)
    row.remaining = int(remaining) if remaining is not None else None
    row.reset_at = reset_at
    row.observed_at = datetime.now(timezone.utc)


__all__ = [
    "REDDIT_OAUTH_BASE",
    "RedditApiError",
    "RedditApiResponse",
    "api_call",
]
