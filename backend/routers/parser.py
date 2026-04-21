"""Парсер списка друзей по донорам → автоматический импорт в «Базу контактов»."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.services.cabinet_settings import effective_playwright_headless
from backend.database import get_db
from backend.models import Donor, FBAccount, Job
from backend.services.fb_account_slots import account_in_active_slot, fb_accounts_for_jobs
from backend.services.job_logging import create_job
from backend.services.person_language import LANGUAGE_FILTER_LABELS, normalize_person_language_filter
from backend.services.tenancy import require_org_id
from backend.services.parser_worker import (
    JOB_TYPE,
    finish_stale_parser_jobs,
    normalize_parser_language_mode,
    parser_org_live_job,
    parser_worker_at_capacity,
    parser_worker_busy,
    parser_worker_busy_for_org,
    request_parser_cancel,
    spawn_parser_worker,
)
from backend.services.proxy_health_guard import (
    PROXY_AUTOMATION_BLOCKED_HINT,
    account_proxy_tunnel_blocked,
)

router = APIRouter(prefix="/parser", tags=["parser"])


def _parser_progress_percent(p: dict[str, Any] | None) -> int | None:
    if not p:
        return None
    t = int(p.get("donors_total") or 0)
    if t <= 0:
        return None
    d = int(p.get("donors_done") or 0)
    ph = str(p.get("phase") or "")
    sr = int(p.get("scroll_round") or 0)
    exp = int(p.get("expected_friends") or 0)
    found = int(p.get("friends_found") or 0)
    extra = 0.0
    if ph == "starting":
        extra = 0.03
    elif ph == "browser_open":
        extra = 0.06
    elif ph == "navigating":
        extra = 0.08
    elif ph == "scrolling":
        if exp >= 50 and found > 0:
            extra = min(0.88, 0.05 + 0.83 * min(1.0, float(found) / float(exp)))
        else:
            extra = min(0.88, sr / 240.0)
    elif ph == "importing":
        extra = 0.94
    elif ph == "idle":
        extra = 0.0
    return min(100, max(0, int(round(100 * (d + extra) / t))))


@router.get("")
async def parser_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    parser_language_filter = normalize_person_language_filter(
        request.query_params.get("language_filter")
    )
    parser_language_mode = normalize_parser_language_mode(
        request.query_params.get("parser_language_mode")
    )
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    accounts = fb_accounts_for_jobs(db, org_id)
    last_job = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.organization_id == org_id)
        .order_by(Job.id.desc())
        .first()
    )
    parser_running_fb_account_label: str | None = None
    if last_job and last_job.status in ("running", "queued"):
        snap = last_job.config_snapshot if isinstance(last_job.config_snapshot, dict) else {}
        aid = snap.get("fb_account_id")
        if aid is not None:
            a = db.get(FBAccount, int(aid))
            if a and a.organization_id == org_id:
                parser_running_fb_account_label = (a.label or "").strip() or f"ID {a.id}"
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "parser/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "parser",
            "donors": donors,
            "accounts": accounts,
            "parser_busy": parser_worker_busy(),
            "last_parser_job": last_job,
            "parser_running_fb_account_label": parser_running_fb_account_label,
            "parser_headless": effective_playwright_headless(db),
            "parser_language_filter": parser_language_filter,
            "parser_language_mode": parser_language_mode,
            "parser_language_filters": {
                key: label
                for key, label in LANGUAGE_FILTER_LABELS.items()
                if key != "unknown"
            },
        },
    )


@router.post("/run")
async def parser_run(request: Request, db: Session = Depends(get_db)):
    if parser_worker_at_capacity():
        return RedirectResponse("/parser?err=busy", status_code=303)

    org_id = require_org_id(request, db)
    form = await request.form()
    donor_ids = [int(x) for x in form.getlist("donor_id") if str(x).strip().isdigit()]
    fb_raw = (form.get("fb_account_id") or "").strip()
    language_filter = normalize_person_language_filter(form.get("language_filter"))
    parser_language_mode = normalize_parser_language_mode(form.get("parser_language_mode"))
    if parser_language_mode == "none" and language_filter != "all":
        return RedirectResponse("/parser?err=language_mode_none", status_code=303)
    if not donor_ids or not fb_raw.isdigit():
        return RedirectResponse("/parser?err=form", status_code=303)

    n_donors = (
        db.query(Donor.id)
        .filter(
            Donor.id.in_(donor_ids),
            Donor.organization_id == org_id,
        )
        .count()
    )
    if n_donors != len(set(donor_ids)):
        return RedirectResponse("/parser?err=form", status_code=303)
    acc = (
        db.query(FBAccount)
        .filter(FBAccount.id == int(fb_raw), FBAccount.organization_id == org_id)
        .first()
    )
    if not acc or not account_in_active_slot(acc):
        return RedirectResponse("/parser?err=form", status_code=303)
    if account_proxy_tunnel_blocked(acc):
        return RedirectResponse(
            "/parser?err=proxy_tunnel&why=" + quote(PROXY_AUTOMATION_BLOCKED_HINT, safe=""),
            status_code=303,
        )

    admin_id = (request.session.get("user") or {}).get("id")
    job = create_job(
        db,
        organization_id=org_id,
        job_type=JOB_TYPE,
        config_snapshot={
            "donor_ids": donor_ids,
            "fb_account_id": int(fb_raw),
            "language_filter": language_filter,
            "parser_language_mode": parser_language_mode,
        },
        admin_id=admin_id,
    )
    spawn_parser_worker(job.id)
    return RedirectResponse(
        f"/parser?started=1&language_filter={quote(language_filter, safe='')}"
        f"&parser_language_mode={quote(parser_language_mode, safe='')}",
        status_code=303,
    )


@router.get("/progress")
async def parser_progress_json(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    busy = parser_worker_busy_for_org(db, org_id)
    job = parser_org_live_job(db, org_id)
    if not job or job.job_type != JOB_TYPE or job.organization_id != org_id:
        job = (
            db.query(Job)
            .filter(Job.job_type == JOB_TYPE, Job.organization_id == org_id)
            .order_by(Job.id.desc())
            .first()
        )
    progress = job.progress_json if job and isinstance(job.progress_json, dict) else None
    fb_account_label: str | None = None
    if job and isinstance(getattr(job, "config_snapshot", None), dict):
        aid = job.config_snapshot.get("fb_account_id")
        if aid is not None:
            acc = db.get(FBAccount, int(aid))
            if acc and acc.organization_id == org_id:
                fb_account_label = (acc.label or "").strip() or f"ID {acc.id}"
    return JSONResponse(
        {
            "busy": busy,
            "job_id": job.id if job else None,
            "status": job.status if job else None,
            "progress": progress,
            "percent": _parser_progress_percent(progress),
            "error_summary": (job.error_summary or None) if job else None,
            "fb_account_label": fb_account_label,
        }
    )


@router.post("/cancel")
async def parser_cancel_json(db: Session = Depends(get_db)):
    ok = request_parser_cancel()
    stale_cleared = 0
    if not ok:
        stale_cleared = finish_stale_parser_jobs(db)
    return JSONResponse({"ok": ok or stale_cleared > 0, "stale_cleared": stale_cleared})
