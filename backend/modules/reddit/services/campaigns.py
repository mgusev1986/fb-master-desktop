"""Reddit campaign builder + queue management.

Функции:
  - CRUD кампаний
  - build_queue: из lead-фильтров создаёт `reddit_campaign_queue` с preview-body
  - approve/reject/skip: смена review_state
  - send_next: отправка одного `approved` элемента через conversation_sender/comment_worker
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import (
    RedditCampaign,
    RedditCampaignQueue,
    RedditLead,
    RedditTemplate,
    RedditUserProfile,
)
from backend.modules.reddit.services import templates as templates_svc

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


CAMPAIGN_MODES = ("dm", "comment")
CAMPAIGN_STATUSES = ("draft", "running", "paused", "finished", "cancelled")
REVIEW_STATES = ("pending_review", "approved", "sent", "failed", "skipped", "rejected")


# ── CRUD ──────────────────────────────────────────────────────────────


def list_campaigns(db: "Session", organization_id: int | None) -> list[RedditCampaign]:
    return (
        db.query(RedditCampaign)
        .filter(RedditCampaign.organization_id == organization_id)
        .order_by(RedditCampaign.id.desc())
        .all()
    )


def get_campaign(db: "Session", campaign_id: int) -> RedditCampaign | None:
    return db.get(RedditCampaign, int(campaign_id))


def create_campaign(
    db: "Session",
    organization_id: int | None,
    *,
    name: str,
    mode: str,
    template_id: int | None,
    audience_query: dict | None = None,
    cadence_config: dict | None = None,
    send_window_config: dict | None = None,
    per_account_caps: dict | None = None,
    approval_mode: str = "manual",
) -> RedditCampaign:
    row = RedditCampaign(
        organization_id=organization_id,
        name=(name or "").strip()[:200] or "Campaign",
        mode=mode if mode in CAMPAIGN_MODES else "dm",
        template_id=template_id,
        audience_query_json=audience_query or {},
        cadence_config_json=cadence_config or {},
        send_window_config_json=send_window_config or {},
        per_account_caps_json=per_account_caps or {},
        approval_mode=approval_mode if approval_mode in ("manual", "semi", "auto") else "manual",
        status="draft",
    )
    db.add(row)
    db.commit()
    return row


def update_campaign(
    db: "Session",
    campaign_id: int,
    **fields: Any,
) -> RedditCampaign | None:
    row = db.get(RedditCampaign, int(campaign_id))
    if row is None:
        return None
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)
    db.commit()
    return row


def delete_campaign(db: "Session", campaign_id: int) -> None:
    row = db.get(RedditCampaign, int(campaign_id))
    if row is None:
        return
    db.delete(row)
    db.commit()


# ── Build queue ───────────────────────────────────────────────────────


def _render_for_lead(
    db: "Session",
    template: RedditTemplate,
    lead: RedditLead,
    profile: RedditUserProfile | None,
) -> str:
    body = template.body or ""
    # variant rotation (простая): random -> берём первый из variants_json если есть
    variants = template.variants_json or []
    if variants:
        from secrets import choice

        body = choice([body] + list(variants))

    topic = ""
    if lead.topic_tags_json and isinstance(lead.topic_tags_json, list) and lead.topic_tags_json:
        topic = str(lead.topic_tags_json[0] or "")

    ctx = {
        "username": lead.username,
        "subreddit": topic.lstrip("r/") if topic else "",
        "last_post_title": (lead.notes or "")[:160],
        "last_comment_body": "",
        "topic": topic,
        "karma": str((profile.karma_link or 0) + (profile.karma_comment or 0)) if profile else "",
    }
    return templates_svc.render_body(body, ctx)


def build_queue_from_filter(
    db: "Session",
    campaign: RedditCampaign,
    *,
    lead_ids: list[int] | None = None,
    max_items: int = 500,
) -> int:
    """Построить очередь сообщений из списка lead_ids (или всех лидов организации).

    Для каждого lead создаётся RedditCampaignQueue с preview-body через плейсхолдеры.
    Возвращает число добавленных записей.
    """
    template = db.get(RedditTemplate, int(campaign.template_id)) if campaign.template_id else None
    if template is None:
        return 0

    q = db.query(RedditLead).filter(RedditLead.organization_id == campaign.organization_id)
    if lead_ids:
        q = q.filter(RedditLead.id.in_(lead_ids))
    leads = q.limit(max_items).all()
    if not leads:
        return 0

    existing = {
        row[0]
        for row in db.query(RedditCampaignQueue.lead_id).filter(
            RedditCampaignQueue.campaign_id == campaign.id
        ).all()
    }

    profiles_map: dict[str, RedditUserProfile] = {
        p.username: p for p in db.query(RedditUserProfile)
        .filter(
            RedditUserProfile.organization_id == campaign.organization_id,
            RedditUserProfile.username.in_([l.username for l in leads if l.username]),
        ).all()
    }

    added = 0
    for lead in leads:
        if lead.id in existing:
            continue
        body = _render_for_lead(db, template, lead, profiles_map.get(lead.username))
        item = RedditCampaignQueue(
            campaign_id=campaign.id,
            lead_id=lead.id,
            account_id=None,
            payload_body=body,
            payload_meta_json={
                "template_id": template.id,
                "mode": campaign.mode,
            },
            review_state="pending_review",
        )
        db.add(item)
        added += 1
    db.commit()
    return added


def list_queue(
    db: "Session",
    campaign_id: int,
    review_state: str | None = None,
) -> list[RedditCampaignQueue]:
    q = db.query(RedditCampaignQueue).filter(RedditCampaignQueue.campaign_id == int(campaign_id))
    if review_state:
        q = q.filter(RedditCampaignQueue.review_state == review_state)
    return q.order_by(RedditCampaignQueue.id.asc()).all()


def approve_item(db: "Session", item_id: int) -> RedditCampaignQueue | None:
    row = db.get(RedditCampaignQueue, int(item_id))
    if row is None:
        return None
    if row.review_state == "pending_review":
        row.review_state = "approved"
        row.reviewed_at = datetime.now(timezone.utc)
        db.commit()
    return row


def reject_item(db: "Session", item_id: int, reason: str | None = None) -> RedditCampaignQueue | None:
    row = db.get(RedditCampaignQueue, int(item_id))
    if row is None:
        return None
    row.review_state = "rejected"
    row.reviewed_at = datetime.now(timezone.utc)
    row.reason_code = (reason or "")[:80] or "rejected_by_user"
    db.commit()
    return row


def skip_item(db: "Session", item_id: int) -> RedditCampaignQueue | None:
    row = db.get(RedditCampaignQueue, int(item_id))
    if row is None:
        return None
    row.review_state = "skipped"
    row.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return row


def bulk_approve(db: "Session", campaign_id: int, item_ids: list[int]) -> int:
    rows = db.query(RedditCampaignQueue).filter(
        RedditCampaignQueue.campaign_id == int(campaign_id),
        RedditCampaignQueue.id.in_(item_ids),
        RedditCampaignQueue.review_state == "pending_review",
    ).all()
    n = 0
    now = datetime.now(timezone.utc)
    for r in rows:
        r.review_state = "approved"
        r.reviewed_at = now
        n += 1
    db.commit()
    return n


def update_item_body(db: "Session", item_id: int, body: str) -> RedditCampaignQueue | None:
    row = db.get(RedditCampaignQueue, int(item_id))
    if row is None:
        return None
    row.payload_body = body
    db.commit()
    return row


def count_by_state(db: "Session", campaign_id: int) -> dict[str, int]:
    out: dict[str, int] = {s: 0 for s in REVIEW_STATES}
    for st in REVIEW_STATES:
        out[st] = (
            db.query(RedditCampaignQueue)
            .filter(
                RedditCampaignQueue.campaign_id == int(campaign_id),
                RedditCampaignQueue.review_state == st,
            )
            .count()
        )
    return out


__all__ = [
    "CAMPAIGN_MODES",
    "CAMPAIGN_STATUSES",
    "REVIEW_STATES",
    "approve_item",
    "build_queue_from_filter",
    "bulk_approve",
    "count_by_state",
    "create_campaign",
    "delete_campaign",
    "get_campaign",
    "list_campaigns",
    "list_queue",
    "reject_item",
    "skip_item",
    "update_campaign",
    "update_item_body",
]
