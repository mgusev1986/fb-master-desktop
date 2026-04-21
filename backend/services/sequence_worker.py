"""Фоновый воркер: шаги сценариев по дням для зачисленных контактов."""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import (
    FBAccount,
    Job,
    Person,
    SequenceCampaign,
    SequenceEnrollment,
    SequenceStep,
    Setting,
)
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.account_rotation import increment_usage, resolve_sequence_executor, rotation_enabled_sequence
from backend.services.sending_window import is_now_within_send_window, seconds_until_send_window_opens
from backend.services.job_logging import create_job, finish_job, log_crm_activity, log_job_event, start_job
from backend.services.sequence_campaigns import enroll_next_batch, step_wait_settings, wait_timedelta
from backend.services.contacted_registry import upsert_contacted
from backend.services.messenger_worker import record_outbound_dm_in_cabinet
from backend.services.parallel_automation_slots import sequence_worker_slots
from backend.services.playwright_resource import playwright_run_slot
from backend.services.proxy_health_guard import (
    campaign_fb_account_ids_blocked_message,
    pause_all_automation_for_fb_account,
)
from backend.services.sequence_actions import execute_sequence_step, throttle_action_for_step
from backend.services.sequence_timezone import days_since_enrollment_local
from backend.services.throttle import is_valid_throttle_preset, resolve_delay
from backend.services.warmup_actions import parse_storage_state
from backend.services.warmup_worker import fb_account_playwright_profile_ephemeral

logger = logging.getLogger(__name__)

JOB_TYPE = "sequence"


def release_sequence_worker_lock_for_job(job_id: int | None) -> None:
    if job_id is None:
        return
    sequence_worker_slots.end(int(job_id))


def _release_stale_sequence_slots() -> None:
    for jid in list(sequence_worker_slots.running_ids()):
        db = SessionLocal()
        try:
            job = db.get(Job, jid)
            stale = (
                not job
                or job.job_type != JOB_TYPE
                or job.status not in ("running", "queued")
            )
            if stale:
                release_sequence_worker_lock_for_job(jid)
        finally:
            db.close()


def sequence_worker_busy() -> bool:
    _release_stale_sequence_slots()
    return sequence_worker_slots.has_any()


def sequence_worker_at_capacity() -> bool:
    _release_stale_sequence_slots()
    return sequence_worker_slots.at_capacity()


def sequence_worker_holds_campaign_job(db: Session, campaign_id: int) -> bool:
    """Есть ли живой поток sequence с этой кампанией в config_snapshot."""
    _release_stale_sequence_slots()
    cid = int(campaign_id)
    for jid in sequence_worker_slots.running_ids():
        job = db.get(Job, jid)
        if not job or job.job_type != JOB_TYPE:
            continue
        snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
        try:
            if int(snap.get("campaign_id") or 0) == cid:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _global_throttle_preset(db) -> str:
    row = db.get(Setting, "throttle_preset")
    v = row.value if row else None
    if is_valid_throttle_preset(v):
        return v
    return "slow"


def sequence_autotick_enabled(db) -> bool:
    row = db.get(Setting, "sequence_autotick")
    if row is None or row.value is None:
        return True
    v = row.value
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


def sorted_steps_for_campaign(db, sequence_id: int) -> list[SequenceStep]:
    return (
        db.query(SequenceStep)
        .filter(SequenceStep.sequence_id == sequence_id)
        .order_by(SequenceStep.step_index.asc(), SequenceStep.id.asc())
        .all()
    )


def enrollment_is_due(db, enroll: SequenceEnrollment, steps: list[SequenceStep]) -> bool:
    if enroll.status != "active":
        return False
    if not steps:
        return False
    if enroll.current_step_index >= len(steps):
        return False
    step = steps[enroll.current_step_index]
    cfg = step.config if isinstance(step.config, dict) else {}
    if "wait_value" in cfg:
        wait_value, wait_unit = step_wait_settings(step)
        if enroll.current_step_index <= 0:
            anchor = enroll.created_at
        elif enroll.last_run_at is not None:
            anchor = enroll.last_run_at
        else:
            # Шаг > 0, но нет last_run_at (старые данные / сбой) — не держим цепочку вечным ожиданием
            return True
        if anchor is None:
            return True
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= anchor.astimezone(timezone.utc) + wait_timedelta(wait_value, wait_unit)
    return days_since_enrollment_local(db, enroll) >= int(step.day_offset or 0)


def count_due_enrollments(db, campaign_id: int) -> int:
    steps = sorted_steps_for_campaign(db, campaign_id)
    if not steps:
        return 0
    n = 0
    for e in (
        db.query(SequenceEnrollment)
        .filter(SequenceEnrollment.sequence_id == campaign_id)
        .all()
    ):
        if enrollment_is_due(db, e, steps):
            n += 1
    return n


def _sleep_throttle(
    action_type: str,
    *,
    preset: str,
    time_budget_seconds: float | None,
    total_actions: int | None,
) -> None:
    lo, hi = resolve_delay(
        action_type,
        preset=preset,
        time_budget_seconds=time_budget_seconds,
        total_actions=total_actions,
    )
    time.sleep(random.uniform(lo, hi))


def _campaign_preset(camp: SequenceCampaign, db) -> str:
    pf = camp.person_filter if isinstance(camp.person_filter, dict) else {}
    p = pf.get("preset")
    if is_valid_throttle_preset(p):
        return p
    return _global_throttle_preset(db)


def _campaign_time_budget(camp: SequenceCampaign) -> float | None:
    pf = camp.person_filter if isinstance(camp.person_filter, dict) else {}
    raw = pf.get("time_budget_seconds")
    if raw is None:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _sequence_diagnostic_context(
    *,
    campaign_id: int,
    campaign_name: str,
    person: Person | None,
    account: FBAccount | None,
    step: SequenceStep | None,
    step_config: dict[str, Any] | None,
    page_url: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "worker": "sequence",
        "campaign": {
            "id": campaign_id,
            "name": campaign_name,
        },
        "person": {
            "id": person.id if person else None,
            "display_name": (person.display_name if person else None) or (person.first_name if person else None),
            "canonical_url": person.canonical_url if person else None,
        },
        "fb_account": {
            "id": account.id if account else None,
            "label": account.label if account else None,
            "profile_dir": account.profile_dir if account else None,
        },
        "step": {
            "index": step.step_index if step else None,
            "action": step.action_type if step else None,
            "config": step_config or {},
        },
    }
    if page_url:
        ctx["page_url"] = page_url
    if extra:
        ctx["extra"] = extra
    return ctx


def process_sequence_job(job_id: int) -> None:
    err = sequence_worker_slots.try_begin(job_id)
    if err == "duplicate":
        logger.warning("Duplicate sequence thread for job %s", job_id)
        return
    if err == "capacity":
        logger.warning(
            "Sequence job %s skipped: достигнут лимит параллельных сценариев (%s)",
            job_id,
            sequence_worker_slots.cap,
        )
        return

    chain_campaign_id: int | None = None
    chain_after_success = False

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                logger.error("Sequence job %s missing", job_id)
                return

            snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
            campaign_id = snap.get("campaign_id")
            if not isinstance(campaign_id, int):
                finish_job(db, job, status="failed", error="Нет campaign_id в задаче")
                return

            camp = db.get(SequenceCampaign, campaign_id)
            if not camp:
                finish_job(db, job, status="failed", error="Кампания не найдена")
                return

            if camp.status != "active":
                finish_job(db, job, status="cancelled", error="Кампания не активна")
                return

            chain_campaign_id = campaign_id

            if job.status == "queued":
                start_job(db, job)

            preset = _campaign_preset(camp, db)
            tb = _campaign_time_budget(camp)
            throttle_key = "next_person"
            paused_or_stopped = False

            with sync_playwright() as p:
                while True:
                    db.expire_all()
                    camp = db.get(SequenceCampaign, campaign_id)
                    if not camp or camp.status != "active":
                        paused_or_stopped = True
                        break

                    _cfg_send = camp.config if isinstance(camp.config, dict) else {}
                    if not is_now_within_send_window(db, _cfg_send):
                        time.sleep(seconds_until_send_window_opens(db, _cfg_send))
                        continue

                    steps = sorted_steps_for_campaign(db, campaign_id)
                    all_enrolls = (
                        db.query(SequenceEnrollment)
                        .filter(SequenceEnrollment.sequence_id == campaign_id)
                        .order_by(SequenceEnrollment.id.asc())
                        .all()
                    )
                    picked: SequenceEnrollment | None = None
                    for e in all_enrolls:
                        if enrollment_is_due(db, e, steps):
                            picked = e
                            break

                    if not picked or not steps:
                        break

                    step = steps[picked.current_step_index]
                    person = db.get(Person, picked.person_id)
                    aid = picked.fb_account_id
                    if aid is None:
                        ids = camp.fb_account_ids or []
                        if ids:
                            aid = ids[picked.id % len(ids)]

                    if not person:
                        picked.error = "Нет человека в базе"
                        picked.status = "failed"
                        db.commit()
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="sequence_row_failed",
                            severity="error",
                            person_id=picked.person_id,
                            fb_account_id=aid,
                            outcome="fail",
                            payload={"reason": "missing_person"},
                            diagnostic_context=_sequence_diagnostic_context(
                                campaign_id=campaign_id,
                                campaign_name=camp.name,
                                person=person,
                                account=None,
                                step=step,
                                step_config=step.config if isinstance(step.config, dict) else {},
                            ),
                        )
                        total_left = max(1, count_due_enrollments(db, campaign_id))
                        _sleep_throttle(
                            throttle_key,
                            preset=preset,
                            time_budget_seconds=tb,
                            total_actions=total_left,
                        )
                        continue

                    account, rot_err, new_fb_id = resolve_sequence_executor(
                        db, camp, picked.fb_account_id, picked.id
                    )
                    if new_fb_id is not None:
                        picked.fb_account_id = new_fb_id
                        aid = new_fb_id

                    if rot_err:
                        defer = rot_err in (
                            "daily_cap_all_accounts",
                            "need_active_slot",
                            "no_slot_in_pool",
                        )
                        if defer:
                            db.commit()
                            log_job_event(
                                db,
                                job_id=job_id,
                                event_type="sequence_step_deferred",
                                severity="warning",
                                person_id=picked.person_id,
                                fb_account_id=aid,
                                outcome="deferred",
                                payload={"reason": rot_err},
                                diagnostic_context=_sequence_diagnostic_context(
                                    campaign_id=campaign_id,
                                    campaign_name=camp.name,
                                    person=person,
                                    account=account,
                                    step=step,
                                    step_config=step.config if isinstance(step.config, dict) else {},
                                ),
                            )
                            total_left = max(1, count_due_enrollments(db, campaign_id))
                            if rot_err == "daily_cap_all_accounts":
                                time.sleep(60)
                            else:
                                _sleep_throttle(
                                    throttle_key,
                                    preset=preset,
                                    time_budget_seconds=tb,
                                    total_actions=total_left,
                                )
                            continue
                        picked.error = (
                            "Нет аккаунта"
                            if rot_err == "missing_account"
                            else (
                                "Не выбраны аккаунты кампании"
                                if rot_err == "empty_pool"
                                else rot_err
                            )
                        )[:2000]
                        picked.status = "failed"
                        db.commit()
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="sequence_row_failed",
                            severity="error",
                            person_id=picked.person_id,
                            fb_account_id=aid,
                            outcome="fail",
                            payload={"reason": rot_err},
                            diagnostic_context=_sequence_diagnostic_context(
                                campaign_id=campaign_id,
                                campaign_name=camp.name,
                                person=person,
                                account=account,
                                step=step,
                                step_config=step.config if isinstance(step.config, dict) else {},
                            ),
                        )
                        total_left = max(1, count_due_enrollments(db, campaign_id))
                        _sleep_throttle(
                            throttle_key,
                            preset=preset,
                            time_budget_seconds=tb,
                            total_actions=total_left,
                        )
                        continue

                    if not account:
                        picked.error = "Нет аккаунта"
                        picked.status = "failed"
                        db.commit()
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="sequence_row_failed",
                            severity="error",
                            person_id=picked.person_id,
                            fb_account_id=aid,
                            outcome="fail",
                            payload={"reason": "missing_entity"},
                            diagnostic_context=_sequence_diagnostic_context(
                                campaign_id=campaign_id,
                                campaign_name=camp.name,
                                person=person,
                                account=None,
                                step=step,
                                step_config=step.config if isinstance(step.config, dict) else {},
                            ),
                        )
                        total_left = max(1, count_due_enrollments(db, campaign_id))
                        _sleep_throttle(
                            throttle_key,
                            preset=preset,
                            time_budget_seconds=tb,
                            total_actions=total_left,
                        )
                        continue

                    db.commit()

                    cfg = step.config if isinstance(step.config, dict) else {}
                    display = person.display_name or person.first_name or ""
                    first = person.first_name or display or ""

                    t0 = time.perf_counter()
                    page = None
                    step_debug = None
                    step_side_effect: dict[str, Any] = {}
                    throttle_key = throttle_action_for_step(step.action_type)
                    try:
                        with playwright_run_slot(fb_account_id=account.id):
                            page = None
                            ok = False
                            detail = ""
                            with fb_account_playwright_profile_ephemeral(
                                    p,
                                    account,
                                    storage_state=parse_storage_state(account.session_state_json),
                                    db=db,
                                ) as ctx:
                                page = ctx.pages[0] if ctx.pages else ctx.new_page()

                                ok, detail, step_debug = execute_sequence_step(
                                    db,
                                    page,
                                    person_canonical_url=person.canonical_url,
                                    action_type=step.action_type,
                                    step_config=cfg,
                                    display_name=display,
                                    first_name=first,
                                    step_side_effect=step_side_effect,
                                    person_raw_meta=person.raw_meta
                                    if isinstance(person.raw_meta, dict)
                                    else None,
                                )

                                ms = int((time.perf_counter() - t0) * 1000)

                                if ok:
                                    if rotation_enabled_sequence(camp):
                                        increment_usage(db, account.id, "sequence")
                                    if "outbound_dm_text" in step_side_effect:
                                        try:
                                            record_outbound_dm_in_cabinet(
                                                db,
                                                fb_account_id=account.id,
                                                person=person,
                                                message_text=str(step_side_effect["outbound_dm_text"]),
                                            )
                                        except Exception:
                                            logger.exception("record_outbound_dm_in_cabinet")
                                        try:
                                            upsert_contacted(
                                                db,
                                                person_id=person.id,
                                                fb_account_id=account.id,
                                                canonical_url=person.canonical_url or "",
                                            )
                                            now = datetime.now(timezone.utc)
                                            person.crm_stage = "new"
                                            person.crm_stage_changed_at = now
                                        except Exception:
                                            logger.exception("upsert_contacted (sequence DM)")
                                    picked.current_step_index += 1
                                    picked.last_run_at = datetime.now(timezone.utc)
                                    picked.error = None
                                    if picked.current_step_index >= len(steps):
                                        picked.status = "completed"
                                    sev = (
                                        "warning"
                                        if (
                                            step.action_type in (
                                                "like_post",
                                                "like_and_comment",
                                                "like_comment",
                                                "like_comment_friend_request",
                                                "like_comment_friend_request_send_dm",
                                            )
                                            and "INCOMPLETE" in (detail or "")
                                        )
                                        else "info"
                                    )
                                    log_job_event(
                                        db,
                                        job_id=job_id,
                                        event_type="sequence_step_ok",
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        outcome="ok",
                                        severity=sev,
                                        duration_ms=ms,
                                        payload={
                                            "step_index": step.step_index,
                                            "action": step.action_type,
                                            "detail": (detail or "")[:2000],
                                        },
                                    )
                                    log_crm_activity(
                                        db,
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        activity_type=f"sequence_{step.action_type}",
                                        detail=(detail or "")[:2000],
                                    )
                                else:
                                    if "facebook_message_request_limit" in (detail or ""):
                                        pause_all_automation_for_fb_account(db, account.id)
                                        logger.warning(
                                            "Sequence: лимит Messenger — пауза автоматизации "
                                            "fb_account_id=%s",
                                            account.id,
                                        )
                                    picked.status = "failed"
                                    picked.error = (detail or "")[:2000]
                                    log_job_event(
                                        db,
                                        job_id=job_id,
                                        event_type="sequence_step_fail",
                                        severity="error"
                                        if "facebook_message_request_limit" in (detail or "")
                                        else "warning",
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        outcome="fail",
                                        duration_ms=ms,
                                        payload={
                                            "step_index": step.step_index,
                                            "action": step.action_type,
                                            "detail": (detail or "")[:2000],
                                        },
                                        diagnostic_context=_sequence_diagnostic_context(
                                            campaign_id=campaign_id,
                                            campaign_name=camp.name,
                                            person=person,
                                            account=account,
                                            step=step,
                                            step_config=cfg,
                                            page_url=page.url if page else None,
                                            extra={"step_debug": step_debug} if step_debug else None,
                                        ),
                                    )
                    except Exception as e:
                        logger.exception("sequence enroll %s", picked.id)
                        ms = int((time.perf_counter() - t0) * 1000)
                        picked.status = "failed"
                        picked.error = str(e)[:1000]
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="sequence_step_error",
                            severity="error",
                            person_id=picked.person_id,
                            fb_account_id=picked.fb_account_id,
                            outcome="fail",
                            duration_ms=ms,
                            payload={
                                "step_index": step.step_index,
                                "action": step.action_type,
                                "error": str(e)[:300],
                            },
                            diagnostic_context=_sequence_diagnostic_context(
                                campaign_id=campaign_id,
                                campaign_name=camp.name,
                                person=person,
                                account=account,
                                step=step,
                                step_config=cfg,
                                page_url=page.url if "page" in locals() and page else None,
                                extra={
                                    "exception_type": type(e).__name__,
                                    "step_debug": step_debug,
                                },
                            ),
                        )

                    db.commit()

                    total_left = max(1, count_due_enrollments(db, campaign_id))
                    _sleep_throttle(
                        throttle_key,
                        preset=preset,
                        time_budget_seconds=tb,
                        total_actions=total_left,
                    )

            db.expire_all()
            job = db.get(Job, job_id)
            if job and job.status == "running":
                finish_job(db, job, status="success")
                chain_after_success = not paused_or_stopped
            db.commit()
        finally:
            db.close()
    finally:
        sequence_worker_slots.end(job_id)

    if chain_after_success and chain_campaign_id is not None:
        sequence_autopilot_try_continue(chain_campaign_id)


def spawn_sequence_worker(job_id: int) -> None:
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        logger.warning("spawn_sequence_worker пропущен (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
        return
    t = threading.Thread(
        target=process_sequence_job,
        args=(job_id,),
        name=f"sequence-job-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned sequence worker for job %s", job_id)


def sequence_autopilot_try_continue(campaign_id: int) -> None:
    """После успешного job: при включённых авто-партиях сразу подобрать следующую партию и запустить воркер, не дожидаясь тика ~15 мин."""
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        return
    if sequence_worker_at_capacity():
        return
    db = SessionLocal()
    try:
        camp = db.get(SequenceCampaign, campaign_id)
        if not camp or camp.status != "active":
            return
        ids = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
        if campaign_fb_account_ids_blocked_message(db, ids):
            return
        if account_automation_conflict_message(
            db,
            ids,
            ignore_sequence_campaign_id=camp.id,
        ):
            return
        enroll_next_batch(db, camp, force=False)
        db.expire_all()
        if count_due_enrollments(db, campaign_id) <= 0:
            return
        if sequence_worker_at_capacity():
            return
        job = create_job(
            db,
            organization_id=camp.organization_id,
            job_type=JOB_TYPE,
            config_snapshot={"campaign_id": camp.id, "autopilot_chain": True},
            admin_id=None,
        )
        db.commit()
        spawn_sequence_worker(job.id)
        logger.info("Autopilot chain spawned job %s for campaign %s", job.id, campaign_id)
    except Exception:
        logger.exception("sequence_autopilot_try_continue campaign_id=%s", campaign_id)
    finally:
        db.close()


def try_sequence_autotick() -> None:
    """Вызывается из фонового потока: одна активная кампания с очередью «пора»."""
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        return
    if sequence_worker_at_capacity():
        return

    db = SessionLocal()
    try:
        if not sequence_autotick_enabled(db):
            return
        for camp in (
            db.query(SequenceCampaign)
            .filter(SequenceCampaign.status == "active")
            .order_by(SequenceCampaign.id.asc())
            .all()
        ):
            ids = camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else []
            if campaign_fb_account_ids_blocked_message(db, ids):
                continue
            if account_automation_conflict_message(
                db,
                ids,
                ignore_sequence_campaign_id=camp.id,
            ):
                continue
            enroll_next_batch(db, camp, force=False)
            if count_due_enrollments(db, camp.id) <= 0:
                continue
            job = create_job(
                db,
                organization_id=camp.organization_id,
                job_type=JOB_TYPE,
                config_snapshot={"campaign_id": camp.id, "autotick": True},
                admin_id=None,
            )
            db.commit()
            spawn_sequence_worker(job.id)
            return
    finally:
        db.close()
