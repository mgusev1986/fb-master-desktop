"""Черновики промпта после самоанализа Messenger (ручное применение)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.models import MessengerAIPromptDraft
from backend.services.messenger_ai_settings import load_messenger_ai_settings, save_messenger_ai_settings


def list_pending_drafts(db: Session, *, organization_id: int, limit: int = 50) -> list[MessengerAIPromptDraft]:
    return (
        db.query(MessengerAIPromptDraft)
        .filter(
            MessengerAIPromptDraft.organization_id == int(organization_id),
            MessengerAIPromptDraft.status == "pending",
        )
        .order_by(MessengerAIPromptDraft.id.desc())
        .limit(int(limit))
        .all()
    )


def merge_draft_into_prompt_addon(
    db: Session, *, organization_id: int, draft_id: int
) -> tuple[bool, str]:
    d = db.get(MessengerAIPromptDraft, int(draft_id))
    if not d or d.organization_id != int(organization_id):
        return False, "not_found"
    if d.status != "pending":
        return False, "already_resolved"
    s = load_messenger_ai_settings(db)
    addon = (s.get("prompt_addon") or "").rstrip()
    block = (d.body or "").strip()
    if not block:
        return False, "empty_body"
    sep = "\n\n--- Из самоанализа Messenger ---\n\n"
    new_addon = (addon + sep + block).strip()[:12000]
    save_messenger_ai_settings(
        db,
        prompt_addon=new_addon,
        materials_text="\n".join(s.get("materials_lines") or []),
        delay_min_sec=s["delay_min_sec"],
        delay_max_sec=s["delay_max_sec"],
        temperature=s["temperature"],
        max_tokens=s["max_tokens"],
        max_reply_steps=s["max_reply_steps"],
        rag_enabled=s.get("rag_enabled", False),
    )
    d.status = "merged"
    d.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return True, "ok"


def dismiss_draft(db: Session, *, organization_id: int, draft_id: int) -> bool:
    d = db.get(MessengerAIPromptDraft, int(draft_id))
    if not d or d.organization_id != int(organization_id) or d.status != "pending":
        return False
    d.status = "dismissed"
    d.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return True
