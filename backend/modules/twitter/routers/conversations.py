"""Twitter conversations / inbox."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.twitter.models import (
    TwitterConversation,
    TwitterLead,
    TwitterMessage,
)

router = APIRouter(prefix="/twitter/conversations")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def conversations_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        items = (
            db.query(TwitterConversation)
            .filter(TwitterConversation.organization_id == org_id)
            .order_by(TwitterConversation.last_message_at.desc().nullslast(), TwitterConversation.id.desc())
            .limit(500)
            .all()
        )
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "twitter/conversations/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "twitter_conversations",
            "conversations": items,
            "notice": request.query_params.get("notice"),
        },
    )


@router.get("/{conv_id}", response_class=HTMLResponse)
async def conversations_detail(conv_id: int, request: Request):
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        org_id = _org_id(request)
        conv = db.get(TwitterConversation, int(conv_id))
        if conv is None or conv.organization_id != org_id:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        msgs = (
            db.query(TwitterMessage)
            .filter(TwitterMessage.conversation_id == conv.id)
            .order_by(TwitterMessage.sent_at.asc(), TwitterMessage.id.asc())
            .all()
        )
        # Mark inbound as read.
        n = (
            db.query(TwitterMessage)
            .filter(
                TwitterMessage.conversation_id == conv.id,
                TwitterMessage.direction == "in",
                TwitterMessage.read_at.is_(None),
            )
            .count()
        )
        if n:
            db.query(TwitterMessage).filter(
                TwitterMessage.conversation_id == conv.id,
                TwitterMessage.direction == "in",
                TwitterMessage.read_at.is_(None),
            ).update({"read_at": datetime.now(timezone.utc)}, synchronize_session=False)
            conv.unread_count = 0
            db.commit()

        leads = (
            db.query(TwitterLead).filter(TwitterLead.organization_id == org_id).limit(500).all()
        )
        linked_lead = next((l for l in leads if l.id == conv.lead_id), None) if conv.lead_id else None
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "twitter/conversations/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "twitter_conversations",
            "conversation": conv,
            "messages": msgs,
            "leads": leads,
            "linked_lead": linked_lead,
            "marked_read": n,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/{conv_id}/send")
async def conversations_send(conv_id: int, request: Request, body: str = Form(...), headless: str = Form(default="on")):
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        conv = db.get(TwitterConversation, int(conv_id))
        if conv is None:
            return RedirectResponse("/twitter/conversations?notice=conv_not_found", status_code=303)
        body = (body or "").strip()
        if not body:
            return RedirectResponse(f"/twitter/conversations/{conv_id}?notice=empty_body", status_code=303)
        # Если есть linked_lead — используем его handle. Иначе counterpart_handle.
        target_handle = ""
        if conv.lead_id:
            lead = db.get(TwitterLead, conv.lead_id)
            target_handle = (lead.handle if lead else "") or ""
        if not target_handle:
            target_handle = (conv.counterpart_handle or "")
        if not target_handle:
            return RedirectResponse(f"/twitter/conversations/{conv_id}?notice=no_target", status_code=303)

        from backend.modules.twitter.runtime.dm_sender import send_dm

        try:
            res = send_dm(
                db, account_id=conv.account_id, to_handle=target_handle, body=body,
                headless=(headless.lower() in ("on", "1", "true", "yes")),
            )
            if res.get("ok"):
                # Записываем outgoing message в БД.
                m = TwitterMessage(
                    conversation_id=conv.id, direction="out",
                    body=body, sent_at=datetime.now(timezone.utc),
                    out_source="manual",
                )
                db.add(m)
                conv.last_message_at = m.sent_at
                conv.last_message_from = "out"
                db.commit()
                qs = "send_ok"
            else:
                qs = f"send_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"send_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/twitter/conversations/{conv_id}?notice={qs}", status_code=303)


@router.post("/{conv_id}/link-lead")
async def conversations_link_lead(conv_id: int, request: Request, lead_id: str = Form(default="")):
    db = SessionLocal()
    try:
        conv = db.get(TwitterConversation, int(conv_id))
        if conv is None:
            return RedirectResponse("/twitter/conversations?notice=conv_not_found", status_code=303)
        if not lead_id or not lead_id.isdigit():
            conv.lead_id = None
        else:
            lead = db.get(TwitterLead, int(lead_id))
            if lead is None or lead.organization_id != conv.organization_id:
                return RedirectResponse(f"/twitter/conversations/{conv_id}?notice=lead_not_found", status_code=303)
            conv.lead_id = lead.id
        db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/twitter/conversations/{conv_id}?notice=linked", status_code=303)


__all__ = ["router"]
