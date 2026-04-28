"""Twitter accounts — CRUD + import + helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.config import BROWSER_PROFILES_DIR
from backend.modules.twitter.models import TwitterAccount, TwitterComplianceEvent
from backend.modules.twitter.services.cookie_import import ParsedTwitterAccount, parse_text
from backend.services.fb_credentials_crypto import encrypt_secret


_PROFILE_PREFIX = "twitter"


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-")
    return (s or "account")[:64]


def _make_profile_dir(label: str, account_id: int | None = None) -> str:
    base = Path(BROWSER_PROFILES_DIR) / "twitter"
    base.mkdir(parents=True, exist_ok=True)
    suffix = f"-{account_id}" if account_id else ""
    return str(base / f"{_PROFILE_PREFIX}-{_slugify(label)}{suffix}")


def list_accounts(db: Session, organization_id: int) -> list[TwitterAccount]:
    return (
        db.query(TwitterAccount)
        .filter(TwitterAccount.organization_id == organization_id)
        .order_by(TwitterAccount.created_at.desc())
        .all()
    )


def get_account(db: Session, account_id: int) -> TwitterAccount | None:
    return db.get(TwitterAccount, int(account_id))


def create_from_parsed(db: Session, organization_id: int, parsed: ParsedTwitterAccount) -> TwitterAccount:
    label = parsed.label or "twitter-account"
    handle = parsed.handle
    profile_url = f"https://x.com/{handle}" if handle else None
    acc = TwitterAccount(
        organization_id=organization_id,
        label=label[:255],
        handle=handle,
        full_name=parsed.full_name,
        profile_url=profile_url,
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
    log_event(db, organization_id, acc.id, "account_imported", "info", {"label": acc.label, "handle": handle})
    db.commit()
    return acc


def create_from_credentials(
    db: Session,
    organization_id: int,
    *,
    username: str,
    password: str,
    label: str | None = None,
    totp_secret: str | None = None,
    proxy_url: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    handle_hint: str | None = None,
) -> TwitterAccount:
    """Импорт «своего» X/Twitter-аккаунта через логин + пароль.

    Cookies ещё нет — они появятся после первого Playwright-логина
    (кнопка «Войти» на карточке аккаунта). Статус — `needs_login`.
    Аналог `InstagramAccount.create_from_credentials` (v2.93+).
    """
    user_s = (username or "").strip()
    pwd_s = (password or "").strip()
    if not user_s:
        raise ValueError("Укажите логин/email/телефон X (Twitter)")
    if not pwd_s:
        raise ValueError("Укажите пароль")

    # handle = логин без @, если он похож на @handle (без точек/email/цифр в начале)
    handle = (handle_hint or "").strip().lstrip("@") or None
    if not handle and re.match(r"^[a-zA-Z0-9_]{2,15}$", user_s) and "@" not in user_s:
        handle = user_s.lstrip("@")

    final_label = (label or "").strip() or (handle or user_s)[:64]

    # 2.95: автоматически парсим прокси формата host:port:user:pass или URL с embedded auth
    from backend.services.fb_account_import_parse import normalize_proxy_fields
    px_url, px_user, px_pass = normalize_proxy_fields(proxy_url, proxy_username, proxy_password)

    acc = TwitterAccount(
        organization_id=organization_id,
        label=final_label[:255],
        handle=handle,
        full_name=None,
        profile_url=f"https://x.com/{handle}" if handle else None,
        profile_dir=_make_profile_dir(final_label),
        cookies_json=None,
        cookies_imported_at=None,
        proxy_enabled=bool(px_url),
        proxy_url=px_url,
        proxy_username=px_user,
        proxy_password=px_pass,
        status="needs_login",
        session_ok=None,
        login_username=user_s,
        enc_password=encrypt_secret(pwd_s),
        enc_totp_secret=encrypt_secret(totp_secret.strip()) if (totp_secret or "").strip() else None,
    )
    db.add(acc)
    db.flush()
    acc.profile_dir = _make_profile_dir(final_label, acc.id)
    Path(acc.profile_dir).mkdir(parents=True, exist_ok=True)
    log_event(
        db, organization_id, acc.id, "account_imported_credentials", "info",
        {"label": acc.label, "handle": handle, "username": user_s},
    )
    db.commit()
    return acc


def import_from_text(db: Session, organization_id: int, blob: str) -> dict[str, Any]:
    parsed_items = parse_text(blob)
    imported = 0
    errors: list[dict[str, Any]] = []
    for idx, p in enumerate(parsed_items, start=1):
        if p.error:
            errors.append({"line": idx, "label": p.label, "error": p.error})
            continue
        try:
            create_from_parsed(db, organization_id, p)
            imported += 1
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errors.append({"line": idx, "label": p.label, "error": str(e)})
    return {"imported": imported, "errors": errors, "total": len(parsed_items)}


def update_proxy(
    db: Session,
    account_id: int,
    *,
    proxy_url: str | None,
    proxy_username: str | None,
    proxy_password: str | None,
    proxy_enabled: bool | None = None,
) -> TwitterAccount | None:
    acc = get_account(db, account_id)
    if acc is None:
        return None
    # 2.95: автоматически парсим формат host:port:user:pass или URL с embedded auth
    from backend.services.fb_account_import_parse import normalize_proxy_fields
    clean_url, clean_user, clean_pass = normalize_proxy_fields(proxy_url, proxy_username, proxy_password)
    acc.proxy_url = clean_url
    acc.proxy_username = clean_user
    acc.proxy_password = clean_pass
    acc.proxy_enabled = bool(acc.proxy_url) if proxy_enabled is None else bool(proxy_enabled)
    db.commit()
    return acc


def delete_account(db: Session, account_id: int) -> bool:
    """Удалить аккаунт. Cascade FK обработают зависимые таблицы."""
    acc = get_account(db, account_id)
    if acc is None:
        return False
    db.delete(acc)
    db.commit()
    return True


def status_summary(db: Session, organization_id: int) -> dict[str, int]:
    out = {"connected": 0, "needs_attention": 0, "restricted": 0, "cooldown": 0, "disabled": 0, "total": 0}
    for a in list_accounts(db, organization_id):
        out["total"] += 1
        out[a.status] = out.get(a.status, 0) + 1
    return out


def log_event(
    db: Session,
    organization_id: int,
    account_id: int | None,
    event_type: str,
    severity: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(
        TwitterComplianceEvent(
            organization_id=organization_id,
            account_id=account_id,
            event_type=event_type,
            severity=severity,
            payload_json=payload or None,
        )
    )


__all__ = [
    "create_from_parsed",
    "delete_account",
    "get_account",
    "import_from_text",
    "list_accounts",
    "log_event",
    "status_summary",
    "update_proxy",
]
