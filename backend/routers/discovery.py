"""Автопоиск аудитории: поиск групп / страниц / профилей по ключевым словам на Facebook."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import DiscoveryResult, DiscoveryTask, Donor, FBAccount, Job
from backend.services.desktop_client import user_agent_is_fb_master_desktop
from backend.services.discovery_worker import (
    JOB_TYPE,
    active_discovery_job_id,
    discovery_worker_busy_for_org,
    finish_stale_discovery_jobs,
    request_discovery_cancel,
    spawn_discovery_worker,
)
from backend.services.fb_account_profile_paths import playwright_profile_dir_for_account
from backend.services.fb_account_slots import account_in_active_slot, fb_accounts_for_jobs
from backend.services.fb_url_normalize import normalize_facebook_group_members_url
from backend.services.job_logging import create_job
from backend.services.parallel_automation_slots import discovery_worker_slots
from backend.services.proxy_health_guard import (
    PROXY_AUTOMATION_BLOCKED_HINT,
    account_proxy_tunnel_blocked,
)
from backend.services.tenancy import require_org_id

router = APIRouter(prefix="/discovery", tags=["discovery"])

VALID_SEARCH_TYPES = {"groups", "pages", "people"}


def _discovery_playwright_profile_path_for_task(
    db: Session, org_id: int, task: DiscoveryTask | None
) -> str | None:
    """Путь user-data Chromium для аккаунта, с которым запускали автопоиск (та же сессия, что у воркера)."""
    if not task or not task.job_id:
        return None
    job = db.get(Job, task.job_id)
    if not job or not isinstance(job.config_snapshot, dict):
        return None
    raw_id = job.config_snapshot.get("fb_account_id")
    try:
        fb_acc_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    acc = (
        db.query(FBAccount)
        .filter(FBAccount.id == fb_acc_id, FBAccount.organization_id == org_id)
        .first()
    )
    if not acc:
        return None
    return str(playwright_profile_dir_for_account(db, acc.id, acc.profile_dir or "").resolve())


def _discovery_progress_percent(p: dict[str, Any] | None) -> int | None:
    if not p:
        return None
    total = int(p.get("total_steps") or 0)
    if total <= 0:
        return None
    done = int(p.get("done_steps") or 0)
    phase = str(p.get("phase") or "")
    extra = 0.0
    if phase == "starting":
        extra = 0.05
    elif phase == "browser_open":
        extra = 0.1
    elif phase == "searching":
        extra = 0.5
    return min(100, max(0, int(round(100 * (done + extra) / total))))


@router.get("")
async def discovery_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    accounts = fb_accounts_for_jobs(db, org_id)
    tasks = (
        db.query(DiscoveryTask)
        .filter(DiscoveryTask.organization_id == org_id)
        .order_by(DiscoveryTask.id.desc())
        .limit(20)
        .all()
    )
    latest_task = tasks[0] if tasks else None
    results = []
    if latest_task:
        results = (
            db.query(DiscoveryResult)
            .filter(DiscoveryResult.task_id == latest_task.id)
            .order_by(DiscoveryResult.relevance_score.asc(), DiscoveryResult.id.asc())
            .all()
        )
    busy = discovery_worker_busy_for_org(db, org_id)
    last_job = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.organization_id == org_id)
        .order_by(Job.id.desc())
        .first()
    )
    ua = request.headers.get("user-agent")
    is_fb_master_desktop = user_agent_is_fb_master_desktop(ua)
    discovery_playwright_profile_path = _discovery_playwright_profile_path_for_task(
        db, org_id, latest_task
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "discovery/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "discovery",
            "accounts": accounts,
            "tasks": tasks,
            "latest_task": latest_task,
            "results": results,
            "discovery_busy": busy,
            "last_job": last_job,
            "is_fb_master_desktop": is_fb_master_desktop,
            "discovery_playwright_profile_path": discovery_playwright_profile_path,
        },
    )


@router.post("/run")
async def discovery_run(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    if discovery_worker_busy_for_org(db, org_id):
        return RedirectResponse("/discovery?err=busy", status_code=303)

    form = await request.form()
    keywords = (form.get("keywords") or "").strip()
    fb_raw = (form.get("fb_account_id") or "").strip()
    search_types_raw = form.getlist("search_type")
    max_results = int(form.get("max_results") or 50)
    max_results = max(10, min(200, max_results))

    search_types = [t for t in search_types_raw if t in VALID_SEARCH_TYPES]
    if not search_types:
        search_types = ["groups"]

    if not keywords:
        return RedirectResponse("/discovery?err=no_keywords", status_code=303)
    if not fb_raw.isdigit():
        return RedirectResponse("/discovery?err=form", status_code=303)

    acc = db.query(FBAccount).filter(FBAccount.id == int(fb_raw), FBAccount.organization_id == org_id).first()
    if not acc or not account_in_active_slot(acc):
        return RedirectResponse("/discovery?err=form", status_code=303)
    if account_proxy_tunnel_blocked(acc):
        return RedirectResponse(
            "/discovery?err=proxy_tunnel&why=" + quote(PROXY_AUTOMATION_BLOCKED_HINT, safe=""),
            status_code=303,
        )

    task = DiscoveryTask(
        organization_id=org_id,
        keywords=keywords,
        search_types=search_types,
        max_results_per_type=max_results,
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()

    admin_id = (request.session.get("user") or {}).get("id")
    job = create_job(
        db,
        organization_id=org_id,
        job_type=JOB_TYPE,
        config_snapshot={
            "task_id": task.id,
            "fb_account_id": int(fb_raw),
        },
        admin_id=admin_id,
    )
    task.job_id = job.id
    db.commit()

    spawn_discovery_worker(job.id)
    return RedirectResponse("/discovery?started=1", status_code=303)


@router.get("/progress")
async def discovery_progress_json(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    busy = discovery_worker_busy_for_org(db, org_id)
    job = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.organization_id == org_id)
        .order_by(Job.id.desc())
        .first()
    )
    progress = job.progress_json if job and isinstance(job.progress_json, dict) else None
    return JSONResponse({
        "busy": busy,
        "job_id": job.id if job else None,
        "status": job.status if job else None,
        "progress": progress,
        "percent": _discovery_progress_percent(progress),
        "error_summary": (job.error_summary or None) if job else None,
    })


@router.post("/cancel")
async def discovery_cancel_json(db: Session = Depends(get_db)):
    ok = request_discovery_cancel()
    stale = 0
    if not ok:
        stale = finish_stale_discovery_jobs(db)
    return JSONResponse({"ok": ok or stale > 0, "stale_cleared": stale})


@router.get("/{task_id}/results")
async def discovery_results_json(task_id: int, request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    task = db.query(DiscoveryTask).filter(
        DiscoveryTask.id == task_id, DiscoveryTask.organization_id == org_id
    ).first()
    if not task:
        return JSONResponse({"error": "not_found"}, status_code=404)
    results = (
        db.query(DiscoveryResult)
        .filter(DiscoveryResult.task_id == task_id)
        .order_by(DiscoveryResult.relevance_score.asc(), DiscoveryResult.id.asc())
        .all()
    )
    return JSONResponse({
        "task_id": task.id,
        "status": task.status,
        "results": [
            {
                "id": r.id,
                "type": r.result_type,
                "url": r.url,
                "name": r.name,
                "description": r.description,
                "member_count": r.member_count,
                "category": r.category,
                "relevance_score": r.relevance_score,
                "is_approved": r.is_approved,
                "donor_id": r.donor_id,
            }
            for r in results
        ],
    })


@router.post("/results/approve")
async def discovery_approve(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    result_ids = [int(x) for x in form.getlist("result_id") if str(x).strip().isdigit()]
    if not result_ids:
        return RedirectResponse("/discovery?err=no_selection", status_code=303)

    results = (
        db.query(DiscoveryResult)
        .filter(DiscoveryResult.id.in_(result_ids), DiscoveryResult.organization_id == org_id)
        .all()
    )

    existing_urls: set[str] = set()
    for d in db.query(Donor).filter(Donor.organization_id == org_id).all():
        existing_urls.add(d.url.strip().lower())

    created = 0
    for r in results:
        url = r.url.strip()
        if r.result_type == "group":
            gm = normalize_facebook_group_members_url(url)
            if gm:
                url = gm
        if url.lower() in existing_urls:
            r.is_approved = True
            continue
        donor = Donor(
            organization_id=org_id,
            url=url,
            name=r.name or None,
            notes=f"Автопоиск: {r.description[:200] if r.description else r.result_type}",
        )
        db.add(donor)
        db.flush()
        r.is_approved = True
        r.donor_id = donor.id
        existing_urls.add(url.lower())
        created += 1

    db.commit()
    return RedirectResponse(f"/discovery?approved={created}", status_code=303)


@router.post("/results/approve-and-parse")
async def discovery_approve_and_parse(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    result_ids = [int(x) for x in form.getlist("result_id") if str(x).strip().isdigit()]
    fb_raw = (form.get("fb_account_id") or "").strip()
    if not result_ids:
        return RedirectResponse("/discovery?err=no_selection", status_code=303)
    if not fb_raw.isdigit():
        return RedirectResponse("/discovery?err=form", status_code=303)

    acc = db.query(FBAccount).filter(FBAccount.id == int(fb_raw), FBAccount.organization_id == org_id).first()
    if not acc or not account_in_active_slot(acc):
        return RedirectResponse("/discovery?err=form", status_code=303)

    results = (
        db.query(DiscoveryResult)
        .filter(DiscoveryResult.id.in_(result_ids), DiscoveryResult.organization_id == org_id)
        .all()
    )

    existing_urls: set[str] = set()
    for d in db.query(Donor).filter(Donor.organization_id == org_id).all():
        existing_urls.add(d.url.strip().lower())

    new_donor_ids: list[int] = []
    for r in results:
        url = r.url.strip()
        if r.result_type == "group":
            gm = normalize_facebook_group_members_url(url)
            if gm:
                url = gm
        if url.lower() in existing_urls:
            r.is_approved = True
            existing = db.query(Donor).filter(Donor.organization_id == org_id, Donor.url == url).first()
            if existing:
                new_donor_ids.append(existing.id)
            continue
        donor = Donor(
            organization_id=org_id,
            url=url,
            name=r.name or None,
            notes=f"Автопоиск: {r.description[:200] if r.description else r.result_type}",
        )
        db.add(donor)
        db.flush()
        r.is_approved = True
        r.donor_id = donor.id
        existing_urls.add(url.lower())
        new_donor_ids.append(donor.id)

    if not new_donor_ids:
        db.commit()
        return RedirectResponse("/discovery?approved=0", status_code=303)

    from backend.services.parser_worker import JOB_TYPE as PARSER_JOB_TYPE, spawn_parser_worker

    admin_id = (request.session.get("user") or {}).get("id")
    parser_job = create_job(
        db,
        organization_id=org_id,
        job_type=PARSER_JOB_TYPE,
        config_snapshot={
            "donor_ids": new_donor_ids,
            "fb_account_id": int(fb_raw),
            "language_filter": "all",
            "parser_language_mode": "fast",
        },
        admin_id=admin_id,
    )
    db.commit()

    spawn_parser_worker(parser_job.id)
    return RedirectResponse(f"/parser?started=1&from_discovery=1", status_code=303)


@router.post("/{task_id}/delete")
async def discovery_delete(task_id: int, request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    task = db.query(DiscoveryTask).filter(
        DiscoveryTask.id == task_id, DiscoveryTask.organization_id == org_id
    ).first()
    if task:
        db.query(DiscoveryResult).filter(DiscoveryResult.task_id == task.id).delete(synchronize_session=False)
        db.delete(task)
        db.commit()
    return RedirectResponse("/discovery", status_code=303)
