"""LinkedIn-шаблоны: CRUD."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import LinkedInTemplate


VALID_KINDS = (
    "invitation",
    "dm_first_touch",
    "dm_follow_up",
    "dm_no_reply",
    "comment",
    "warm_reengagement",
)


def list_templates(db: Session, organization_id: int) -> list[LinkedInTemplate]:
    return (
        db.query(LinkedInTemplate)
        .filter(
            LinkedInTemplate.organization_id == organization_id,
            LinkedInTemplate.is_archived.is_(False),
        )
        .order_by(LinkedInTemplate.updated_at.desc())
        .all()
    )


def get_template(db: Session, template_id: int) -> LinkedInTemplate | None:
    return db.get(LinkedInTemplate, int(template_id))


def create_template(db: Session, organization_id: int, **fields: Any) -> LinkedInTemplate:
    name = (fields.get("name") or "").strip() or "Без названия"
    kind = (fields.get("kind") or "dm_first_touch").strip().lower()
    if kind not in VALID_KINDS:
        kind = "dm_first_touch"
    body = (fields.get("body") or "").strip()
    if not body:
        raise ValueError("Текст шаблона обязателен")
    tpl = LinkedInTemplate(
        organization_id=organization_id,
        name=name[:255],
        kind=kind,
        body=body,
        tone=(fields.get("tone") or "").strip() or None,
        tags=fields.get("tags"),
    )
    db.add(tpl)
    db.commit()
    return tpl


def update_template(db: Session, template_id: int, **fields: Any) -> LinkedInTemplate | None:
    tpl = get_template(db, template_id)
    if tpl is None:
        return None
    if "name" in fields:
        tpl.name = (fields["name"] or "").strip()[:255] or tpl.name
    if "kind" in fields:
        kind = (fields["kind"] or "").strip().lower()
        if kind in VALID_KINDS:
            tpl.kind = kind
    if "body" in fields:
        body = (fields["body"] or "").strip()
        if body:
            tpl.body = body
    if "tone" in fields:
        tpl.tone = (fields["tone"] or "").strip() or None
    if "tags" in fields:
        tpl.tags = fields["tags"]
    tpl.updated_at = datetime.now(timezone.utc)
    db.commit()
    return tpl


def delete_template(db: Session, template_id: int) -> bool:
    tpl = get_template(db, template_id)
    if tpl is None:
        return False
    tpl.is_archived = True
    db.commit()
    return True


__all__ = [
    "VALID_KINDS",
    "create_template",
    "delete_template",
    "get_template",
    "list_templates",
    "update_template",
]
