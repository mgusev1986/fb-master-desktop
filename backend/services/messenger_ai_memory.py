"""Индексация переписок в чанки и выборка контекста для RAG AI Messenger."""

from __future__ import annotations

import logging
import re
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.models import (
    Conversation,
    FBAccount,
    Message,
    MessengerAIConversationSummary,
    MessengerAIMemoryChunk,
)

logger = logging.getLogger(__name__)

_MAX_CHUNKS_PER_CONVERSATION = 120
_CHUNK_STRIDE = 2
_CHUNK_MAX_MESSAGES = 4
_MAX_BODY_IN_CHUNK = 1200
_MAX_SUMMARY_CHARS = 8000


def _organization_id_for_conversation(db: Session, conv: Conversation) -> int | None:
    acc = db.get(FBAccount, conv.fb_account_id)
    return int(acc.organization_id) if acc else None


def rebuild_messenger_ai_memory_for_conversation(db: Session, conversation_id: int) -> None:
    """
    Пересобрать чанки и сводку по одному чату (вызывать в той же транзакции, что и обновление messages).
    """
    conv = db.get(Conversation, int(conversation_id))
    if not conv:
        return
    org_id = _organization_id_for_conversation(db, conv)
    if org_id is None:
        return

    db.query(MessengerAIMemoryChunk).filter(
        MessengerAIMemoryChunk.conversation_id == conv.id
    ).delete(synchronize_session=False)
    db.query(MessengerAIConversationSummary).filter(
        MessengerAIConversationSummary.conversation_id == conv.id
    ).delete(synchronize_session=False)

    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.asc())
        .all()
    )
    if not msgs:
        return

    lines: list[str] = []
    for m in msgs[-60:]:
        role = "Собеседник" if (m.direction or "").strip().lower() == "in" else "Мы"
        body = (m.body or "").strip()[:500]
        if body:
            lines.append(f"{role}: {body}")
    summary_text = "\n".join(lines)[:_MAX_SUMMARY_CHARS]
    db.add(
        MessengerAIConversationSummary(
            conversation_id=conv.id,
            organization_id=org_id,
            summary_text=summary_text or "—",
        )
    )

    n_chunks = 0
    for start in range(0, len(msgs), _CHUNK_STRIDE):
        window = msgs[start : start + _CHUNK_MAX_MESSAGES]
        if not window:
            break
        parts: list[str] = []
        for m in window:
            role = "Собеседник" if (m.direction or "").strip().lower() == "in" else "Мы"
            body = (m.body or "").strip()
            if not body:
                continue
            parts.append(f"{role}: {body[:_MAX_BODY_IN_CHUNK]}")
        if len(parts) < 1:
            continue
        text = "\n".join(parts)
        if len(text.strip()) < 12:
            continue
        db.add(
            MessengerAIMemoryChunk(
                organization_id=org_id,
                conversation_id=conv.id,
                person_id=conv.person_id,
                source="message_span",
                text=text[:12000],
                first_message_id=window[0].id,
                last_message_id=window[-1].id,
            )
        )
        n_chunks += 1
        if n_chunks >= _MAX_CHUNKS_PER_CONVERSATION:
            break


_TERM_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{3,}")


def _terms_from_messages(msgs: list[Message], *, last_n: int = 6) -> list[str]:
    buf: list[str] = []
    tail = msgs[-last_n:] if msgs else []
    for m in reversed(tail):
        if (m.direction or "").strip().lower() != "in":
            continue
        buf.append(m.body or "")
    text = " ".join(buf)
    seen: set[str] = set()
    out: list[str] = []
    for w in _TERM_RE.findall(text):
        low = w.lower()
        if low in seen or len(out) >= 12:
            continue
        seen.add(low)
        out.append(w[:48])
    return out


def retrieve_messenger_ai_memory_block(
    db: Session,
    *,
    organization_id: int,
    conversation: Conversation,
    msgs: list[Message],
    limit: int = 6,
) -> str:
    """
    Собрать блок текста для системного промпта: релевантные фрагменты из памяти организации.
    """
    terms = _terms_from_messages(msgs)
    q = db.query(MessengerAIMemoryChunk).filter(
        MessengerAIMemoryChunk.organization_id == int(organization_id)
    )
    if terms:
        conds = []
        for t in terms[:8]:
            pat = f"%{t}%"
            conds.append(MessengerAIMemoryChunk.text.ilike(pat))
        rows = q.filter(or_(*conds)).order_by(MessengerAIMemoryChunk.id.desc()).limit(160).all()
    else:
        rows = (
            q.filter(MessengerAIMemoryChunk.conversation_id == conversation.id)
            .order_by(MessengerAIMemoryChunk.id.desc())
            .limit(40)
            .all()
        )
    if not rows:
        return ""

    scored: list[tuple[int, MessengerAIMemoryChunk]] = []
    tlow = [x.lower() for x in terms]
    for row in rows:
        s = 0
        blob = (row.text or "").lower()
        for tl in tlow:
            if tl in blob:
                s += 2
        if row.conversation_id == conversation.id:
            s += 10
        if conversation.person_id and row.person_id == conversation.person_id:
            s += 4
        if row.conversation_id != conversation.id:
            s -= 1
        scored.append((s, row))
    scored.sort(key=lambda x: x[0], reverse=True)

    lines: list[str] = []
    for _, row in scored[: max(1, min(limit, 12))]:
        tag = "этот_чат" if row.conversation_id == conversation.id else "другой_чат"
        snippet = (row.text or "").strip()[:900]
        if snippet:
            lines.append(f"[{tag}]\n{snippet}")
    if not lines:
        return ""
    return (
        "Релевантная память из прошлых диалогов (не выдумывай факты о текущем собеседнике; "
        "используй как стиль, аргументы и типовые ходы, если уместно):\n\n"
        + "\n\n---\n\n".join(lines)
    )
