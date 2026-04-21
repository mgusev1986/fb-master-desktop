"""Instagram leads + audience search router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.instagram.services import accounts as accounts_svc
from backend.modules.instagram.services import audience_segments as seg_svc
from backend.modules.instagram.services import leads as leads_svc

router = APIRouter(prefix="/instagram/leads")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def leads_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        items = leads_svc.list_leads(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
        segments = seg_svc.list_segments(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/leads/list.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_leads",
            "leads": items, "account_options": accounts, "segments": segments,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/create")
async def leads_create(request: Request, handle: str = Form(...), full_name: str = Form(default=""), bio: str = Form(default=""), notes: str = Form(default="")):
    db = SessionLocal()
    try:
        try:
            leads_svc.create_lead(db, _org_id(request), handle=handle, full_name=full_name, bio=bio, notes=notes)
        except ValueError:
            return RedirectResponse("/instagram/leads?notice=invalid_handle", status_code=303)
    finally:
        db.close()
    return RedirectResponse("/instagram/leads?notice=created", status_code=303)


@router.post("/bulk-import")
async def leads_bulk(request: Request, handles_blob: str = Form(default="")):
    db = SessionLocal()
    try:
        res = leads_svc.bulk_import_handles(db, _org_id(request), handles_blob or "")
    finally:
        db.close()
    return RedirectResponse(
        f"/instagram/leads?notice=imported&imported={res['imported']}&skipped={res['skipped']}&errors={len(res['errors'])}",
        status_code=303,
    )


@router.post("/{lead_id}/delete")
async def leads_delete(lead_id: int, request: Request):
    db = SessionLocal()
    try:
        leads_svc.delete_lead(db, lead_id)
    finally:
        db.close()
    return RedirectResponse("/instagram/leads?notice=deleted", status_code=303)


# ── Audience research router (отдельно для UX, но логика — здесь) ──


audience_router = APIRouter(prefix="/instagram/audience")


@audience_router.get("", response_class=HTMLResponse)
async def audience_page(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        accounts = accounts_svc.list_accounts(db, org_id)
        segments = seg_svc.list_segments(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/audience/index.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_audience_research",
            "accounts": accounts, "segments": segments,
            "notice": request.query_params.get("notice"),
        },
    )


@audience_router.post("/search")
async def audience_search(request: Request, account_id: int = Form(...), keywords: str = Form(...),
                          search_kind: str = Form(default="hashtag"), max_results: int = Form(default=20),
                          save_as_segment: str = Form(default=""), save_segment_name: str = Form(default=""),
                          segment_id: str = Form(default="")):
    """Поиск через X Search или hashtag в Instagram."""
    db = SessionLocal()
    try:
        seg_id_int: int | None = None
        actual_keywords = keywords
        if segment_id and segment_id.isdigit():
            seg_id_int = int(segment_id)
            seg = seg_svc.get_segment(db, seg_id_int)
            if seg is not None:
                actual_keywords = seg.keywords
                search_kind = seg.search_kind

        if save_as_segment.lower() in ("on", "1", "true", "yes") and not seg_id_int:
            try:
                seg = seg_svc.create_segment(db, _org_id(request), name=save_segment_name or keywords[:80], keywords=keywords, search_kind=search_kind)
                seg_id_int = seg.id
            except ValueError:
                pass

        try:
            if (search_kind or "hashtag").strip().lower() == "hashtag":
                from backend.modules.instagram.runtime.audience_scraper_browser import scrape_hashtag
                res = scrape_hashtag(db, account_id, actual_keywords, headless=True, max_results=max(5, min(60, int(max_results or 20))), segment_id=seg_id_int)
            else:
                from backend.modules.instagram.runtime.audience_scraper_browser import search_users
                res = search_users(db, account_id, actual_keywords, headless=True, max_results=max(5, min(60, int(max_results or 20))), segment_id=seg_id_int)
            if res.get("ok"):
                qs = f"search_ok&found={res.get('found',0)}&saved={res.get('saved',0)}&skipped={res.get('skipped',0)}"
            else:
                qs = f"search_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"search_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/instagram/audience?notice={qs}", status_code=303)


@audience_router.post("/segments/{segment_id}/archive")
async def segments_archive(segment_id: int, request: Request):
    db = SessionLocal()
    try:
        seg_svc.archive_segment(db, segment_id)
    finally:
        db.close()
    return RedirectResponse("/instagram/audience?notice=segment_archived", status_code=303)


__all__ = ["router", "audience_router"]
