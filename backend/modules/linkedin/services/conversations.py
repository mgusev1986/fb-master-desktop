"""LinkedIn conversations CRUD + helpers для inbox UI."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import (
    LinkedInConversation,
    LinkedInLead,
    LinkedInMessage,
)


def list_conversations(db: Session, organization_id: int) -> list[LinkedInConversation]:
    return (
        db.query(LinkedInConversation)
        .filter(LinkedInConversation.organization_id == organization_id)
        .order_by(LinkedInConversation.last_message_at.desc().nullslast(), LinkedInConversation.id.desc())
        .limit(500)
        .all()
    )


def get_conversation(db: Session, conv_id: int) -> LinkedInConversation | None:
    return db.get(LinkedInConversation, int(conv_id))


def list_messages(db: Session, conv_id: int) -> list[LinkedInMessage]:
    return (
        db.query(LinkedInMessage)
        .filter(LinkedInMessage.conversation_id == conv_id)
        .order_by(LinkedInMessage.sent_at.asc(), LinkedInMessage.id.asc())
        .all()
    )


def mark_read(db: Session, conv_id: int) -> int:
    conv = get_conversation(db, conv_id)
    if conv is None:
        return 0
    affected = (
        db.query(LinkedInMessage)
        .filter(
            LinkedInMessage.conversation_id == conv.id,
            LinkedInMessage.direction == "in",
            LinkedInMessage.read_at.is_(None),
        )
        .count()
    )
    if affected:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        db.query(LinkedInMessage).filter(
            LinkedInMessage.conversation_id == conv.id,
            LinkedInMessage.direction == "in",
            LinkedInMessage.read_at.is_(None),
        ).update({"read_at": now}, synchronize_session=False)
    conv.unread_count = 0
    db.commit()
    return affected


def append_outgoing(
    db: Session, conv_id: int, body: str, *, sent: bool = True
) -> LinkedInMessage | None:
    """Зафиксировать исходящее сообщение в БД (после реального отправления)."""
    from datetime import datetime, timezone

    conv = get_conversation(db, conv_id)
    if conv is None:
        return None
    msg = LinkedInMessage(
        conversation_id=conv.id,
        direction="out",
        body=body,
        sent_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    if sent:
        conv.last_message_at = msg.sent_at
        conv.last_message_from = "out"
    db.commit()
    return msg


def link_to_lead(db: Session, conv_id: int, lead_id: int | None) -> bool:
    conv = get_conversation(db, conv_id)
    if conv is None:
        return False
    if lead_id is None:
        conv.lead_id = None
    else:
        lead = db.get(LinkedInLead, int(lead_id))
        if lead is None or lead.organization_id != conv.organization_id:
            return False
        conv.lead_id = lead.id
    db.commit()
    return True


__all__ = [
    "append_outgoing",
    "get_conversation",
    "link_to_lead",
    "list_conversations",
    "list_messages",
    "mark_read",
]
