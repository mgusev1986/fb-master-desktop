"""Reddit conversations router (inbox + compose + manual send)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit import config as reddit_config
from backend.modules.reddit.services import accounts as accounts_svc
from backend.modules.reddit.services import conversations as conv_svc

router = APIRouter(prefix="/reddit/conversations")
api_router = APIRouter(prefix="/reddit/api/conversations")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


def _pick_account(db, org_id):
    for a in accounts_svc.list_accounts(db, org_id):
        if (a.status or "") == "connected":
            return a
    return None


@router.get("", response_class=HTMLResponse)
async def conversations_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        conversations = conv_svc.list_conversations(db, org_id)
        has_account = _pick_account(db, org_id) is not None
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/conversations/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_conversations",
            "conversations": conversations,
            "has_account": has_account,
            "notice": request.query_params.get("notice"),
        },
    )


@router.get("/compose", response_class=HTMLResponse)
async def conversations_compose(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        accounts = [
            a for a in accounts_svc.list_accounts(db, org_id) if (a.status or "") == "connected"
        ]
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/conversations/compose.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_conversations",
            "accounts": accounts,
            "prefill_username": request.query_params.get("to", "").strip(),
        },
    )


@router.post("/compose")
async def conversations_compose_submit(
    request: Request,
    account_id: str = Form(default=""),
    to: str = Form(default=""),
    subject: str = Form(default=""),
    body: str = Form(default=""),
):
    if not account_id.strip().isdigit() or not to.strip() or not body.strip():
        return RedirectResponse("/reddit/conversations/compose?notice=missing_fields", status_code=303)
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        account = accounts_svc.get_account(db, int(account_id))
        if account is None:
            return RedirectResponse("/reddit/conversations/compose?notice=account_missing", status_code=303)
        conv = conv_svc.get_or_create_conversation(
            db, org_id, account, to, subject=subject or None
        )
        db.flush()  # нужен conv.id для draft
        conv_svc.create_draft(db, conv, body)
        conv_id = conv.id
    finally:
        db.close()
    return RedirectResponse(f"/reddit/conversations/{conv_id}", status_code=303)


@router.get("/{conversation_id}", response_class=HTMLResponse)
async def conversation_detail(conversation_id: int, request: Request):
    """Старая HTML-страница диалога заменена на двухколоночный мессенджер.

    Сохраняем роут — редиректим на /reddit/conversations#conv=<id>, чтобы
    JS-мессенджер сразу открыл нужный тред.
    """
    return RedirectResponse(f"/reddit/conversations#conv={int(conversation_id)}", status_code=303)


@router.post("/{conversation_id}/draft/{draft_id}/approve")
async def draft_approve(conversation_id: int, draft_id: int, request: Request):
    db = SessionLocal()
    try:
        conv_svc.approve_draft(db, draft_id)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/conversations/{conversation_id}", status_code=303)


@router.post("/{conversation_id}/draft/{draft_id}/reject")
async def draft_reject(conversation_id: int, draft_id: int, request: Request):
    db = SessionLocal()
    try:
        conv_svc.reject_draft(db, draft_id)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/conversations/{conversation_id}", status_code=303)


@router.post("/{conversation_id}/draft/{draft_id}/edit")
async def draft_edit(conversation_id: int, draft_id: int, request: Request, body: str = Form(default="")):
    db = SessionLocal()
    try:
        conv_svc.update_draft_body(db, draft_id, body)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/conversations/{conversation_id}", status_code=303)


@router.post("/{conversation_id}/draft/{draft_id}/send")
async def draft_send(conversation_id: int, draft_id: int, request: Request):
    db = SessionLocal()
    try:
        result = conv_svc.send_draft(db, draft_id)
    finally:
        db.close()
    notice = "sent" if result.get("ok") else f"failed_{result.get('reason_code') or 'unknown'}"
    return RedirectResponse(f"/reddit/conversations/{conversation_id}?notice={notice}", status_code=303)


# ───── JSON API для мессенджера ─────


def _conv_brief(conv, unread: int) -> dict[str, Any]:
    return {
        "id": int(conv.id),
        "account_id": int(conv.account_id) if conv.account_id else None,
        "counterpart_username": conv.counterpart_username,
        "subject": conv.subject,
        "thread_kind": conv.thread_kind,
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        "last_message_from": conv.last_message_from,
        "unread_count": int(unread),
    }


@api_router.get("")
async def api_list_conversations(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        rows = conv_svc.list_conversations(db, org_id)
        unread = conv_svc.unread_count_by_conversation(db, org_id)
        items = [_conv_brief(c, unread.get(int(c.id), 0)) for c in rows]
    finally:
        db.close()
    return JSONResponse({"items": items})


@api_router.get("/{conversation_id}/messages")
async def api_conversation_messages(conversation_id: int, request: Request):
    from backend.modules.reddit.models import RedditConversation

    db = SessionLocal()
    try:
        org_id = _org_id(request)
        conv = db.get(RedditConversation, int(conversation_id))
        if conv is None or conv.organization_id != org_id:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        unread = conv_svc.unread_count_by_conversation(db, org_id).get(int(conv.id), 0)
        messages = conv_svc.list_messages(db, conv.id)
        payload = {
            "conversation": _conv_brief(conv, unread),
            "messages": messages,
        }
    finally:
        db.close()
    return JSONResponse(payload)


@api_router.post("/{conversation_id}/send")
async def api_conversation_send(conversation_id: int, request: Request, payload: dict = Body(default={})):
    from backend.modules.reddit.models import RedditConversation

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty_text")
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        conv = db.get(RedditConversation, int(conversation_id))
        if conv is None or conv.organization_id != org_id:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        approval_mode = reddit_config.get_approval_mode(db)
        draft = conv_svc.create_draft(db, conv, text, review_mode=approval_mode)
        result: dict[str, Any] = {"draft_id": int(draft.id), "state": "pending_review"}
        if approval_mode == "auto":
            conv_svc.approve_draft(db, draft.id)
            send = conv_svc.send_draft(db, draft.id)
            result["state"] = "sent" if send.get("ok") else "failed"
            result["reason_code"] = send.get("reason_code")
    finally:
        db.close()
    return JSONResponse(result)


@api_router.post("/{conversation_id}/draft/{draft_id}/approve-send")
async def api_draft_approve_send(conversation_id: int, draft_id: int, request: Request):
    db = SessionLocal()
    try:
        conv_svc.approve_draft(db, draft_id)
        result = conv_svc.send_draft(db, draft_id)
    finally:
        db.close()
    return JSONResponse({"ok": bool(result.get("ok")), "reason_code": result.get("reason_code")})


@api_router.post("/{conversation_id}/mark-read")
async def api_mark_read(conversation_id: int, request: Request):
    db = SessionLocal()
    try:
        marked = conv_svc.mark_conversation_read(db, conversation_id)
    finally:
        db.close()
    return JSONResponse({"marked": int(marked)})


@api_router.post("/poll-inbox")
async def api_poll_inbox(request: Request, payload: dict = Body(default={})):
    account_id = payload.get("account_id")
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        if account_id:
            acc = accounts_svc.get_account(db, int(account_id))
            accts = [acc] if acc and acc.organization_id == org_id and (acc.status or "") == "connected" else []
        else:
            accts = [a for a in accounts_svc.list_accounts(db, org_id) if (a.status or "") == "connected"]
        total_saved = 0
        per_account = []
        for a in accts:
            res = conv_svc.poll_inbox(db, a, org_id, limit=50)
            saved = int(res.get("saved", 0))
            total_saved += saved
            per_account.append({"account_id": a.id, "saved": saved, "ok": bool(res.get("ok"))})
    finally:
        db.close()
    return JSONResponse({"saved": total_saved, "accounts": per_account})


__all__ = ["api_router", "router"]
