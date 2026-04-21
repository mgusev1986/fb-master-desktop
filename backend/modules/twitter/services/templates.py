"""Twitter templates — CRUD."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import TwitterTemplate


VALID_KINDS = (
    "dm_first_touch",
    "dm_follow_up",
    "dm_no_reply",
    "reply",
    "quote_tweet",
    "warm_reengagement",
)


def list_templates(db: Session, organization_id: int) -> list[TwitterTemplate]:
    return (
        db.query(TwitterTemplate)
        .filter(TwitterTemplate.organization_id == organization_id, TwitterTemplate.is_archived.is_(False))
        .order_by(TwitterTemplate.updated_at.desc())
        .all()
    )


def get_template(db: Session, template_id: int) -> TwitterTemplate | None:
    return db.get(TwitterTemplate, int(template_id))


def create_template(db: Session, organization_id: int, **fields: Any) -> TwitterTemplate:
    name = (fields.get("name") or "").strip() or "Без названия"
    kind = (fields.get("kind") or "dm_first_touch").strip().lower()
    if kind not in VALID_KINDS:
        kind = "dm_first_touch"
    body = (fields.get("body") or "").strip()
    if not body:
        raise ValueError("Текст шаблона обязателен")
    tpl = TwitterTemplate(
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


def delete_template(db: Session, template_id: int) -> bool:
    tpl = get_template(db, template_id)
    if tpl is None:
        return False
    tpl.is_archived = True
    db.commit()
    return True


__all__ = ["VALID_KINDS", "create_template", "delete_template", "get_template", "list_templates"]
