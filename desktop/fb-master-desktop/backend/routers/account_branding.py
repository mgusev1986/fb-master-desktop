"""Раздел «Оформление аккаунтов»: сценарий внешнего вида и постов по эталону / ИИ / папке медиа."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import FBAccount, Job
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.account_branding_worker import (
    JOB_TYPE,
    account_branding_worker_at_capacity,
    spawn_account_branding_worker,
)
from backend.services.fb_account_slots import account_in_active_slot, fb_accounts_for_jobs
from backend.services.job_logging import create_job, finish_job
from backend.services.proxy_health_guard import PROXY_AUTOMATION_BLOCKED_HINT, account_proxy_tunnel_blocked
from backend.services.tenancy import require_org_id

router = APIRouter(prefix="/account-branding", tags=["account_branding"])


def _default_draft() -> dict[str, Any]:
    return {
        "example_profile_url": "",
        "copy_avatar": True,
        "copy_cover": False,
        "posts_count": 5,
        "post_content_mode": "copy_from_example",
        "images_mode": "from_example",
        "images_folder_path": "",
        "schedule_mode": "smart",
        "interval_hours_min": 2.0,
        "interval_hours_max": 5.0,
        "daily_posts_max": 2,
        "tone_notes": "",
        "ai_image_prompt_extra": "",
    }


def _merge_draft(base: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
    out = {**_default_draft(), **base}
    url = (form.get("example_profile_url") or out.get("example_profile_url") or "").strip()
    out["example_profile_url"] = url[:2000]
    out["copy_avatar"] = form.get("copy_avatar") == "1"
    out["copy_cover"] = form.get("copy_cover") == "1"
    raw_n = (form.get("posts_count") or out.get("posts_count") or "5").strip()
    try:
        n = int(raw_n)
    except ValueError:
        n = 5
    out["posts_count"] = max(0, min(50, n))
    pcm = (form.get("post_content_mode") or out.get("post_content_mode") or "copy_from_example").strip()
    if pcm not in ("copy_from_example", "ai_rewrite", "ai_from_scratch"):
        pcm = "copy_from_example"
    out["post_content_mode"] = pcm
    im = (form.get("images_mode") or out.get("images_mode") or "from_example").strip()
    if im not in ("from_example", "from_folder", "mixed"):
        im = "from_example"
    out["images_mode"] = im
    out["images_folder_path"] = (form.get("images_folder_path") or out.get("images_folder_path") or "").strip()[:2000]
    sm = (form.get("schedule_mode") or out.get("schedule_mode") or "smart").strip()
    if sm not in ("smart", "fixed_interval"):
        sm = "smart"
    out["schedule_mode"] = sm
    try:
        lo = float((form.get("interval_hours_min") or out.get("interval_hours_min") or 2))
    except (TypeError, ValueError):
        lo = 2.0
    try:
        hi = float((form.get("interval_hours_max") or out.get("interval_hours_max") or 5))
    except (TypeError, ValueError):
        hi = 5.0
    out["interval_hours_min"] = max(0.5, min(72.0, lo))
    out["interval_hours_max"] = max(out["interval_hours_min"], min(120.0, hi))
    try:
        dmax = int((form.get("daily_posts_max") or out.get("daily_posts_max") or 2))
    except (TypeError, ValueError):
        dmax = 2
    out["daily_posts_max"] = max(1, min(8, dmax))
    out["tone_notes"] = (form.get("tone_notes") or out.get("tone_notes") or "").strip()[:4000]
    out["ai_image_prompt_extra"] = (form.get("ai_image_prompt_extra") or out.get("ai_image_prompt_extra") or "").strip()[
        :2000
    ]
    return out


def _redirect(
    msg: str | None = None,
    *,
    err: str | None = None,
    why: str | None = None,
) -> RedirectResponse:
    q: list[str] = []
    if msg:
        q.append("msg=" + quote(msg[:500]))
    if err:
        q.append("err=" + quote(err[:200]))
    if why:
        q.append("why=" + quote(why[:500]))
    suf = ("?" + "&".join(q)) if q else ""
    return RedirectResponse(f"/account-branding{suf}", status_code=303)


def _list_pending_branding_jobs(db: Session, org_id: int) -> list[Job]:
    return (
        db.query(Job)
        .filter(
            Job.organization_id == org_id,
            Job.job_type == JOB_TYPE,
            Job.status.in_(("queued", "running")),
        )
        .order_by(Job.id.desc())
        .limit(50)
        .all()
    )


def _pick_job_for_account(jobs: list[Job], fb_account_id: int) -> Job | None:
    for j in jobs:
        snap = j.config_snapshot if isinstance(j.config_snapshot, dict) else {}
        try:
            if int(snap.get("fb_account_id") or 0) == fb_account_id:
                return j
        except (TypeError, ValueError):
            continue
    return None


@router.get("")
async def account_branding_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(FBAccount.organization_id == org_id)
        .order_by(FBAccount.active_slot.asc().nullslast(), FBAccount.label)
        .all()
    )
    slotted = {a.id for a in fb_accounts_for_jobs(db, org_id)}
    pending = _list_pending_branding_jobs(db, org_id)
    jobs_by_account: dict[int, Job] = {}
    for a in accounts:
        j = _pick_job_for_account(list(pending), a.id)
        if j:
            jobs_by_account[a.id] = j

    drafts: dict[int, dict[str, Any]] = {}
    for a in accounts:
        raw = a.account_branding_draft_json if isinstance(a.account_branding_draft_json, dict) else {}
        drafts[a.id] = {**_default_draft(), **raw}

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "account_branding/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "account_branding",
            "accounts": accounts,
            "slotted_ids": slotted,
            "branding_drafts": drafts,
            "branding_jobs_by_account": jobs_by_account,
            "branding_worker_busy": account_branding_worker_at_capacity(),
        },
    )


@router.post("/save-draft")
async def account_branding_save_draft(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    aid = int(raw)
    acc = db.query(FBAccount).filter(FBAccount.id == aid, FBAccount.organization_id == org_id).first()
    if not acc:
        return _redirect(err="bad_account")
    prev = acc.account_branding_draft_json if isinstance(acc.account_branding_draft_json, dict) else {}
    acc.account_branding_draft_json = _merge_draft(prev, dict(form))
    db.commit()
    return _redirect(msg="Черновик настроек сохранён для этого аккаунта.")


@router.post("/start")
async def account_branding_start(request: Request, db: Session = Depends(get_db)):
    if account_branding_worker_at_capacity():
        return _redirect(err="worker_busy")

    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    aid = int(raw)
    acc = db.query(FBAccount).filter(FBAccount.id == aid, FBAccount.organization_id == org_id).first()
    if not acc:
        return _redirect(err="bad_account")
    if not account_in_active_slot(acc):
        return _redirect(err="no_slot")
    if account_proxy_tunnel_blocked(acc):
        return _redirect(err="proxy_tunnel", why=PROXY_AUTOMATION_BLOCKED_HINT)

    prev = acc.account_branding_draft_json if isinstance(acc.account_branding_draft_json, dict) else {}
    draft = _merge_draft(prev, dict(form))
    acc.account_branding_draft_json = draft
    db.commit()

    busy_msg = account_automation_conflict_message(db, [aid])
    if busy_msg:
        return _redirect(err="account_busy", why=busy_msg)

    pending = _list_pending_branding_jobs(db, org_id)
    if _pick_job_for_account(pending, aid):
        return _redirect(err="already")

    admin_id = (request.session.get("user") or {}).get("id")
    job = create_job(
        db,
        organization_id=org_id,
        job_type=JOB_TYPE,
        config_snapshot={
            "fb_account_id": aid,
            "posts_count": draft.get("posts_count"),
            "example_profile_url": (draft.get("example_profile_url") or "")[:500],
        },
        admin_id=admin_id,
    )
    spawn_account_branding_worker(job.id)
    return _redirect(msg="Задача оформления поставлена в очередь. Статус и журнал — в «История и отчёты».")


@router.post("/cancel")
async def account_branding_cancel(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    aid = int(raw)
    jobs = (
        db.query(Job)
        .filter(
            Job.organization_id == org_id,
            Job.job_type == JOB_TYPE,
            Job.status == "queued",
        )
        .order_by(Job.id.desc())
        .limit(20)
        .all()
    )
    j = _pick_job_for_account(list(jobs), aid)
    if not j:
        return _redirect(err="no_queued_job")
    finish_job(db, j, status="cancelled", error="Отменено в интерфейсе «Оформление аккаунтов».")
    db.commit()
    return _redirect(msg="Ожидающая задача снята с очереди.")
