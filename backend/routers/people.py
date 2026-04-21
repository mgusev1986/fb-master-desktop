"""Список людей (контактов)."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Job, Person
from backend.services.fb_account_slots import fb_accounts_for_jobs
from backend.services.job_logging import create_job
from backend.services.people_language_worker import (
    JOB_TYPE as PEOPLE_LANGUAGE_JOB_TYPE,
    active_people_language_job_id_for_org,
    finish_stale_people_language_jobs,
    people_language_worker_at_capacity,
    people_language_worker_busy,
    people_language_worker_busy_for_org,
    spawn_people_language_worker,
)
from backend.services.person_language import (
    LANGUAGE_FILTER_LABELS,
    apply_person_language_filter,
    language_segment_badge,
    language_segment_label,
    normalize_person_language_filter,
)
from backend.services.crm_stages_registry import get_ordered_stages
from backend.services.person_delete import delete_person_cascade
from backend.services.person_search import apply_person_text_search_filter
from backend.services.tenancy import require_org_id

router = APIRouter(prefix="/people", tags=["people"])

PAGE_SIZE = 50


def _people_base_query(db: Session, *, org_id: int, q: str, stage: str, lang: str):
    query = (
        db.query(Person)
        .filter(Person.organization_id == org_id)
        .order_by(Person.created_at.desc())
    )
    query = apply_person_text_search_filter(query, q)
    if stage:
        query = query.filter(Person.crm_stage == stage)
    query = apply_person_language_filter(query, lang)
    return query


@router.get("")
async def people_list(request: Request, db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    page = int(request.query_params.get("page", "1"))
    stage = request.query_params.get("stage", "")
    lang = normalize_person_language_filter(request.query_params.get("lang"))

    org_id = require_org_id(request, db)
    query = _people_base_query(db, org_id=org_id, q=q, stage=stage, lang=lang)

    total = query.count()
    people = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    crm_stages = get_ordered_stages(db)
    accounts = fb_accounts_for_jobs(db, org_id)
    last_language_job = (
        db.query(Job)
        .filter(Job.job_type == PEOPLE_LANGUAGE_JOB_TYPE, Job.organization_id == org_id)
        .order_by(Job.id.desc())
        .first()
    )
    language_running_fb_account_label: str | None = None
    if last_language_job and isinstance(last_language_job.config_snapshot, dict):
        aid = last_language_job.config_snapshot.get("fb_account_id")
        if aid is not None:
            for acc in accounts:
                if acc.id == int(aid):
                    language_running_fb_account_label = (acc.label or "").strip() or f"ID {acc.id}"
                    break

    templates = request.app.state.templates
    return templates.TemplateResponse("people/list.html", {
        "request": request,
        "user": request.session.get("user"),
        "people": people,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "total_pages": total_pages,
        "search": q,
        "stage": stage,
        "lang": lang,
        "page_id": "people",
        "crm_stages": crm_stages,
        "language_filter_labels": LANGUAGE_FILTER_LABELS,
        "language_segment_label": language_segment_label,
        "language_segment_badge": language_segment_badge,
        "segment_accounts": accounts,
        "segment_busy": people_language_worker_busy(),
        "last_language_job": last_language_job,
        "segment_running_fb_account_label": language_running_fb_account_label,
    })


@router.get("/api/search")
async def people_api_search(request: Request, db: Session = Depends(get_db)):
    """JSON для живого поиска в «Базе контактов» (debounce на клиенте)."""
    q = request.query_params.get("q", "").strip()
    page = max(1, int(request.query_params.get("page", "1") or "1"))
    stage = (request.query_params.get("stage") or "").strip()
    lang = normalize_person_language_filter(request.query_params.get("lang"))

    org_id = require_org_id(request, db)
    query = _people_base_query(db, org_id=org_id, q=q, stage=stage, lang=lang)

    total = query.count()
    people = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    stage_rows = get_ordered_stages(db)
    label_by_slug = {s.slug: s.label for s in stage_rows}

    items = []
    for p in people:
        items.append(
            {
                "id": p.id,
                "display_name": p.display_name or "",
                "canonical_url": p.canonical_url or "",
                "canonical_short": (p.canonical_url or "").replace(
                    "https://www.facebook.com/", ""
                ),
                "crm_stage": p.crm_stage or "",
                "crm_stage_label": label_by_slug.get(p.crm_stage or "", p.crm_stage or "—"),
                "created_at": p.created_at.strftime("%d.%m.%Y") if p.created_at else "—",
                "fb_profile_restricted": bool(p.fb_profile_restricted),
                "language_segment": p.language_segment or "unknown",
                "language_label": language_segment_label(p.language_segment),
                "language_badge": language_segment_badge(p.language_segment),
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": total_pages,
            "items": items,
        }
    )


def _people_list_redirect(ret_q: str, ret_stage: str, ret_lang: str, ret_page: int) -> RedirectResponse:
    params: dict[str, str] = {}
    q = (ret_q or "").strip()
    st = (ret_stage or "").strip()
    lg = normalize_person_language_filter(ret_lang)
    if q:
        params["q"] = q
    if st:
        params["stage"] = st
    if lg and lg != "all":
        params["lang"] = lg
    if ret_page and ret_page > 1:
        params["page"] = str(int(ret_page))
    url = "/people"
    if params:
        url += "?" + urlencode(params)
    return RedirectResponse(url, status_code=303)


@router.post("/{person_id}/delete")
async def person_delete(
    request: Request,
    person_id: int,
    db: Session = Depends(get_db),
    ret_q: str = Form(""),
    ret_stage: str = Form(""),
    ret_lang: str = Form("all"),
    ret_page: int = Form(1),
):
    org_id = require_org_id(request, db)
    if not delete_person_cascade(db, person_id, organization_id=org_id):
        raise HTTPException(status_code=404, detail="Контакт не найден")
    return _people_list_redirect(ret_q, ret_stage, ret_lang, ret_page)


@router.post("/segment/run")
async def people_segment_run(request: Request, db: Session = Depends(get_db)):
    if people_language_worker_at_capacity():
        return RedirectResponse("/people?segerr=busy", status_code=303)

    org_id = require_org_id(request, db)
    form = await request.form()
    q = (form.get("q") or "").strip()
    stage = (form.get("stage") or "").strip()
    lang = normalize_person_language_filter(form.get("lang"))
    fb_raw = (form.get("fb_account_id") or "").strip()
    force_rescan = form.get("force_rescan") == "1"
    if not fb_raw.isdigit():
        return RedirectResponse("/people?segerr=account", status_code=303)

    accounts = {a.id: a for a in fb_accounts_for_jobs(db, org_id)}
    account = accounts.get(int(fb_raw))
    if not account:
        return RedirectResponse("/people?segerr=account", status_code=303)

    query = _people_base_query(
        db,
        org_id=org_id,
        q=q,
        stage=stage,
        lang=lang if force_rescan else "all",
    )
    if not force_rescan:
        query = apply_person_language_filter(query, "unknown")
    person_ids = [pid for (pid,) in query.with_entities(Person.id).order_by(Person.id.asc()).all()]
    if not person_ids:
        return RedirectResponse("/people?segerr=no_people", status_code=303)

    admin_id = (request.session.get("user") or {}).get("id")
    job = create_job(
        db,
        organization_id=org_id,
        job_type=PEOPLE_LANGUAGE_JOB_TYPE,
        config_snapshot={
            "person_ids": person_ids,
            "fb_account_id": int(fb_raw),
            "q": q,
            "stage": stage,
            "lang": lang,
            "force_rescan": force_rescan,
        },
        admin_id=admin_id,
    )
    spawn_people_language_worker(job.id)
    return RedirectResponse("/people?segstarted=1", status_code=303)


@router.get("/segment/progress")
async def people_segment_progress(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    busy = people_language_worker_busy_for_org(db, org_id)
    jid = active_people_language_job_id_for_org(db, org_id)
    job = db.get(Job, jid) if jid else None
    if not job or job.job_type != PEOPLE_LANGUAGE_JOB_TYPE or job.organization_id != org_id:
        job = (
            db.query(Job)
            .filter(Job.job_type == PEOPLE_LANGUAGE_JOB_TYPE, Job.organization_id == org_id)
            .order_by(Job.id.desc())
            .first()
        )
    progress = job.progress_json if job and isinstance(job.progress_json, dict) else None
    if not busy:
        finish_stale_people_language_jobs(db)
    return JSONResponse(
        {
            "busy": busy,
            "job_id": job.id if job else None,
            "status": job.status if job else None,
            "progress": progress,
            "error_summary": (job.error_summary or None) if job else None,
        }
    )
