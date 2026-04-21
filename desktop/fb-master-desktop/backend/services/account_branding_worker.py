"""Фоновая задача «Оформление аккаунтов»: приём плана и подготовка к пошаговому Playwright-пайплайну."""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import FBAccount, Job
from backend.services.automation_switch import playwright_workers_enabled
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.job_logging import finish_job, log_job_event, start_job, update_job_progress
from backend.services.parallel_automation_slots import account_branding_worker_slots

logger = logging.getLogger(__name__)

JOB_TYPE = "account_branding"


def _release_stale_branding_slots() -> None:
    for jid in list(account_branding_worker_slots.running_ids()):
        db = SessionLocal()
        try:
            job = db.get(Job, jid)
            stale = not job or job.job_type != JOB_TYPE or job.status not in ("running", "queued")
            if stale:
                account_branding_worker_slots.end(int(jid))
        finally:
            db.close()


def account_branding_worker_busy() -> bool:
    _release_stale_branding_slots()
    return account_branding_worker_slots.has_any()


def account_branding_worker_at_capacity() -> bool:
    _release_stale_branding_slots()
    return account_branding_worker_slots.at_capacity()


def process_account_branding_job(job_id: int) -> None:
    """
    Сейчас: фиксируем план в журнале и завершаем задачу без реальных действий в Facebook.
    Дальше сюда поэтапно подключаются: скрейп эталона → медиа → ИИ-тексты → очередь публикаций с джиттером.
    """
    db = SessionLocal()
    slot_taken = False
    try:
        job = db.get(Job, job_id)
        if not job or job.job_type != JOB_TYPE:
            logger.error("account_branding job %s missing", job_id)
            return
        snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
        raw_aid = snap.get("fb_account_id")
        if raw_aid is None or not str(raw_aid).strip().isdigit():
            finish_job(db, job, status="failed", error="Нет fb_account_id в конфигурации задачи")
            db.commit()
            return
        aid = int(raw_aid)

        why = account_branding_worker_slots.try_begin(job_id)
        if why:
            finish_job(
                db,
                job,
                status="failed",
                error="Не удалось занять слот воркера (занято или лимит параллельных задач).",
            )
            db.commit()
            return
        slot_taken = True

        if not playwright_workers_enabled():
            finish_job(
                db,
                job,
                status="cancelled",
                error="Фоновые воркеры Playwright отключены (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).",
            )
            db.commit()
            return

        if job.status == "queued":
            start_job(db, job)

        acc = db.get(FBAccount, aid)
        if not acc or acc.organization_id != job.organization_id:
            finish_job(db, job, status="failed", error="Аккаунт не найден")
            db.commit()
            return
        if not account_in_active_slot(acc):
            finish_job(
                db,
                job,
                status="failed",
                error="Нужен слот 1–3 в разделе «Аккаунты»",
            )
            db.commit()
            return

        draft = acc.account_branding_draft_json if isinstance(acc.account_branding_draft_json, dict) else {}
        pipeline = _pipeline_summary(draft, snap)

        log_job_event(
            db,
            job_id=job_id,
            event_type="account_branding_plan_accepted",
            severity="info",
            fb_account_id=aid,
            outcome="ok",
            payload={"pipeline": pipeline, "note": "Пошаговое выполнение в Facebook подключается в обновлениях продукта."},
        )
        db.commit()

        update_job_progress(
            db,
            job_id,
            {
                "phase": "registered",
                "pipeline": pipeline,
                "operator_message": (
                    "План оформления принят и записан в журнал. Автоматическая смена аватара, обложки "
                    "и публикация постов по расписанию (с джиттером и «умным» темпом) будут выполняться "
                    "из этой задачи после подключения браузерного сценария."
                ),
            },
        )
        finish_job(db, job, status="success", error=None)
        db.commit()
    except Exception:
        logger.exception("account_branding job %s", job_id)
        try:
            job2 = db.get(Job, job_id)
            if job2 and job2.status in ("running", "queued"):
                finish_job(db, job2, status="failed", error="Внутренняя ошибка воркера оформления")
                db.commit()
        except Exception:
            logger.exception("account_branding job %s cleanup", job_id)
    finally:
        if slot_taken:
            account_branding_worker_slots.end(job_id)
        db.close()


def _pipeline_summary(draft: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    out = {
        "example_profile_url": (draft.get("example_profile_url") or snap.get("example_profile_url") or "")[:500],
        "copy_avatar": bool(draft.get("copy_avatar", True)),
        "copy_cover": bool(draft.get("copy_cover", False)),
        "posts_count": int(draft.get("posts_count") or snap.get("posts_count") or 0),
        "post_content_mode": str(draft.get("post_content_mode") or "copy_from_example"),
        "images_mode": str(draft.get("images_mode") or "from_example"),
        "schedule_mode": str(draft.get("schedule_mode") or "smart"),
        "interval_hours_min": float(draft.get("interval_hours_min") or 2),
        "interval_hours_max": float(draft.get("interval_hours_max") or 5),
        "daily_posts_max": int(draft.get("daily_posts_max") or 2),
    }
    return out


def spawn_account_branding_worker(job_id: int) -> None:
    t = threading.Thread(
        target=process_account_branding_job,
        args=(job_id,),
        name=f"account-branding-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned account_branding job %s", job_id)
