"""LinkedIn outreach builder: HTML список кампаний + создание + сборка очереди + review queue."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.linkedin.services import accounts as accounts_svc
from backend.modules.linkedin.services import leads as leads_svc
from backend.modules.linkedin.services import outreach_builder as outreach_svc
from backend.modules.linkedin.services import templates as templates_svc

router = APIRouter(prefix="/linkedin/outreach")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def outreach_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        campaigns = outreach_svc.list_campaigns(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
        templates = templates_svc.list_templates(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/outreach/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_outreach",
            "campaigns": campaigns,
            "accounts": accounts,
            "templates": templates,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/create")
async def outreach_create(
    request: Request,
    name: str = Form(...),
    mode: str = Form(default="dm_first_degree"),
    account_id: int = Form(...),
    template_id: int = Form(...),
    daily_cap: str = Form(default=""),
    approval_mode: str = Form(default="manual"),
    notes: str = Form(default=""),
):
    db = SessionLocal()
    try:
        camp = outreach_svc.create_campaign(
            db,
            _org_id(request),
            name=name,
            mode=mode,
            account_id=account_id,
            template_id=template_id,
            daily_cap=daily_cap or None,
            approval_mode=approval_mode,
            notes=notes,
        )
        cid = camp.id
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/outreach/{cid}", status_code=303)


@router.get("/{campaign_id}", response_class=HTMLResponse)
async def outreach_detail(campaign_id: int, request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        camp = outreach_svc.get_campaign(db, campaign_id)
        if camp is None or camp.organization_id != org_id:
            raise HTTPException(status_code=404, detail="campaign_not_found")
        leads = leads_svc.list_leads(db, org_id)
        items = outreach_svc.list_queue_items(db, campaign_id)
        summary = outreach_svc.queue_summary(db, campaign_id)
        # Сразу подхватим имена лидов в map для шаблона.
        from backend.modules.linkedin.models import LinkedInLead

        lead_map: dict[int, LinkedInLead] = {l.id: l for l in leads}
        for it in items:
            if it.lead_id not in lead_map:
                full = db.get(LinkedInLead, it.lead_id)
                if full is not None:
                    lead_map[full.id] = full
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/outreach/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_outreach",
            "campaign": camp,
            "leads": leads,
            "items": items,
            "summary": summary,
            "lead_map": lead_map,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/{campaign_id}/build-queue")
async def outreach_build_queue(
    campaign_id: int,
    request: Request,
    lead_ids: list[int] = Form(default=[]),
):
    db = SessionLocal()
    try:
        result = outreach_svc.build_queue(db, campaign_id, lead_ids)
    finally:
        db.close()
    return RedirectResponse(
        f"/linkedin/outreach/{campaign_id}?notice=queued&created={result['created']}&skipped={result['skipped']}",
        status_code=303,
    )


@router.post("/{campaign_id}/queue/{item_id}/state")
async def outreach_queue_state(
    campaign_id: int,
    item_id: int,
    request: Request,
    state: str = Form(...),
):
    db = SessionLocal()
    try:
        outreach_svc.set_queue_item_state(db, item_id, state)
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/outreach/{campaign_id}?notice=state_{state}", status_code=303)


@router.post("/{campaign_id}/run-once")
async def outreach_run_once(
    campaign_id: int,
    request: Request,
    max_items: int = Form(default=3),
    headless: str = Form(default="on"),
):
    """Запустить рассылку прямо сейчас: обработать до N approved items.

    Синхронный вызов — UI ждёт результата. Headless по умолчанию ON (без
    окна), но в desktop-сборке имеет смысл оставить headless=off для дебага.
    """
    from backend.modules.linkedin.runtime.outreach_worker import process_campaign_once

    db = SessionLocal()
    try:
        try:
            res = process_campaign_once(
                db,
                campaign_id,
                max_items=max(1, min(20, int(max_items or 3))),
                headless=(headless.lower() in ("on", "1", "true", "yes")),
            )
            sent, failed, skipped = int(res.get("sent", 0)), int(res.get("failed", 0)), int(res.get("skipped", 0))
            qs = f"sent={sent}&failed={failed}&skipped={skipped}"
            if res.get("halted_reason"):
                qs += f"&halted={res['halted_reason']}"
        except Exception as e:  # noqa: BLE001
            qs = f"error={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/outreach/{campaign_id}?notice=run&{qs}", status_code=303)


@router.post("/{campaign_id}/status")
async def outreach_status(campaign_id: int, request: Request, status: str = Form(...)):
    """Перевести кампанию в active / paused / stopped."""
    db = SessionLocal()
    try:
        camp = outreach_svc.get_campaign(db, campaign_id)
        if camp is not None and status in ("active", "paused", "stopped", "draft"):
            camp.status = status
            db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/outreach/{campaign_id}?notice=status_{status}", status_code=303)


__all__ = ["router"]
