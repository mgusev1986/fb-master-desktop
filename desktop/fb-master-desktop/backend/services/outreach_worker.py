"""Фоновый воркер очереди кампаний: ЛС и комментинг."""

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
    FBAccountDayUsage,
    Job,
    OutreachCampaign,
    OutreachQueue,
    Person,
    Setting,
)
from backend.services.contacted_registry import upsert_contacted
from backend.services.account_rotation import increment_usage, resolve_outreach_executor, rotation_enabled_outreach
from backend.services.sending_window import is_now_within_send_window, seconds_until_send_window_opens
from backend.services.outreach_campaign_done import outreach_append_done_person
from backend.services.first_touch_templates import pick_first_touch_templates
from backend.services.job_logging import finish_job, log_crm_activity, log_job_event, start_job
from backend.services.messenger_worker import record_outbound_dm_in_cabinet
from backend.services.message_template_render import (
    apply_template_placeholders,
    placeholder_kwargs_from_person,
    render_outreach_message_for_person,
    resolve_template_variant,
)
from backend.services.outreach_actions import run_commenting_on_profile, run_outreach_on_profile
from backend.services.outreach_error_display import humanize_outreach_row_error
from backend.services.outreach_job_wait import (
    clear_outreach_wait,
    reason_humanize,
    record_outreach_wait,
    should_abort_due_to_wait,
)
from backend.services.sequence_ai_comment import (
    extract_timeline_post_text,
    generate_dm_message_sync,
    generate_profile_post_comment_sync,
    infer_dm_output_locale_for_ai_dm,
)
from backend.services.throttle import is_valid_throttle_preset, resolve_delay
from backend.services.parallel_automation_slots import outreach_worker_slots
from backend.services.playwright_resource import playwright_run_slot
from backend.services.proxy_health_guard import ProxyTunnelBlockedError, pause_all_automation_for_fb_account
from backend.services.warmup_actions import parse_storage_state, profile_url_from_person
from backend.services.outreach_shared_profile import (
    outreach_shared_disk_profile_eligible,
    outreach_shared_profile_feature_enabled,
    release_outreach_shared_profile_lock,
    try_acquire_outreach_shared_profile_lock,
)
from backend.services.warmup_worker import fb_account_playwright_profile_ephemeral

logger = logging.getLogger(__name__)

JOB_TYPE = "outreach"

# job_id → активный BrowserContext рассылки (для немедленного terminate Chromium при «Пауза» в UI).
_outreach_pw_kill_lock = threading.Lock()
_outreach_pw_kill_registry: dict[int, Any] = {}


def _register_outreach_ctx_for_kill(job_id: int, ctx: Any) -> None:
    if job_id and ctx is not None:
        with _outreach_pw_kill_lock:
            _outreach_pw_kill_registry[int(job_id)] = ctx


def _unregister_outreach_ctx_for_kill(job_id: int | None) -> None:
    if not job_id:
        return
    with _outreach_pw_kill_lock:
        _outreach_pw_kill_registry.pop(int(job_id), None)


def force_kill_outreach_playwright_for_job(job_id: int | None) -> None:
    """
    Сразу завершает процесс Chromium рассылки (не ждать конца try_send_dm / sleep в воркере).
    Вызывается из POST /outreach/.../pause после сохранения статуса paused в БД.
    """
    if not job_id:
        return
    jid = int(job_id)
    with _outreach_pw_kill_lock:
        ctx = _outreach_pw_kill_registry.get(jid)
    if ctx is None:
        logger.info("Outreach pause: нет зарегистрированного Playwright-контекста job_id=%s", jid)
        return
    browser = None
    try:
        browser = ctx.browser
    except Exception:
        pass
    proc = getattr(browser, "process", None) if browser else None
    if proc is not None:
        try:
            proc.terminate()
            logger.info("Outreach pause: Chromium terminate() job_id=%s pid=%s", jid, getattr(proc, "pid", None))
        except Exception:
            logger.warning("Outreach pause: terminate() job_id=%s", jid, exc_info=True)
        return
    try:
        ctx.close()
        logger.info("Outreach pause: context.close() job_id=%s (без proc)", jid)
    except Exception:
        logger.debug("Outreach pause: context.close job_id=%s", jid, exc_info=True)


def _sleep_throttle_outreach_interruptible(
    db: Session,
    *,
    campaign_id: int,
    action_type: str,
    preset: str,
    time_budget_seconds: float | None,
    total_actions: int | None,
    chunk_sec: float = 0.4,
) -> None:
    """Как _sleep_throttle, но выходит сразу, если кампанию поставили на паузу (не ждать весь jitter)."""
    lo, hi = resolve_delay(
        action_type,
        preset=preset,
        time_budget_seconds=time_budget_seconds,
        total_actions=total_actions,
    )
    target = random.uniform(lo, hi)
    slept = 0.0
    while slept + 1e-9 < target:
        try:
            db.expire_all()
            c = db.get(OutreachCampaign, campaign_id)
            if not c or c.status != "running":
                return
        except Exception:
            return
        step = min(chunk_sec, target - slept)
        time.sleep(step)
        slept += step


def _sleep_seconds_outreach_interruptible(
    db: Session,
    *,
    campaign_id: int,
    total_sec: float,
    chunk_sec: float = 0.45,
) -> None:
    """Спит до total_sec секунд, но прерывается, если кампания не running (пауза)."""
    if total_sec <= 0:
        return
    deadline = time.perf_counter() + float(total_sec)
    while time.perf_counter() < deadline:
        try:
            db.expire_all()
            c = db.get(OutreachCampaign, campaign_id)
            if not c or c.status != "running":
                return
        except Exception:
            return
        time.sleep(min(chunk_sec, max(0.0, deadline - time.perf_counter())))


def _open_outreach_account_session(
    p,
    *,
    account: FBAccount,
    storage_state: dict[str, Any] | None,
    db: Session,
    job_id: int,
) -> dict[str, Any]:
    """
    Держим живой Chromium-контекст для текущего аккаунта кампании и переиспользуем его
    между отправками, пока аккаунт не сменился или задача не завершилась.
    """
    slot_cm = playwright_run_slot(fb_account_id=account.id)
    slot_cm.__enter__()
    profile_cm = None
    ctx = None
    profile_entered = False
    slot_released = False
    prefer_shared = False
    lock_acquired = False
    try:
        elig = outreach_shared_profile_feature_enabled() and outreach_shared_disk_profile_eligible(
            db, account, storage_state
        )
        if elig:
            lock_acquired = try_acquire_outreach_shared_profile_lock(
                db, account_id=int(account.id), job_id=int(job_id)
            )
            prefer_shared = lock_acquired
            if not lock_acquired:
                logger.warning(
                    "outreach account=%s: mutex профиля занят — рассылка во временном профиле",
                    account.id,
                )
        profile_cm = fb_account_playwright_profile_ephemeral(
            p,
            account,
            storage_state=storage_state,
            db=db,
            prefer_shared_messenger_disk_profile=prefer_shared,
        )
        try:
            ctx = profile_cm.__enter__()
            profile_entered = True
        except Exception:
            if lock_acquired:
                release_outreach_shared_profile_lock(
                    db, account_id=int(account.id), job_id=int(job_id)
                )
                lock_acquired = False
            slot_cm.__exit__(None, None, None)
            slot_released = True
            raise
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        sess = {
            "account_id": int(account.id),
            "job_id": int(job_id),
            "slot_cm": slot_cm,
            "profile_cm": profile_cm,
            "ctx": ctx,
            "page": page,
            "shared_disk_lock": lock_acquired,
            "lock_job_id": int(job_id) if lock_acquired else None,
            "db": db,
        }
        _register_outreach_ctx_for_kill(int(job_id), ctx)
        return sess
    except Exception:
        logger.exception("open outreach browser session account=%s", account.id)
        if profile_entered and profile_cm is not None:
            try:
                profile_cm.__exit__(None, None, None)
            except Exception:
                logger.exception(
                    "cleanup partially opened outreach browser profile account=%s",
                    account.id,
                )
        if lock_acquired:
            try:
                release_outreach_shared_profile_lock(
                    db, account_id=int(account.id), job_id=int(job_id)
                )
            except Exception:
                logger.exception(
                    "release partial outreach shared profile lock account=%s",
                    account.id,
                )
        if not slot_released:
            try:
                slot_cm.__exit__(None, None, None)
            except Exception:
                logger.exception(
                    "release partial outreach browser slot account=%s", account.id
                )
        raise


def _close_outreach_account_session(session: dict[str, Any] | None) -> None:
    if not session:
        return
    jid = session.get("job_id")
    profile_cm = session.get("profile_cm")
    slot_cm = session.get("slot_cm")
    db = session.get("db")
    try:
        if profile_cm is not None:
            profile_cm.__exit__(None, None, None)
    except Exception:
        logger.exception(
            "close outreach browser profile account=%s", session.get("account_id")
        )
    finally:
        try:
            if (
                db is not None
                and session.get("shared_disk_lock")
                and session.get("lock_job_id")
                and session.get("account_id")
            ):
                release_outreach_shared_profile_lock(
                    db,
                    account_id=int(session["account_id"]),
                    job_id=int(session["lock_job_id"]),
                )
        except Exception:
            logger.exception(
                "release outreach shared profile lock account=%s",
                session.get("account_id"),
            )
        try:
            if slot_cm is not None:
                slot_cm.__exit__(None, None, None)
        except Exception:
            logger.exception(
                "release outreach browser slot account=%s", session.get("account_id")
            )
        _unregister_outreach_ctx_for_kill(jid if jid is not None else None)


def release_outreach_worker_lock_for_job(job_id: int | None) -> None:
    """Снять in-memory слот воркера для этой задачи (удаление кампании, сброс зависшего состояния)."""
    if job_id is None:
        return
    outreach_worker_slots.end(int(job_id))


def _release_stale_outreach_slots() -> None:
    """Если задача в БД уже не running/queued — убрать «залипший» job_id из слотов."""
    for jid in list(outreach_worker_slots.running_ids()):
        db = SessionLocal()
        try:
            job = db.get(Job, jid)
            stale = (
                not job
                or job.job_type != JOB_TYPE
                or job.status not in ("running", "queued")
            )
            if stale:
                release_outreach_worker_lock_for_job(jid)
        finally:
            db.close()


def outreach_org_has_running_campaign(db: Session, org_id: int) -> bool:
    """Есть ли у организации кампания в статусе «выполняется» (истина для плашки в UI)."""
    return (
        db.query(OutreachCampaign.id)
        .filter(
            OutreachCampaign.organization_id == org_id,
            OutreachCampaign.status == "running",
        )
        .first()
        is not None
    )


def outreach_worker_busy() -> bool:
    """Есть ли хотя бы один активный поток рассылки с задачей в running/queued в БД."""
    _release_stale_outreach_slots()
    return outreach_worker_slots.has_any()


def outreach_worker_at_capacity() -> bool:
    """Достигнут лимит параллельных кампаний рассылки (см. PLAYWRIGHT_MAX_CONCURRENT)."""
    _release_stale_outreach_slots()
    return outreach_worker_slots.at_capacity()


def _global_throttle_preset(db) -> str:
    row = db.get(Setting, "throttle_preset")
    v = row.value if row else None
    if is_valid_throttle_preset(v):
        return v
    return "slow"


def _resume_job(db, job: Job) -> None:
    job.status = "running"
    job.ended_at = None
    db.commit()


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


def _record_contacted(db, *, person_id: int, fb_account_id: int, canonical_url: str) -> None:
    upsert_contacted(
        db,
        person_id=person_id,
        fb_account_id=fb_account_id,
        canonical_url=canonical_url,
        touched_at=datetime.now(timezone.utc),
    )
    db.commit()


def _mark_outreach_account_limited_today(db: Session, account_id: int) -> None:
    """
    Meta message-request limit is account-scoped. Saturating today's outreach usage
    lets rotation skip this account for the rest of the day without pausing the whole run.
    """
    usage_date = datetime.now(timezone.utc).date().isoformat()
    row = (
        db.query(FBAccountDayUsage)
        .filter(
            FBAccountDayUsage.fb_account_id == int(account_id),
            FBAccountDayUsage.usage_date == usage_date,
        )
        .first()
    )
    if not row:
        row = FBAccountDayUsage(
            fb_account_id=int(account_id),
            usage_date=usage_date,
            outreach_actions=0,
            warmup_actions=0,
            sequence_actions=0,
        )
        db.add(row)
        db.flush()
    row.outreach_actions = max(int(row.outreach_actions or 0), 500)


def _outreach_detail_needs_browser_reset(detail: str | None) -> bool:
    low = (detail or "").lower()
    if not low:
        return False
    needles = (
        "err:",
        "timeout",
        "target closed",
        "browser has been closed",
        "context or browser has been closed",
        "page has been closed",
        "net::",
        "econnrefused",
    )
    return any(n in low for n in needles)


def _campaign_can_continue_after_account_limit(
    db: Session,
    camp: OutreachCampaign,
    blocked_account_id: int,
) -> bool:
    if not rotation_enabled_outreach(camp):
        return False
    account, err, _new_fb_id = resolve_outreach_executor(db, camp, int(blocked_account_id))
    return bool(account is not None and err is None and int(account.id) != int(blocked_account_id))


def _message_mode(cfg: dict[str, Any] | None) -> str:
    mode = str((cfg or {}).get("message_mode") or "template").strip().lower()
    return mode if mode in ("template", "ai_dm", "first_touch") else "template"


def _campaign_kind(cfg: dict[str, Any] | None) -> str:
    kind = str((cfg or {}).get("campaign_kind") or "dm").strip().lower()
    if kind in ("comment", "commenting", "comments"):
        return "comment"
    return "dm"


def _comment_mode(cfg: dict[str, Any] | None) -> str:
    mode = str((cfg or {}).get("comment_mode") or "template").strip().lower()
    return mode if mode in ("template", "ai_comment") else "template"


def _provider_override(cfg: dict[str, Any] | None, key: str) -> str:
    prov = str((cfg or {}).get(key) or "").strip().lower()
    if prov == "ollama":
        return ""
    return prov if prov in ("openai", "gemini") else ""


def _comment_target_mode(cfg: dict[str, Any] | None) -> str:
    mode = str((cfg or {}).get("comment_post_mode") or "first").strip().lower()
    return mode if mode in ("first", "random") else "first"


def _comment_pool_size(cfg: dict[str, Any] | None) -> int:
    try:
        pool = int((cfg or {}).get("comment_pool_size") or 5)
    except (TypeError, ValueError):
        return 5
    return max(1, min(pool, 10))


def _first_touch_message_for_person(person: Person) -> str:
    variants = pick_first_touch_templates(1)
    if not variants:
        return ""
    return apply_template_placeholders(variants[0], **placeholder_kwargs_from_person(person)).strip()


def _reference_text_for_outreach_ai(
    db,
    camp: OutreachCampaign,
    person: Person,
) -> str:
    cfg = camp.config if isinstance(camp.config, dict) else {}
    example = str(cfg.get("ai_dm_example") or "").strip()
    if example:
        single = resolve_template_variant(example, db=None, template_row=None)
        return apply_template_placeholders(single, **placeholder_kwargs_from_person(person)).strip()
    if camp.template_id:
        return render_outreach_message_for_person(db, camp.template_id, person).strip()
    return ""


def process_outreach_job(job_id: int) -> None:
    err = outreach_worker_slots.try_begin(job_id)
    if err == "duplicate":
        logger.warning("Duplicate outreach thread for job %s", job_id)
        return
    if err == "capacity":
        logger.warning(
            "Outreach job %s skipped: достигнут лимит параллельных рассылок (%s)",
            job_id,
            outreach_worker_slots.cap,
        )
        return

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                logger.error("Outreach job %s not found", job_id)
                return

            campaign = (
                db.query(OutreachCampaign).filter(OutreachCampaign.job_id == job_id).first()
            )
            if not campaign:
                finish_job(db, job, status="failed", error="Кампания не найдена")
                return

            if job.status == "queued":
                start_job(db, job)
            elif job.status == "cancelled":
                _resume_job(db, job)
            elif job.status != "running":
                logger.info("Outreach job %s status=%s skip", job_id, job.status)
                return

            preset = _global_throttle_preset(db)
            cfg = campaign.config if isinstance(campaign.config, dict) else {}
            if is_valid_throttle_preset(cfg.get("preset")):
                preset = str(cfg["preset"])

            tb_raw = cfg.get("time_budget_seconds")
            tb: float | None = None
            if tb_raw is not None:
                try:
                    tb = float(tb_raw)
                    if tb <= 0:
                        tb = None
                except (TypeError, ValueError):
                    tb = None

            queued_n = (
                db.query(OutreachQueue)
                .filter(
                    OutreachQueue.job_id == job_id,
                    OutreachQueue.status == "queued",
                )
                .count()
            )
            total_actions = max(1, queued_n)

            job_cancel_reason: str | None = None
            with sync_playwright() as p:
                active_pw_session: dict[str, Any] | None = None
                try:
                    while True:
                        db.expire_all()
                        camp = db.get(OutreachCampaign, campaign.id)
                        if not camp or camp.status != "running":
                            break

                        _cfg_send = camp.config if isinstance(camp.config, dict) else {}
                        if not is_now_within_send_window(db, _cfg_send):
                            delay_sec = seconds_until_send_window_opens(db, _cfg_send)
                            if delay_sec > 60:
                                _close_outreach_account_session(active_pw_session)
                                active_pw_session = None
                            time.sleep(delay_sec)
                            continue

                        row = (
                            db.query(OutreachQueue)
                            .filter(
                                OutreachQueue.job_id == job_id,
                                OutreachQueue.status == "queued",
                            )
                            .order_by(OutreachQueue.id)
                            .first()
                        )
                        if not row:
                            break

                        row.status = "running"
                        db.commit()

                        person = db.get(Person, row.person_id)
                        t0 = time.perf_counter()

                        if not person:
                            row.status = "failed"
                            row.error = "Нет человека в базе"
                            row.processed_at = datetime.now(timezone.utc)
                            db.commit()
                            log_job_event(
                                db,
                                job_id=job_id,
                                event_type="outreach_row_failed",
                                severity="error",
                                person_id=row.person_id,
                                fb_account_id=row.fb_account_id,
                                outcome="fail",
                                payload={"reason": "missing_person"},
                            )
                            _sleep_throttle_outreach_interruptible(
                                db,
                                campaign_id=campaign.id,
                                action_type="next_person",
                                preset=preset,
                                time_budget_seconds=tb,
                                total_actions=total_actions,
                            )
                            continue

                        account, rot_err, new_fb_id = resolve_outreach_executor(
                            db, camp, row.fb_account_id
                        )
                        if new_fb_id is not None:
                            row.fb_account_id = new_fb_id

                        if rot_err:
                            defer = rot_err in (
                                "daily_cap_all_accounts",
                                "need_active_slot",
                                "no_slot_in_pool",
                            )
                            if defer:
                                row.status = "queued"
                                row.error = None
                                row.processed_at = None
                                db.commit()
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="outreach_row_deferred",
                                    severity="warning",
                                    person_id=row.person_id,
                                    fb_account_id=row.fb_account_id,
                                    outcome="deferred",
                                    payload={"reason": rot_err},
                                )
                                wait_count = record_outreach_wait(db, job_id, rot_err)
                                if should_abort_due_to_wait(wait_count):
                                    job_cancel_reason = reason_humanize(rot_err)[:1000]
                                    finish_job(
                                        db,
                                        job,
                                        status="failed",
                                        error=job_cancel_reason,
                                    )
                                    db.commit()
                                    break
                                if rot_err == "daily_cap_all_accounts":
                                    _sleep_seconds_outreach_interruptible(
                                        db, campaign_id=campaign.id, total_sec=60.0
                                    )
                                else:
                                    _sleep_throttle_outreach_interruptible(
                                        db,
                                        campaign_id=campaign.id,
                                        action_type="next_person",
                                        preset=preset,
                                        time_budget_seconds=tb,
                                        total_actions=total_actions,
                                    )
                                continue
                            row.status = "failed"
                            row.error = (
                                "Нет аккаунта"
                                if rot_err == "missing_account"
                                else (
                                    "Не выбраны аккаунты кампании"
                                    if rot_err == "empty_pool"
                                    else humanize_outreach_row_error(rot_err)
                                )
                            )[:1000]
                            row.processed_at = datetime.now(timezone.utc)
                            db.commit()
                            log_job_event(
                                db,
                                job_id=job_id,
                                event_type="outreach_row_failed",
                                severity="error",
                                person_id=row.person_id,
                                fb_account_id=row.fb_account_id,
                                outcome="fail",
                                payload={"reason": rot_err},
                            )
                            _sleep_throttle_outreach_interruptible(
                                db,
                                campaign_id=campaign.id,
                                action_type="next_person",
                                preset=preset,
                                time_budget_seconds=tb,
                                total_actions=total_actions,
                            )
                            continue

                        if not account:
                            row.status = "failed"
                            row.error = "Нет аккаунта"
                            row.processed_at = datetime.now(timezone.utc)
                            db.commit()
                            log_job_event(
                                db,
                                job_id=job_id,
                                event_type="outreach_row_failed",
                                severity="error",
                                person_id=row.person_id,
                                fb_account_id=row.fb_account_id,
                                outcome="fail",
                                payload={"reason": "missing_account"},
                            )
                            _sleep_throttle_outreach_interruptible(
                                db,
                                campaign_id=campaign.id,
                                action_type="next_person",
                                preset=preset,
                                time_budget_seconds=tb,
                                total_actions=total_actions,
                            )
                            continue

                        db.commit()

                        storage = parse_storage_state(account.session_state_json)
                        current_account_id = int(account.id)
                        action_type = "next_person"

                        try:
                            if (
                                active_pw_session is not None
                                and int(active_pw_session.get("account_id") or 0)
                                != current_account_id
                            ):
                                _close_outreach_account_session(active_pw_session)
                                active_pw_session = None
                            if active_pw_session is None:
                                active_pw_session = _open_outreach_account_session(
                                    p,
                                    account=account,
                                    storage_state=storage,
                                    db=db,
                                    job_id=job_id,
                                )
                            page = active_pw_session.get("page")
                            if page is None or page.is_closed():
                                _close_outreach_account_session(active_pw_session)
                                active_pw_session = _open_outreach_account_session(
                                    p,
                                    account=account,
                                    storage_state=storage,
                                    db=db,
                                    job_id=job_id,
                                )
                                page = active_pw_session.get("page")

                            _cfg_live = camp.config if isinstance(camp.config, dict) else {}
                            _lm = str(_cfg_live.get("like_mode") or "first").strip().lower()
                            if _lm not in ("first", "random"):
                                _lm = "first"
                            try:
                                _lp = int(_cfg_live.get("like_pool_size") or 5)
                            except (TypeError, ValueError):
                                _lp = 5
                            _lp = max(2, min(_lp, 10))
                            try:
                                _lc = int(_cfg_live.get("like_count") or 1)
                            except (TypeError, ValueError):
                                _lc = 1
                            _lc = max(1, min(_lc, 10))

                            campaign_kind = _campaign_kind(_cfg_live)
                            action_type = "send_dm"
                            if campaign_kind == "comment":
                                comment_mode = _comment_mode(_cfg_live)
                                comment_target_mode = _comment_target_mode(_cfg_live)
                                comment_pool = _comment_pool_size(_cfg_live)
                                comment_pick_index = 0
                                if comment_target_mode == "random":
                                    comment_pick_index = random.randrange(comment_pool)
                                if comment_mode == "ai_comment":
                                    profile_url = profile_url_from_person(person.canonical_url)
                                    if profile_url:
                                        page.goto(
                                            profile_url,
                                            wait_until="domcontentloaded",
                                            timeout=90_000,
                                        )
                                        page.wait_for_timeout(1200)
                                    post_excerpt = extract_timeline_post_text(
                                        page,
                                        mode=comment_target_mode,
                                        pool_size=comment_pool,
                                        pick_index=comment_pick_index,
                                    )
                                    pb = (post_excerpt or "").strip()
                                    if len(pb) < 12:
                                        pb = (
                                            "[Пост в ленте профиля: мало извлекаемого текста или в основном медиа — "
                                            "нужен короткий нейтральный доброжелательный комментарий без выдуманных фактов.]"
                                        )
                                    msg = generate_profile_post_comment_sync(
                                        db,
                                        post_text=pb,
                                        display_name=(person.display_name or person.first_name or "").strip(),
                                        first_name=(person.first_name or "").strip(),
                                        hint=str(_cfg_live.get("ai_comment_hint") or "").strip(),
                                        provider_override=_provider_override(
                                            _cfg_live, "ai_comment_provider"
                                        ),
                                    ).strip()
                                else:
                                    tid = row.template_id or camp.template_id
                                    if tid:
                                        msg = render_outreach_message_for_person(
                                            db, tid, person
                                        ).strip()
                                    else:
                                        msg = (row.message_text or "").strip()
                                if not msg:
                                    row.status = "failed"
                                    row.error = "Пустой текст комментария"
                                    row.processed_at = datetime.now(timezone.utc)
                                    db.commit()
                                    _sleep_throttle_outreach_interruptible(
                                        db,
                                        campaign_id=campaign.id,
                                        action_type="next_person",
                                        preset=preset,
                                        time_budget_seconds=tb,
                                        total_actions=total_actions,
                                    )
                                    continue
                                row.message_text = msg
                                ok, detail = run_commenting_on_profile(
                                    page,
                                    person.canonical_url,
                                    comment_text=msg,
                                    like_first=bool(row.like_first),
                                    like_mode=_lm,
                                    like_pool_size=_lp,
                                    like_count=_lc,
                                    comment_mode=comment_target_mode,
                                    comment_pool_size=comment_pool,
                                    comment_pick_index=comment_pick_index,
                                )
                                action_type = "comment_post"
                            else:
                                msg_mode = _message_mode(_cfg_live)
                                if msg_mode == "first_touch":
                                    msg = (row.message_text or "").strip() or _first_touch_message_for_person(person)
                                elif msg_mode == "ai_dm":
                                    profile_ctx = ""
                                    profile_url = profile_url_from_person(person.canonical_url)
                                    if profile_url:
                                        page.goto(
                                            profile_url,
                                            wait_until="domcontentloaded",
                                            timeout=90_000,
                                        )
                                        page.wait_for_timeout(1200)
                                    if bool(_cfg_live.get("ai_dm_use_profile_post")):
                                        profile_ctx = extract_timeline_post_text(page)
                                    dm_loc = infer_dm_output_locale_for_ai_dm(
                                        page,
                                        profile_content_sample=profile_ctx,
                                        recipient_label=(
                                            f"{(person.display_name or '').strip()}\n"
                                            f"{(person.first_name or '').strip()}"
                                        ).strip(),
                                    )
                                    msg = generate_dm_message_sync(
                                        db,
                                        display_name=(person.display_name or person.first_name or "").strip(),
                                        first_name=(person.first_name or "").strip(),
                                        hint=str(_cfg_live.get("ai_dm_hint") or "").strip(),
                                        reference_message=_reference_text_for_outreach_ai(
                                            db, camp, person
                                        ),
                                        profile_context=profile_ctx if len(profile_ctx.strip()) >= 12 else "",
                                        provider_override=_provider_override(
                                            _cfg_live, "ai_dm_provider"
                                        ),
                                        dm_output_locale=dm_loc,
                                    ).strip()
                                else:
                                    tid = row.template_id or camp.template_id
                                    if tid:
                                        msg = render_outreach_message_for_person(
                                            db, tid, person
                                        ).strip()
                                    else:
                                        msg = (row.message_text or "").strip()
                                if not msg:
                                    row.status = "failed"
                                    row.error = "Пустой текст сообщения"
                                    row.processed_at = datetime.now(timezone.utc)
                                    db.commit()
                                    _sleep_throttle_outreach_interruptible(
                                        db,
                                        campaign_id=campaign.id,
                                        action_type="next_person",
                                        preset=preset,
                                        time_budget_seconds=tb,
                                        total_actions=total_actions,
                                    )
                                    continue
                                row.message_text = msg

                                ok, detail = run_outreach_on_profile(
                                    page,
                                    person.canonical_url,
                                    message_text=msg,
                                    like_first=bool(row.like_first),
                                    add_friend_first=bool(row.add_friend_first),
                                    like_mode=_lm,
                                    like_pool_size=_lp,
                                    like_count=_lc,
                                    person_raw_meta=person.raw_meta
                                    if isinstance(person.raw_meta, dict)
                                    else None,
                                )
                            ms = int((time.perf_counter() - t0) * 1000)

                            if ok:
                                if rotation_enabled_outreach(camp):
                                    increment_usage(db, account.id, "outreach")
                                row.status = "done"
                                row.error = None
                                clear_outreach_wait(db, job_id)
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="outreach_row_done",
                                    person_id=person.id,
                                    fb_account_id=account.id,
                                    outcome="ok",
                                    duration_ms=ms,
                                    payload={"detail": detail[:500]},
                                )
                                log_crm_activity(
                                    db,
                                    person_id=person.id,
                                    fb_account_id=account.id,
                                    activity_type="outreach_comment" if campaign_kind == "comment" else "outreach_dm",
                                    detail=detail[:500],
                                )
                                outreach_append_done_person(db, camp, person.id)
                                if campaign_kind == "dm":
                                    _record_contacted(
                                        db,
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        canonical_url=person.canonical_url,
                                    )
                                    person.crm_stage = "new"
                                    person.crm_stage_changed_at = datetime.now(timezone.utc)
                                    try:
                                        record_outbound_dm_in_cabinet(
                                            db,
                                            fb_account_id=account.id,
                                            person=person,
                                            message_text=msg,
                                        )
                                    except Exception:
                                        logger.exception(
                                            "record_outbound_dm_in_cabinet (outreach)"
                                        )
                                elif campaign_kind == "comment":
                                    _record_contacted(
                                        db,
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        canonical_url=person.canonical_url,
                                    )
                                    person.crm_stage = "new"
                                    person.crm_stage_changed_at = datetime.now(timezone.utc)
                            else:
                                row.status = "failed"
                                row_error_ru = humanize_outreach_row_error(detail)
                                messenger_limit_hit = "facebook_message_request_limit" in (
                                    detail or ""
                                )
                                reset_browser_after_row = _outreach_detail_needs_browser_reset(detail)
                                if messenger_limit_hit:
                                    _mark_outreach_account_limited_today(db, account.id)
                                    can_continue = _campaign_can_continue_after_account_limit(
                                        db, camp, account.id
                                    )
                                    if can_continue:
                                        pause_all_automation_for_fb_account(
                                            db,
                                            account.id,
                                            exclude_outreach_campaign_ids={int(camp.id)},
                                        )
                                        row_error_ru = (
                                            "Facebook ограничил отправку запросов в переписку для этого аккаунта "
                                            "(лимит Meta, обычно на 24 ч). Аккаунт исключён из рассылки на сегодня; "
                                            "кампания продолжит работу с другим доступным аккаунтом."
                                        )
                                    else:
                                        pause_all_automation_for_fb_account(db, account.id)
                                        job_cancel_reason = (
                                            "Лимит запросов Messenger Meta (обычно 24 ч): "
                                            "рассылка и связанные кампании по аккаунту приостановлены."
                                        )
                                    logger.warning(
                                        "Outreach: лимит Messenger fb_account_id=%s continue_with_rotation=%s",
                                        account.id,
                                        can_continue,
                                    )
                                    reset_browser_after_row = True
                                row.error = row_error_ru[:1000]
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="outreach_row_failed",
                                    severity="error"
                                    if messenger_limit_hit
                                    else "warning",
                                    person_id=person.id,
                                    fb_account_id=account.id,
                                    outcome="fail",
                                    duration_ms=ms,
                                    payload={
                                        "detail": detail[:500],
                                        "detail_ru": row_error_ru[:500],
                                        "messenger_message_request_limit": messenger_limit_hit,
                                        "browser_reset": reset_browser_after_row,
                                    },
                                )
                                if campaign_kind == "dm" and bool(
                                    (_cfg_live or {}).get(
                                        "record_contacted_after_any_dm_attempt", False
                                    )
                                ):
                                    log_crm_activity(
                                        db,
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        activity_type="outreach_dm_failed",
                                        detail=humanize_outreach_row_error(detail or "")[:500],
                                    )
                                    _record_contacted(
                                        db,
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        canonical_url=person.canonical_url,
                                    )
                                if (
                                    reset_browser_after_row
                                    and active_pw_session is not None
                                    and int(active_pw_session.get("account_id") or 0)
                                    == current_account_id
                                ):
                                    _close_outreach_account_session(active_pw_session)
                                    active_pw_session = None
                        except Exception as e:
                            logger.exception("outreach row %s", row.id)
                            ms = int((time.perf_counter() - t0) * 1000)
                            db.expire_all()
                            camp_now = db.get(OutreachCampaign, campaign.id)
                            user_paused = bool(camp_now and camp_now.status == "paused")
                            is_proxy_blocked = isinstance(e, ProxyTunnelBlockedError)
                            if user_paused:
                                row.status = "queued"
                                row.error = None
                                row.processed_at = None
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="outreach_row_interrupted",
                                    severity="info",
                                    person_id=row.person_id,
                                    fb_account_id=row.fb_account_id,
                                    outcome="paused",
                                    duration_ms=ms,
                                    payload={
                                        "reason": "paused_while_running",
                                        "error": str(e)[:300],
                                    },
                                )
                            elif is_proxy_blocked:
                                row.status = "queued"
                                row.error = None
                                row.processed_at = None
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="outreach_row_deferred",
                                    severity="warning",
                                    person_id=row.person_id,
                                    fb_account_id=row.fb_account_id,
                                    outcome="deferred",
                                    payload={
                                        "reason": "proxy_tunnel_blocked",
                                        "error": str(e)[:300],
                                    },
                                )
                                wait_count = record_outreach_wait(
                                    db,
                                    job_id,
                                    "proxy_tunnel_blocked",
                                    reason_details=str(e)[:300],
                                )
                                if should_abort_due_to_wait(wait_count):
                                    job_cancel_reason = reason_humanize(
                                        "proxy_tunnel_blocked"
                                    )[:1000]
                                    finish_job(
                                        db,
                                        job,
                                        status="failed",
                                        error=job_cancel_reason,
                                    )
                                    db.commit()
                                    if (
                                        active_pw_session is not None
                                        and int(active_pw_session.get("account_id") or 0)
                                        == current_account_id
                                    ):
                                        _close_outreach_account_session(active_pw_session)
                                        active_pw_session = None
                                    break
                            else:
                                row.status = "failed"
                                row.error = str(e)[:1000]
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="outreach_row_error",
                                    severity="error",
                                    person_id=row.person_id,
                                    fb_account_id=row.fb_account_id,
                                    outcome="fail",
                                    duration_ms=ms,
                                    payload={"error": str(e)[:300]},
                                )
                            if (
                                active_pw_session is not None
                                and int(active_pw_session.get("account_id") or 0)
                                == current_account_id
                            ):
                                _close_outreach_account_session(active_pw_session)
                                active_pw_session = None

                        row.processed_at = datetime.now(timezone.utc)
                        db.commit()

                        _sleep_throttle_outreach_interruptible(
                            db,
                            campaign_id=campaign.id,
                            action_type=action_type,
                            preset=preset,
                            time_budget_seconds=tb,
                            total_actions=total_actions,
                        )
                finally:
                    _close_outreach_account_session(active_pw_session)

            db.expire_all()
            camp = db.get(OutreachCampaign, campaign.id)
            job = db.get(Job, job_id)
            remaining = (
                db.query(OutreachQueue)
                .filter(
                    OutreachQueue.job_id == job_id,
                    OutreachQueue.status == "queued",
                )
                .count()
            )

            if camp and camp.status == "running" and remaining == 0:
                camp.status = "completed"
                finish_job(db, job, status="success")
            elif camp and camp.status != "running":
                finish_job(
                    db,
                    job,
                    status="cancelled",
                    error=job_cancel_reason or "Остановлено пользователем",
                )
            elif remaining > 0:
                finish_job(db, job, status="failed", error="Очередь прервана")

            db.commit()
        finally:
            db.close()
    finally:
        outreach_worker_slots.end(job_id)
        try:
            from backend.services.automation_auto_resume import try_auto_resume_next_outreach

            try_auto_resume_next_outreach()
        except Exception:
            logger.exception("try_auto_resume_next_outreach after job %s", job_id)


def spawn_outreach_worker(job_id: int) -> None:
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        logger.warning("spawn_outreach_worker пропущен (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
        return
    t = threading.Thread(
        target=process_outreach_job,
        args=(job_id,),
        name=f"outreach-job-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned outreach worker for job %s", job_id)
