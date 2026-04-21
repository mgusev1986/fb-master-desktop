"""Twitter leads — CRUD + bulk import."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import TwitterLead


def _normalize_handle(value: str) -> str:
    return (value or "").strip().lstrip("@").lstrip("/").split()[0] if (value or "").strip() else ""


def list_leads(db: Session, organization_id: int) -> list[TwitterLead]:
    return (
        db.query(TwitterLead)
        .filter(TwitterLead.organization_id == organization_id)
        .order_by(TwitterLead.created_at.desc())
        .limit(500)
        .all()
    )


def get_lead(db: Session, lead_id: int) -> TwitterLead | None:
    return db.get(TwitterLead, int(lead_id))


def create_lead(db: Session, organization_id: int, **fields: Any) -> TwitterLead:
    handle = _normalize_handle(fields.get("handle") or fields.get("username") or "")
    if not handle:
        raise ValueError("handle обязателен")
    profile_url = (fields.get("profile_url") or "").strip() or f"https://x.com/{handle}"

    # Дедупликация по (org_id, handle).
    existing = (
        db.query(TwitterLead)
        .filter(TwitterLead.organization_id == organization_id, TwitterLead.handle == handle)
        .first()
    )
    if existing is not None:
        return existing

    lead = TwitterLead(
        organization_id=organization_id,
        handle=handle,
        profile_url=profile_url,
        full_name=(fields.get("full_name") or "").strip() or None,
        bio=(fields.get("bio") or "").strip() or None,
        location=(fields.get("location") or "").strip() or None,
        source_kind=(fields.get("source_kind") or "manual").strip(),
        source_ref=(fields.get("source_ref") or "").strip() or None,
        notes=(fields.get("notes") or "").strip() or None,
    )
    db.add(lead)
    db.commit()
    return lead


def bulk_import_handles(db: Session, organization_id: int, blob: str, source_kind: str = "bulk_import") -> dict[str, Any]:
    imported = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    for raw in (blob or "").splitlines():
        v = (raw or "").strip()
        if not v or v.startswith("#"):
            continue
        # Принимаем @handle, handle, https://x.com/handle, https://twitter.com/handle.
        if "://" in v:
            try:
                handle = v.rstrip("/").split("/")[-1].split("?")[0]
            except Exception:  # noqa: BLE001
                errors.append({"value": v, "error": "не удалось извлечь handle"})
                continue
        else:
            handle = v
        try:
            before = (
                db.query(TwitterLead)
                .filter(TwitterLead.organization_id == organization_id, TwitterLead.handle == _normalize_handle(handle))
                .first()
            )
            create_lead(db, organization_id, handle=handle, source_kind=source_kind)
            if before is None:
                imported += 1
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errors.append({"value": v, "error": str(e)})
    return {"imported": imported, "skipped": skipped, "errors": errors}


def delete_lead(db: Session, lead_id: int) -> bool:
    lead = get_lead(db, lead_id)
    if lead is None:
        return False
    db.delete(lead)
    db.commit()
    return True


__all__ = ["bulk_import_handles", "create_lead", "delete_lead", "get_lead", "list_leads"]
