"""Reddit comments router (review-first publishing)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import accounts as accounts_svc
from backend.modules.reddit.services import comments as comments_svc

router = APIRouter(prefix="/reddit/comments")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("", response_class=HTMLResponse)
async def comments_index(request: Request):
    state = request.query_params.get("state", "").strip() or None
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        drafts = comments_svc.list_drafts(db, org_id, review_state=state)
        accounts = [
            a for a in accounts_svc.list_accounts(db, org_id)
            if (a.status or "") == "connected"
        ]
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/comments/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_comments",
            "drafts": drafts,
            "accounts": accounts,
            "state": state or "",
            "review_states": comments_svc.REVIEW_STATES,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/new")
async def comment_new(
    request: Request,
    account_id: str = Form(default=""),
    target_ref: str = Form(default=""),
    body: str = Form(default=""),
):
    if not account_id.strip().isdigit() or not target_ref.strip() or not body.strip():
        return RedirectResponse("/reddit/comments?notice=missing_fields", status_code=303)
    db = SessionLocal()
    try:
        comments_svc.create_draft(
            db, _org_id(request),
            account_id=int(account_id),
            target_ref=target_ref.strip(),
            body=body,
        )
    finally:
        db.close()
    return RedirectResponse("/reddit/comments?notice=created", status_code=303)


@router.post("/{draft_id}/edit")
async def comment_edit(
    draft_id: int,
    request: Request,
    body: str = Form(default=""),
    account_id: str = Form(default=""),
):
    db = SessionLocal()
    try:
        acc_id = int(account_id) if account_id.strip().isdigit() else None
        comments_svc.update_draft(db, draft_id, body=body or None, account_id=acc_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/comments", status_code=303)


@router.post("/{draft_id}/approve")
async def comment_approve(draft_id: int, request: Request):
    db = SessionLocal()
    try:
        comments_svc.approve_draft(db, draft_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/comments?state=approved", status_code=303)


@router.post("/{draft_id}/reject")
async def comment_reject(draft_id: int, request: Request):
    db = SessionLocal()
    try:
        comments_svc.reject_draft(db, draft_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/comments", status_code=303)


@router.post("/{draft_id}/delete")
async def comment_delete(draft_id: int, request: Request):
    db = SessionLocal()
    try:
        comments_svc.delete_draft(db, draft_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/comments", status_code=303)


@router.post("/{draft_id}/publish")
async def comment_publish(draft_id: int, request: Request):
    db = SessionLocal()
    try:
        result = comments_svc.publish_draft(db, draft_id)
    finally:
        db.close()
    notice = "published" if result.get("ok") else f"failed_{result.get('reason_code') or 'unknown'}"
    return RedirectResponse(f"/reddit/comments?notice={notice}", status_code=303)


__all__ = ["router"]
