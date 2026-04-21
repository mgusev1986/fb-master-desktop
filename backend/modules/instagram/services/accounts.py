"""Instagram accounts — CRUD + import + helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.config import BROWSER_PROFILES_DIR
from backend.modules.instagram.models import InstagramAccount, InstagramComplianceEvent
from backend.modules.instagram.services.cookie_import import ParsedInstagramAccount, parse_text


_PROFILE_PREFIX = "instagram"


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-")
    return (s or "account")[:64]


def _make_profile_dir(label: str, account_id: int | None = None) -> str:
    base = Path(BROWSER_PROFILES_DIR) / "instagram"
    base.mkdir(parents=True, exist_ok=True)
    suffix = f"-{account_id}" if account_id else ""
    return str(base / f"{_PROFILE_PREFIX}-{_slugify(label)}{suffix}")


def list_accounts(db: Session, organization_id: int) -> list[InstagramAccount]:
    return (
        db.query(InstagramAccount)
        .filter(InstagramAccount.organization_id == organization_id)
        .order_by(InstagramAccount.created_at.desc())
        .all()
    )


def get_account(db: Session, account_id: int) -> InstagramAccount | None:
    return db.get(InstagramAccount, int(account_id))


def create_from_parsed(db: Session, organization_id: int, parsed: ParsedInstagramAccount) -> InstagramAccount:
    label = parsed.label or "instagram-account"
    handle = parsed.handle
    profile_url = f"https://www.instagram.com/{handle}/" if handle else None
    acc = InstagramAccount(
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


def update_proxy(db: Session, account_id: int, *, proxy_url: str | None, proxy_username: str | None, proxy_password: str | None, proxy_enabled: bool | None = None) -> InstagramAccount | None:
    acc = get_account(db, account_id)
    if acc is None:
        return None
    acc.proxy_url = (proxy_url or "").strip() or None
    acc.proxy_username = (proxy_username or "").strip() or None
    acc.proxy_password = (proxy_password or "").strip() or None
    acc.proxy_enabled = bool(acc.proxy_url) if proxy_enabled is None else bool(proxy_enabled)
    db.commit()
    return acc


def delete_account(db: Session, account_id: int) -> bool:
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


def log_event(db: Session, organization_id: int, account_id: int | None, event_type: str, severity: str = "info", payload: dict[str, Any] | None = None) -> None:
    db.add(
        InstagramComplianceEvent(
            organization_id=organization_id, account_id=account_id,
            event_type=event_type, severity=severity, payload_json=payload or None,
        )
    )


__all__ = ["create_from_parsed", "delete_account", "get_account", "import_from_text", "list_accounts", "log_event", "status_summary", "update_proxy"]
