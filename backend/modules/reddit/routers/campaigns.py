"""Reddit campaigns router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import campaigns as campaigns_svc
from backend.modules.reddit.services import leads as leads_svc
from backend.modules.reddit.services import templates as templates_svc

router = APIRouter(prefix="/reddit/campaigns")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("", response_class=HTMLResponse)
async def campaigns_list(request: Request):
    db = SessionLocal()
    try:
        rows = campaigns_svc.list_campaigns(db, _org_id(request))
        per_row = []
        for r in rows:
            counts = campaigns_svc.count_by_state(db, r.id)
            per_row.append({
                "row": r,
                "counts": counts,
                "total": sum(counts.values()),
            })
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/campaigns/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_campaigns",
            "items": per_row,
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def campaigns_new(request: Request):
    db = SessionLocal()
    try:
        tpl_rows = templates_svc.list_templates(db, _org_id(request))
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/campaigns/form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_campaigns",
            "campaign": None,
            "templates_list": tpl_rows,
            "modes": campaigns_svc.CAMPAIGN_MODES,
        },
    )


@router.post("/save")
async def campaigns_save(
    request: Request,
    campaign_id: str = Form(default=""),
    name: str = Form(default=""),
    mode: str = Form(default="dm"),
    template_id: str = Form(default=""),
    approval_mode: str = Form(default="manual"),
    per_day_cap: str = Form(default="10"),
    per_hour_cap: str = Form(default="3"),
    send_window_hours: str = Form(default="9-21"),
):
    try:
        tpl_id = int(template_id) if template_id.strip() else None
    except ValueError:
        tpl_id = None
    try:
        pd = int(per_day_cap)
    except ValueError:
        pd = 10
    try:
        ph = int(per_hour_cap)
    except ValueError:
        ph = 3
    caps = {"per_day": max(0, min(500, pd)), "per_hour": max(0, min(100, ph))}
    window = {"hours": send_window_hours}

    db = SessionLocal()
    try:
        if campaign_id.strip().isdigit():
            campaigns_svc.update_campaign(
                db, int(campaign_id),
                name=(name or "").strip()[:200] or "Campaign",
                mode=mode if mode in campaigns_svc.CAMPAIGN_MODES else "dm",
                template_id=tpl_id,
                approval_mode=approval_mode if approval_mode in ("manual", "semi", "auto") else "manual",
                per_account_caps_json=caps,
                send_window_config_json=window,
            )
            new_id = int(campaign_id)
        else:
            row = campaigns_svc.create_campaign(
                db, _org_id(request),
                name=name, mode=mode, template_id=tpl_id,
                approval_mode=approval_mode, per_account_caps=caps, send_window_config=window,
            )
            new_id = row.id
    finally:
        db.close()
    return RedirectResponse(f"/reddit/campaigns/{new_id}", status_code=303)


@router.get("/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(campaign_id: int, request: Request):
    state_filter = request.query_params.get("state", "pending_review").strip() or "pending_review"
    db = SessionLocal()
    try:
        camp = campaigns_svc.get_campaign(db, campaign_id)
        if camp is None:
            return RedirectResponse("/reddit/campaigns", status_code=303)
        counts = campaigns_svc.count_by_state(db, camp.id)
        queue = campaigns_svc.list_queue(db, camp.id, review_state=(state_filter if state_filter != "all" else None))
        template = db.get(templates_svc.RedditTemplate, int(camp.template_id)) if camp.template_id else None
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/campaigns/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_campaigns",
            "campaign": camp,
            "template": template,
            "counts": counts,
            "queue": queue,
            "state_filter": state_filter,
            "review_states": campaigns_svc.REVIEW_STATES,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/{campaign_id}/build-queue")
async def campaign_build_queue(
    campaign_id: int,
    request: Request,
    lead_ids: str = Form(default=""),
    max_items: str = Form(default="100"),
):
    ids = [int(x) for x in lead_ids.split(",") if x.strip().isdigit()]
    try:
        mx = int(max_items)
    except ValueError:
        mx = 100
    db = SessionLocal()
    try:
        camp = campaigns_svc.get_campaign(db, campaign_id)
        if camp is None:
            return RedirectResponse("/reddit/campaigns", status_code=303)
        added = campaigns_svc.build_queue_from_filter(db, camp, lead_ids=ids or None, max_items=mx)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/campaigns/{campaign_id}?notice=built_{added}", status_code=303)


@router.post("/{campaign_id}/item/{item_id}/approve")
async def campaign_item_approve(campaign_id: int, item_id: int, request: Request):
    db = SessionLocal()
    try:
        campaigns_svc.approve_item(db, item_id)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/campaigns/{campaign_id}?state=pending_review", status_code=303)


@router.post("/{campaign_id}/item/{item_id}/reject")
async def campaign_item_reject(campaign_id: int, item_id: int, request: Request):
    db = SessionLocal()
    try:
        campaigns_svc.reject_item(db, item_id)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/campaigns/{campaign_id}?state=pending_review", status_code=303)


@router.post("/{campaign_id}/item/{item_id}/skip")
async def campaign_item_skip(campaign_id: int, item_id: int, request: Request):
    db = SessionLocal()
    try:
        campaigns_svc.skip_item(db, item_id)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/campaigns/{campaign_id}?state=pending_review", status_code=303)


@router.post("/{campaign_id}/item/{item_id}/edit-body")
async def campaign_item_edit_body(campaign_id: int, item_id: int, request: Request, body: str = Form(default="")):
    db = SessionLocal()
    try:
        campaigns_svc.update_item_body(db, item_id, body)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/campaigns/{campaign_id}?state=pending_review", status_code=303)


@router.post("/{campaign_id}/bulk-approve")
async def campaign_bulk_approve(campaign_id: int, request: Request, ids: str = Form(default="")):
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    db = SessionLocal()
    try:
        campaigns_svc.bulk_approve(db, campaign_id, id_list)
    finally:
        db.close()
    return RedirectResponse(f"/reddit/campaigns/{campaign_id}?state=approved", status_code=303)


@router.post("/{campaign_id}/delete")
async def campaign_delete(campaign_id: int, request: Request):
    db = SessionLocal()
    try:
        campaigns_svc.delete_campaign(db, campaign_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/campaigns", status_code=303)


__all__ = ["router"]
