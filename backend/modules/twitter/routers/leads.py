"""Twitter leads router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.twitter.services import accounts as accounts_svc
from backend.modules.twitter.services import audience_segments as seg_svc
from backend.modules.twitter.services import leads as leads_svc

router = APIRouter(prefix="/twitter/leads")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


def render_leads_page(
    request: Request,
    *,
    page_id: str,
    page_title: str,
    page_subtitle: str,
):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        items = leads_svc.list_leads(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
        segments = seg_svc.list_segments(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "twitter/leads/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": page_id,
            "page_title": page_title,
            "page_subtitle": page_subtitle,
            "leads": items,
            "account_options": accounts,
            "segments": segments,
            "notice": request.query_params.get("notice"),
        },
    )


@router.get("", response_class=HTMLResponse)
async def leads_index(request: Request):
    return render_leads_page(
        request,
        page_id="twitter_leads",
        page_title="Лиды",
        page_subtitle="Хранилище X-handles для outreach. Поиск через X Search или ручной импорт.",
    )


@router.post("/create")
async def leads_create(
    request: Request,
    handle: str = Form(...),
    full_name: str = Form(default=""),
    bio: str = Form(default=""),
    location: str = Form(default=""),
    notes: str = Form(default=""),
):
    db = SessionLocal()
    try:
        try:
            leads_svc.create_lead(db, _org_id(request), handle=handle, full_name=full_name, bio=bio, location=location, notes=notes)
        except ValueError:
            return RedirectResponse("/twitter/leads?notice=invalid_handle", status_code=303)
    finally:
        db.close()
    return RedirectResponse("/twitter/leads?notice=created", status_code=303)


@router.post("/bulk-import")
async def leads_bulk(request: Request, handles_blob: str = Form(default="")):
    db = SessionLocal()
    try:
        res = leads_svc.bulk_import_handles(db, _org_id(request), handles_blob or "")
    finally:
        db.close()
    return RedirectResponse(
        f"/twitter/leads?notice=imported&imported={res['imported']}&skipped={res['skipped']}&errors={len(res['errors'])}",
        status_code=303,
    )


@router.post("/audience-search")
async def leads_audience_search(
    request: Request,
    account_id: int = Form(...),
    keywords: str = Form(...),
    search_kind: str = Form(default="users"),
    max_results: int = Form(default=15),
    save_as_segment: str = Form(default=""),
    save_segment_name: str = Form(default=""),
    segment_id: str = Form(default=""),
    return_to: str = Form(default="/twitter/audience"),
):
    """Browser-парсер X Search."""
    from backend.modules.twitter.runtime.audience_scraper_browser import search_users

    db = SessionLocal()
    try:
        seg_id_int: int | None = None
        actual_keywords = keywords
        if segment_id and segment_id.isdigit():
            seg_id_int = int(segment_id)
            seg = seg_svc.get_segment(db, seg_id_int)
            if seg is not None:
                actual_keywords = seg.keywords

        if save_as_segment.lower() in ("on", "1", "true", "yes") and not seg_id_int:
            try:
                seg = seg_svc.create_segment(db, _org_id(request), name=save_segment_name or keywords[:80], keywords=keywords, search_kind=search_kind)
                seg_id_int = seg.id
            except ValueError:
                pass

        try:
            res = search_users(
                db, account_id=account_id, keywords=actual_keywords,
                headless=True, max_results=max(5, min(50, int(max_results or 15))),
                segment_id=seg_id_int,
            )
            if res.get("ok"):
                qs = f"search_ok&found={res.get('found',0)}&saved={res.get('saved',0)}&skipped={res.get('skipped',0)}"
            else:
                qs = f"search_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"search_exception&err={str(e)[:120]}"
    finally:
        db.close()
    redirect_to = (return_to or "").strip()
    if redirect_to not in ("/twitter/audience", "/twitter/leads"):
        redirect_to = "/twitter/audience"
    return RedirectResponse(f"{redirect_to}?notice={qs}", status_code=303)


@router.post("/segments/{segment_id}/archive")
async def segments_archive(segment_id: int, request: Request):
    db = SessionLocal()
    try:
        seg_svc.archive_segment(db, segment_id)
    finally:
        db.close()
    return RedirectResponse("/twitter/leads?notice=segment_archived", status_code=303)


@router.post("/{lead_id}/delete")
async def leads_delete(lead_id: int, request: Request):
    db = SessionLocal()
    try:
        leads_svc.delete_lead(db, lead_id)
    finally:
        db.close()
    return RedirectResponse("/twitter/leads?notice=deleted", status_code=303)


__all__ = ["render_leads_page", "router"]
