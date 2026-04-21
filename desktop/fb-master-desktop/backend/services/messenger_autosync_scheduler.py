"""Периодический запуск фоновой синхронизации Messenger (интервал и глубина зависят от фокуса на /messenger)."""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_last_spawn_mono: float = 0.0
_AUTOSYNC_INITIAL_DELAY_SEC = 15
_AUTOSYNC_TICK_SEC = 15


def try_spawn_messenger_inbox_autosync() -> None:
    """
    Если в настройках включено и прошёл интервал — создать задачу messenger_sync
    (все аккаунты, полный inbox), если воркер мессенджера свободен и нет другой автоматизации.
    """
    global _last_spawn_mono
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        return
    from backend.database import SessionLocal
    from backend.services.job_logging import create_job
    from backend.services.tenancy import default_organization_id_for_system_jobs
    from backend.services.messenger_settings import (
        get_messenger_auto_inbox_sync_enabled,
        get_messenger_effective_autosync_interval_sec,
        is_messenger_page_focus_recent,
    )
    from backend.services.messenger_worker import JOB_TYPE, messenger_worker_busy, spawn_messenger_sync_worker

    db = SessionLocal()
    try:
        if not get_messenger_auto_inbox_sync_enabled(db):
            return
        org_id = default_organization_id_for_system_jobs(db)
        interval = float(get_messenger_effective_autosync_interval_sec(db, org_id))
        now = time.monotonic()
        if _last_spawn_mono > 0 and (now - _last_spawn_mono) < interval:
            return
        if messenger_worker_busy():
            return
        focus = is_messenger_page_focus_recent(db, org_id)
        job = create_job(
            db,
            organization_id=org_id,
            job_type=JOB_TYPE,
            config_snapshot={
                "inbox_autosync": True,
                # Вне страницы мессенджера — только список чатов (сниппеты), без открытия N тредов.
                "inbox_autosync_light": not focus,
            },
            admin_id=None,
        )
        _last_spawn_mono = now
        spawn_messenger_sync_worker(job.id)
        logger.info("Messenger inbox autosync: queued job #%s", job.id)
    except Exception:
        logger.exception("try_spawn_messenger_inbox_autosync")
    finally:
        db.close()


def _inbox_autosync_loop() -> None:
    time.sleep(_AUTOSYNC_INITIAL_DELAY_SEC)
    while True:
        try:
            try_spawn_messenger_inbox_autosync()
        except Exception:
            logger.exception("messenger inbox autosync loop iteration")
        try:
            from backend.services.messenger_ai_worker import try_schedule_messenger_followup_jobs

            try_schedule_messenger_followup_jobs()
        except Exception:
            logger.exception("messenger follow-up scheduler tick")
        time.sleep(_AUTOSYNC_TICK_SEC)


def start_messenger_inbox_autosync_thread() -> None:
    threading.Thread(
        target=_inbox_autosync_loop,
        daemon=True,
        name="messenger-inbox-autosync",
    ).start()
