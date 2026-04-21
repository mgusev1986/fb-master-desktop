"""LinkedIn-аккаунты: CRUD + импорт + status helpers."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from backend.config import BROWSER_PROFILES_DIR
from backend.modules.linkedin.models import (
    LinkedInAccount,
    LinkedInComplianceEvent,
)
from backend.modules.linkedin.services.cookie_import import ParsedAccount, parse_text


_PROFILE_PREFIX = "linkedin"


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-")
    return (s or "account")[:64]


def _make_profile_dir(label: str, account_id: int | None = None) -> str:
    base = Path(BROWSER_PROFILES_DIR) / "linkedin"
    base.mkdir(parents=True, exist_ok=True)
    suffix = f"-{account_id}" if account_id else ""
    return str(base / f"{_PROFILE_PREFIX}-{_slugify(label)}{suffix}")


def list_accounts(db: Session, organization_id: int) -> list[LinkedInAccount]:
    return (
        db.query(LinkedInAccount)
        .filter(LinkedInAccount.organization_id == organization_id)
        .order_by(LinkedInAccount.created_at.desc())
        .all()
    )


def get_account(db: Session, account_id: int) -> LinkedInAccount | None:
    return db.get(LinkedInAccount, int(account_id))


def create_from_parsed(
    db: Session,
    organization_id: int,
    parsed: ParsedAccount,
) -> LinkedInAccount:
    """Создать аккаунт из распарсенной строки import-файла."""
    label = parsed.label or "linkedin-account"
    account = LinkedInAccount(
        organization_id=organization_id,
        label=label[:255],
        public_identifier=parsed.public_identifier,
        full_name=parsed.full_name,
        profile_dir=_make_profile_dir(label),
        cookies_json=parsed.cookies,
        cookies_imported_at=datetime.now(timezone.utc),
        proxy_enabled=bool(parsed.proxy_url),
        proxy_url=parsed.proxy_url,
        proxy_username=parsed.proxy_username,
        proxy_password=parsed.proxy_password,
        stealth_user_agent=parsed.user_agent,
        li_login_email=parsed.li_login_email,
        li_login_password=parsed.li_login_password,
        status="connected" if parsed.cookies else "needs_attention",
        session_ok=True if parsed.cookies else None,
    )
    db.add(account)
    db.flush()
    # После flush у account есть id — обновим profile_dir с суффиксом, чтобы не было коллизий по label.
    account.profile_dir = _make_profile_dir(label, account.id)
    Path(account.profile_dir).mkdir(parents=True, exist_ok=True)
    log_event(db, organization_id, account.id, "account_imported", "info", {"label": account.label})
    db.commit()
    return account


def import_from_text(
    db: Session,
    organization_id: int,
    blob: str,
) -> dict[str, Any]:
    """Импорт нескольких аккаунтов одним текстовым blob'ом.

    Возвращает {"imported": N, "errors": [{"line": i, "error": "..."}, ...]}.
    """
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
) -> LinkedInAccount | None:
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
    """Удалить аккаунт и связанные с ним records (без CASCADE-FK)."""
    from backend.modules.linkedin.models import (
        LinkedInAccountDayUsage,
        LinkedInComplianceEvent,
        LinkedInConversation,
        LinkedInMessage,
        LinkedInOutreachCampaign,
        LinkedInOutreachQueueItem,
        LinkedInSequenceRun,
    )

    acc = get_account(db, account_id)
    if acc is None:
        return False
    # Сначала вычищаем зависимые таблицы (Postgres FK без CASCADE).
    db.query(LinkedInAccountDayUsage).filter(LinkedInAccountDayUsage.account_id == acc.id).delete(synchronize_session=False)
    db.query(LinkedInSequenceRun).filter(LinkedInSequenceRun.account_id == acc.id).delete(synchronize_session=False)
    # Очередь outreach — вычищаем queue items по кампаниям этого аккаунта, потом сами кампании.
    camp_ids = [c.id for c in db.query(LinkedInOutreachCampaign).filter(LinkedInOutreachCampaign.account_id == acc.id).all()]
    if camp_ids:
        db.query(LinkedInOutreachQueueItem).filter(LinkedInOutreachQueueItem.campaign_id.in_(camp_ids)).delete(synchronize_session=False)
        db.query(LinkedInOutreachCampaign).filter(LinkedInOutreachCampaign.id.in_(camp_ids)).delete(synchronize_session=False)
    # Conversations + messages.
    conv_ids = [c.id for c in db.query(LinkedInConversation).filter(LinkedInConversation.account_id == acc.id).all()]
    if conv_ids:
        db.query(LinkedInMessage).filter(LinkedInMessage.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
        db.query(LinkedInConversation).filter(LinkedInConversation.id.in_(conv_ids)).delete(synchronize_session=False)
    # Compliance events последними.
    db.query(LinkedInComplianceEvent).filter(LinkedInComplianceEvent.account_id == acc.id).delete(synchronize_session=False)
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
        LinkedInComplianceEvent(
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
