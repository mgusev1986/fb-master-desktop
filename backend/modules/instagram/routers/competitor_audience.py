"""Instagram competitor audience router — парсинг likers/commenters/followers конкурентов."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.instagram.models import InstagramCompetitorAudienceTask
from backend.modules.instagram.services import accounts as accounts_svc

router = APIRouter(prefix="/instagram/competitor-audience")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def competitor_audience_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        accounts = accounts_svc.list_accounts(db, org_id)
        tasks = (
            db.query(InstagramCompetitorAudienceTask)
            .filter(InstagramCompetitorAudienceTask.organization_id == org_id)
            .order_by(InstagramCompetitorAudienceTask.created_at.desc())
            .limit(100)
            .all()
        )
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/competitor_audience/index.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_competitor_audience",
            "accounts": accounts, "tasks": tasks,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/create")
async def competitor_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),  # competitor_likers | competitor_commenters | competitor_followers
    competitor_handle: str = Form(default=""),
    target_post_url: str = Form(default=""),
    account_id: int = Form(...),
    max_results: int = Form(default=50),
):
    if kind not in ("competitor_likers", "competitor_commenters", "competitor_followers"):
        return RedirectResponse("/instagram/competitor-audience?notice=invalid_kind", status_code=303)
    if kind in ("competitor_likers", "competitor_commenters") and not target_post_url.strip():
        return RedirectResponse("/instagram/competitor-audience?notice=missing_post_url", status_code=303)
    if kind == "competitor_followers" and not competitor_handle.strip():
        return RedirectResponse("/instagram/competitor-audience?notice=missing_competitor_handle", status_code=303)

    db = SessionLocal()
    try:
        task = InstagramCompetitorAudienceTask(
            organization_id=_org_id(request),
            name=name.strip()[:255],
            kind=kind,
            competitor_handle=competitor_handle.strip().lstrip("@") or None,
            target_post_url=target_post_url.strip() or None,
            account_id=account_id,
            max_results=max(10, min(500, int(max_results or 50))),
        )
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()
    return RedirectResponse(f"/instagram/competitor-audience?notice=task_created&id={tid}", status_code=303)


@router.post("/{task_id}/run")
async def competitor_run(task_id: int, request: Request):
    """Запустить парсинг (sync, через Playwright) — обновляет task.found/saved."""
    from backend.modules.instagram.runtime.audience_scraper_browser import run_competitor_audience_task

    db = SessionLocal()
    try:
        try:
            res = run_competitor_audience_task(db, task_id, headless=True)
            if res.get("ok"):
                qs = f"task_ok&found={res.get('found',0)}&saved={res.get('saved',0)}"
            else:
                qs = f"task_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"task_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/instagram/competitor-audience?notice={qs}", status_code=303)


@router.post("/{task_id}/delete")
async def competitor_delete(task_id: int, request: Request):
    db = SessionLocal()
    try:
        task = db.get(InstagramCompetitorAudienceTask, int(task_id))
        if task is not None:
            db.delete(task)
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/instagram/competitor-audience?notice=deleted", status_code=303)


__all__ = ["router"]
