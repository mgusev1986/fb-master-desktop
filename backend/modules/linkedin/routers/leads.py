"""LinkedIn-лиды: HTML список + добавление + bulk-import URL'ов."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.linkedin.services import leads as leads_svc

router = APIRouter(prefix="/linkedin/leads")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def leads_index(request: Request):
    from backend.modules.linkedin.services import accounts as accounts_svc
    from backend.modules.linkedin.services import audience_segments as seg_svc

    db = SessionLocal()
    try:
        org_id = _org_id(request)
        items = leads_svc.list_leads(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
        segments = seg_svc.list_segments(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/leads/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_leads",
            "leads": items,
            "account_options": accounts,
            "segments": segments,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/create")
async def leads_create(
    request: Request,
    profile_url: str = Form(...),
    full_name: str = Form(default=""),
    headline: str = Form(default=""),
    company: str = Form(default=""),
    job_title: str = Form(default=""),
    location: str = Form(default=""),
    notes: str = Form(default=""),
):
    db = SessionLocal()
    try:
        leads_svc.create_lead(
            db,
            _org_id(request),
            profile_url=profile_url,
            full_name=full_name,
            headline=headline,
            company=company,
            job_title=job_title,
            location=location,
            notes=notes,
        )
    finally:
        db.close()
    return RedirectResponse("/linkedin/leads?notice=created", status_code=303)


@router.post("/bulk-import")
async def leads_bulk_import(request: Request, urls_blob: str = Form(default="")):
    db = SessionLocal()
    try:
        result = leads_svc.bulk_import_urls(db, _org_id(request), urls_blob or "")
    finally:
        db.close()
    return RedirectResponse(
        f"/linkedin/leads?notice=imported&imported={result['imported']}&errors={len(result['errors'])}",
        status_code=303,
    )


@router.post("/{lead_id}/delete")
async def leads_delete(lead_id: int, request: Request):
    db = SessionLocal()
    try:
        leads_svc.delete_lead(db, lead_id)
    finally:
        db.close()
    return RedirectResponse("/linkedin/leads?notice=deleted", status_code=303)


@router.post("/audience-search")
async def leads_audience_search(
    request: Request,
    account_id: int = Form(...),
    keywords: str = Form(...),
    max_results: int = Form(default=10),
    segment_id: str = Form(default=""),
    save_as_segment: str = Form(default=""),
    save_segment_name: str = Form(default=""),
):
    """Поиск аудитории через LinkedIn search → сохраняет лидов.

    Опционально:
      * segment_id — выполнить уже сохранённый сегмент (`build_search_keywords`).
      * save_as_segment=on + save_segment_name — сохранить параметры как новый сегмент.
    """
    from backend.modules.linkedin.runtime.audience_scraper import search_people
    from backend.modules.linkedin.services import audience_segments as seg_svc

    db = SessionLocal()
    try:
        seg = None
        actual_keywords = keywords
        seg_id_int: int | None = None
        if segment_id and segment_id.isdigit():
            seg_id_int = int(segment_id)
            seg = seg_svc.get_segment(db, seg_id_int)
            if seg is not None:
                actual_keywords = seg_svc.build_search_keywords(seg) or keywords

        if save_as_segment.lower() in ("on", "1", "true", "yes") and not seg:
            try:
                seg = seg_svc.create_segment(
                    db, _org_id(request),
                    name=save_segment_name or keywords[:80],
                    keywords=keywords,
                )
                seg_id_int = seg.id
            except ValueError:
                pass

        try:
            res = search_people(
                db,
                account_id=account_id,
                keywords=actual_keywords,
                headless=True,
                max_results=max(1, min(50, int(max_results or 10))),
                segment_id=seg_id_int,
            )
            if res.get("ok"):
                qs = f"search_ok&found={res.get('found',0)}&saved={res.get('saved',0)}&skipped={res.get('skipped',0)}"
                if seg_id_int:
                    qs += f"&segment_id={seg_id_int}"
            else:
                qs = f"search_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"search_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/leads?notice={qs}", status_code=303)


@router.post("/segments/create")
async def leads_segments_create(
    request: Request,
    name: str = Form(...),
    keywords: str = Form(...),
    company: str = Form(default=""),
    location: str = Form(default=""),
    industry: str = Form(default=""),
    title: str = Form(default=""),
):
    from backend.modules.linkedin.services import audience_segments as seg_svc

    db = SessionLocal()
    try:
        try:
            seg_svc.create_segment(
                db, _org_id(request),
                name=name, keywords=keywords, company=company,
                location=location, industry=industry, title=title,
            )
        except ValueError:
            return RedirectResponse("/linkedin/leads?notice=segment_invalid", status_code=303)
    finally:
        db.close()
    return RedirectResponse("/linkedin/leads?notice=segment_saved", status_code=303)


@router.post("/segments/{segment_id}/archive")
async def leads_segments_archive(segment_id: int, request: Request):
    from backend.modules.linkedin.services import audience_segments as seg_svc

    db = SessionLocal()
    try:
        seg_svc.archive_segment(db, segment_id)
    finally:
        db.close()
    return RedirectResponse("/linkedin/leads?notice=segment_archived", status_code=303)


__all__ = ["router"]
