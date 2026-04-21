"""Instagram AI persona — CRUD."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import InstagramAIPersona


def list_personas(db: Session, organization_id: int) -> list[InstagramAIPersona]:
    return (
        db.query(InstagramAIPersona)
        .filter(InstagramAIPersona.organization_id == organization_id, InstagramAIPersona.is_archived.is_(False))
        .order_by(InstagramAIPersona.updated_at.desc())
        .all()
    )


def get_persona(db: Session, persona_id: int) -> InstagramAIPersona | None:
    return db.get(InstagramAIPersona, int(persona_id))


def create_persona(db: Session, organization_id: int, **fields: Any) -> InstagramAIPersona:
    name = (fields.get("name") or "").strip() or "Без названия"
    p = InstagramAIPersona(
        organization_id=organization_id,
        name=name[:255],
        role=(fields.get("role") or "").strip() or None,
        company=(fields.get("company") or "").strip() or None,
        offer_summary=(fields.get("offer_summary") or "").strip() or None,
        tone=(fields.get("tone") or "").strip() or None,
        languages=fields.get("languages") if isinstance(fields.get("languages"), list) else None,
        knowledge_base=(fields.get("knowledge_base") or "").strip() or None,
        cta_call_link=(fields.get("cta_call_link") or "").strip() or None,
        cta_info_link=(fields.get("cta_info_link") or "").strip() or None,
        forbidden_topics=(fields.get("forbidden_topics") or "").strip() or None,
    )
    db.add(p)
    db.commit()
    return p


def archive_persona(db: Session, persona_id: int) -> bool:
    p = get_persona(db, persona_id)
    if p is None:
        return False
    p.is_archived = True
    db.commit()
    return True


__all__ = ["archive_persona", "create_persona", "get_persona", "list_personas"]
