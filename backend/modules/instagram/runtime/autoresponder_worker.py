"""Instagram AI autoresponder background worker."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import (
    InstagramAIDialogDraft, InstagramAIDialogSession,
    InstagramConversation, InstagramLead, InstagramMessage,
)
from backend.modules.instagram.services.autoresponder import think_next_response

logger = logging.getLogger(__name__)


_thread_started = False
_DEFAULT_INTERVAL_SEC = 180
_INITIAL_DELAY_SEC = 90
_SILENT_AFTER_DAYS = 7


def _interval_sec() -> int:
    raw = os.getenv("FB_MASTER_INSTAGRAM_AUTORESPONDER_TICK_SEC", "").strip()
    try:
        return max(60, int(raw) if raw else _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


def _has_new_inbound(db: Session, sess: InstagramAIDialogSession) -> bool:
    if not sess.conversation_id:
        return False
    cutoff = sess.last_action_at or sess.created_at
    new_in = (
        db.query(InstagramMessage)
        .filter(
            InstagramMessage.conversation_id == sess.conversation_id,
            InstagramMessage.direction == "in",
            InstagramMessage.sent_at > cutoff,
        ).first()
    )
    return new_in is not None


def _send_draft(db: Session, sess: InstagramAIDialogSession, draft: InstagramAIDialogDraft) -> dict[str, Any]:
    from backend.modules.instagram.runtime.dm_sender import send_dm

    lead = db.get(InstagramLead, sess.lead_id)
    if lead is None:
        draft.review_state = "failed"; db.commit()
        return {"ok": False, "reason_code": "lead_missing"}
    res = send_dm(db, sess.account_id, lead.handle, draft.body, headless=True, daily_cap=None)
    if res.get("ok"):
        draft.review_state = "sent"
        draft.sent_at = datetime.now(timezone.utc)
        if sess.conversation_id:
            conv = db.get(InstagramConversation, sess.conversation_id)
            if conv is not None:
                m = InstagramMessage(
                    conversation_id=conv.id, direction="out",
                    body=draft.body, sent_at=draft.sent_at,
                    out_source="ai_autoresponder",
                )
                db.add(m)
                conv.last_message_at = m.sent_at
                conv.last_message_from = "out"
        sess.msg_count_out = (sess.msg_count_out or 0) + 1
        sess.last_outbound_at = draft.sent_at
        db.commit()
    else:
        draft.review_state = "failed"; db.commit()
    return res


def step_session(db: Session, sess: InstagramAIDialogSession) -> dict[str, Any]:
    if sess.state != "active":
        return {"ok": False, "reason_code": f"session_state_{sess.state}"}
    now = datetime.now(timezone.utc)
    last_evt = sess.last_inbound_at or sess.last_outbound_at or sess.created_at
    if last_evt and (now - last_evt) > timedelta(days=_SILENT_AFTER_DAYS):
        sess.state = "stopped"
        sess.final_outcome = "silent"
        sess.last_action_at = now
        db.commit()
        return {"ok": True, "stopped": "silent"}

    if not _has_new_inbound(db, sess):
        sess.next_check_at = now + timedelta(seconds=_interval_sec())
        db.commit()
        return {"ok": True, "skipped": "no_new_inbound"}

    if sess.conversation_id:
        last_in = (
            db.query(InstagramMessage)
            .filter(
                InstagramMessage.conversation_id == sess.conversation_id,
                InstagramMessage.direction == "in",
            )
            .order_by(InstagramMessage.sent_at.desc()).first()
        )
        if last_in is not None:
            sess.last_inbound_at = last_in.sent_at
            sess.msg_count_in = (sess.msg_count_in or 0) + 1
            db.commit()

    res = asyncio.run(think_next_response(db, sess.id))
    if not res.get("ok"):
        sess.next_check_at = now + timedelta(minutes=10)
        db.commit()
        return {"ok": False, "reason_code": res.get("reason_code"), "details": res.get("details")}

    draft_id = res.get("draft_id")
    draft = db.get(InstagramAIDialogDraft, int(draft_id)) if draft_id else None

    if draft is not None and sess.approval_mode == "auto" and sess.state == "active":
        send_res = _send_draft(db, sess, draft)
        sess.next_check_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, _interval_sec()))
        db.commit()
        return {"ok": send_res.get("ok", False), "draft_id": draft_id, "auto_sent": True, "send_result": send_res}

    sess.next_check_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, _interval_sec()))
    db.commit()
    return {"ok": True, "draft_id": draft_id, "auto_sent": False}


def _tick_once() -> None:
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        sessions = (
            db.query(InstagramAIDialogSession)
            .filter(InstagramAIDialogSession.state == "active").all()
        )
        ready = [s for s in sessions if (s.next_check_at is None or s.next_check_at <= now)]
        for s in ready:
            try:
                step_session(db, s)
            except Exception:
                logger.exception("instagram autoresponder step failed: session_id=%s", s.id)
    finally:
        db.close()


def _loop() -> None:
    time.sleep(_INITIAL_DELAY_SEC)
    interval = _interval_sec()
    while True:
        try:
            _tick_once()
        except Exception:
            logger.exception("instagram_autoresponder_worker tick failed")
        time.sleep(interval)


def start_autoresponder_worker_thread() -> None:
    global _thread_started
    if _thread_started:
        return
    if (os.getenv("FB_MASTER_INSTAGRAM_AUTORESPONDER_DISABLED", "") or "").strip().lower() in ("1", "true", "yes"):
        logger.info("instagram_autoresponder_worker отключён переменной FB_MASTER_INSTAGRAM_AUTORESPONDER_DISABLED")
        return
    threading.Thread(target=_loop, daemon=True, name="instagram-autoresponder-worker").start()
    _thread_started = True
    logger.info("instagram_autoresponder_worker запущен (интервал=%ss)", _interval_sec())


__all__ = ["start_autoresponder_worker_thread", "step_session"]
