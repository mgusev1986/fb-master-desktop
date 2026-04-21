"""Раздел «Прогрев»: кампании, очередь, старт воркера Playwright."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, nullslast
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    Donor,
    FBAccount,
    Job,
    Person,
    WarmupCampaign,
    WarmupQueue,
)
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.job_logging import create_job
from backend.services.tenancy import require_org_id
from backend.services.throttle import (
    PRESET_LABELS,
    TIME_BUDGET_PRESETS,
    check_time_budget_feasibility,
    is_valid_throttle_preset,
)
from backend.services.person_ids_input import parse_person_ids_field
from backend.services.proxy_health_guard import campaign_fb_account_ids_blocked_message
from backend.services.warmup_worker import (
    JOB_TYPE,
    spawn_warmup_worker,
    warmup_worker_at_capacity,
    warmup_worker_busy,
)

router = APIRouter(prefix="/warmup", tags=["warmup"])


def _warmup_campaign_scoped(
    request: Request, db: Session, campaign_id: int
) -> WarmupCampaign | None:
    org_id = require_org_id(request, db)
    camp = db.get(WarmupCampaign, campaign_id)
    if not camp or camp.organization_id != org_id:
        return None
    return camp


def _warmup_accounts_for_form(db: Session, org_id: int) -> list[FBAccount]:
    return (
        db.query(FBAccount)
        .filter(FBAccount.organization_id == org_id)
        .order_by(nullslast(FBAccount.active_slot.asc()), FBAccount.label.asc())
        .all()
    )


def _warmup_selected_accounts_ok(
    db: Session, org_id: int, sel: list[int], *, account_rotation: bool
) -> bool:
    if not sel:
        return True
    uniq = list(dict.fromkeys(sel))
    q = db.query(FBAccount.id).filter(
        FBAccount.id.in_(uniq),
        FBAccount.organization_id == org_id,
    )
    if not account_rotation:
        q = q.filter(FBAccount.active_slot.isnot(None))
    return q.count() == len(uniq)


def _config_from_form(
    *,
    do_like: bool,
    do_comment: bool,
    comment_template: str,
    preset: str,
    time_budget_seconds: str,
    ui_donor_id: str = "",
    account_rotation: bool = False,
    auto_slot_rotation: bool = False,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "do_like": do_like,
        "do_comment": do_comment,
        "comment_template": (comment_template or "").strip(),
        "preset": preset if is_valid_throttle_preset(preset) else "slow",
    }
    if ui_donor_id.strip().isdigit():
        cfg["ui_donor_id"] = int(ui_donor_id.strip())
    tb_raw = (time_budget_seconds or "").strip()
    if tb_raw:
        try:
            v = float(tb_raw.replace(",", "."))
            if v > 0:
                cfg["time_budget_seconds"] = v
        except ValueError:
            pass
    if account_rotation:
        cfg["account_rotation"] = True
    else:
        cfg.pop("account_rotation", None)
    if auto_slot_rotation and account_rotation:
        cfg["auto_slot_rotation"] = True
    else:
        cfg.pop("auto_slot_rotation", None)
    return cfg


def _row_actions_from_campaign_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    c = cfg if isinstance(cfg, dict) else {}
    return {
        "do_like": bool(c.get("do_like", True)),
        "do_comment": bool(c.get("do_comment", False)),
        "comment_template": (c.get("comment_template") or "").strip(),
    }


@router.get("")
async def warmup_list(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    campaigns = (
        db.query(WarmupCampaign)
        .filter(WarmupCampaign.organization_id == org_id)
        .order_by(WarmupCampaign.id.desc())
        .limit(50)
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "warmup/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "warmup",
            "campaigns": campaigns,
            "worker_busy": warmup_worker_busy(),
        },
    )


@router.get("/new")
async def warmup_new_form(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    accounts = _warmup_accounts_for_form(db, org_id)
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "warmup/campaign_form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "warmup",
            "campaign": None,
            "accounts": accounts,
            "donors": donors,
            "preset_labels": PRESET_LABELS,
            "time_budget_presets": TIME_BUDGET_PRESETS,
        },
    )


@router.post("/new")
async def warmup_new_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    org_id = require_org_id(request, db)
    name = (form.get("name") or "").strip() or "Без названия"
    try:
        cap = max(1, min(int(form.get("actions_per_account") or 10), 500))
    except (TypeError, ValueError):
        cap = 10

    sel_accounts = [
        int(x)
        for x in form.getlist("fb_account_id")
        if str(x).strip().isdigit()
    ]
    sel_accounts = list(dict.fromkeys(sel_accounts))

    donor_id = (form.get("donor_id") or "").strip()
    extra_person_ids = form.get("extra_person_ids") or ""
    person_ids: list[int] = []
    if donor_id.isdigit():
        did = int(donor_id)
        person_ids = [
            r[0]
            for r in db.query(Person.id)
            .filter(Person.donor_id == did)
            .order_by(Person.id)
            .all()
        ]
    person_ids.extend(parse_person_ids_field(str(extra_person_ids)))
    person_ids = list(dict.fromkeys(person_ids))

    account_rotation = form.get("account_rotation") == "1"
    auto_slot_rotation = form.get("auto_slot_rotation") == "1"
    if sel_accounts and not _warmup_selected_accounts_ok(
        db, org_id, sel_accounts, account_rotation=account_rotation
    ):
        return RedirectResponse("/warmup/new?err=accounts", status_code=303)

    cfg = _config_from_form(
        do_like=bool(form.get("do_like")),
        do_comment=bool(form.get("do_comment")),
        comment_template=str(form.get("comment_template") or ""),
        preset=str(form.get("preset") or "slow"),
        time_budget_seconds=str(form.get("time_budget_seconds") or ""),
        ui_donor_id=donor_id,
        account_rotation=account_rotation,
        auto_slot_rotation=auto_slot_rotation,
    )

    camp = WarmupCampaign(
        organization_id=org_id,
        name=name,
        status="draft",
        fb_account_ids=sel_accounts,
        person_ids=person_ids,
        actions_per_account=cap,
        config=cfg,
    )
    db.add(camp)
    db.commit()
    db.refresh(camp)
    return RedirectResponse(f"/warmup/{camp.id}", status_code=303)


@router.get("/{campaign_id:int}")
async def warmup_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp:
        return RedirectResponse("/warmup", status_code=303)
    org_id = require_org_id(request, db)
    accounts = _warmup_accounts_for_form(db, org_id)
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    job = db.get(Job, camp.job_id) if camp.job_id else None

    q = db.query(WarmupQueue).filter(WarmupQueue.warmup_campaign_id == camp.id)
    cnt_by = dict(
        db.query(WarmupQueue.status, func.count(WarmupQueue.id))
        .filter(WarmupQueue.warmup_campaign_id == camp.id)
        .group_by(WarmupQueue.status)
        .all()
    )
    rows = q.order_by(WarmupQueue.id.desc()).limit(150).all()
    pids = list({r.person_id for r in rows})
    aids = list({r.fb_account_id for r in rows})
    people = (
        {p.id: p for p in db.query(Person).filter(Person.id.in_(pids)).all()}
        if pids
        else {}
    )
    accs = (
        {a.id: a for a in db.query(FBAccount).filter(FBAccount.id.in_(aids)).all()}
        if aids
        else {}
    )

    total_queue = sum(cnt_by.values()) or 0
    cfg = camp.config if isinstance(camp.config, dict) else {}
    tb = cfg.get("time_budget_seconds")
    feas = None
    if tb and total_queue:
        feas = check_time_budget_feasibility(total_queue, float(tb), "next_person")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "warmup/detail.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "warmup",
            "campaign": camp,
            "accounts": accounts,
            "donors": donors,
            "job": job,
            "queue_rows": rows,
            "cnt_by": cnt_by,
            "people": people,
            "accs": accs,
            "preset_labels": PRESET_LABELS,
            "time_budget_presets": TIME_BUDGET_PRESETS,
            "worker_busy": warmup_worker_busy(),
            "budget_feasibility": feas,
        },
    )


@router.get("/{campaign_id:int}/edit")
async def warmup_edit_form(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp:
        return RedirectResponse("/warmup", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/warmup/{campaign_id}", status_code=303)
    org_id = require_org_id(request, db)
    accounts = _warmup_accounts_for_form(db, org_id)
    donors = (
        db.query(Donor)
        .filter(Donor.organization_id == org_id)
        .order_by(Donor.name, Donor.url)
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "warmup/campaign_form.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "warmup",
            "campaign": camp,
            "accounts": accounts,
            "donors": donors,
            "preset_labels": PRESET_LABELS,
            "time_budget_presets": TIME_BUDGET_PRESETS,
        },
    )


@router.post("/{campaign_id:int}/edit")
async def warmup_edit_save(
    request: Request,
    campaign_id: int,
    db: Session = Depends(get_db),
):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp:
        return RedirectResponse("/warmup", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/warmup/{campaign_id}", status_code=303)

    form = await request.form()
    name = (form.get("name") or "").strip() or camp.name
    try:
        cap = max(1, min(int(form.get("actions_per_account") or 10), 500))
    except (TypeError, ValueError):
        cap = camp.actions_per_account or 10

    sel_accounts = [
        int(x)
        for x in form.getlist("fb_account_id")
        if str(x).strip().isdigit()
    ]
    sel_accounts = list(dict.fromkeys(sel_accounts))

    donor_id = (form.get("donor_id") or "").strip()
    extra_person_ids = form.get("extra_person_ids") or ""
    person_ids: list[int] = []
    if donor_id.isdigit():
        did = int(donor_id)
        person_ids = [
            r[0]
            for r in db.query(Person.id)
            .filter(Person.donor_id == did)
            .order_by(Person.id)
            .all()
        ]
    person_ids.extend(parse_person_ids_field(str(extra_person_ids)))
    person_ids = list(dict.fromkeys(person_ids))

    org_id = require_org_id(request, db)
    account_rotation = form.get("account_rotation") == "1"
    auto_slot_rotation = form.get("auto_slot_rotation") == "1"
    if sel_accounts and not _warmup_selected_accounts_ok(
        db, org_id, sel_accounts, account_rotation=account_rotation
    ):
        return RedirectResponse(f"/warmup/{campaign_id}/edit?err=accounts", status_code=303)

    camp.name = name
    camp.actions_per_account = cap
    camp.fb_account_ids = sel_accounts
    camp.person_ids = person_ids
    camp.config = _config_from_form(
        do_like=bool(form.get("do_like")),
        do_comment=bool(form.get("do_comment")),
        comment_template=str(form.get("comment_template") or ""),
        preset=str(form.get("preset") or "slow"),
        time_budget_seconds=str(form.get("time_budget_seconds") or ""),
        ui_donor_id=donor_id,
        account_rotation=account_rotation,
        auto_slot_rotation=auto_slot_rotation,
    )
    db.commit()
    return RedirectResponse(f"/warmup/{campaign_id}", status_code=303)


def _distribute_queue_rows(
    db: Session,
    camp: WarmupCampaign,
) -> int:
    acc_ids = list(camp.fb_account_ids or [])
    person_ids = list(camp.person_ids or [])
    cap = max(1, min(int(camp.actions_per_account or 10), 500))
    if not acc_ids or not person_ids:
        return 0

    db.query(WarmupQueue).filter(WarmupQueue.warmup_campaign_id == camp.id).delete()
    db.commit()

    counts = {aid: 0 for aid in acc_ids}
    actions = _row_actions_from_campaign_config(camp.config)
    created = 0
    for pid in person_ids:
        acc_sorted = sorted(acc_ids, key=lambda a: counts.get(a, 0))
        picked = None
        for aid in acc_sorted:
            if counts[aid] < cap:
                picked = aid
                break
        if picked is None:
            break
        counts[picked] += 1
        row = WarmupQueue(
            warmup_campaign_id=camp.id,
            campaign_name=camp.name,
            person_id=pid,
            fb_account_id=picked,
            actions=actions,
            status="queued",
            job_id=None,
        )
        db.add(row)
        created += 1
    db.commit()
    return created


@router.post("/{campaign_id:int}/build-queue")
async def warmup_build_queue(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp or camp.status == "running":
        return RedirectResponse(f"/warmup/{campaign_id}" if camp else "/warmup", status_code=303)
    n = _distribute_queue_rows(db, camp)
    if n:
        camp.status = "ready"
    else:
        camp.status = "draft"
    db.commit()
    return RedirectResponse(f"/warmup/{campaign_id}?built={n}", status_code=303)


@router.post("/{campaign_id:int}/start")
async def warmup_start(
    request: Request,
    campaign_id: int,
    db: Session = Depends(get_db),
):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp:
        return RedirectResponse("/warmup", status_code=303)

    admin_id = (request.session.get("user") or {}).get("id")

    ids = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
    proxy_blk = campaign_fb_account_ids_blocked_message(db, ids)
    if proxy_blk:
        return RedirectResponse(
            f"/warmup/{campaign_id}?err=proxy_tunnel&why={quote(proxy_blk, safe='')}",
            status_code=303,
        )
    busy_msg = account_automation_conflict_message(
        db,
        ids,
        ignore_warmup_campaign_id=camp.id,
    )
    if busy_msg:
        return RedirectResponse(
            f"/warmup/{campaign_id}?err=account_busy&why={quote(busy_msg, safe='')}",
            status_code=303,
        )

    if camp.status == "paused":
        camp.status = "running"
        db.commit()
        if camp.job_id:
            spawn_warmup_worker(camp.job_id)
        return RedirectResponse(f"/warmup/{campaign_id}", status_code=303)

    if camp.status not in ("ready", "draft"):
        return RedirectResponse(f"/warmup/{campaign_id}?err=state", status_code=303)

    queued = (
        db.query(WarmupQueue)
        .filter(
            WarmupQueue.warmup_campaign_id == camp.id,
            WarmupQueue.status == "queued",
        )
        .all()
    )
    if not queued:
        return RedirectResponse(f"/warmup/{campaign_id}?err=no_queue", status_code=303)

    if warmup_worker_at_capacity():
        return RedirectResponse(f"/warmup/{campaign_id}?err=busy", status_code=303)

    need_new_job = True
    if camp.job_id:
        old = db.get(Job, camp.job_id)
        if old and old.status == "running":
            return RedirectResponse(f"/warmup/{campaign_id}?err=job_running", status_code=303)
        if old and old.status == "queued":
            need_new_job = False
        elif old and old.status not in ("success", "failed", "cancelled"):
            need_new_job = False

    cfg_snap = {
        "campaign_id": camp.id,
        "campaign_name": camp.name,
        "config": camp.config,
    }
    if need_new_job:
        job = create_job(
            db,
            organization_id=camp.organization_id,
            job_type=JOB_TYPE,
            config_snapshot=cfg_snap,
            admin_id=admin_id,
        )
        camp.job_id = job.id
    job = db.get(Job, camp.job_id)
    if not job:
        return RedirectResponse(f"/warmup/{campaign_id}?err=no_job", status_code=303)

    for row in (
        db.query(WarmupQueue)
        .filter(
            WarmupQueue.warmup_campaign_id == camp.id,
            WarmupQueue.status == "queued",
        )
        .all()
    ):
        row.job_id = job.id
        row.actions = _row_actions_from_campaign_config(camp.config)

    camp.status = "running"
    db.commit()

    spawn_warmup_worker(job.id)
    return RedirectResponse(f"/warmup/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/pause")
async def warmup_pause(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp or camp.status != "running":
        return RedirectResponse(f"/warmup/{campaign_id}" if camp else "/warmup", status_code=303)
    camp.status = "paused"
    from backend.services.automation_auto_resume import strip_auto_resume_flag_from_config

    camp.config = strip_auto_resume_flag_from_config(
        camp.config if isinstance(camp.config, dict) else {}
    )
    db.commit()
    return RedirectResponse(f"/warmup/{campaign_id}", status_code=303)


@router.post("/{campaign_id:int}/delete")
async def warmup_delete(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp:
        return RedirectResponse("/warmup", status_code=303)
    if camp.status == "running":
        return RedirectResponse(f"/warmup/{campaign_id}?err=running", status_code=303)
    if camp.job_id:
        job = db.get(Job, camp.job_id)
        if job and job.status == "running":
            return RedirectResponse(f"/warmup/{campaign_id}?err=job_running", status_code=303)
    db.query(WarmupQueue).filter(WarmupQueue.warmup_campaign_id == camp.id).delete()
    db.delete(camp)
    db.commit()
    return RedirectResponse("/warmup", status_code=303)


@router.post("/{campaign_id:int}/retry-failed")
async def warmup_retry_failed(
    request: Request, campaign_id: int, db: Session = Depends(get_db)
):
    camp = _warmup_campaign_scoped(request, db, campaign_id)
    if not camp or camp.status == "running":
        return RedirectResponse(f"/warmup/{campaign_id}" if camp else "/warmup", status_code=303)
    for row in (
        db.query(WarmupQueue)
        .filter(
            WarmupQueue.warmup_campaign_id == camp.id,
            WarmupQueue.status == "failed",
        )
        .all()
    ):
        row.status = "queued"
        row.error = None
        row.processed_at = None
        row.job_id = None
    camp.status = "ready"
    camp.job_id = None
    db.commit()
    return RedirectResponse(f"/warmup/{campaign_id}", status_code=303)
