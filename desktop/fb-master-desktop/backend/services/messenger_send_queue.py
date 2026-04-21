"""Очередь исходящих сообщений мессенджера (несколько отправок подряд при одной сессии Playwright)."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import Conversation, FBAccount, MessengerSendQueue
from backend.services.job_logging import create_job

logger = logging.getLogger(__name__)

MESSENGER_SEND_QUEUE_MAX_PENDING = 50

_messenger_spawn_guard = threading.Lock()


def count_pending_messenger_sends_for_conversation(db: Session, conversation_id: int) -> int:
    return (
        db.query(func.count(MessengerSendQueue.id))
        .filter(
            MessengerSendQueue.conversation_id == conversation_id,
            MessengerSendQueue.status.in_(("queued", "processing")),
        )
        .scalar()
        or 0
    )


def enqueue_messenger_send(
    db: Session,
    *,
    organization_id: int,
    conversation_id: int,
    body: str,
    created_by_admin_id: int | None,
    ai_transcript_fingerprint: str | None = None,
) -> MessengerSendQueue:
    text = (body or "").strip()[:4000]
    if not text:
        raise ValueError("empty_body")
    n = count_pending_messenger_sends_for_conversation(db, conversation_id)
    if n >= MESSENGER_SEND_QUEUE_MAX_PENDING:
        raise ValueError("queue_full")
    fp = (ai_transcript_fingerprint or "").strip()[:64] or None
    row = MessengerSendQueue(
        organization_id=organization_id,
        conversation_id=conversation_id,
        body=text,
        status="queued",
        created_by_admin_id=created_by_admin_id,
        ai_transcript_fingerprint=fp,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def finalize_messenger_send_queue_row(
    db: Session,
    send_queue_row_id: int | None,
    *,
    success: bool,
    error: str | None = None,
) -> None:
    if not send_queue_row_id:
        return
    row = db.get(MessengerSendQueue, send_queue_row_id)
    if not row:
        return
    now = datetime.now(timezone.utc)
    row.processed_at = now
    if success:
        row.status = "sent"
        row.error_summary = None
    else:
        row.status = "failed"
        row.error_summary = (error or "")[:2000]
    db.commit()


def kick_messenger_send_queue_after_idle() -> None:
    """
    Если воркер мессенджера свободен — взять старейшую строку queued, создать job и spawn.
    Пропускает битые строки (помечает failed и берёт следующую).
    """
    from backend.services.messenger_worker import (
        JOB_TYPE,
        messenger_worker_busy,
        spawn_messenger_sync_worker,
    )

    with _messenger_spawn_guard:
        while True:
            if messenger_worker_busy():
                return
            db = SessionLocal()
            jid: int | None = None
            try:
                row = (
                    db.query(MessengerSendQueue)
                    .filter(MessengerSendQueue.status == "queued")
                    .order_by(MessengerSendQueue.id.asc())
                    .first()
                )
                if not row:
                    return
                conv = db.get(Conversation, row.conversation_id)
                if not conv or not (conv.peer_url or "").strip():
                    row.status = "failed"
                    row.error_summary = "Чат не найден или нет ссылки на тред"
                    row.processed_at = datetime.now(timezone.utc)
                    db.commit()
                    continue
                acc = db.get(FBAccount, conv.fb_account_id)
                if not acc or acc.organization_id != row.organization_id:
                    row.status = "failed"
                    row.error_summary = "Аккаунт не найден или не совпадает организация"
                    row.processed_at = datetime.now(timezone.utc)
                    db.commit()
                    continue
                from backend.services.proxy_health_guard import account_proxy_tunnel_blocked

                if account_proxy_tunnel_blocked(acc):
                    return
                job = create_job(
                    db,
                    organization_id=row.organization_id,
                    job_type=JOB_TYPE,
                    config_snapshot={
                        "send_message": {
                            "conversation_id": conv.id,
                            "text": row.body,
                            "send_queue_row_id": row.id,
                        }
                    },
                    admin_id=row.created_by_admin_id,
                )
                row.status = "processing"
                row.messenger_job_id = job.id
                db.commit()
                jid = job.id
            except Exception:
                logger.exception("kick_messenger_send_queue_after_idle")
                db.rollback()
                return
            finally:
                db.close()
            if jid is not None:
                spawn_messenger_sync_worker(jid)
            return


def reset_stale_messenger_send_queue_rows(db: Session) -> int:
    """
    После сброса зависших jobs: строки «processing» с уже завершённым job → снова queued.
    """
    from backend.models import Job

    n = 0
    rows = db.query(MessengerSendQueue).filter(MessengerSendQueue.status == "processing").all()
    for row in rows:
        if not row.messenger_job_id:
            row.status = "queued"
            n += 1
            continue
        j = db.get(Job, row.messenger_job_id)
        if j and j.status in ("running", "queued"):
            continue
        row.status = "queued"
        row.messenger_job_id = None
        row.error_summary = None
        n += 1
    if n:
        db.commit()
    return n
