"""Фоновый воркер: выполнение очереди прогрева (лайки/комментарии) через Playwright."""

from __future__ import annotations

import json
import logging
import random
import shutil
import threading
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import FBAccount, Job, Person, Setting, WarmupCampaign, WarmupQueue
from backend.services.cabinet_settings import effective_playwright_headless
from backend.services.account_rotation import increment_usage, resolve_warmup_executor, rotation_enabled_warmup
from backend.services.fb_playwright import (
    _close_browser_or_context_with_timeout,
    _launch_kwargs_persistent,
    _playwright_profile_in_use_error,
    _storage_state_with_timeout,
    apply_storage_state_cookies_to_context,
    chromium_profile_dir_locked,
    launch_persistent_context_from_bundle,
    pregrant_facebook_automation_permissions,
    prime_persistent_context_local_chrome,
    profile_has_chromium_data,
    proxy_dict_for_playwright,
)
from backend.services.fb_stealth_profile import stealth_playwright_options_from_account
from backend.services.job_logging import (
    finish_job,
    log_crm_activity,
    log_job_event,
    start_job,
)
from backend.services.parallel_automation_slots import warmup_worker_slots
from backend.services.playwright_resource import playwright_run_slot
from backend.services.message_template_render import (
    apply_template_placeholders,
    placeholder_kwargs_from_person,
)
from backend.services.throttle import is_valid_throttle_preset, resolve_delay
from backend.services.warmup_actions import parse_storage_state, profile_url_from_person, run_warmup_on_profile

logger = logging.getLogger(__name__)

JOB_TYPE = "warmup"


def release_warmup_worker_lock_for_job(job_id: int | None) -> None:
    if job_id is None:
        return
    warmup_worker_slots.end(int(job_id))


def _release_stale_warmup_slots() -> None:
    for jid in list(warmup_worker_slots.running_ids()):
        db = SessionLocal()
        try:
            job = db.get(Job, jid)
            stale = (
                not job
                or job.job_type != JOB_TYPE
                or job.status not in ("running", "queued")
            )
            if stale:
                release_warmup_worker_lock_for_job(jid)
        finally:
            db.close()


def warmup_worker_busy() -> bool:
    _release_stale_warmup_slots()
    return warmup_worker_slots.has_any()


def warmup_worker_at_capacity() -> bool:
    _release_stale_warmup_slots()
    return warmup_worker_slots.at_capacity()


def _persist_account_storage_state_snapshot(
    account_id: int | None,
    snapshot: dict[str, Any] | None,
) -> None:
    if account_id is None or not isinstance(snapshot, dict):
        return
    cookies = snapshot.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return
    profile_dir_for_sidecar: str | None = None
    db = SessionLocal()
    try:
        acc = db.get(FBAccount, int(account_id))
        if not acc:
            return
        acc.session_state_json = json.dumps(snapshot, ensure_ascii=False)
        acc.session_saved_at = datetime.now(timezone.utc)
        db.commit()
        profile_dir_for_sidecar = acc.profile_dir
    except Exception:
        logger.exception("persist automation storage_state account=%s", account_id)
        db.rollback()
    finally:
        db.close()
    if profile_dir_for_sidecar:
        _write_storage_state_sidecar(profile_dir_for_sidecar, snapshot, account_id)


def _write_storage_state_sidecar(
    profile_dir: str | Path,
    snapshot: dict[str, Any],
    account_id: int | None,
) -> None:
    """
    Пишем JSON-снимок storage_state рядом с боевой папкой профиля аккаунта.
    Это бэкап на случай порчи БД и задел для будущих потребителей, которые
    будут читать cookies с диска напрямую (Electron disk-mode webview).
    Файл: <profile_dir>/.fbm-session-state.json
    """
    try:
        p = Path(profile_dir)
        p.mkdir(parents=True, exist_ok=True)
        sidecar = p / ".fbm-session-state.json"
        payload = {
            "cookies": snapshot.get("cookies") or [],
            "origins": snapshot.get("origins") or [],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = sidecar.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(sidecar)
    except Exception:
        logger.debug(
            "persist storage_state sidecar account=%s dir=%s",
            account_id,
            profile_dir,
            exc_info=True,
        )


def _global_throttle_preset(db) -> str:
    row = db.get(Setting, "throttle_preset")
    v = row.value if row else None
    if is_valid_throttle_preset(v):
        return v
    return "slow"


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


def _resume_job(db, job: Job) -> None:
    job.status = "running"
    job.ended_at = None
    db.commit()


def _storage_state_has_cookies(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies")
    return isinstance(cookies, list) and len(cookies) > 0


def _playwright_profile_dir_for_account(
    account: FBAccount,
    *,
    storage_state: dict[str, Any] | None,
) -> tuple[Path, bool]:
    """
    Для автоматизации по возможности используем отдельный временный persistent-профиль:
    Встроенный Мессенджер живёт в Electron, а рабочий Chromium не трогает «боевую» папку профиля на диске.

    Если снимка сессии в БД ещё нет, откатываемся к старому поведению — живой profile_dir аккаунта.
    """
    if _storage_state_has_cookies(storage_state):
        tmp_path = Path(tempfile.mkdtemp(prefix=f"fbm-pw-{account.id}-"))
        logger.info(
            "Playwright account=%s using isolated temp profile %s",
            account.id,
            tmp_path,
        )
        return tmp_path, True

    path = Path(account.profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Playwright account=%s fallback to live profile_dir=%s (no DB storage_state snapshot)",
        account.id,
        path,
    )
    return path, False


def _launch_context_for_account(
    p,
    account: FBAccount,
    *,
    storage_state: dict[str, Any] | None,
    headless: bool | None = None,
    profile_dir: str | Path | None = None,
    db: Session | None = None,
):
    path = Path(profile_dir) if profile_dir is not None else Path(account.profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    profile_was_empty = not profile_has_chromium_data(path)
    proxy = proxy_dict_for_playwright(
        enabled=bool(account.proxy_enabled),
        url=account.proxy_url,
        username=account.proxy_username,
        password=account.proxy_password,
    )
    launch_headless = effective_playwright_headless(db) if headless is None else bool(headless)
    stealth_bundle = None
    if account.stealth_locale or account.stealth_user_agent:
        stealth_bundle = stealth_playwright_options_from_account(account)
    bundled = _launch_kwargs_persistent(
        headless=launch_headless,
        proxy=proxy,
        stealth_bundle=stealth_bundle,
    )
    init_s = bundled["init_script"]
    ctx = launch_persistent_context_from_bundle(p, path, bundled)
    pregrant_facebook_automation_permissions(ctx)
    if init_s:
        try:
            ctx.add_init_script(init_s)
        except Exception:
            logger.debug("warmup add_init_script", exc_info=True)
    apply_storage_state_cookies_to_context(ctx, storage_state)
    prime_persistent_context_local_chrome(
        ctx,
        path,
        storage_state,
        profile_was_empty_before_launch=profile_was_empty,
        timeout_ms=90_000,
    )
    return ctx


@contextmanager
def fb_account_playwright_profile_ephemeral(
    p,
    account: FBAccount,
    *,
    storage_state,
    headless: bool | None = None,
    db: Session | None = None,
    prefer_shared_messenger_disk_profile: bool = False,
):
    """
    Открыть изолированный persistent-контекст для автоматизации и гарантированно закрыть его.

    Основной сценарий: отдельный временный профиль, авторизованный через сохранённый storage_state
    из БД. Тогда Мессенджер в Electron и фоновый Chromium не спорят за одну папку профиля.

    Fallback: если снимка сессии нет, используем старый живой profile_dir аккаунта.

    prefer_shared_messenger_disk_profile: тот же каталог user-data, что у встроенного Messenger
    (Desktop + FB_MASTER_OUTREACH_SHARED_PROFILE + mutex в outreach_worker).
    """
    use_shared = False
    shared_profile_busy = False
    profile_dir: Path
    is_temp_profile: bool
    if prefer_shared_messenger_disk_profile and db is not None:
        from backend.services.fb_account_profile_paths import playwright_profile_dir_for_account
        from backend.services.outreach_shared_profile import outreach_shared_disk_profile_eligible

        if outreach_shared_disk_profile_eligible(db, account, storage_state):
            candidate = playwright_profile_dir_for_account(db, account.id, account.profile_dir)
            if chromium_profile_dir_locked(candidate):
                shared_profile_busy = True
                logger.warning(
                    "Playwright account=%s shared disk profile is busy, fallback to isolated temp profile: %s",
                    account.id,
                    candidate,
                )
            else:
                profile_dir = candidate
                is_temp_profile = False
                use_shared = True
                logger.info(
                    "Playwright account=%s shared disk profile with Messenger: %s",
                    account.id,
                    profile_dir,
                )
    if not use_shared:
        profile_dir, is_temp_profile = _playwright_profile_dir_for_account(
            account,
            storage_state=storage_state,
        )
        if shared_profile_busy:
            logger.info(
                "Playwright account=%s switched to isolated temp profile because shared disk profile is already open",
                account.id,
            )
    try:
        ctx = _launch_context_for_account(
            p,
            account,
            storage_state=storage_state,
            headless=headless,
            profile_dir=profile_dir,
            db=db,
        )
    except Exception as e:
        if use_shared and _storage_state_has_cookies(storage_state) and _playwright_profile_in_use_error(e):
            logger.warning(
                "Playwright account=%s shared profile launch failed (%s); retrying in isolated temp profile",
                account.id,
                str(e)[:240],
            )
            profile_dir, is_temp_profile = _playwright_profile_dir_for_account(
                account,
                storage_state=storage_state,
            )
            use_shared = False
            ctx = _launch_context_for_account(
                p,
                account,
                storage_state=storage_state,
                headless=headless,
                profile_dir=profile_dir,
                db=db,
            )
        else:
            raise
    try:
        yield ctx
    finally:
        snapshot = _storage_state_with_timeout(ctx)
        _close_browser_or_context_with_timeout(None, ctx)
        _persist_account_storage_state_snapshot(getattr(account, "id", None), snapshot)
        if is_temp_profile:
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                logger.exception(
                    "cleanup temp playwright profile account=%s dir=%s",
                    getattr(account, "id", None),
                    profile_dir,
                )


def process_warmup_job(job_id: int) -> None:
    err = warmup_worker_slots.try_begin(job_id)
    if err == "duplicate":
        logger.warning("Warmup job %s already active, skip duplicate thread", job_id)
        return
    if err == "capacity":
        logger.warning(
            "Warmup job %s skipped: достигнут лимит параллельных прогревов (%s)",
            job_id,
            warmup_worker_slots.cap,
        )
        return

    try:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id, Job.job_type == JOB_TYPE).first()
            if not job:
                logger.error("Warmup job %s not found", job_id)
                return

            campaign = (
                db.query(WarmupCampaign).filter(WarmupCampaign.job_id == job_id).first()
            )
            if not campaign:
                logger.error("Warmup campaign for job %s not found", job_id)
                finish_job(db, job, status="failed", error="Кампания не найдена")
                return

            if job.status == "queued":
                start_job(db, job)
            elif job.status == "cancelled":
                _resume_job(db, job)
            elif job.status != "running":
                logger.info("Warmup job %s status=%s skip start", job_id, job.status)
                return

            preset = _global_throttle_preset(db)
            cfg = campaign.config if isinstance(campaign.config, dict) else {}
            if is_valid_throttle_preset(cfg.get("preset")):
                preset = str(cfg["preset"])

            time_budget = cfg.get("time_budget_seconds")
            tb: float | None = None
            if time_budget is not None:
                try:
                    tb = float(time_budget)
                    if tb <= 0:
                        tb = None
                except (TypeError, ValueError):
                    tb = None

            queued_n = (
                db.query(WarmupQueue)
                .filter(
                    WarmupQueue.job_id == job_id,
                    WarmupQueue.status == "queued",
                )
                .count()
            )
            total_actions = max(1, queued_n)

            with sync_playwright() as p:
                while True:
                    db.expire_all()
                    camp = db.get(WarmupCampaign, campaign.id)
                    if not camp or camp.status != "running":
                        logger.info("Warmup: campaign %s stopped (status=%s)", campaign.id, getattr(camp, "status", None))
                        break

                    row = (
                        db.query(WarmupQueue)
                        .filter(
                            WarmupQueue.job_id == job_id,
                            WarmupQueue.status == "queued",
                        )
                        .order_by(WarmupQueue.id)
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
                            event_type="warmup_row_failed",
                            severity="error",
                            person_id=row.person_id,
                            fb_account_id=row.fb_account_id,
                            outcome="fail",
                            payload={"reason": "missing_person"},
                        )
                        _sleep_throttle(
                            "next_person",
                            preset=preset,
                            time_budget_seconds=tb,
                            total_actions=total_actions,
                        )
                        continue

                    account, rot_err, new_fb_id = resolve_warmup_executor(db, camp, row.fb_account_id)
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
                                event_type="warmup_row_deferred",
                                severity="warning",
                                person_id=row.person_id,
                                fb_account_id=row.fb_account_id,
                                outcome="deferred",
                                payload={"reason": rot_err},
                            )
                            if rot_err == "daily_cap_all_accounts":
                                time.sleep(60)
                            else:
                                _sleep_throttle(
                                    "next_person",
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
                                else rot_err
                            )
                        )[:1000]
                        row.processed_at = datetime.now(timezone.utc)
                        db.commit()
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="warmup_row_failed",
                            severity="error",
                            person_id=row.person_id,
                            fb_account_id=row.fb_account_id,
                            outcome="fail",
                            payload={"reason": rot_err},
                        )
                        _sleep_throttle(
                            "next_person",
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
                            event_type="warmup_row_failed",
                            severity="error",
                            person_id=row.person_id,
                            fb_account_id=row.fb_account_id,
                            outcome="fail",
                            payload={"reason": "missing_account"},
                        )
                        _sleep_throttle(
                            "next_person",
                            preset=preset,
                            time_budget_seconds=tb,
                            total_actions=total_actions,
                        )
                        continue

                    db.commit()

                    actions = row.actions if isinstance(row.actions, dict) else {}
                    do_like = bool(actions.get("do_like", True))
                    do_comment = bool(actions.get("do_comment", False))
                    comment_tpl = (actions.get("comment_template") or "").strip()
                    comment_text = apply_template_placeholders(
                        comment_tpl,
                        **placeholder_kwargs_from_person(person),
                    )

                    profile_url = profile_url_from_person(person.canonical_url)
                    storage = parse_storage_state(account.session_state_json)

                    try:
                        with playwright_run_slot(fb_account_id=account.id):
                            ok = False
                            detail = ""
                            with fb_account_playwright_profile_ephemeral(
                                p, account, storage_state=storage, db=db
                            ) as ctx:
                                page = ctx.pages[0] if ctx.pages else ctx.new_page()

                                ok, detail = run_warmup_on_profile(
                                    page,
                                    profile_url,
                                    do_like=do_like,
                                    do_comment=do_comment,
                                    comment_text=comment_text,
                                )

                            ms = int((time.perf_counter() - t0) * 1000)

                            if ok:
                                if rotation_enabled_warmup(camp):
                                    increment_usage(db, account.id, "warmup")
                                row.status = "done"
                                row.error = None
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="warmup_row_done",
                                    person_id=person.id,
                                    fb_account_id=account.id,
                                    outcome="ok",
                                    duration_ms=ms,
                                    payload={"detail": detail[:500]},
                                )
                                if do_like:
                                    log_crm_activity(
                                        db,
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        activity_type="warmup_like",
                                        detail=detail[:500],
                                    )
                                if do_comment and comment_text:
                                    log_crm_activity(
                                        db,
                                        person_id=person.id,
                                        fb_account_id=account.id,
                                        activity_type="warmup_comment",
                                        detail=detail[:500],
                                    )
                            else:
                                row.status = "failed"
                                row.error = detail[:1000]
                                log_job_event(
                                    db,
                                    job_id=job_id,
                                    event_type="warmup_row_failed",
                                    severity="warning",
                                    person_id=person.id,
                                    fb_account_id=account.id,
                                    outcome="fail",
                                    duration_ms=ms,
                                    payload={"detail": detail[:500]},
                                )
                    except Exception as e:
                        logger.exception("warmup row %s", row.id)
                        ms = int((time.perf_counter() - t0) * 1000)
                        row.status = "failed"
                        row.error = str(e)[:1000]
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="warmup_row_error",
                            severity="error",
                            person_id=row.person_id,
                            fb_account_id=row.fb_account_id,
                            outcome="fail",
                            duration_ms=ms,
                            payload={"error": str(e)[:300]},
                        )

                    row.processed_at = datetime.now(timezone.utc)
                    db.commit()

                    _sleep_throttle(
                        "next_person",
                        preset=preset,
                        time_budget_seconds=tb,
                        total_actions=total_actions,
                    )

            db.expire_all()
            camp = db.get(WarmupCampaign, campaign.id)
            job = db.get(Job, job_id)

            remaining = (
                db.query(WarmupQueue)
                .filter(
                    WarmupQueue.job_id == job_id,
                    WarmupQueue.status == "queued",
                )
                .count()
            )

            if camp and camp.status == "running" and remaining == 0:
                camp.status = "completed"
                finish_job(db, job, status="success")
            elif camp and camp.status != "running":
                finish_job(db, job, status="cancelled", error="Остановлено пользователем")
            elif remaining > 0:
                finish_job(db, job, status="failed", error="Очередь прервана")

            db.commit()
        finally:
            db.close()
    finally:
        warmup_worker_slots.end(job_id)
        try:
            from backend.services.automation_auto_resume import try_auto_resume_next_warmup

            try_auto_resume_next_warmup()
        except Exception:
            logger.exception("try_auto_resume_next_warmup after job %s", job_id)


def spawn_warmup_worker(job_id: int) -> None:
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        logger.warning("spawn_warmup_worker пропущен (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
        return
    t = threading.Thread(
        target=process_warmup_job,
        args=(job_id,),
        name=f"warmup-job-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned warmup worker for job %s", job_id)
