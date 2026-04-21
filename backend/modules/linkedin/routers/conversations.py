"""LinkedIn inbox: список тредов + детали + compose + sync одного треда."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.linkedin.services import accounts as accounts_svc
from backend.modules.linkedin.services import conversations as conv_svc
from backend.modules.linkedin.services import leads as leads_svc

router = APIRouter(prefix="/linkedin/conversations")


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
        items = conv_svc.list_conversations(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/conversations/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_conversations",
            "conversations": items,
            "accounts": accounts,
            "notice": request.query_params.get("notice"),
        },
    )


@router.get("/{conv_id}", response_class=HTMLResponse)
async def conversations_detail(conv_id: int, request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        conv = conv_svc.get_conversation(db, conv_id)
        if conv is None or conv.organization_id != org_id:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        msgs = conv_svc.list_messages(db, conv_id)
        # При открытии треда — пометить прочитанным.
        marked = conv_svc.mark_read(db, conv_id)
        leads = leads_svc.list_leads(db, org_id)
        linked_lead = None
        if conv.lead_id:
            linked_lead = next((l for l in leads if l.id == conv.lead_id), None)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/conversations/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_conversations",
            "conversation": conv,
            "messages": msgs,
            "leads": leads,
            "linked_lead": linked_lead,
            "marked_read": marked,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/{conv_id}/send")
async def conversations_send(
    conv_id: int,
    request: Request,
    body: str = Form(...),
    headless: str = Form(default="on"),
):
    """Отправить DM в этот тред: использует dm_sender, если в conv есть linked lead."""
    db = SessionLocal()
    try:
        conv = conv_svc.get_conversation(db, conv_id)
        if conv is None:
            return RedirectResponse("/linkedin/conversations?notice=conv_not_found", status_code=303)
        body = (body or "").strip()
        if not body:
            return RedirectResponse(f"/linkedin/conversations/{conv_id}?notice=empty_body", status_code=303)
        if not conv.lead_id:
            return RedirectResponse(
                f"/linkedin/conversations/{conv_id}?notice=no_linked_lead",
                status_code=303,
            )
        from backend.modules.linkedin.runtime.dm_sender import send_dm

        try:
            res = send_dm(
                db,
                account_id=conv.account_id,
                lead_id=conv.lead_id,
                body=body,
                headless=(headless.lower() in ("on", "1", "true", "yes")),
            )
            if res.get("ok"):
                conv_svc.append_outgoing(db, conv.id, body, sent=True)
                qs = "send_ok"
            else:
                qs = f"send_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"send_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/conversations/{conv_id}?notice={qs}", status_code=303)


@router.post("/{conv_id}/link-lead")
async def conversations_link_lead(
    conv_id: int,
    request: Request,
    lead_id: str = Form(default=""),
):
    db = SessionLocal()
    try:
        lid = int(lead_id) if lead_id and lead_id.isdigit() else None
        conv_svc.link_to_lead(db, conv_id, lid)
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/conversations/{conv_id}?notice=linked", status_code=303)


__all__ = ["router"]
