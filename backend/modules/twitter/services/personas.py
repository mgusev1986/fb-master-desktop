"""Twitter AI persona — CRUD."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import TwitterAIPersona


def list_personas(db: Session, organization_id: int) -> list[TwitterAIPersona]:
    return (
        db.query(TwitterAIPersona)
        .filter(TwitterAIPersona.organization_id == organization_id, TwitterAIPersona.is_archived.is_(False))
        .order_by(TwitterAIPersona.updated_at.desc())
        .all()
    )


def get_persona(db: Session, persona_id: int) -> TwitterAIPersona | None:
    return db.get(TwitterAIPersona, int(persona_id))


def create_persona(db: Session, organization_id: int, **fields: Any) -> TwitterAIPersona:
    name = (fields.get("name") or "").strip() or "Без названия"
    p = TwitterAIPersona(
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


def update_persona(db: Session, persona_id: int, **fields: Any) -> TwitterAIPersona | None:
    p = get_persona(db, persona_id)
    if p is None:
        return None
    if "name" in fields:
        v = (fields["name"] or "").strip()
        if v:
            p.name = v[:255]
    for k in ("role", "company", "offer_summary", "tone", "knowledge_base", "cta_call_link", "cta_info_link", "forbidden_topics"):
        if k in fields:
            v = fields[k]
            v = (v or "").strip() if isinstance(v, str) else v
            setattr(p, k, v or None)
    if "languages" in fields and isinstance(fields["languages"], list):
        p.languages = fields["languages"]
    p.updated_at = datetime.now(timezone.utc)
    db.commit()
    return p


def archive_persona(db: Session, persona_id: int) -> bool:
    p = get_persona(db, persona_id)
    if p is None:
        return False
    p.is_archived = True
    db.commit()
    return True


__all__ = ["archive_persona", "create_persona", "get_persona", "list_personas", "update_persona"]
