"""Фоновый воркер автопоиска аудитории: поиск групп / страниц / профилей по ключевым словам."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import sync_playwright

from backend.config import effective_fb_local_chrome_cookies
from backend.database import SessionLocal
from backend.models import DiscoveryResult, DiscoveryTask, FBAccount, Job
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.fb_playwright import (
    inject_local_chrome_facebook_cookies_and_reload,
    login_window_busy,
    page_requires_facebook_login,
)
from backend.services.fb_search_scrape import build_search_url, scroll_search_results
from backend.services.friends_list_scrape import dismiss_facebook_dom_overlays
from backend.services.job_logging import (
    finish_job,
    log_job_event,
    start_job,
    update_job_progress,
)
from backend.services.parallel_automation_slots import discovery_worker_slots
from backend.services.playwright_resource import playwright_run_slot
from backend.services.warmup_actions import parse_storage_state
from backend.services.warmup_worker import fb_account_playwright_profile_ephemeral

logger = logging.getLogger(__name__)

JOB_TYPE = "discovery"

_discovery_events_lock = threading.Lock()
_discovery_cancel_events: dict[int, threading.Event] = {}
_last_progress_ts: float = 0.0


def _release_stale_discovery_slots() -> None:
    for jid in list(discovery_worker_slots.running_ids()):
        db = SessionLocal()
        try:
            job = db.get(Job, jid)
            stale = not job or job.job_type != JOB_TYPE or job.status not in ("running", "queued")
            if stale:
                discovery_worker_slots.end(jid)
        finally:
            db.close()


def discovery_worker_busy() -> bool:
    _release_stale_discovery_slots()
    return discovery_worker_slots.has_any()


def discovery_worker_busy_for_org(db, org_id: int) -> bool:
    _release_stale_discovery_slots()
    alive = discovery_worker_slots.running_ids()
    if not alive:
        return False
    return (
        db.query(Job)
        .filter(Job.id.in_(alive), Job.job_type == JOB_TYPE, Job.organization_id == int(org_id))
        .first()
    ) is not None


def active_discovery_job_id() -> int | None:
    _release_stale_discovery_slots()
    ids = discovery_worker_slots.running_ids()
    return ids[0] if ids else None


def request_discovery_cancel(job_id: int | None = None) -> bool:
    with _discovery_events_lock:
        if job_id is not None:
            ev = _discovery_cancel_events.get(int(job_id))
            if not ev:
                return False
            ev.set()
            return True
        if not _discovery_cancel_events:
            return False
        for ev in _discovery_cancel_events.values():
            ev.set()
        return True


def finish_stale_discovery_jobs(db) -> int:
    alive = set(discovery_worker_slots.running_ids())
    rows = db.query(Job).filter(Job.job_type == JOB_TYPE, Job.status.in_(["running", "queued"])).all()
    n = 0
    for j in rows:
        if j.id in alive:
            continue
        finish_job(db, j, status="cancelled", error="Задача сброшена: процесс не найден (перезапуск?).", clear_progress=True)
        n += 1
    if n:
        db.commit()
    return n


def _throttled_progress(db, job_id: int, data: dict[str, Any], *, force: bool = False) -> None:
    global _last_progress_ts
    now = time.monotonic()
    if not force and now - _last_progress_ts < 1.25:
        return
    _last_progress_ts = now
    update_job_progress(db, job_id, data)


def _is_blank_or_ntp(url: str | None) -> bool:
    u = (url or "").strip().lower()
    return not u or u.startswith("about:") or u.startswith("chrome://") or u == "data:,"


def _pick_page(ctx: Any) -> Any:
    try:
        for pg in list(ctx.pages):
            try:
                if pg.is_closed():
                    continue
                if "facebook.com" in (pg.url or "").lower():
                    return pg
            except Exception:
                continue
    except Exception:
        pass
    try:
        return ctx.new_page()
    except Exception:
        for pg in list(ctx.pages):
            try:
                if not pg.is_closed():
                    return pg
            except Exception:
                continue
        return ctx.new_page()


def _warm_facebook(ctx: Any, page: Any) -> None:
    try:
        cur = (page.url or "").strip().lower()
        if "facebook.com" in cur and not _is_blank_or_ntp(page.url):
            return
    except Exception:
        pass
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(2000)
    dismiss_facebook_dom_overlays(page)
    if effective_fb_local_chrome_cookies():
        try:
            inject_local_chrome_facebook_cookies_and_reload(ctx, page, timeout_ms=120_000, log_warning_if_zero=False)
        except Exception:
            logger.exception("discovery: inject Chrome cookies after warm")


def process_discovery_job(job_id: int) -> None:
    err = discovery_worker_slots.try_begin(job_id)
    if err == "duplicate":
        logger.warning("Duplicate discovery thread for job %s", job_id)
        return
    if err == "capacity":
        logger.warning("Discovery job %s skipped: capacity %s", job_id, discovery_worker_slots.cap)
        return

    cancel_ev = threading.Event()
    with _discovery_events_lock:
        _discovery_cancel_events[job_id] = cancel_ev

    def is_cancelled() -> bool:
        return cancel_ev.is_set()

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                return

            snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
            task_id = snap.get("task_id")
            fb_account_id_raw = snap.get("fb_account_id")

            task = db.get(DiscoveryTask, int(task_id)) if task_id else None
            if not task:
                finish_job(db, job, status="failed", error="Задача автопоиска не найдена", clear_progress=True)
                db.commit()
                return

            if not str(fb_account_id_raw or "").strip().isdigit():
                finish_job(db, job, status="failed", error="Не выбран аккаунт Facebook", clear_progress=True)
                task.status = "failed"
                db.commit()
                return

            fb_account_id = int(fb_account_id_raw)
            account = db.get(FBAccount, fb_account_id)
            if not account:
                finish_job(db, job, status="failed", error="Аккаунт Facebook не найден", clear_progress=True)
                task.status = "failed"
                db.commit()
                return
            if account.organization_id != job.organization_id:
                finish_job(db, job, status="failed", error="Аккаунт не принадлежит организации", clear_progress=True)
                task.status = "failed"
                db.commit()
                return
            if not account_in_active_slot(account):
                finish_job(db, job, status="failed", error="Аккаунт не в активном слоте (1–3)", clear_progress=True)
                task.status = "failed"
                db.commit()
                return

            keywords_raw = (task.keywords or "").strip()
            keywords = [k.strip() for k in keywords_raw.replace("\n", ",").split(",") if k.strip()]
            if not keywords:
                finish_job(db, job, status="failed", error="Нет ключевых слов", clear_progress=True)
                task.status = "failed"
                db.commit()
                return

            search_types = task.search_types if isinstance(task.search_types, list) else ["groups"]
            max_per_type = task.max_results_per_type or 50

            task.status = "running"
            start_job(db, job)
            total_steps = len(keywords) * len(search_types)
            _throttled_progress(db, job_id, {
                "total_steps": total_steps,
                "done_steps": 0,
                "current_keyword": "",
                "current_type": "",
                "results_found": 0,
                "phase": "starting",
            }, force=True)

            for _ in range(10):
                if not login_window_busy(account.id):
                    break
                time.sleep(0.6)
            if login_window_busy(account.id):
                finish_job(db, job, status="failed", error="Профиль аккаунта занят окном входа", clear_progress=True)
                task.status = "failed"
                db.commit()
                return

            global_seen_urls: set[str] = set()
            total_results = 0
            done_steps = 0

            try:
                with sync_playwright() as p:
                    try:
                        with playwright_run_slot(fb_account_id=account.id):
                            with fb_account_playwright_profile_ephemeral(
                                p,
                                account,
                                storage_state=parse_storage_state(account.session_state_json),
                                db=db,
                            ) as ctx:
                                page = _pick_page(ctx)
                                try:
                                    page.set_default_navigation_timeout(120_000)
                                    page.set_default_timeout(120_000)
                                except Exception:
                                    pass

                                _throttled_progress(db, job_id, {
                                    "total_steps": total_steps,
                                    "done_steps": 0,
                                    "current_keyword": "",
                                    "current_type": "",
                                    "results_found": 0,
                                    "phase": "browser_open",
                                }, force=True)

                                warm_failed = False
                                try:
                                    _warm_facebook(ctx, page)
                                    need_login, login_msg = page_requires_facebook_login(page)
                                    if need_login:
                                        raise RuntimeError(
                                            f"{login_msg} Войдите в Facebook через «Аккаунты» → «Войти в Facebook» и запустите автопоиск снова."
                                        )
                                except RuntimeError:
                                    raise
                                except Exception as e:
                                    logger.warning("discovery warm: %s", e)

                                if is_cancelled():
                                    finish_job(db, job, status="cancelled", error="Остановлено пользователем", clear_progress=True)
                                    task.status = "done"
                                    task.ended_at = datetime.now(timezone.utc)
                                    db.commit()
                                    return

                                for kw in keywords:
                                    for stype in search_types:
                                        if is_cancelled():
                                            break

                                        _throttled_progress(db, job_id, {
                                            "total_steps": total_steps,
                                            "done_steps": done_steps,
                                            "current_keyword": kw,
                                            "current_type": stype,
                                            "results_found": total_results,
                                            "phase": "searching",
                                        }, force=True)

                                        search_url = build_search_url(kw, stype)
                                        try:
                                            page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
                                            page.wait_for_timeout(int(2000 + 1000 * (0.5)))
                                            dismiss_facebook_dom_overlays(page)
                                        except Exception as e:
                                            logger.warning("discovery: goto %s failed: %s", search_url[:120], e)
                                            log_job_event(
                                                db,
                                                job_id=job_id,
                                                event_type="discovery_search_error",
                                                severity="warning",
                                                outcome="fail",
                                                payload={
                                                    "error": str(e)[:500],
                                                    "url": search_url[:300],
                                                },
                                            )
                                            done_steps += 1
                                            continue

                                        items = scroll_search_results(
                                            page,
                                            stype,
                                            max_results=max_per_type,
                                            cancelled=is_cancelled,
                                        )

                                        new_count = 0
                                        for idx, item in enumerate(items):
                                            url = item["url"]
                                            if url in global_seen_urls:
                                                continue
                                            global_seen_urls.add(url)
                                            dr = DiscoveryResult(
                                                task_id=task.id,
                                                organization_id=job.organization_id,
                                                result_type=stype.rstrip("s") if stype.endswith("s") else stype,
                                                url=url,
                                                name=item.get("name") or None,
                                                description=item.get("description") or None,
                                                member_count=item.get("member_count"),
                                                category=item.get("category") or None,
                                                relevance_score=idx + 1,
                                                created_at=datetime.now(timezone.utc),
                                            )
                                            db.add(dr)
                                            new_count += 1
                                            total_results += 1
                                        if new_count:
                                            db.flush()

                                        log_job_event(
                                            db,
                                            job_id=job_id,
                                            event_type="discovery_search_done",
                                            outcome="ok",
                                            payload={
                                                "keyword": kw,
                                                "type": stype,
                                                "found": len(items),
                                                "new": new_count,
                                            },
                                        )

                                        done_steps += 1
                                        _throttled_progress(db, job_id, {
                                            "total_steps": total_steps,
                                            "done_steps": done_steps,
                                            "current_keyword": kw,
                                            "current_type": stype,
                                            "results_found": total_results,
                                            "phase": "searching",
                                        }, force=True)

                                        time.sleep(2 + 1.5 * (done_steps % 3))

                                    if is_cancelled():
                                        break

                                task.results_count = total_results
                                task.status = "done"
                                task.ended_at = datetime.now(timezone.utc)

                                if is_cancelled():
                                    finish_job(db, job, status="cancelled", error="Остановлено пользователем", clear_progress=True)
                                else:
                                    finish_job(db, job, status="success", clear_progress=True)
                                db.commit()
                    except Exception:
                        raise
            except Exception as e:
                logger.exception("discovery job %s error", job_id)
                if is_cancelled():
                    finish_job(db, job, status="cancelled", error="Остановлено пользователем", clear_progress=True)
                else:
                    finish_job(db, job, status="failed", error=str(e)[:2000], clear_progress=True)
                task.status = "failed"
                task.ended_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()
    finally:
        with _discovery_events_lock:
            _discovery_cancel_events.pop(job_id, None)
        discovery_worker_slots.end(job_id)


def spawn_discovery_worker(job_id: int) -> None:
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        logger.warning("spawn_discovery_worker пропущен (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
        return
    t = threading.Thread(
        target=process_discovery_job,
        args=(job_id,),
        name=f"discovery-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned discovery worker for job %s", job_id)
