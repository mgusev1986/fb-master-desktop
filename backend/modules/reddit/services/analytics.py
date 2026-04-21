"""Reddit Master analytics — сводные подсчёты."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func

from backend.modules.reddit.models import (
    RedditAccount,
    RedditActionAttempt,
    RedditCampaign,
    RedditCampaignQueue,
    RedditCommentDraft,
    RedditComplianceEvent,
    RedditConversation,
    RedditLead,
    RedditMessageDraft,
    RedditSubreddit,
    RedditUserProfile,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _group_count(db: "Session", model, column, *filters) -> dict[str, int]:
    q = db.query(column, func.count()).group_by(column)
    for f in filters:
        q = q.filter(f)
    out: dict[str, int] = {}
    for key, n in q.all():
        out[str(key) if key is not None else "—"] = int(n)
    return out


def dashboard(db: "Session", organization_id: int | None) -> dict[str, Any]:
    def _filter(model):
        return model.organization_id == organization_id

    since_7d = datetime.now(timezone.utc) - timedelta(days=7)

    accounts_total = db.query(RedditAccount).filter(_filter(RedditAccount)).count()
    accounts_by_status = _group_count(db, RedditAccount, RedditAccount.status, _filter(RedditAccount))

    subreddits_total = db.query(RedditSubreddit).filter(_filter(RedditSubreddit)).count()

    leads_total = db.query(RedditLead).filter(_filter(RedditLead)).count()
    leads_by_status = _group_count(db, RedditLead, RedditLead.status, _filter(RedditLead))
    leads_by_source = _group_count(db, RedditLead, RedditLead.source_type, _filter(RedditLead))

    profiles_total = db.query(RedditUserProfile).filter(_filter(RedditUserProfile)).count()

    campaigns_total = db.query(RedditCampaign).filter(_filter(RedditCampaign)).count()
    campaigns_by_status = _group_count(db, RedditCampaign, RedditCampaign.status, _filter(RedditCampaign))

    queue_by_state = _group_count(
        db, RedditCampaignQueue, RedditCampaignQueue.review_state,
        RedditCampaignQueue.campaign_id.in_(
            db.query(RedditCampaign.id).filter(_filter(RedditCampaign))
        ),
    )

    msg_drafts_by_state = _group_count(
        db, RedditMessageDraft, RedditMessageDraft.review_state,
        RedditMessageDraft.conversation_id.in_(
            db.query(RedditConversation.id).filter(_filter(RedditConversation))
        ),
    )

    comment_drafts_by_state = _group_count(
        db, RedditCommentDraft, RedditCommentDraft.review_state,
        _filter(RedditCommentDraft),
    )

    attempts_total = db.query(RedditActionAttempt).filter(_filter(RedditActionAttempt)).count()
    attempts_by_outcome = _group_count(
        db, RedditActionAttempt, RedditActionAttempt.outcome, _filter(RedditActionAttempt)
    )
    attempts_by_kind = _group_count(
        db, RedditActionAttempt, RedditActionAttempt.action_kind, _filter(RedditActionAttempt)
    )

    attempts_last_7d = (
        db.query(RedditActionAttempt)
        .filter(
            _filter(RedditActionAttempt),
            RedditActionAttempt.attempted_at >= since_7d,
        )
        .count()
    )
    failed_last_7d = (
        db.query(RedditActionAttempt)
        .filter(
            _filter(RedditActionAttempt),
            RedditActionAttempt.attempted_at >= since_7d,
            RedditActionAttempt.outcome == "failed",
        )
        .count()
    )

    compliance_last_7d = (
        db.query(RedditComplianceEvent)
        .filter(
            _filter(RedditComplianceEvent),
            RedditComplianceEvent.at >= since_7d,
        )
        .count()
    )

    # Recent failure reason codes
    recent_failures = (
        db.query(RedditActionAttempt.reason_code, func.count())
        .filter(
            _filter(RedditActionAttempt),
            RedditActionAttempt.outcome == "failed",
            RedditActionAttempt.attempted_at >= since_7d,
        )
        .group_by(RedditActionAttempt.reason_code)
        .order_by(func.count().desc())
        .limit(10)
        .all()
    )

    return {
        "accounts": {
            "total": accounts_total,
            "by_status": accounts_by_status,
        },
        "subreddits_total": subreddits_total,
        "leads": {
            "total": leads_total,
            "by_status": leads_by_status,
            "by_source": leads_by_source,
        },
        "profiles_total": profiles_total,
        "campaigns": {
            "total": campaigns_total,
            "by_status": campaigns_by_status,
            "queue_by_state": queue_by_state,
        },
        "messages": {
            "drafts_by_state": msg_drafts_by_state,
        },
        "comments": {
            "drafts_by_state": comment_drafts_by_state,
        },
        "attempts": {
            "total": attempts_total,
            "by_outcome": attempts_by_outcome,
            "by_kind": attempts_by_kind,
            "last_7d": attempts_last_7d,
            "failed_last_7d": failed_last_7d,
        },
        "compliance_events_last_7d": compliance_last_7d,
        "recent_failure_reasons": [
            {"code": r[0] or "—", "count": int(r[1])} for r in recent_failures
        ],
    }


__all__ = ["dashboard"]
