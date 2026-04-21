"""Instagram engagement tasks router (лайки/комменты/follow/story-like)."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.instagram.services import accounts as accounts_svc
from backend.modules.instagram.services import audience_segments as seg_svc
from backend.modules.instagram.services import engagement as eng_svc
from backend.modules.instagram.services import templates as templates_svc

router = APIRouter(prefix="/instagram/engagement")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def engagement_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        tasks = eng_svc.list_tasks(db, org_id)
        accounts = accounts_svc.list_accounts(db, org_id)
        templates = templates_svc.list_templates(db, org_id)
        segments = seg_svc.list_segments(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/engagement/list.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_engagement",
            "tasks": tasks, "accounts": accounts, "templates": templates, "segments": segments,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/create")
async def engagement_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),  # like_post | comment_post | follow_user | story_like
    account_id: int = Form(...),
    template_id: str = Form(default=""),
    segment_id: str = Form(default=""),
    daily_cap: str = Form(default=""),
    targets_blob: str = Form(default=""),
):
    if kind not in eng_svc.VALID_KINDS:
        return RedirectResponse("/instagram/engagement?notice=invalid_kind", status_code=303)
    db = SessionLocal()
    try:
        try:
            task = eng_svc.create_task(
                db, _org_id(request),
                name=name, kind=kind, account_id=account_id,
                template_id=template_id or None,
                segment_id=segment_id or None,
                daily_cap=daily_cap or None,
            )
        except ValueError as e:
            return RedirectResponse(f"/instagram/engagement?notice=invalid_{str(e)[:30]}", status_code=303)
        # Если есть targets_blob — разворачиваем в queue.
        manual_targets = [ln.strip() for ln in (targets_blob or "").splitlines() if ln.strip()]
        if manual_targets or task.segment_id:
            eng_svc.expand_targets_to_queue(db, task.id, manual_targets=manual_targets or None)
        tid = task.id
    finally:
        db.close()
    return RedirectResponse(f"/instagram/engagement/{tid}", status_code=303)


@router.get("/{task_id}", response_class=HTMLResponse)
async def engagement_detail(task_id: int, request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        task = eng_svc.get_task(db, task_id)
        if task is None or task.organization_id != org_id:
            raise HTTPException(status_code=404, detail="task_not_found")
        items = eng_svc.list_queue_items(db, task_id)
        summary = eng_svc.queue_summary(db, task_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/engagement/detail.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_engagement",
            "task": task, "items": items, "summary": summary,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/{task_id}/run-once")
async def engagement_run_once(task_id: int, request: Request, max_items: int = Form(default=5), headless: str = Form(default="on")):
    from backend.modules.instagram.runtime.engagement_worker import process_task_once

    db = SessionLocal()
    try:
        try:
            res = process_task_once(db, task_id, max_items=max(1, min(30, int(max_items or 5))),
                                    headless=(headless.lower() in ("on", "1", "true", "yes")))
            done, failed, skipped = int(res.get("done", 0)), int(res.get("failed", 0)), int(res.get("skipped", 0))
            qs = f"done={done}&failed={failed}&skipped={skipped}"
            if res.get("halted_reason"):
                qs += f"&halted={res['halted_reason']}"
        except Exception as e:  # noqa: BLE001
            qs = f"error={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/instagram/engagement/{task_id}?notice=run&{qs}", status_code=303)


@router.post("/{task_id}/status")
async def engagement_status(task_id: int, request: Request, status: str = Form(...)):
    db = SessionLocal()
    try:
        eng_svc.set_task_status(db, task_id, status)
    finally:
        db.close()
    return RedirectResponse(f"/instagram/engagement/{task_id}?notice=status_{status}", status_code=303)


__all__ = ["router"]
