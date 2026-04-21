"""LinkedIn sequence worker — выполнение Agent Mode цепочек.

Берёт из БД `LinkedInSequenceRun` со state="active" и `next_action_at <= now`,
обрабатывает текущий узел (node_id), вычисляет следующий по edges, обновляет
state_json и next_action_at.

Каждый node-handler возвращает dict
  {"branch": "ok"|"fail"|"accepted"|"not_yet"|"replied"|"no_reply"|"timeout"|None,
   "wait_hours": int|None,        # сколько ждать перед следующим узлом
   "stop_reason": str|None,       # если узел завершает выполнение
   "details": dict|None}

Если для текущего branch нет outgoing edge → run помечается "completed".
Если узел "stop" → run "completed" с reason из params.

Worker запускается из `app_factory.lifespan` если LinkedIn включён.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import (
    LinkedInLead,
    LinkedInMessage,
    LinkedInSequence,
    LinkedInSequenceRun,
)

logger = logging.getLogger(__name__)


_thread_started = False
_DEFAULT_INTERVAL_SEC = 240  # 4 мин — реже чем outreach worker
_INITIAL_DELAY_SEC = 90


def _interval_sec() -> int:
    raw = os.getenv("FB_MASTER_LINKEDIN_SEQUENCE_TICK_SEC", "").strip()
    try:
        return max(60, int(raw) if raw else _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


# ── node handlers ──────────────────────────────────────────


def _node_send_invitation(db: Session, run: LinkedInSequenceRun, params: dict, headless: bool) -> dict[str, Any]:
    from backend.modules.linkedin.runtime.invitation_sender import send_invitation

    template_id = params.get("template_id")
    body = ""
    if template_id:
        from backend.modules.linkedin.models import LinkedInTemplate

        tpl = db.get(LinkedInTemplate, int(template_id))
        if tpl is not None:
            body = _render_for_lead(db, tpl.body, run.lead_id)
    res = send_invitation(db, run.account_id, run.lead_id, note=body or "", headless=headless)
    branch = "ok" if res.get("ok") else "fail"
    return {"branch": branch, "details": res}


def _node_send_dm(db: Session, run: LinkedInSequenceRun, params: dict, headless: bool) -> dict[str, Any]:
    from backend.modules.linkedin.runtime.dm_sender import send_dm

    template_id = params.get("template_id")
    body = ""
    if template_id:
        from backend.modules.linkedin.models import LinkedInTemplate

        tpl = db.get(LinkedInTemplate, int(template_id))
        if tpl is not None:
            body = _render_for_lead(db, tpl.body, run.lead_id)
    if not body:
        return {"branch": "fail", "details": {"reason": "empty_body"}}
    res = send_dm(db, run.account_id, run.lead_id, body=body, headless=headless)
    branch = "ok" if res.get("ok") else "fail"
    return {"branch": branch, "details": res}


def _node_wait_hours(db: Session, run: LinkedInSequenceRun, params: dict, headless: bool) -> dict[str, Any]:
    h = max(0, int(params.get("hours", 24) or 0))
    return {"branch": None, "wait_hours": h, "details": {"hours": h}}


def _node_check_invite_accepted(db: Session, run: LinkedInSequenceRun, params: dict, headless: bool) -> dict[str, Any]:
    """Лёгкая эвристика: смотрим lead.outreach_stage. Полная Playwright-проверка — отдельная итерация."""
    lead = db.get(LinkedInLead, run.lead_id)
    if lead is None:
        return {"branch": "not_yet", "details": {"reason": "lead_missing"}}
    accepted = (lead.outreach_stage or "") in ("invite_accepted", "dm_sent", "replied", "converted")
    return {"branch": "accepted" if accepted else "not_yet", "details": {"stage": lead.outreach_stage}}


def _node_check_reply(db: Session, run: LinkedInSequenceRun, params: dict, headless: bool) -> dict[str, Any]:
    """Эвристика: ищем входящие сообщения в conversation этого lead/account после last_outreach_at."""
    from backend.modules.linkedin.models import LinkedInConversation

    lead = db.get(LinkedInLead, run.lead_id)
    if lead is None:
        return {"branch": "no_reply", "details": {"reason": "lead_missing"}}
    cutoff = lead.last_outreach_at or run.created_at
    convs = db.query(LinkedInConversation).filter(
        LinkedInConversation.account_id == run.account_id,
        LinkedInConversation.lead_id == lead.id,
    ).all()
    for c in convs:
        m = db.query(LinkedInMessage).filter(
            LinkedInMessage.conversation_id == c.id,
            LinkedInMessage.direction == "in",
            LinkedInMessage.sent_at > cutoff,
        ).first()
        if m is not None:
            lead.outreach_stage = "replied"
            db.commit()
            return {"branch": "replied", "details": {"conv_id": c.id}}
    return {"branch": "no_reply", "details": {"checked_convs": len(convs)}}


def _node_stop(db: Session, run: LinkedInSequenceRun, params: dict, headless: bool) -> dict[str, Any]:
    return {"branch": None, "stop_reason": str(params.get("reason") or "stop"), "details": {}}


_HANDLERS = {
    "send_invitation": _node_send_invitation,
    "send_dm": _node_send_dm,
    "wait_hours": _node_wait_hours,
    "check_invite_accepted": _node_check_invite_accepted,
    "check_reply": _node_check_reply,
    "stop": _node_stop,
}


# ── helpers ─────────────────────────────────────────────────


def _render_for_lead(db: Session, body: str, lead_id: int) -> str:
    from backend.modules.linkedin.services.outreach_builder import _PLACEHOLDER_RE, _render

    lead = db.get(LinkedInLead, lead_id)
    if lead is None:
        return body
    return _render(body, lead)


def _next_node(seq: LinkedInSequence, current_node_id: str, branch: str | None) -> str | None:
    edges = seq.edges_json or []
    # Сначала ищем edge с конкретным branch.
    if branch is not None:
        for e in edges:
            if e.get("from") == current_node_id and e.get("on") == branch:
                return str(e["to"])
    # Потом edge без on (default).
    for e in edges:
        if e.get("from") == current_node_id and not e.get("on"):
            return str(e["to"])
    return None


def _node_by_id(seq: LinkedInSequence, node_id: str) -> dict[str, Any] | None:
    for n in (seq.nodes_json or []):
        if str(n.get("id")) == str(node_id):
            return n
    return None


# ── main entry ──────────────────────────────────────────────


def step_run(db: Session, run: LinkedInSequenceRun, *, headless: bool = True) -> dict[str, Any]:
    """Сделать один шаг по run'у. Возвращает diagnostic dict."""
    seq = db.get(LinkedInSequence, run.sequence_id)
    if seq is None:
        run.state = "failed"
        db.commit()
        return {"ok": False, "reason": "sequence_missing"}
    if not run.current_node_id:
        run.state = "completed"
        db.commit()
        return {"ok": True, "reason": "no_current_node"}

    node = _node_by_id(seq, run.current_node_id)
    if node is None:
        run.state = "failed"
        db.commit()
        return {"ok": False, "reason": "node_missing", "current": run.current_node_id}
    handler = _HANDLERS.get(node.get("type"))
    if handler is None:
        run.state = "failed"
        db.commit()
        return {"ok": False, "reason": "no_handler", "type": node.get("type")}

    try:
        out = handler(db, run, node.get("params") or {}, headless)
    except Exception as e:  # noqa: BLE001
        logger.exception("sequence step handler failed")
        run.state = "failed"
        run.last_event_at = datetime.now(timezone.utc)
        run.state_json = {**(run.state_json or {}), "last_error": str(e)[:200]}
        db.commit()
        return {"ok": False, "reason": "handler_exception", "details": str(e)[:200]}

    run.last_event_at = datetime.now(timezone.utc)
    state_json = dict(run.state_json or {})
    state_json[run.current_node_id] = {"branch": out.get("branch"), "details": out.get("details")}
    run.state_json = state_json

    if out.get("stop_reason"):
        run.state = "completed"
        run.next_action_at = None
        db.commit()
        return {"ok": True, "stopped": out["stop_reason"]}

    next_id = _next_node(seq, run.current_node_id, out.get("branch"))
    if next_id is None:
        run.state = "completed"
        run.next_action_at = None
        db.commit()
        return {"ok": True, "reason": "no_outgoing_edge"}

    wait_hours = out.get("wait_hours")
    if wait_hours and wait_hours > 0:
        run.next_action_at = datetime.now(timezone.utc) + timedelta(hours=int(wait_hours))
        run.state = "waiting"
    else:
        run.next_action_at = datetime.now(timezone.utc)
        run.state = "active"
    run.current_node_id = next_id
    db.commit()
    return {"ok": True, "next_node": next_id, "wait_hours": wait_hours, "branch": out.get("branch")}


def _tick_once(headless: bool = True) -> None:
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        runs = (
            db.query(LinkedInSequenceRun)
            .filter(
                LinkedInSequenceRun.state.in_(("active", "waiting")),
            )
            .all()
        )
        ready = [r for r in runs if (r.next_action_at is None or r.next_action_at <= now)]
        for r in ready:
            try:
                step_run(db, r, headless=headless)
            except Exception:
                logger.exception("sequence_worker step: run_id=%s", r.id)
    finally:
        db.close()


def _loop() -> None:
    time.sleep(_INITIAL_DELAY_SEC)
    interval = _interval_sec()
    while True:
        try:
            _tick_once(headless=True)
        except Exception:
            logger.exception("linkedin_sequence_worker tick failed")
        time.sleep(interval)


def start_sequence_worker_thread() -> None:
    global _thread_started
    if _thread_started:
        return
    if (os.getenv("FB_MASTER_LINKEDIN_SEQUENCE_DISABLED", "") or "").strip().lower() in ("1", "true", "yes"):
        logger.info("linkedin_sequence_worker отключён переменной FB_MASTER_LINKEDIN_SEQUENCE_DISABLED")
        return
    threading.Thread(target=_loop, daemon=True, name="linkedin-sequence-worker").start()
    _thread_started = True
    logger.info("linkedin_sequence_worker запущен (интервал=%ss)", _interval_sec())


__all__ = ["start_sequence_worker_thread", "step_run"]
