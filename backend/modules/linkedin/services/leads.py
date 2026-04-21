"""LinkedIn-лиды: CRUD + bulk import."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import LinkedInLead


def list_leads(db: Session, organization_id: int) -> list[LinkedInLead]:
    return (
        db.query(LinkedInLead)
        .filter(LinkedInLead.organization_id == organization_id)
        .order_by(LinkedInLead.created_at.desc())
        .limit(500)
        .all()
    )


def get_lead(db: Session, lead_id: int) -> LinkedInLead | None:
    return db.get(LinkedInLead, int(lead_id))


def create_lead(db: Session, organization_id: int, **fields: Any) -> LinkedInLead:
    profile_url = (fields.get("profile_url") or "").strip()
    if not profile_url:
        raise ValueError("profile_url обязателен")
    lead = LinkedInLead(
        organization_id=organization_id,
        profile_url=profile_url,
        public_identifier=(fields.get("public_identifier") or "").strip() or None,
        full_name=(fields.get("full_name") or "").strip() or None,
        first_name=(fields.get("first_name") or "").strip() or None,
        last_name=(fields.get("last_name") or "").strip() or None,
        headline=(fields.get("headline") or "").strip() or None,
        company=(fields.get("company") or "").strip() or None,
        job_title=(fields.get("job_title") or "").strip() or None,
        industry=(fields.get("industry") or "").strip() or None,
        location=(fields.get("location") or "").strip() or None,
        seniority=(fields.get("seniority") or "").strip() or None,
        source_kind=(fields.get("source_kind") or "manual").strip(),
        source_ref=(fields.get("source_ref") or "").strip() or None,
        notes=(fields.get("notes") or "").strip() or None,
    )
    db.add(lead)
    db.commit()
    return lead


def bulk_import_urls(
    db: Session,
    organization_id: int,
    urls_blob: str,
    source_kind: str = "bulk_import",
) -> dict[str, Any]:
    """Импорт списка LinkedIn-URL'ов (по строке)."""
    imported = 0
    errors: list[dict[str, Any]] = []
    for raw in (urls_blob or "").splitlines():
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        if not url.startswith("http"):
            errors.append({"value": url, "error": "не URL"})
            continue
        try:
            create_lead(db, organization_id, profile_url=url, source_kind=source_kind)
            imported += 1
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errors.append({"value": url, "error": str(e)})
    return {"imported": imported, "errors": errors}


def delete_lead(db: Session, lead_id: int) -> bool:
    """Удалить лид + связанные queue items, sequence runs, conversation refs."""
    from backend.modules.linkedin.models import (
        LinkedInConversation,
        LinkedInOutreachQueueItem,
        LinkedInSequenceRun,
    )

    lead = get_lead(db, lead_id)
    if lead is None:
        return False
    db.query(LinkedInOutreachQueueItem).filter(LinkedInOutreachQueueItem.lead_id == lead.id).delete(synchronize_session=False)
    db.query(LinkedInSequenceRun).filter(LinkedInSequenceRun.lead_id == lead.id).delete(synchronize_session=False)
    # Conversation: освобождаем lead_id, не трогая остальные данные.
    db.query(LinkedInConversation).filter(LinkedInConversation.lead_id == lead.id).update(
        {"lead_id": None}, synchronize_session=False,
    )
    db.delete(lead)
    db.commit()
    return True


__all__ = ["bulk_import_urls", "create_lead", "delete_lead", "get_lead", "list_leads"]
