"""Instagram outreach builder."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import (
    InstagramLead,
    InstagramOutreachCampaign,
    InstagramOutreachQueueItem,
    InstagramTemplate,
)


_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.IGNORECASE)


def list_campaigns(db: Session, organization_id: int) -> list[InstagramOutreachCampaign]:
    return (
        db.query(InstagramOutreachCampaign)
        .filter(InstagramOutreachCampaign.organization_id == organization_id)
        .order_by(InstagramOutreachCampaign.created_at.desc())
        .all()
    )


def get_campaign(db: Session, campaign_id: int) -> InstagramOutreachCampaign | None:
    return db.get(InstagramOutreachCampaign, int(campaign_id))


def create_campaign(db: Session, organization_id: int, **fields: Any) -> InstagramOutreachCampaign:
    camp = InstagramOutreachCampaign(
        organization_id=organization_id,
        name=(fields.get("name") or "Без названия").strip()[:255],
        mode=(fields.get("mode") or "dm").strip(),
        account_id=int(fields["account_id"]) if fields.get("account_id") else None,
        template_id=int(fields["template_id"]) if fields.get("template_id") else None,
        follow_up_template_id=int(fields["follow_up_template_id"]) if fields.get("follow_up_template_id") else None,
        approval_mode=(fields.get("approval_mode") or "manual").strip(),
        daily_cap=int(fields["daily_cap"]) if fields.get("daily_cap") else None,
        autoresponder_enabled=bool(fields.get("autoresponder_enabled")),
        autoresponder_persona_id=int(fields["autoresponder_persona_id"]) if fields.get("autoresponder_persona_id") else None,
        autoresponder_goal=(fields.get("autoresponder_goal") or "").strip() or None,
        notes=(fields.get("notes") or "").strip() or None,
    )
    db.add(camp)
    db.commit()
    return camp


def _render(template_body: str, lead: InstagramLead) -> str:
    ctx = {
        "handle": (lead.handle or "").strip(),
        "full_name": (lead.full_name or lead.handle or "").strip(),
        "first_name": (lead.full_name or "").strip().split(" ")[0] if lead.full_name else (lead.handle or "").strip(),
        "bio": (lead.bio or "").strip(),
        "topic": "",
        "recent_post": "",
    }

    def repl(m: re.Match[str]) -> str:
        return ctx.get(m.group(1).lower(), "")

    return _PLACEHOLDER_RE.sub(repl, template_body)


def build_queue(db: Session, campaign_id: int, lead_ids: list[int], target_post_url: str | None = None) -> dict[str, Any]:
    camp = get_campaign(db, campaign_id)
    if camp is None:
        return {"created": 0, "skipped": 0, "errors": ["campaign_not_found"]}
    if camp.template_id is None:
        return {"created": 0, "skipped": 0, "errors": ["no_template"]}
    tpl = db.get(InstagramTemplate, camp.template_id)
    if tpl is None:
        return {"created": 0, "skipped": 0, "errors": ["template_missing"]}

    existing = {
        row[0]
        for row in db.query(InstagramOutreachQueueItem.lead_id)
        .filter(InstagramOutreachQueueItem.campaign_id == camp.id)
        .all()
    }
    created = 0
    skipped = 0
    for lid in lead_ids:
        try:
            lid_int = int(lid)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if lid_int in existing:
            skipped += 1
            continue
        lead = db.get(InstagramLead, lid_int)
        if lead is None or lead.organization_id != camp.organization_id:
            skipped += 1
            continue
        body = _render(tpl.body, lead)
        db.add(
            InstagramOutreachQueueItem(
                campaign_id=camp.id, lead_id=lead.id,
                draft_body=body, target_post_url=target_post_url,
                review_state="pending_review",
            )
        )
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped, "errors": []}


def list_queue_items(db: Session, campaign_id: int) -> list[InstagramOutreachQueueItem]:
    return (
        db.query(InstagramOutreachQueueItem)
        .filter(InstagramOutreachQueueItem.campaign_id == campaign_id)
        .order_by(InstagramOutreachQueueItem.created_at.asc())
        .limit(500)
        .all()
    )


def set_queue_item_state(db: Session, item_id: int, state: str, error: str | None = None) -> InstagramOutreachQueueItem | None:
    item = db.get(InstagramOutreachQueueItem, int(item_id))
    if item is None:
        return None
    if state in ("pending_review", "approved", "rejected", "sent", "failed", "skipped"):
        item.review_state = state
    if state == "failed":
        item.last_error = (error or "")[:512] or None
    db.commit()
    return item


def queue_summary(db: Session, campaign_id: int) -> dict[str, int]:
    items = list_queue_items(db, campaign_id)
    out = {"total": len(items), "pending_review": 0, "approved": 0, "rejected": 0, "sent": 0, "failed": 0, "skipped": 0}
    for it in items:
        out[it.review_state] = out.get(it.review_state, 0) + 1
    return out


__all__ = [
    "build_queue", "create_campaign", "get_campaign", "list_campaigns",
    "list_queue_items", "queue_summary", "set_queue_item_state",
]
