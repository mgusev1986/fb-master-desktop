"""Reddit leads router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import leads as leads_svc

router = APIRouter(prefix="/reddit/leads")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("", response_class=HTMLResponse)
async def leads_page(request: Request):
    params = dict(request.query_params)
    f = leads_svc.LeadFilter(
        search=params.get("search", "").strip(),
        status=params.get("status", "").strip(),
        source_type=params.get("source_type", "").strip(),
        owner_account_id=(int(params["owner_account_id"]) if params.get("owner_account_id", "").strip().isdigit() else None),
        has_campaign=params.get("has_campaign", "").strip(),
    )
    try:
        page = max(1, int(params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per_page = max(10, min(500, int(params.get("per_page", "50"))))
    except ValueError:
        per_page = 50

    db = SessionLocal()
    try:
        rows, total = leads_svc.load_rows_for_render(db, _org_id(request), f, page=page, per_page=per_page)
    finally:
        db.close()

    return _tpl(request).TemplateResponse(
        "reddit/leads/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_leads",
            "rows": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "filter": f,
            "statuses": leads_svc.LEAD_STATUSES,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/{lead_id}/update")
async def lead_update(
    lead_id: int,
    request: Request,
    notes: str = Form(default=""),
    persona_tags: str = Form(default=""),
    status: str = Form(default=""),
):
    db = SessionLocal()
    try:
        leads_svc.update_lead(
            db, lead_id,
            notes=notes,
            persona_tags=persona_tags,
            status=status if status in leads_svc.LEAD_STATUSES else None,
        )
    finally:
        db.close()
    return RedirectResponse(f"/reddit/leads?notice=updated", status_code=303)


@router.post("/bulk")
async def lead_bulk(
    request: Request,
    action: str = Form(default=""),
    status: str = Form(default=""),
    ids: str = Form(default=""),
):
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return RedirectResponse("/reddit/leads", status_code=303)
    db = SessionLocal()
    try:
        if action == "set_status" and status in leads_svc.LEAD_STATUSES:
            leads_svc.bulk_set_status(db, _org_id(request), id_list, status)
        elif action == "delete":
            leads_svc.bulk_delete(db, _org_id(request), id_list)
    finally:
        db.close()
    return RedirectResponse("/reddit/leads?notice=bulk", status_code=303)


__all__ = ["router"]
