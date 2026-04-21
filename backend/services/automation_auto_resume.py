"""
Автовозобновление рассылки и прогрева после перезапуска сервера / деплоя.

Очереди (outreach_queue, warmup_queue) и шаги сценариев (sequence_enrollments) уже хранятся в БД.
При старте stale_job_recovery помечает прерванные кампании флагом в config и ставит paused;
этот модуль при включённой настройке кабинета снова запускает воркеры без кнопки «Продолжить».
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import OutreachCampaign, OutreachQueue, WarmupCampaign, WarmupQueue
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.cabinet_settings import effective_automation_auto_resume_after_restart

logger = logging.getLogger(__name__)

RESTART_AUTO_RESUME_FLAG = "_restart_auto_resume"


def mark_campaign_config_interrupted(campaign: Any) -> None:
    """Пометить кампанию для автозапуска после recover_stale (вызывается из stale_job_recovery)."""
    if campaign is None:
        return
    cfg = dict(campaign.config) if isinstance(campaign.config, dict) else {}
    cfg[RESTART_AUTO_RESUME_FLAG] = True
    campaign.config = cfg


def strip_auto_resume_flag_from_config(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    return {k: v for k, v in cfg.items() if k != RESTART_AUTO_RESUME_FLAG}


def clear_auto_resume_flags_all_campaigns(db: Session) -> int:
    """Убрать флаг у всех кампаний (настройка «не автовозобновлять»)."""
    n = 0
    for camp in db.query(OutreachCampaign).all():
        if not isinstance(camp.config, dict) or not camp.config.get(RESTART_AUTO_RESUME_FLAG):
            continue
        camp.config = strip_auto_resume_flag_from_config(camp.config)
        n += 1
    for camp in db.query(WarmupCampaign).all():
        if not isinstance(camp.config, dict) or not camp.config.get(RESTART_AUTO_RESUME_FLAG):
            continue
        camp.config = strip_auto_resume_flag_from_config(camp.config)
        n += 1
    if n:
        db.commit()
    return n


def _outreach_candidate(db: Session) -> OutreachCampaign | None:
    q = (
        db.query(OutreachCampaign)
        .filter(OutreachCampaign.status == "paused")
        .order_by(OutreachCampaign.id.asc())
    )
    for camp in q.all():
        cfg = camp.config if isinstance(camp.config, dict) else {}
        if not cfg.get(RESTART_AUTO_RESUME_FLAG):
            continue
        nq = (
            db.query(OutreachQueue)
            .filter(
                OutreachQueue.outreach_campaign_id == camp.id,
                OutreachQueue.status == "queued",
            )
            .count()
        )
        if nq > 0 and camp.job_id:
            return camp
    return None


def _warmup_candidate(db: Session) -> WarmupCampaign | None:
    q = (
        db.query(WarmupCampaign)
        .filter(WarmupCampaign.status == "paused")
        .order_by(WarmupCampaign.id.asc())
    )
    for camp in q.all():
        cfg = camp.config if isinstance(camp.config, dict) else {}
        if not cfg.get(RESTART_AUTO_RESUME_FLAG):
            continue
        nq = (
            db.query(WarmupQueue)
            .filter(
                WarmupQueue.warmup_campaign_id == camp.id,
                WarmupQueue.status == "queued",
            )
            .count()
        )
        if nq > 0 and camp.job_id:
            return camp
    return None


def try_auto_resume_next_outreach() -> bool:
    from backend.services.outreach_worker import outreach_worker_at_capacity, spawn_outreach_worker

    db = SessionLocal()
    try:
        if not effective_automation_auto_resume_after_restart(db):
            return False
        if outreach_worker_at_capacity():
            return False
        camp = _outreach_candidate(db)
        if not camp:
            return False
        busy_msg = account_automation_conflict_message(
            db,
            camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else [],
            ignore_outreach_campaign_id=camp.id,
        )
        if busy_msg:
            logger.info(
                "Автовозобновление рассылки пропущено: campaign=%s reason=%s",
                camp.id,
                busy_msg,
            )
            return False
        cfg = strip_auto_resume_flag_from_config(
            camp.config if isinstance(camp.config, dict) else {}
        )
        camp.config = cfg
        camp.status = "running"
        db.commit()
        logger.info(
            "Автовозобновление рассылки: кампания id=%s после перезапуска сервера",
            camp.id,
        )
        spawn_outreach_worker(int(camp.job_id))
        return True
    except Exception:
        logger.exception("try_auto_resume_next_outreach")
        db.rollback()
        return False
    finally:
        db.close()


def try_auto_resume_next_warmup() -> bool:
    from backend.services.warmup_worker import spawn_warmup_worker, warmup_worker_at_capacity

    db = SessionLocal()
    try:
        if not effective_automation_auto_resume_after_restart(db):
            return False
        if warmup_worker_at_capacity():
            return False
        camp = _warmup_candidate(db)
        if not camp:
            return False
        busy_msg = account_automation_conflict_message(
            db,
            camp.fb_account_ids if isinstance(camp.fb_account_ids, list) else [],
            ignore_warmup_campaign_id=camp.id,
        )
        if busy_msg:
            logger.info(
                "Автовозобновление прогрева пропущено: campaign=%s reason=%s",
                camp.id,
                busy_msg,
            )
            return False
        cfg = strip_auto_resume_flag_from_config(
            camp.config if isinstance(camp.config, dict) else {}
        )
        camp.config = cfg
        camp.status = "running"
        db.commit()
        logger.info(
            "Автовозобновление прогрева: кампания id=%s после перезапуска сервера",
            camp.id,
        )
        spawn_warmup_worker(int(camp.job_id))
        return True
    except Exception:
        logger.exception("try_auto_resume_next_warmup")
        db.rollback()
        return False
    finally:
        db.close()


def run_auto_resume_after_stale_recovery(db: Session) -> dict[str, Any]:
    """
    Вызывать сразу после recover_stale_automation_on_startup (в той же или новой сессии).
    """
    if not effective_automation_auto_resume_after_restart(db):
        n = clear_auto_resume_flags_all_campaigns(db)
        return {"enabled": False, "flags_cleared": n}

    out = try_auto_resume_next_outreach()
    warm = try_auto_resume_next_warmup()

    try:
        from backend.services.sequence_worker import try_sequence_autotick

        try_sequence_autotick()
    except Exception:
        logger.exception("run_auto_resume: try_sequence_autotick")

    return {
        "enabled": True,
        "outreach_resumed": out,
        "warmup_resumed": warm,
    }
