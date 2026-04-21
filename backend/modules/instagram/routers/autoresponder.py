"""Instagram AI autoresponder UI + endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.instagram.models import InstagramAIDialogDraft, InstagramConversation, InstagramLead
from backend.modules.instagram.services import accounts as accounts_svc
from backend.modules.instagram.services import autoresponder as ar_svc
from backend.modules.instagram.services import leads as leads_svc
from backend.modules.instagram.services import personas as p_svc

router = APIRouter(prefix="/instagram/autoresponder")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def autoresponder_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        personas = p_svc.list_personas(db, org_id)
        sessions = ar_svc.list_sessions(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
        leads = leads_svc.list_leads(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/autoresponder/index.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_autoresponder",
            "personas": personas, "sessions": sessions, "accounts": accounts, "leads": leads,
            "goal_descriptions": ar_svc._GOAL_DESCRIPTIONS,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/personas/create")
async def personas_create(
    request: Request,
    name: str = Form(...), role: str = Form(default=""), company: str = Form(default=""),
    offer_summary: str = Form(default=""), tone: str = Form(default=""),
    knowledge_base: str = Form(default=""),
    cta_call_link: str = Form(default=""), cta_info_link: str = Form(default=""),
    forbidden_topics: str = Form(default=""),
):
    db = SessionLocal()
    try:
        p_svc.create_persona(
            db, _org_id(request),
            name=name, role=role, company=company, offer_summary=offer_summary,
            tone=tone, knowledge_base=knowledge_base,
            cta_call_link=cta_call_link, cta_info_link=cta_info_link,
            forbidden_topics=forbidden_topics,
        )
    finally:
        db.close()
    return RedirectResponse("/instagram/autoresponder?notice=persona_created", status_code=303)


@router.post("/personas/{persona_id}/archive")
async def personas_archive(persona_id: int, request: Request):
    db = SessionLocal()
    try:
        p_svc.archive_persona(db, persona_id)
    finally:
        db.close()
    return RedirectResponse("/instagram/autoresponder?notice=persona_archived", status_code=303)


@router.post("/sessions/create")
async def sessions_create(
    request: Request,
    account_id: int = Form(...), lead_id: int = Form(...), persona_id: int = Form(...),
    goal: str = Form(default="qualify_lead"), approval_mode: str = Form(default="manual"),
):
    db = SessionLocal()
    try:
        lead = db.get(InstagramLead, lead_id)
        if lead is None:
            return RedirectResponse("/instagram/autoresponder?notice=lead_not_found", status_code=303)
        conv = (
            db.query(InstagramConversation)
            .filter(
                InstagramConversation.account_id == account_id,
                InstagramConversation.organization_id == _org_id(request),
                InstagramConversation.counterpart_handle == lead.handle,
            ).first()
        )
        if conv is None:
            conv = InstagramConversation(
                organization_id=_org_id(request), account_id=account_id, lead_id=lead.id,
                counterpart_handle=lead.handle, counterpart_full_name=lead.full_name,
            )
            db.add(conv)
            db.flush()
        if conv.lead_id is None:
            conv.lead_id = lead.id
            db.commit()
        ar_svc.create_session(
            db, _org_id(request),
            account_id=account_id, lead_id=lead.id, persona_id=persona_id,
            goal=goal, approval_mode=approval_mode, conversation_id=conv.id,
        )
    finally:
        db.close()
    return RedirectResponse("/instagram/autoresponder?notice=session_created", status_code=303)


@router.post("/sessions/{session_id}/state")
async def sessions_set_state(session_id: int, request: Request, state: str = Form(...)):
    db = SessionLocal()
    try:
        ar_svc.update_session_state(db, session_id, state=state)
    finally:
        db.close()
    return RedirectResponse(f"/instagram/autoresponder?notice=session_{state}", status_code=303)


@router.post("/sessions/{session_id}/approval-mode")
async def sessions_set_approval(session_id: int, request: Request, approval_mode: str = Form(...)):
    db = SessionLocal()
    try:
        ar_svc.update_session_state(db, session_id, approval_mode=approval_mode)
    finally:
        db.close()
    return RedirectResponse(f"/instagram/autoresponder?notice=approval_{approval_mode}", status_code=303)


@router.post("/sessions/{session_id}/think-now")
async def sessions_think_now(session_id: int, request: Request):
    db = SessionLocal()
    try:
        try:
            res = await ar_svc.think_next_response(db, session_id)
            qs = "think_ok" if res.get("ok") else f"think_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"think_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/instagram/autoresponder?notice={qs}", status_code=303)


@router.post("/drafts/{draft_id}/approve-and-send")
async def draft_approve_and_send(draft_id: int, request: Request):
    from backend.modules.instagram.runtime.autoresponder_worker import _send_draft

    db = SessionLocal()
    try:
        draft = db.get(InstagramAIDialogDraft, int(draft_id))
        if draft is None:
            return RedirectResponse("/instagram/autoresponder?notice=draft_not_found", status_code=303)
        sess = ar_svc.get_session(db, draft.session_id)
        if sess is None:
            return RedirectResponse("/instagram/autoresponder?notice=session_missing", status_code=303)
        draft.review_state = "approved"
        db.commit()
        try:
            res = _send_draft(db, sess, draft)
            qs = "draft_sent" if res.get("ok") else f"draft_send_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"draft_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/instagram/autoresponder?notice={qs}", status_code=303)


@router.post("/drafts/{draft_id}/reject")
async def draft_reject(draft_id: int, request: Request):
    db = SessionLocal()
    try:
        draft = db.get(InstagramAIDialogDraft, int(draft_id))
        if draft is not None:
            draft.review_state = "rejected"
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/instagram/autoresponder?notice=draft_rejected", status_code=303)


__all__ = ["router"]
