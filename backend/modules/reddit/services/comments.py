"""Reddit comment drafts: CRUD + publish + toxicity risk."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import (
    RedditActionAttempt,
    RedditCommentDraft,
)
from backend.modules.reddit.services.api_client import RedditApiError, api_call

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.modules.reddit.models import RedditAccount


REVIEW_STATES = ("pending_review", "approved", "published", "failed", "skipped", "rejected")

_TOXIC_HINTS = (
    "scam", "idiot", "stupid", "moron", "trash", "hate", "kill", "shut up",
    "racist", "misogynist",
)


def extract_thing_id_from_permalink(permalink: str) -> str | None:
    """Из permalink-а вида /r/<sub>/comments/<post_id>/<slug>/<comment_id>
    вернуть t3_<post_id> или t1_<comment_id> (если последний компонент — comment).
    """
    if not permalink:
        return None
    p = permalink.strip()
    if p.startswith("http"):
        from urllib.parse import urlparse

        p = urlparse(p).path
    parts = [x for x in p.strip("/").split("/") if x]
    # /r/<sub>/comments/<post_id>/<slug>[/<comment_id>]
    try:
        idx = parts.index("comments")
    except ValueError:
        return None
    after = parts[idx + 1 :]
    if not after:
        return None
    post_id = after[0]
    if len(after) >= 3:
        comment_id = after[2].rstrip("/")
        if comment_id and re.match(r"[a-z0-9]{5,10}", comment_id):
            return f"t1_{comment_id}"
    return f"t3_{post_id}"


def toxicity_risk(body: str) -> float:
    text = (body or "").lower()
    hits = sum(1 for h in _TOXIC_HINTS if h in text)
    risk = min(1.0, hits / 3.0)
    if len(text) < 20 and any(w in text for w in ("nope", "no")):
        risk = max(risk, 0.2)
    return round(risk, 2)


def list_drafts(
    db: "Session",
    organization_id: int | None,
    review_state: str | None = None,
) -> list[RedditCommentDraft]:
    q = db.query(RedditCommentDraft).filter(RedditCommentDraft.organization_id == organization_id)
    if review_state:
        q = q.filter(RedditCommentDraft.review_state == review_state)
    return q.order_by(RedditCommentDraft.id.desc()).all()


def get_draft(db: "Session", draft_id: int) -> RedditCommentDraft | None:
    return db.get(RedditCommentDraft, int(draft_id))


def create_draft(
    db: "Session",
    organization_id: int | None,
    account_id: int | None,
    target_ref: str,
    body: str,
    *,
    review_mode: str = "manual",
) -> RedditCommentDraft:
    thing_id = extract_thing_id_from_permalink(target_ref) or ""
    target_kind = "comment" if thing_id.startswith("t1_") else "post"
    row = RedditCommentDraft(
        organization_id=organization_id,
        account_id=account_id,
        target_ref=target_ref,
        target_kind=target_kind,
        body=body,
        review_mode=review_mode if review_mode in ("manual", "semi", "auto") else "manual",
        review_state="pending_review",
        toxicity_risk_score=toxicity_risk(body),
    )
    db.add(row)
    db.commit()
    return row


def update_draft(
    db: "Session",
    draft_id: int,
    body: str | None = None,
    account_id: int | None = None,
) -> RedditCommentDraft | None:
    row = db.get(RedditCommentDraft, int(draft_id))
    if row is None or row.review_state in ("published",):
        return row
    if body is not None:
        row.body = body
        row.toxicity_risk_score = toxicity_risk(body)
    if account_id is not None:
        row.account_id = account_id or None
    db.commit()
    return row


def approve_draft(db: "Session", draft_id: int) -> RedditCommentDraft | None:
    row = db.get(RedditCommentDraft, int(draft_id))
    if row is None or row.review_state != "pending_review":
        return row
    row.review_state = "approved"
    row.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return row


def reject_draft(db: "Session", draft_id: int, reason: str | None = None) -> RedditCommentDraft | None:
    row = db.get(RedditCommentDraft, int(draft_id))
    if row is None:
        return None
    row.review_state = "rejected"
    row.reason_code = (reason or "rejected_by_user")[:80]
    row.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return row


def delete_draft(db: "Session", draft_id: int) -> None:
    row = db.get(RedditCommentDraft, int(draft_id))
    if row is None:
        return
    db.delete(row)
    db.commit()


def publish_draft(db: "Session", draft_id: int) -> dict[str, Any]:
    """Публикация approved-комментария через /api/comment."""
    row = db.get(RedditCommentDraft, int(draft_id))
    if row is None:
        return {"ok": False, "reason_code": "not_found"}
    if row.review_state != "approved":
        return {"ok": False, "reason_code": "not_approved"}

    from backend.modules.reddit.models import RedditAccount

    account: RedditAccount | None = db.get(RedditAccount, int(row.account_id)) if row.account_id else None
    if account is None:
        row.review_state = "failed"
        row.reason_code = "account_missing"
        db.commit()
        return {"ok": False, "reason_code": "account_missing"}

    thing_id = extract_thing_id_from_permalink(row.target_ref)
    if not thing_id:
        row.review_state = "failed"
        row.reason_code = "bad_permalink"
        row.last_error = "Cannot extract thing_id from target_ref"
        db.commit()
        return {"ok": False, "reason_code": "bad_permalink"}

    attempt = RedditActionAttempt(
        organization_id=row.organization_id,
        account_id=account.id,
        action_kind="publish_comment",
        target_ref=row.target_ref,
        source_id=row.id,
        source_kind="comment_draft",
        outcome="pending",
    )
    db.add(attempt)
    db.flush()

    data = {
        "api_type": "json",
        "thing_id": thing_id,
        "text": row.body or "",
    }
    try:
        resp = api_call(db, account, "POST", "/api/comment", data=data)
    except RedditApiError as exc:
        row.review_state = "failed"
        row.reason_code = (exc.reason_code or "api_error")[:80]
        row.last_error = str(exc)[:1000]
        attempt.outcome = "failed"
        attempt.reason_code = row.reason_code
        attempt.detail = str(exc)[:1000]
        db.commit()
        return {"ok": False, "reason_code": row.reason_code}

    payload = resp.data or {}
    errors = []
    thing_payload = None
    if isinstance(payload, dict):
        errors = payload.get("json", {}).get("errors", []) or []
        things = payload.get("json", {}).get("data", {}).get("things", []) or []
        if things and isinstance(things, list):
            thing_payload = (things[0] or {}).get("data") or {}
    if errors:
        row.review_state = "failed"
        row.reason_code = "reddit_api_rejected"
        row.last_error = str(errors)[:1000]
        attempt.outcome = "failed"
        attempt.reason_code = "reddit_api_rejected"
        attempt.detail = str(errors)[:1000]
        db.commit()
        return {"ok": False, "reason_code": "reddit_api_rejected"}

    row.review_state = "published"
    row.published_at = datetime.now(timezone.utc)
    if isinstance(thing_payload, dict):
        row.reddit_comment_id = thing_payload.get("name")
    attempt.outcome = "ok"
    db.commit()
    return {"ok": True, "reason_code": None}


__all__ = [
    "REVIEW_STATES",
    "approve_draft",
    "create_draft",
    "delete_draft",
    "extract_thing_id_from_permalink",
    "get_draft",
    "list_drafts",
    "publish_draft",
    "reject_draft",
    "toxicity_risk",
    "update_draft",
]
