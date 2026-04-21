"""Accounts domain: CRUD + подключение/отключение + readiness.

Поддерживаются ДВА режима:
  * `auth_mode = "oauth"` — официальный OAuth2 flow (исходный, сохранён).
  * `auth_mode = "browser_profile"` — Chromium + cookies (как FB / LinkedIn Master).

Для browser-mode импорт идёт через `import_browser_accounts_from_text`,
для OAuth — через `create_account_from_tokens` (исходный flow).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import RedditAccount, RedditReadinessProbe
from backend.modules.reddit.services.api_client import RedditApiError, api_call
from backend.modules.reddit.services.cookie_import_browser import (
    ParsedRedditAccount,
    parse_text as _parse_browser_text,
)
from backend.modules.reddit.services.oauth import OAuthTokens

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def list_accounts(db: "Session", organization_id: int | None) -> list[RedditAccount]:
    q = db.query(RedditAccount).order_by(RedditAccount.id.asc())
    if organization_id is not None:
        q = q.filter(
            (RedditAccount.organization_id == organization_id)
            | (RedditAccount.organization_id.is_(None))
        )
    return q.all()


def get_account(db: "Session", account_id: int) -> RedditAccount | None:
    return db.get(RedditAccount, int(account_id))


def create_account_from_tokens(
    db: "Session",
    organization_id: int | None,
    tokens: OAuthTokens,
    label_hint: str | None = None,
) -> RedditAccount:
    """Создать (или переиспользовать) RedditAccount из свежих OAuth tokens.

    Сразу вызывает /api/v1/me, чтобы получить username, karma и т.п.
    """
    from backend.services.fb_credentials_crypto import encrypt_secret

    # Предварительно создаём запись (чтобы api_call мог её использовать).
    acc = RedditAccount(
        organization_id=organization_id,
        label=(label_hint or "").strip() or "Reddit account",
        oauth_refresh_token_enc=encrypt_secret(tokens.refresh_token) if tokens.refresh_token else None,
        oauth_scope=tokens.scope,
        oauth_expires_at=tokens.expires_at,
        status="connected",
    )
    # Кешируем access-token in-memory, чтобы api_call не шёл на refresh сразу
    object.__setattr__(acc, "_runtime_access_token", tokens.access_token)
    object.__setattr__(acc, "_runtime_access_expires_at", tokens.expires_at)
    db.add(acc)
    db.flush()  # нужен id

    try:
        me = api_call(db, acc, "GET", "/api/v1/me")
    except RedditApiError as exc:
        acc.status = "needs_reauth"
        acc.last_error = str(exc)
        db.commit()
        return acc

    if isinstance(me.data, dict):
        _apply_me_payload(acc, me.data)

    # Дедупликация по username
    if acc.username_snapshot:
        existing = (
            db.query(RedditAccount)
            .filter(
                RedditAccount.id != acc.id,
                RedditAccount.username_snapshot == acc.username_snapshot,
                (RedditAccount.organization_id == organization_id)
                | (RedditAccount.organization_id.is_(None)),
            )
            .one_or_none()
        )
        if existing is not None:
            # Обновляем refresh-token у существующего, удаляем дубль
            existing.oauth_refresh_token_enc = acc.oauth_refresh_token_enc
            existing.oauth_scope = acc.oauth_scope
            existing.oauth_expires_at = acc.oauth_expires_at
            existing.status = "connected"
            existing.last_error = None
            _apply_me_payload(existing, me.data)
            db.delete(acc)
            db.commit()
            return existing

    db.commit()
    return acc


def _apply_me_payload(acc: RedditAccount, me: dict[str, Any]) -> None:
    acc.username_snapshot = me.get("name") or acc.username_snapshot
    acc.reddit_user_id = _prefix_t2(me.get("id") or "") or acc.reddit_user_id
    acc.karma_link = int(me.get("link_karma") or 0) if me.get("link_karma") is not None else acc.karma_link
    acc.karma_comment = int(me.get("comment_karma") or 0) if me.get("comment_karma") is not None else acc.karma_comment
    created = me.get("created_utc")
    if created:
        try:
            acc.account_age_days = max(
                0,
                int(
                    (datetime.now(timezone.utc) - datetime.fromtimestamp(float(created), tz=timezone.utc)).days
                ),
            )
        except (TypeError, ValueError):
            pass
    acc.email_verified = bool(me.get("has_verified_email", acc.email_verified))
    acc.is_suspended = bool(me.get("is_suspended", False))
    acc.is_employee = bool(me.get("is_employee", False))
    caps = acc.capabilities_json or {}
    caps["post"] = True
    caps["comment"] = True
    caps["dm"] = bool(me.get("pref_no_profanity") is not None)  # proxy: у auth'ed user обычно доступно
    caps["chat"] = bool(me.get("accept_chats", True))
    acc.capabilities_json = caps
    acc.status = "connected"
    acc.last_activity_at = datetime.now(timezone.utc)
    acc.last_error = None


def _prefix_t2(rid: str) -> str:
    rid = (rid or "").strip()
    if not rid:
        return rid
    if rid.startswith("t2_"):
        return rid
    return f"t2_{rid}"


def disconnect(db: "Session", account_id: int) -> None:
    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return
    acc.status = "disconnected"
    acc.oauth_refresh_token_enc = None
    acc.oauth_expires_at = None
    db.commit()


def delete_account(db: "Session", account_id: int) -> None:
    """Удалить аккаунт. Связанные records (drafts/queue/conv/messages/sequences/leads)
    обрабатываются Postgres FK через `ondelete="CASCADE" | "SET NULL"`.

    Дополнительно: чистим compliance events / action attempts если они ссылаются
    на acc.id — на старых FK без ondelete.
    """
    from backend.modules.reddit.models import RedditActionAttempt

    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return
    # action attempts могут не иметь ondelete — почистим вручную (если поле есть).
    try:
        if hasattr(RedditActionAttempt, "account_id"):
            db.query(RedditActionAttempt).filter(RedditActionAttempt.account_id == acc.id).delete(synchronize_session=False)
    except Exception:
        db.rollback()
    db.delete(acc)
    db.commit()


# ── Browser-profile mode ───────────────────────────────────


_PROFILE_PREFIX = "reddit"


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-")
    return (s or "account")[:64]


def _make_profile_dir(label: str, account_id: int | None = None) -> str:
    from backend.config import BROWSER_PROFILES_DIR

    base = Path(BROWSER_PROFILES_DIR) / "reddit"
    base.mkdir(parents=True, exist_ok=True)
    suffix = f"-{account_id}" if account_id else ""
    return str(base / f"{_PROFILE_PREFIX}-{_slugify(label)}{suffix}")


def create_browser_account_from_parsed(
    db: "Session",
    organization_id: int | None,
    parsed: ParsedRedditAccount,
) -> RedditAccount:
    """Создать RedditAccount в browser-mode (без OAuth)."""
    label = parsed.label or "reddit-account"
    username = parsed.username
    acc = RedditAccount(
        organization_id=organization_id,
        label=label[:160],
        username_snapshot=username,
        auth_mode="browser_profile",
        profile_dir=_make_profile_dir(label),
        cookies_json=parsed.cookies,
        cookies_imported_at=datetime.now(timezone.utc),
        proxy_enabled=bool(parsed.proxy_url),
        proxy_url=parsed.proxy_url,
        proxy_username=parsed.proxy_username,
        proxy_password=parsed.proxy_password,
        stealth_user_agent=parsed.user_agent,
        status="connected" if parsed.cookies else "needs_attention",
        session_ok=True if parsed.cookies else None,
    )
    db.add(acc)
    db.flush()
    acc.profile_dir = _make_profile_dir(label, acc.id)
    Path(acc.profile_dir).mkdir(parents=True, exist_ok=True)
    db.commit()
    return acc


def import_browser_accounts_from_text(
    db: "Session",
    organization_id: int | None,
    blob: str,
) -> dict[str, Any]:
    """Импорт нескольких browser-аккаунтов одним textarea."""
    parsed_items = _parse_browser_text(blob)
    imported = 0
    errors: list[dict[str, Any]] = []
    for idx, p in enumerate(parsed_items, start=1):
        if p.error:
            errors.append({"line": idx, "label": p.label, "error": p.error})
            continue
        try:
            create_browser_account_from_parsed(db, organization_id, p)
            imported += 1
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errors.append({"line": idx, "label": p.label, "error": str(e)})
    return {"imported": imported, "errors": errors, "total": len(parsed_items)}


def update_proxy(
    db: "Session",
    account_id: int,
    *,
    proxy_url: str | None,
    proxy_username: str | None,
    proxy_password: str | None,
    proxy_enabled: bool | None = None,
) -> RedditAccount | None:
    acc = get_account(db, account_id)
    if acc is None:
        return None
    acc.proxy_url = (proxy_url or "").strip() or None
    acc.proxy_username = (proxy_username or "").strip() or None
    acc.proxy_password = (proxy_password or "").strip() or None
    acc.proxy_enabled = bool(acc.proxy_url) if proxy_enabled is None else bool(proxy_enabled)
    db.commit()
    return acc


def refresh_probe(db: "Session", account_id: int) -> RedditAccount | None:
    """Перезабрать /api/v1/me и записать readiness-probe."""
    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return None
    checks: dict[str, str] = {}
    warnings: list[str] = []
    overall_ok = True

    try:
        me = api_call(db, acc, "GET", "/api/v1/me")
    except RedditApiError as exc:
        acc.status = "needs_reauth"
        acc.last_error = str(exc)
        acc.last_readiness_probe_at = datetime.now(timezone.utc)
        db.add(RedditReadinessProbe(
            account_id=acc.id,
            overall_ok=False,
            checks_json={"api": "failed"},
            warnings_json=[f"api_error:{exc.reason_code or 'unknown'}"],
            notes=str(exc),
        ))
        db.commit()
        return acc

    if isinstance(me.data, dict):
        _apply_me_payload(acc, me.data)

    # Проверки
    checks["connection"] = "ok"
    if acc.account_age_days is None or acc.account_age_days < 14:
        checks["account_age"] = "warn"
        warnings.append("account_age_below_suggested")
        overall_ok = False
    else:
        checks["account_age"] = "ok"

    total_karma = (acc.karma_link or 0) + (acc.karma_comment or 0)
    if total_karma < 20:
        checks["karma"] = "warn"
        warnings.append("karma_below_suggested")
        overall_ok = False
    else:
        checks["karma"] = "ok"

    if not acc.email_verified:
        checks["email"] = "warn"
        warnings.append("email_not_verified")
    else:
        checks["email"] = "ok"

    caps = acc.capabilities_json or {}
    if not caps.get("chat"):
        checks["chat"] = "warn"
    else:
        checks["chat"] = "ok"
    if not caps.get("dm"):
        checks["dm"] = "warn"
    else:
        checks["dm"] = "ok"

    acc.last_readiness_probe_at = datetime.now(timezone.utc)
    db.add(RedditReadinessProbe(
        account_id=acc.id,
        overall_ok=overall_ok,
        checks_json=checks,
        warnings_json=warnings,
    ))
    db.commit()
    return acc


def readiness_report_for(db: "Session", account_id: int) -> dict[str, Any] | None:
    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return None
    probe = (
        db.query(RedditReadinessProbe)
        .filter(RedditReadinessProbe.account_id == acc.id)
        .order_by(RedditReadinessProbe.performed_at.desc())
        .first()
    )
    if probe is None:
        return {
            "account": acc,
            "ok": False,
            "checks": {},
            "warnings": [],
            "performed_at": None,
            "note": "Запустите проверку готовности.",
        }
    return {
        "account": acc,
        "ok": bool(probe.overall_ok),
        "checks": probe.checks_json or {},
        "warnings": probe.warnings_json or [],
        "performed_at": probe.performed_at,
        "note": probe.notes or "",
    }


def serialize_account_card(acc: RedditAccount) -> dict[str, Any]:
    """Плоский dict для рендера карточки аккаунта."""
    caps = acc.capabilities_json or {}
    return {
        "id": acc.id,
        "label": acc.label,
        "username": acc.username_snapshot,
        "status": acc.status,
        "karma_link": acc.karma_link,
        "karma_comment": acc.karma_comment,
        "karma_total": (acc.karma_link or 0) + (acc.karma_comment or 0),
        "account_age_days": acc.account_age_days,
        "email_verified": bool(acc.email_verified),
        "is_suspended": bool(acc.is_suspended),
        "can_post": bool(caps.get("post")),
        "can_comment": bool(caps.get("comment")),
        "can_chat": bool(caps.get("chat")),
        "can_dm": bool(caps.get("dm")),
        "last_error": acc.last_error,
        "last_readiness_probe_at": acc.last_readiness_probe_at,
        "oauth_expires_at": acc.oauth_expires_at,
        "cooldown_until": acc.cooldown_until,
    }


__all__ = [
    "create_account_from_tokens",
    "create_browser_account_from_parsed",
    "delete_account",
    "disconnect",
    "get_account",
    "import_browser_accounts_from_text",
    "list_accounts",
    "readiness_report_for",
    "refresh_probe",
    "serialize_account_card",
    "update_proxy",
]
