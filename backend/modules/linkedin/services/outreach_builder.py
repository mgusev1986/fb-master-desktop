"""LinkedIn outreach builder: создание кампании + сборка очереди.

Реальная отправка (Playwright runtime) — следующий milestone.
Этот сервис умеет:
  * создать draft-кампанию,
  * собрать queue items из выбранных lead_ids + шаблона,
  * подменить плейсхолдеры {first_name}, {company}, {headline}, {full_name}.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import (
    LinkedInLead,
    LinkedInOutreachCampaign,
    LinkedInOutreachQueueItem,
    LinkedInTemplate,
)


_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.IGNORECASE)


def list_campaigns(db: Session, organization_id: int) -> list[LinkedInOutreachCampaign]:
    return (
        db.query(LinkedInOutreachCampaign)
        .filter(LinkedInOutreachCampaign.organization_id == organization_id)
        .order_by(LinkedInOutreachCampaign.created_at.desc())
        .all()
    )


def get_campaign(db: Session, campaign_id: int) -> LinkedInOutreachCampaign | None:
    return db.get(LinkedInOutreachCampaign, int(campaign_id))


def create_campaign(db: Session, organization_id: int, **fields: Any) -> LinkedInOutreachCampaign:
    camp = LinkedInOutreachCampaign(
        organization_id=organization_id,
        name=(fields.get("name") or "Без названия").strip()[:255],
        mode=(fields.get("mode") or "dm_first_degree").strip(),
        account_id=int(fields["account_id"]),
        template_id=int(fields["template_id"]) if fields.get("template_id") else None,
        follow_up_template_id=int(fields["follow_up_template_id"]) if fields.get("follow_up_template_id") else None,
        approval_mode=(fields.get("approval_mode") or "manual").strip(),
        daily_cap=int(fields["daily_cap"]) if fields.get("daily_cap") else None,
        notes=(fields.get("notes") or "").strip() or None,
    )
    db.add(camp)
    db.commit()
    return camp


def _render(template_body: str, lead: LinkedInLead) -> str:
    ctx = {
        "first_name": (lead.first_name or (lead.full_name or "").split(" ")[0] or "").strip(),
        "last_name": (lead.last_name or "").strip(),
        "full_name": (lead.full_name or "").strip(),
        "company": (lead.company or "").strip(),
        "job_title": (lead.job_title or "").strip(),
        "headline": (lead.headline or "").strip(),
        "industry": (lead.industry or "").strip(),
        "location": (lead.location or "").strip(),
    }

    def repl(m: re.Match[str]) -> str:
        return ctx.get(m.group(1).lower(), "")

    return _PLACEHOLDER_RE.sub(repl, template_body)


def build_queue(db: Session, campaign_id: int, lead_ids: list[int]) -> dict[str, Any]:
    """Собрать queue items для кампании по списку lead_ids."""
    camp = get_campaign(db, campaign_id)
    if camp is None:
        return {"created": 0, "skipped": 0, "errors": ["campaign_not_found"]}
    if camp.template_id is None:
        return {"created": 0, "skipped": 0, "errors": ["no_template"]}
    tpl = db.get(LinkedInTemplate, camp.template_id)
    if tpl is None:
        return {"created": 0, "skipped": 0, "errors": ["template_missing"]}

    existing = {
        row[0]
        for row in db.query(LinkedInOutreachQueueItem.lead_id)
        .filter(LinkedInOutreachQueueItem.campaign_id == camp.id)
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
        lead = db.get(LinkedInLead, lid_int)
        if lead is None or lead.organization_id != camp.organization_id:
            skipped += 1
            continue
        body = _render(tpl.body, lead)
        db.add(
            LinkedInOutreachQueueItem(
                campaign_id=camp.id,
                lead_id=lead.id,
                draft_body=body,
                review_state="pending_review",
            )
        )
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped, "errors": []}


def list_queue_items(db: Session, campaign_id: int) -> list[LinkedInOutreachQueueItem]:
    return (
        db.query(LinkedInOutreachQueueItem)
        .filter(LinkedInOutreachQueueItem.campaign_id == campaign_id)
        .order_by(LinkedInOutreachQueueItem.created_at.asc())
        .limit(500)
        .all()
    )


def set_queue_item_state(
    db: Session,
    item_id: int,
    state: str,
    error: str | None = None,
) -> LinkedInOutreachQueueItem | None:
    item = db.get(LinkedInOutreachQueueItem, int(item_id))
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
    "build_queue",
    "create_campaign",
    "get_campaign",
    "list_campaigns",
    "list_queue_items",
    "queue_summary",
    "set_queue_item_state",
]
