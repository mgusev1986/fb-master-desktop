"""
Сброс задач со статусом running/queued, у которых нет живого воркера (перезапуск процесса, деплой, сбой).

Парсер — через finish_stale_parser_jobs (задача сбрасывается; прогресс парсинга не автопродолжается).

Рассылка и прогрев: кампания → paused, строки «в работе» → снова queued, job → cancelled.
В config кампании ставится флаг для automation_auto_resume; при включённой настройке кабинета
после старта приложения воркер запускается сам (см. automation_auto_resume). Иначе — кнопка «Продолжить».

Сценарии (sequence): enrollments и current_step_index в БД; job сбрасывается — автотик подхватит очередь.
Мессенджер-синк: job сбрасывается; при необходимости запустить вручную или дождаться авто-синка.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models import Job, OutreachCampaign, OutreachQueue, WarmupCampaign, WarmupQueue
from backend.services.automation_auto_resume import mark_campaign_config_interrupted
from backend.services.job_logging import finish_job

_MSG_OUTREACH = (
    "Перезапуск сервера: рассылка остановлена. При включённом автовозобновлении в «Настройках» "
    "выполнение продолжится само; иначе нажмите «Продолжить» в кампании."
)
_MSG_WARMUP = (
    "Перезапуск сервера: прогрев остановлен. При включённом автовозобновлении в «Настройках» "
    "выполнение продолжится само; иначе нажмите «Продолжить» в кампании."
)
_MSG_SEQUENCE = (
    "Перезапуск сервера: задача сценария прервана. Активные кампании продолжат работу "
    "по расписанию (автотик)."
)
_MSG_MESSENGER = (
    "Перезапуск сервера: синхронизация мессенджера прервана. При необходимости запустите её снова "
    "или дождитесь авто-синхронизации."
)
_MSG_NATURAL_WARMUP = (
    "Перезапуск сервера: сессия автопрогрева прервана. Планировщик продолжит по расписанию."
)
_MSG_ACCOUNT_BRANDING = (
    "Перезапуск сервера: задача оформления аккаунта прервана. Очередь постов в БД сохранена; "
    "планировщик возобновит публикации по расписанию. Подготовку (bootstrap) при необходимости запустите снова."
)
_MSG_MESSENGER_AI = (
    "Перезапуск сервера: задача AI мессенджера прервана. После следующей синхронизации чата ответ "
    "сгенерируется снова, если включён ассистент."
)


def _finish_stale_outreach(db: Session) -> int:
    from backend.services.outreach_worker import JOB_TYPE
    from backend.services.parallel_automation_slots import outreach_worker_slots

    alive = set(outreach_worker_slots.running_ids())
    n = 0
    jobs = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.status.in_(["running", "queued"]))
        .all()
    )
    for j in jobs:
        if j.id in alive:
            continue
        camp = (
            db.query(OutreachCampaign)
            .filter(OutreachCampaign.job_id == j.id)
            .first()
        )
        if camp and camp.status == "running":
            camp.status = "paused"
            mark_campaign_config_interrupted(camp)
        for row in (
            db.query(OutreachQueue)
            .filter(OutreachQueue.job_id == j.id, OutreachQueue.status == "running")
            .all()
        ):
            row.status = "queued"
            row.processed_at = None
        finish_job(db, j, status="cancelled", error=_MSG_OUTREACH)
        n += 1
    return n


def _finish_stale_warmup(db: Session) -> int:
    from backend.services.parallel_automation_slots import warmup_worker_slots
    from backend.services.warmup_worker import JOB_TYPE

    alive = set(warmup_worker_slots.running_ids())
    n = 0
    jobs = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.status.in_(["running", "queued"]))
        .all()
    )
    for j in jobs:
        if j.id in alive:
            continue
        camp = (
            db.query(WarmupCampaign)
            .filter(WarmupCampaign.job_id == j.id)
            .first()
        )
        if camp and camp.status == "running":
            camp.status = "paused"
            mark_campaign_config_interrupted(camp)
        for row in (
            db.query(WarmupQueue)
            .filter(WarmupQueue.job_id == j.id, WarmupQueue.status == "running")
            .all()
        ):
            row.status = "queued"
            row.processed_at = None
        finish_job(db, j, status="cancelled", error=_MSG_WARMUP)
        n += 1
    return n


def _finish_stale_sequence(db: Session) -> int:
    from backend.services.parallel_automation_slots import sequence_worker_slots
    from backend.services.sequence_worker import JOB_TYPE

    alive = set(sequence_worker_slots.running_ids())
    n = 0
    jobs = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.status.in_(["running", "queued"]))
        .all()
    )
    for j in jobs:
        if j.id in alive:
            continue
        finish_job(db, j, status="cancelled", error=_MSG_SEQUENCE)
        n += 1
    return n


def _finish_stale_messenger(db: Session) -> int:
    from backend.services.messenger_worker import JOB_TYPE, messenger_worker_busy

    if messenger_worker_busy():
        return 0
    n = 0
    jobs = (
        db.query(Job)
        .filter(Job.job_type == JOB_TYPE, Job.status.in_(["running", "queued"]))
        .all()
    )
    for j in jobs:
        finish_job(db, j, status="cancelled", error=_MSG_MESSENGER)
        n += 1
    from backend.services.messenger_send_queue import reset_stale_messenger_send_queue_rows

    reset_stale_messenger_send_queue_rows(db)
    return n


def _finish_stale_messenger_ai(db: Session) -> int:
    from backend.services.messenger_ai_worker import JOB_TYPE, messenger_ai_worker_busy

    if messenger_ai_worker_busy():
        return 0
    n = 0
    jobs = (
        db.query(Job)
        .filter(
            Job.job_type == JOB_TYPE,
            Job.status.in_(["running", "queued"]),
        )
        .all()
    )
    for j in jobs:
        finish_job(db, j, status="cancelled", error=_MSG_MESSENGER_AI)
        n += 1
    if n:
        from backend.models import Person

        db.query(Person).filter(Person.messenger_followup_status == "processing").update(
            {"messenger_followup_status": "pending"},
            synchronize_session=False,
        )
        db.commit()
    return n


def _finish_stale_natural_warmup(db: Session) -> int:
    n = 0
    jobs = (
        db.query(Job)
        .filter(
            Job.job_type == "natural_warmup",
            Job.status.in_(["running", "queued"]),
        )
        .all()
    )
    for j in jobs:
        finish_job(db, j, status="cancelled", error=_MSG_NATURAL_WARMUP)
        n += 1
    return n


def _finish_stale_account_branding(db: Session) -> int:
    from backend.services.parallel_automation_slots import account_branding_worker_slots

    alive = set(account_branding_worker_slots.running_ids())
    n = 0
    jobs = (
        db.query(Job)
        .filter(
            Job.job_type == "account_branding",
            Job.status.in_(["running", "queued"]),
        )
        .all()
    )
    for j in jobs:
        if j.id in alive:
            continue
        finish_job(db, j, status="cancelled", error=_MSG_ACCOUNT_BRANDING)
        n += 1
    return n


def recover_stale_automation_on_startup(db: Session) -> dict[str, int]:
    """
    Вызывать один раз при старте приложения (после init_db), до фоновых воркеров.
    Возвращает счётчики сброшенных задач по типам.
    Автовозобновление рассылки/прогрева — отдельно через run_auto_resume_after_stale_recovery.
    """
    from backend.services.parser_worker import finish_stale_parser_jobs

    stats: dict[str, int] = {
        "parser": finish_stale_parser_jobs(db),
        "outreach": _finish_stale_outreach(db),
        "warmup": _finish_stale_warmup(db),
        "sequence": _finish_stale_sequence(db),
        "messenger_sync": _finish_stale_messenger(db),
        "messenger_ai": _finish_stale_messenger_ai(db),
        "natural_warmup": _finish_stale_natural_warmup(db),
        "account_branding": _finish_stale_account_branding(db),
    }
    from backend.services.messenger_send_queue import kick_messenger_send_queue_after_idle

    kick_messenger_send_queue_after_idle()
    return stats
