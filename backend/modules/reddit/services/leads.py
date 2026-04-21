"""Lead persistence + фильтры + bulk actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_

from backend.modules.reddit.models import RedditLead, RedditUserProfile

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


LEAD_STATUSES = ("new", "contacted", "replied", "skipped", "blocked", "failed", "dnd")


@dataclass(frozen=True)
class LeadFilter:
    search: str = ""
    status: str = ""
    source_type: str = ""
    owner_account_id: int | None = None
    has_campaign: str = ""  # "", "yes", "no"


def list_leads(
    db: "Session",
    organization_id: int | None,
    f: LeadFilter,
    *,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[RedditLead], int]:
    q = db.query(RedditLead).filter(RedditLead.organization_id == organization_id)
    if f.search:
        pat = f"%{f.search.lower()}%"
        q = q.filter(
            or_(
                RedditLead.username.ilike(pat),
                RedditLead.notes.ilike(pat),
                RedditLead.source_ref.ilike(pat),
            )
        )
    if f.status:
        q = q.filter(RedditLead.status == f.status)
    if f.source_type:
        q = q.filter(RedditLead.source_type == f.source_type)
    if f.owner_account_id is not None:
        q = q.filter(RedditLead.owner_account_id == f.owner_account_id)
    if f.has_campaign == "yes":
        q = q.filter(RedditLead.assigned_campaign_id.isnot(None))
    elif f.has_campaign == "no":
        q = q.filter(RedditLead.assigned_campaign_id.is_(None))

    total = q.count()
    per_page = max(10, min(500, per_page))
    offset = max(0, (page - 1) * per_page)
    rows = q.order_by(RedditLead.activity_timestamp.desc().nullslast(), RedditLead.id.desc()).offset(offset).limit(per_page).all()
    return rows, total


def get_lead(db: "Session", lead_id: int) -> RedditLead | None:
    return db.get(RedditLead, int(lead_id))


def update_lead(
    db: "Session",
    lead_id: int,
    *,
    notes: str | None = None,
    persona_tags: str | None = None,
    status: str | None = None,
    owner_account_id: int | None = None,
    assigned_campaign_id: int | None = None,
    crm_stage_id: int | None = None,
) -> RedditLead | None:
    lead = db.get(RedditLead, int(lead_id))
    if lead is None:
        return None
    if notes is not None:
        lead.notes = notes[:2000]
    if persona_tags is not None:
        lead.persona_tags = persona_tags[:200]
    if status is not None and status in LEAD_STATUSES:
        lead.status = status
    if owner_account_id is not None:
        lead.owner_account_id = owner_account_id or None
    if assigned_campaign_id is not None:
        lead.assigned_campaign_id = assigned_campaign_id or None
    if crm_stage_id is not None:
        lead.crm_stage_id = crm_stage_id or None
    db.commit()
    return lead


def bulk_set_status(db: "Session", organization_id: int | None, lead_ids: list[int], status: str) -> int:
    if status not in LEAD_STATUSES:
        return 0
    q = (
        db.query(RedditLead)
        .filter(
            RedditLead.organization_id == organization_id,
            RedditLead.id.in_(lead_ids),
        )
    )
    n = 0
    for lead in q.all():
        lead.status = status
        n += 1
    db.commit()
    return n


def bulk_delete(db: "Session", organization_id: int | None, lead_ids: list[int]) -> int:
    q = (
        db.query(RedditLead)
        .filter(
            RedditLead.organization_id == organization_id,
            RedditLead.id.in_(lead_ids),
        )
    )
    rows = q.all()
    n = len(rows)
    for r in rows:
        db.delete(r)
    db.commit()
    return n


def serialize_row(lead: RedditLead, user: RedditUserProfile | None) -> dict[str, Any]:
    return {
        "id": lead.id,
        "username": lead.username,
        "status": lead.status,
        "source_type": lead.source_type,
        "source_ref": lead.source_ref,
        "activity_timestamp": lead.activity_timestamp,
        "last_touch_at": lead.last_touch_at,
        "notes": lead.notes or "",
        "persona_tags": lead.persona_tags or "",
        "owner_account_id": lead.owner_account_id,
        "assigned_campaign_id": lead.assigned_campaign_id,
        "user_profile": {
            "karma_link": user.karma_link if user else None,
            "karma_comment": user.karma_comment if user else None,
            "account_age_days": user.account_age_days if user else None,
            "last_visible_action_at": user.last_visible_action_at if user else None,
        } if user else None,
    }


def load_rows_for_render(
    db: "Session",
    organization_id: int | None,
    f: LeadFilter,
    *,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    leads, total = list_leads(db, organization_id, f, page=page, per_page=per_page)
    # собрать user-profile'и одним запросом
    usernames = [l.username for l in leads if l.username]
    profiles: dict[str, RedditUserProfile] = {}
    if usernames:
        rows = (
            db.query(RedditUserProfile)
            .filter(
                RedditUserProfile.organization_id == organization_id,
                RedditUserProfile.username.in_(usernames),
            )
            .all()
        )
        profiles = {p.username: p for p in rows}
    data = [serialize_row(l, profiles.get(l.username)) for l in leads]
    return data, total


__all__ = [
    "LEAD_STATUSES",
    "LeadFilter",
    "bulk_delete",
    "bulk_set_status",
    "get_lead",
    "list_leads",
    "load_rows_for_render",
    "serialize_row",
    "update_lead",
]
