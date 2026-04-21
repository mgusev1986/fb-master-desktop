"""Instagram templates — CRUD."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import InstagramTemplate


VALID_KINDS = (
    "dm_first_touch",
    "dm_follow_up",
    "dm_no_reply",
    "comment",
    "story_reply",
    "warm_reengagement",
)


def list_templates(db: Session, organization_id: int) -> list[InstagramTemplate]:
    return (
        db.query(InstagramTemplate)
        .filter(InstagramTemplate.organization_id == organization_id, InstagramTemplate.is_archived.is_(False))
        .order_by(InstagramTemplate.updated_at.desc())
        .all()
    )


def get_template(db: Session, template_id: int) -> InstagramTemplate | None:
    return db.get(InstagramTemplate, int(template_id))


def create_template(db: Session, organization_id: int, **fields: Any) -> InstagramTemplate:
    name = (fields.get("name") or "").strip() or "Без названия"
    kind = (fields.get("kind") or "dm_first_touch").strip().lower()
    if kind not in VALID_KINDS:
        kind = "dm_first_touch"
    body = (fields.get("body") or "").strip()
    if not body:
        raise ValueError("Текст шаблона обязателен")
    tpl = InstagramTemplate(
        organization_id=organization_id,
        name=name[:255], kind=kind, body=body,
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
