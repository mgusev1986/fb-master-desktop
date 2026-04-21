"""Reddit conversations: compose, draft review, send via API.

PM (private messages) — через POST /api/compose.
Chat (modern) — частично поддерживается; на старте используем PM как
основной канал.

Все сообщения по умолчанию проходят через ручной review:
  draft(pending_review) → user approves → send → marks as sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import (
    RedditActionAttempt,
    RedditCampaign,
    RedditCampaignQueue,
    RedditComplianceEvent,
    RedditConversation,
    RedditMessage,
    RedditMessageDraft,
)
from backend.modules.reddit.services.api_client import RedditApiError, api_call

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.modules.reddit.models import RedditAccount


REVIEW_STATES = ("pending_review", "approved", "sent", "failed", "skipped", "rejected")


def list_conversations(db: "Session", organization_id: int | None) -> list[RedditConversation]:
    return (
        db.query(RedditConversation)
        .filter(RedditConversation.organization_id == organization_id)
        .order_by(RedditConversation.last_message_at.desc().nullslast(), RedditConversation.id.desc())
        .all()
    )


def get_or_create_conversation(
    db: "Session",
    organization_id: int | None,
    account: "RedditAccount",
    counterpart_username: str,
    *,
    subject: str | None = None,
    thread_kind: str = "pm",
) -> RedditConversation:
    counterpart = counterpart_username.strip().lstrip("u/")
    row = (
        db.query(RedditConversation)
        .filter(
            RedditConversation.organization_id == organization_id,
            RedditConversation.account_id == account.id,
            RedditConversation.counterpart_username == counterpart,
        )
        .one_or_none()
    )
    if row is None:
        row = RedditConversation(
            organization_id=organization_id,
            account_id=account.id,
            counterpart_username=counterpart,
            thread_kind=thread_kind,
            subject=subject,
        )
        db.add(row)
        db.flush()
    return row


def create_draft(
    db: "Session",
    conversation: RedditConversation,
    body: str,
    *,
    review_mode: str = "manual",
    context_ref: str | None = None,
) -> RedditMessageDraft:
    draft = RedditMessageDraft(
        conversation_id=conversation.id,
        account_id=conversation.account_id,
        body=body,
        review_mode=review_mode if review_mode in ("manual", "semi", "auto") else "manual",
        context_ref=context_ref,
        review_state="pending_review",
    )
    db.add(draft)
    db.commit()
    return draft


def list_drafts(
    db: "Session",
    conversation_id: int,
) -> list[RedditMessageDraft]:
    return (
        db.query(RedditMessageDraft)
        .filter(RedditMessageDraft.conversation_id == conversation_id)
        .order_by(RedditMessageDraft.id.asc())
        .all()
    )


def approve_draft(db: "Session", draft_id: int) -> RedditMessageDraft | None:
    draft = db.get(RedditMessageDraft, int(draft_id))
    if draft is None or draft.review_state != "pending_review":
        return draft
    draft.review_state = "approved"
    draft.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return draft


def reject_draft(db: "Session", draft_id: int, reason: str | None = None) -> RedditMessageDraft | None:
    draft = db.get(RedditMessageDraft, int(draft_id))
    if draft is None:
        return None
    draft.review_state = "rejected"
    draft.reason_code = (reason or "rejected_by_user")[:80]
    draft.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return draft


def update_draft_body(db: "Session", draft_id: int, body: str) -> RedditMessageDraft | None:
    draft = db.get(RedditMessageDraft, int(draft_id))
    if draft is None or draft.review_state in ("sent", "rejected"):
        return draft
    draft.body = body
    db.commit()
    return draft


def send_draft(
    db: "Session",
    draft_id: int,
) -> dict[str, Any]:
    """Отправить approved-draft через Reddit /api/compose.

    Возвращает {'ok', 'draft', 'reason_code'}.
    Обновляет attempts + conversation last_message.
    """
    draft = db.get(RedditMessageDraft, int(draft_id))
    if draft is None:
        return {"ok": False, "reason_code": "draft_not_found"}
    if draft.review_state not in ("approved",):
        return {"ok": False, "reason_code": "draft_not_approved"}

    conv = db.get(RedditConversation, int(draft.conversation_id))
    if conv is None:
        draft.review_state = "failed"
        draft.reason_code = "conversation_not_found"
        db.commit()
        return {"ok": False, "reason_code": "conversation_not_found"}

    account = db.get(type(conv).__mapper__.relationships["account"].mapper.class_, int(conv.account_id)) if conv.account_id else None
    if account is None:
        # fallback — прямой lookup
        from backend.modules.reddit.models import RedditAccount

        account = db.get(RedditAccount, int(draft.account_id)) if draft.account_id else None
    if account is None:
        draft.review_state = "failed"
        draft.reason_code = "account_missing"
        db.commit()
        return {"ok": False, "reason_code": "account_missing"}

    attempt = RedditActionAttempt(
        organization_id=conv.organization_id,
        account_id=account.id,
        action_kind="send_dm",
        target_ref=f"u/{conv.counterpart_username}",
        source_id=draft.id,
        source_kind="message_draft",
        outcome="pending",
    )
    db.add(attempt)
    db.flush()

    form = {
        "api_type": "json",
        "to": conv.counterpart_username,
        "subject": (conv.subject or "Hi")[:100],
        "text": draft.body or "",
    }

    try:
        resp = api_call(db, account, "POST", "/api/compose", data=form)
    except RedditApiError as exc:
        draft.review_state = "failed"
        draft.reason_code = (exc.reason_code or "api_error")[:80]
        draft.last_error = str(exc)[:1000]
        attempt.outcome = "failed"
        attempt.reason_code = draft.reason_code
        attempt.detail = str(exc)[:1000]
        db.commit()
        return {"ok": False, "reason_code": draft.reason_code}

    # Reddit отвечает {'json': {'errors': [...], 'data': {...}}}
    payload = resp.data or {}
    errors = []
    if isinstance(payload, dict):
        errors = payload.get("json", {}).get("errors", []) or []
    if errors:
        draft.review_state = "failed"
        draft.reason_code = "reddit_api_rejected"
        draft.last_error = str(errors)[:1000]
        attempt.outcome = "failed"
        attempt.reason_code = "reddit_api_rejected"
        attempt.detail = str(errors)[:1000]
        db.commit()
        return {"ok": False, "reason_code": "reddit_api_rejected"}

    now = datetime.now(timezone.utc)
    draft.review_state = "sent"
    draft.sent_at = now
    attempt.outcome = "ok"
    conv.last_message_at = now
    conv.last_message_from = "us"
    db.add(
        RedditMessage(
            conversation_id=conv.id,
            account_id=account.id,
            direction="out",
            body=draft.body or "",
            subject=conv.subject,
            sent_at=now,
            draft_id=draft.id,
        )
    )
    db.commit()
    return {"ok": True, "reason_code": None}


def send_campaign_queue_item(db: "Session", item_id: int) -> dict[str, Any]:
    """Отправить approved-item из RedditCampaignQueue (PM-режим)."""
    item = db.get(RedditCampaignQueue, int(item_id))
    if item is None:
        return {"ok": False, "reason_code": "item_not_found"}
    if item.review_state != "approved":
        return {"ok": False, "reason_code": "not_approved"}

    campaign = db.get(RedditCampaign, int(item.campaign_id))
    if campaign is None:
        return {"ok": False, "reason_code": "campaign_not_found"}

    if item.lead is None or not item.lead.username:
        item.review_state = "failed"
        item.reason_code = "lead_missing"
        db.commit()
        return {"ok": False, "reason_code": "lead_missing"}

    # account: campaign-owner's first connected (если не проставлен явно)
    from backend.modules.reddit.models import RedditAccount

    account = db.get(RedditAccount, int(item.account_id)) if item.account_id else None
    if account is None:
        account = (
            db.query(RedditAccount)
            .filter(
                RedditAccount.organization_id == campaign.organization_id,
                RedditAccount.status == "connected",
            )
            .first()
        )
        if account is None:
            item.review_state = "failed"
            item.reason_code = "no_connected_account"
            db.commit()
            return {"ok": False, "reason_code": "no_connected_account"}
        item.account_id = account.id

    if campaign.mode == "dm":
        conv = get_or_create_conversation(db, campaign.organization_id, account, item.lead.username)
        draft = create_draft(db, conv, item.payload_body or "")
        # approved автоматически, раз уже approved на уровне queue
        draft.review_state = "approved"
        draft.reviewed_at = datetime.now(timezone.utc)
        db.commit()
        result = send_draft(db, draft.id)
        if result.get("ok"):
            item.review_state = "sent"
            item.sent_at = datetime.now(timezone.utc)
        else:
            item.review_state = "failed"
            item.reason_code = result.get("reason_code")
        db.commit()
        return result
    # comment-режим — обрабатывается в M5.8 через comments service
    item.review_state = "failed"
    item.reason_code = "comment_mode_not_in_this_service"
    db.commit()
    return {"ok": False, "reason_code": "comment_mode_not_in_this_service"}


def list_messages(db: "Session", conversation_id: int) -> list[dict[str, Any]]:
    """Все сообщения треда (in + out) в хронологическом порядке."""
    rows = (
        db.query(RedditMessage)
        .filter(RedditMessage.conversation_id == int(conversation_id))
        .order_by(RedditMessage.sent_at.asc(), RedditMessage.id.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for m in rows:
        out.append(
            {
                "id": m.id,
                "direction": m.direction,
                "body": m.body or "",
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "read_at": m.read_at.isoformat() if m.read_at else None,
                "reddit_message_id": m.reddit_message_id,
                "draft_id": m.draft_id,
            }
        )
    # Также подмешиваем pending/approved drafts (ещё не отправленные) — чтобы юзер видел свой черновик
    pending = (
        db.query(RedditMessageDraft)
        .filter(
            RedditMessageDraft.conversation_id == int(conversation_id),
            RedditMessageDraft.review_state.in_(("pending_review", "approved", "failed")),
        )
        .order_by(RedditMessageDraft.id.asc())
        .all()
    )
    for d in pending:
        out.append(
            {
                "id": f"draft-{d.id}",
                "direction": "out",
                "body": d.body or "",
                "sent_at": (d.created_at or datetime.now(timezone.utc)).isoformat(),
                "read_at": None,
                "reddit_message_id": None,
                "draft_id": d.id,
                "draft_state": d.review_state,
                "last_error": d.last_error,
            }
        )
    out.sort(key=lambda x: x["sent_at"] or "")
    return out


def mark_conversation_read(db: "Session", conversation_id: int) -> int:
    """Проставляет read_at всем непрочитанным входящим сообщениям. Возвращает кол-во."""
    now = datetime.now(timezone.utc)
    rows = (
        db.query(RedditMessage)
        .filter(
            RedditMessage.conversation_id == int(conversation_id),
            RedditMessage.direction == "in",
            RedditMessage.read_at.is_(None),
        )
        .all()
    )
    for r in rows:
        r.read_at = now
    db.commit()
    return len(rows)


def unread_count_by_conversation(db: "Session", organization_id: int | None) -> dict[int, int]:
    """{conversation_id: unread_in_count} — для бейджей в списке."""
    from sqlalchemy import func

    rows = (
        db.query(RedditMessage.conversation_id, func.count(RedditMessage.id))
        .join(RedditConversation, RedditConversation.id == RedditMessage.conversation_id)
        .filter(
            RedditConversation.organization_id == organization_id,
            RedditMessage.direction == "in",
            RedditMessage.read_at.is_(None),
        )
        .group_by(RedditMessage.conversation_id)
        .all()
    )
    return {int(cid): int(cnt) for cid, cnt in rows}


def poll_inbox(
    db: "Session",
    account: "RedditAccount",
    organization_id: int | None,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Опросить /message/inbox для аккаунта, сохранить новые входящие PM."""
    try:
        resp = api_call(db, account, "GET", "/message/inbox", params={"limit": limit, "raw_json": 1})
    except RedditApiError as exc:
        db.add(
            RedditComplianceEvent(
                organization_id=organization_id,
                account_id=account.id,
                event_type="inbox_poll_failed",
                severity="warn",
                payload_json={"reason": exc.reason_code, "detail": str(exc)[:500]},
            )
        )
        db.commit()
        return {"ok": False, "reason_code": exc.reason_code or "api_error", "saved": 0}

    data = resp.data or {}
    children = []
    if isinstance(data, dict):
        children = (data.get("data") or {}).get("children") or []

    saved = 0
    for ch in children:
        m = (ch or {}).get("data") or {}
        kind = (ch or {}).get("kind") or ""
        if kind != "t4":  # t4 = private message
            continue
        rid = m.get("id") or m.get("name")
        if not rid:
            continue
        rid = str(rid)
        # дедупликация
        exists = db.query(RedditMessage).filter(RedditMessage.reddit_message_id == rid).one_or_none()
        if exists is not None:
            continue
        author = (m.get("author") or "").strip()
        if not author or author.lower() == (account.username_snapshot or "").lower():
            continue
        body = m.get("body") or ""
        subject = m.get("subject") or None
        created = m.get("created_utc")
        try:
            sent_at = datetime.fromtimestamp(float(created), tz=timezone.utc) if created else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            sent_at = datetime.now(timezone.utc)

        conv = get_or_create_conversation(db, organization_id, account, author, subject=subject)
        db.add(
            RedditMessage(
                conversation_id=conv.id,
                account_id=account.id,
                direction="in",
                body=body,
                subject=subject,
                sent_at=sent_at,
                reddit_message_id=rid,
            )
        )
        if conv.last_message_at is None or conv.last_message_at < sent_at:
            conv.last_message_at = sent_at
            conv.last_message_from = "them"
        saved += 1

    db.commit()
    return {"ok": True, "saved": saved, "total_seen": len(children)}


__all__ = [
    "REVIEW_STATES",
    "approve_draft",
    "create_draft",
    "get_or_create_conversation",
    "list_conversations",
    "list_drafts",
    "list_messages",
    "mark_conversation_read",
    "poll_inbox",
    "reject_draft",
    "send_campaign_queue_item",
    "send_draft",
    "unread_count_by_conversation",
    "update_draft_body",
]
