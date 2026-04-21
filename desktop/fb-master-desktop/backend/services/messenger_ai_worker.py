"""Фоновая генерация ответа AI в Messenger и постановка в очередь отправки."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import threading
import time

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import AIAgentRun, Conversation, FBAccount, Job, Message, Person
from backend.services.ai_agent_llm import LLMError, PREVIEW_LIMIT, complete_llm
from backend.services.ai_agent_service import (
    DEFAULT_MODEL_GEMINI,
    DEFAULT_MODEL_OPENAI,
    load_ui_settings,
)
from backend.services.cabinet_settings import effective_google_llm_api_key, effective_openai_api_key
from backend.services.proxy_health_guard import account_proxy_tunnel_blocked
from backend.services.crm_stages_registry import stage_slugs_ordered
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.job_logging import create_job, finish_job, log_job_event, start_job
from backend.services.messenger_person_link import apply_messenger_crm_stage_to_conversation
from backend.services.messenger_ai_memory import retrieve_messenger_ai_memory_block
from backend.services.messenger_ai_settings import (
    MESSENGER_AI_PROACTIVE_SYSTEM_APPEND,
    build_messenger_ai_proactive_user_payload,
    build_messenger_ai_system_prompt,
    build_messenger_ai_user_payload,
    load_messenger_ai_settings,
    messenger_ai_steps_exceeded,
    transcript_fingerprint_from_messages,
)
from backend.services.messenger_send_queue import enqueue_messenger_send, kick_messenger_send_queue_after_idle

logger = logging.getLogger(__name__)

JOB_TYPE = "messenger_ai"

_messenger_ai_lock = threading.Lock()
_messenger_ai_active_job_id: int | None = None


def messenger_ai_worker_busy() -> bool:
    with _messenger_ai_lock:
        return _messenger_ai_active_job_id is not None


def _pending_proactive_ai_for_person(db: Session, person_id: int) -> bool:
    rows = (
        db.query(Job)
        .filter(
            Job.job_type == JOB_TYPE,
            Job.status.in_(("queued", "running")),
        )
        .limit(120)
        .all()
    )
    pid = int(person_id)
    for j in rows:
        snap = j.config_snapshot if isinstance(j.config_snapshot, dict) else {}
        raw = snap.get("proactive_person_id")
        if raw is not None and str(raw).strip().isdigit() and int(raw) == pid:
            return True
    return False


def _pending_ai_job_for_conversation(db: Session, conversation_id: int) -> bool:
    rows = (
        db.query(Job)
        .filter(
            Job.job_type == JOB_TYPE,
            Job.status.in_(("queued", "running")),
        )
        .all()
    )
    for j in rows:
        snap = j.config_snapshot if isinstance(j.config_snapshot, dict) else {}
        if int(snap.get("conversation_id") or 0) == int(conversation_id):
            return True
    return False


def schedule_messenger_ai_if_needed(db: Session, *, conversation_id: int) -> None:
    """
    После обновления сообщений в БД: если для чата включён AI и последняя реплика от собеседника,
    поставить задачу на генерацию ответа (без Playwright).
    """
    conv = db.get(Conversation, int(conversation_id))
    if not conv or not conv.ai_assistant_enabled:
        return
    acc = db.get(FBAccount, conv.fb_account_id)
    if not acc or not account_in_active_slot(acc):
        return
    if account_proxy_tunnel_blocked(acc):
        return
    ai_cfg = load_messenger_ai_settings(db)
    if messenger_ai_steps_exceeded(conv.ai_reply_steps_used or 0, ai_cfg["max_reply_steps"]):
        return
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.asc())
        .all()
    )
    if not msgs:
        return
    last = msgs[-1]
    if last.direction != "in":
        return
    fp = transcript_fingerprint_from_messages(msgs)
    prev = (conv.ai_last_transcript_fingerprint or "").strip()
    if prev and fp == prev:
        return
    if _pending_ai_job_for_conversation(db, conv.id):
        return
    job = create_job(
        db,
        organization_id=acc.organization_id,
        job_type=JOB_TYPE,
        config_snapshot={
            "conversation_id": conv.id,
            "transcript_fingerprint": fp,
        },
        admin_id=None,
    )
    db.commit()
    spawn_messenger_ai_worker(job.id)


def resolve_conversation_for_person_followup(db: Session, person: Person) -> Conversation | None:
    """Чат Messenger для контакта: приоритет аккаунта в активном слоте, затем по свежести."""
    from sqlalchemy import case

    return (
        db.query(Conversation)
        .join(FBAccount, FBAccount.id == Conversation.fb_account_id)
        .filter(
            Conversation.person_id == person.id,
            FBAccount.organization_id == person.organization_id,
            Conversation.peer_url.isnot(None),
        )
        .order_by(
            case((FBAccount.active_slot.isnot(None), 0), else_=1),
            Conversation.last_at.desc().nullslast(),
            Conversation.id.desc(),
        )
        .first()
    )


def try_schedule_messenger_followup_jobs() -> None:
    """
    Поставить в очередь одну задачу messenger_ai с proactive_person_id, если срок прошёл.
    Не конкурирует с уже запущенным AI-воркером.
    """
    from datetime import datetime, timezone

    from backend.services.ai_agent_service import cabinet_llm_ready_for_workspace

    if messenger_ai_worker_busy():
        return
    db = SessionLocal()
    try:
        if not cabinet_llm_ready_for_workspace(db):
            return
        now = datetime.now(timezone.utc)
        cands = (
            db.query(Person)
            .filter(
                Person.messenger_followup_at.isnot(None),
                Person.messenger_followup_at <= now,
                Person.messenger_followup_status == "pending",
            )
            .order_by(Person.messenger_followup_at.asc())
            .limit(8)
            .all()
        )
        for person in cands:
            if messenger_ai_worker_busy():
                return
            if _pending_proactive_ai_for_person(db, person.id):
                continue
            conv = resolve_conversation_for_person_followup(db, person)
            if not conv or not (conv.peer_url or "").strip():
                person.messenger_followup_status = "failed"
                person.messenger_followup_last_error = (
                    "Нет привязанного чата Messenger (peer_url). Откройте диалог в Мессенджере "
                    "или синхронизируйте инбокс."
                )[:512]
                db.commit()
                continue
            acc = db.get(FBAccount, conv.fb_account_id)
            if not acc or not account_in_active_slot(acc):
                continue
            if account_proxy_tunnel_blocked(acc):
                continue
            job = create_job(
                db,
                organization_id=int(person.organization_id),
                job_type=JOB_TYPE,
                config_snapshot={"proactive_person_id": int(person.id)},
                admin_id=None,
            )
            db.commit()
            spawn_messenger_ai_worker(job.id)
            logger.info(
                "Messenger follow-up AI: queued job #%s person_id=%s conv_id=%s",
                job.id,
                person.id,
                conv.id,
            )
            return
    except Exception:
        logger.exception("try_schedule_messenger_followup_jobs")
    finally:
        db.close()


def _strip_wrapping_quotes(text: str) -> str:
    s = (text or "").strip()
    if len(s) >= 2 and s[0] in "\"«" and s[-1] in "\"»":
        return s[1:-1].strip()
    return s


def _sanitize_reply(text: str) -> str:
    s = _strip_wrapping_quotes(text)
    s = re.sub(r"^(Ассистент|AI|Бот)\s*:\s*", "", s, flags=re.I)
    return s.strip()[:4000]


def _parse_messenger_ai_json(
    raw: str, *, allowed_stages: frozenset[str]
) -> tuple[str, str | None, str | None]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _sanitize_reply(raw), None, None
    if not isinstance(data, dict):
        return _sanitize_reply(raw), None, None
    rt = data.get("reply_text") or data.get("message") or data.get("reply")
    reply = _sanitize_reply(str(rt)) if rt is not None else ""
    if not reply:
        reply = _sanitize_reply(raw)
    stage_raw = data.get("crm_stage")
    stage_s: str | None = None
    if stage_raw is not None:
        s = str(stage_raw).strip()
        if s and s.lower() not in ("null", "none"):
            if s in allowed_stages:
                stage_s = s
    rationale = data.get("crm_rationale") or data.get("rationale")
    rat: str | None = None
    if rationale is not None and str(rationale).strip():
        rat = str(rationale).strip()[:900]
    return reply, stage_s, rat


def _apply_messenger_ai_crm_stage(
    *,
    conversation_id: int,
    organization_id: int,
    stage_slug: str,
    activity_detail: str | None,
) -> None:
    db2 = SessionLocal()
    try:
        conv = db2.get(Conversation, int(conversation_id))
        if not conv:
            return
        acc = db2.get(FBAccount, conv.fb_account_id)
        if not acc or acc.organization_id != int(organization_id):
            return
        payload, status = apply_messenger_crm_stage_to_conversation(
            db2,
            int(organization_id),
            conv,
            stage_slug,
            activity_detail=activity_detail,
        )
        if status != 200:
            logger.warning(
                "messenger AI CRM stage not applied conv=%s: %s %s",
                conversation_id,
                status,
                payload,
            )
    except Exception as e:
        logger.warning("messenger AI CRM stage error conv=%s: %s", conversation_id, e)
    finally:
        db2.close()


async def _run_llm_for_messenger(
    db: Session,
    *,
    conv: Conversation,
    msgs: list[Message],
    proactive_owner_prompt: str | None = None,
    context_type: str = "messenger_ai",
) -> tuple[str, AIAgentRun, str | None, str | None]:
    ui = load_ui_settings(db)
    prov = (ui.get("default_provider") or "gemini").lower().strip()
    if prov == "ollama":
        prov = "gemini"
    if prov not in ("openai", "gemini"):
        prov = "gemini"
    oa = effective_openai_api_key(db)
    gk = effective_google_llm_api_key(db)
    if prov == "openai" and not oa.strip():
        prov = "gemini"
    elif prov == "gemini" and not gk.strip():
        prov = "openai"

    if prov == "openai":
        model = ui.get("model_openai") or DEFAULT_MODEL_OPENAI
    else:
        model = ui.get("model_gemini") or DEFAULT_MODEL_GEMINI

    ai_cfg = load_messenger_ai_settings(db)
    allowed_stages = frozenset(stage_slugs_ordered(db))
    memory_ctx: str | None = None
    if ai_cfg.get("rag_enabled"):
        acc0 = db.get(FBAccount, conv.fb_account_id)
        if acc0:
            mem = retrieve_messenger_ai_memory_block(
                db,
                organization_id=int(acc0.organization_id),
                conversation=conv,
                msgs=msgs,
                limit=6,
            )
            memory_ctx = mem or None
    system = build_messenger_ai_system_prompt(
        db,
        peer_name=(conv.peer_name or ""),
        extra_per_chat=(conv.ai_extra_instructions or ""),
        memory_context=memory_ctx,
    )
    if proactive_owner_prompt is not None:
        system = system + "\n\n" + MESSENGER_AI_PROACTIVE_SYSTEM_APPEND
        user_msg = build_messenger_ai_proactive_user_payload(
            msgs, owner_prompt=proactive_owner_prompt or ""
        )
    else:
        user_msg = build_messenger_ai_user_payload(msgs)

    run = AIAgentRun(
        provider=prov,
        model=str(model)[:100],
        context_type=context_type[:20],
        person_id=conv.person_id,
        conversation_id=conv.id,
        outcome="error",
        input_preview=user_msg[:PREVIEW_LIMIT],
        output_preview=None,
    )
    db.add(run)
    db.flush()

    use_json = prov in ("openai", "gemini")
    t0 = time.perf_counter()
    try:
        text, meta = await complete_llm(
            prov,
            openai_key=oa,
            google_key=gk,
            model=str(model),
            system=system,
            user=user_msg,
            response_mime_json=use_json,
            temperature=ai_cfg["temperature"],
            max_output_tokens=ai_cfg["max_tokens"],
        )
        run.outcome = "success"
        run.error = None
        run.output_preview = text[:PREVIEW_LIMIT]
        run.tokens_prompt = meta.get("tokens_prompt")
        run.tokens_completion = meta.get("tokens_completion")
    except LLMError as e:
        run.outcome = "error"
        run.error = str(e)[:2000]
        run.output_preview = None
        raise
    finally:
        run.duration_ms = int((time.perf_counter() - t0) * 1000)

    reply, stage_s, rat = _parse_messenger_ai_json(text, allowed_stages=allowed_stages)
    return reply, run, stage_s, rat


def _followup_send_fingerprint(person_id: int, job_id: int) -> str:
    return hashlib.sha256(f"followup:{person_id}:{job_id}".encode()).hexdigest()[:64]


def _process_messenger_followup_job(db: Session, job: Job, job_id: int, person_id: int) -> None:
    """CRM: отложенное AI-сообщение (стадия «Написать позже»)."""
    person = db.get(Person, person_id)
    if not person:
        finish_job(db, job, status="failed", error="Контакт не найден")
        db.commit()
        return
    if (person.messenger_followup_status or "") != "pending":
        finish_job(db, job, status="cancelled", error="Напоминание уже обработано или отменено")
        db.commit()
        return
    person.messenger_followup_status = "processing"
    person.messenger_followup_last_error = None
    db.commit()

    conv = resolve_conversation_for_person_followup(db, person)
    if not conv or not (conv.peer_url or "").strip():
        person.messenger_followup_status = "failed"
        person.messenger_followup_last_error = (
            "Нет привязанного чата Messenger (peer_url). Синхронизируйте инбокс или откройте диалог."
        )[:512]
        db.commit()
        finish_job(db, job, status="failed", error="Нет чата Messenger с ссылкой на тред")
        db.commit()
        return

    acc = db.get(FBAccount, conv.fb_account_id)
    if not acc or not account_in_active_slot(acc) or account_proxy_tunnel_blocked(acc):
        person.messenger_followup_status = "pending"
        db.commit()
        finish_job(
            db,
            job,
            status="cancelled",
            error="Аккаунт не в активном слоте (1–3) или прокси в блоке — повторим позже",
        )
        db.commit()
        return

    ai_cfg0 = load_messenger_ai_settings(db)
    if messenger_ai_steps_exceeded(conv.ai_reply_steps_used or 0, ai_cfg0["max_reply_steps"]):
        person.messenger_followup_status = "failed"
        person.messenger_followup_last_error = "Достигнут лимит ответов AI в этом чате"[:512]
        db.commit()
        finish_job(db, job, status="cancelled", error="Достигнут лимит ответов AI в этом чате")
        db.commit()
        return

    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.asc())
        .all()
    )
    ai_cfg = load_messenger_ai_settings(db)
    lo = float(ai_cfg["delay_min_sec"])
    hi = float(ai_cfg["delay_max_sec"])
    if hi > lo:
        time.sleep(random.uniform(lo, hi))

    conv2 = db.get(Conversation, conv.id)
    person2 = db.get(Person, person_id)
    if not conv2 or not person2 or (person2.messenger_followup_status or "") != "processing":
        finish_job(db, job, status="cancelled", error="Состояние контакта или чата изменилось")
        db.commit()
        return
    ai_cfg1 = load_messenger_ai_settings(db)
    if messenger_ai_steps_exceeded(conv2.ai_reply_steps_used or 0, ai_cfg1["max_reply_steps"]):
        person2.messenger_followup_status = "failed"
        person2.messenger_followup_last_error = "Лимит шагов AI в чате"[:512]
        db.commit()
        finish_job(db, job, status="cancelled", error="Лимит ответов AI достигнут во время паузы")
        db.commit()
        return

    owner_prompt = (person2.messenger_followup_prompt or "").strip()
    try:
        reply, run, crm_stage, crm_rat = asyncio.run(
            _run_llm_for_messenger(
                db,
                conv=conv2,
                msgs=msgs,
                proactive_owner_prompt=owner_prompt,
                context_type="m_ai_followup",
            )
        )
    except LLMError as e:
        logger.warning("messenger follow-up AI LLM error person=%s: %s", person_id, e)
        person2.messenger_followup_status = "failed"
        person2.messenger_followup_last_error = str(e)[:512]
        db.commit()
        finish_job(db, job, status="failed", error=str(e)[:900])
        db.commit()
        log_job_event(
            db,
            job_id=job_id,
            event_type="messenger_ai_followup_llm_error",
            severity="warning",
            outcome="fail",
            payload={"person_id": person_id, "error": str(e)[:400]},
        )
        db.commit()
        return

    if not reply:
        person2.messenger_followup_status = "failed"
        person2.messenger_followup_last_error = "Пустой ответ модели"[:512]
        db.commit()
        finish_job(db, job, status="failed", error="Пустой ответ модели")
        db.commit()
        return

    db.commit()
    acc_ai = db.get(FBAccount, conv2.fb_account_id)
    if crm_stage and acc_ai:
        detail = (crm_rat or "").strip() or None
        _apply_messenger_ai_crm_stage(
            conversation_id=conv2.id,
            organization_id=acc_ai.organization_id,
            stage_slug=crm_stage,
            activity_detail=detail,
        )

    fp_send = _followup_send_fingerprint(person_id, job_id)
    try:
        enqueue_messenger_send(
            db,
            organization_id=job.organization_id,
            conversation_id=conv2.id,
            body=reply,
            created_by_admin_id=None,
            ai_transcript_fingerprint=fp_send,
        )
    except ValueError as e:
        person2 = db.get(Person, person_id)
        if person2:
            person2.messenger_followup_status = "failed"
            person2.messenger_followup_last_error = str(e)[:512]
            db.commit()
        finish_job(db, job, status="failed", error=str(e)[:900])
        db.commit()
        return

    person_done = db.get(Person, person_id)
    if person_done:
        person_done.messenger_followup_status = "done"
        person_done.messenger_followup_at = None
        person_done.messenger_followup_prompt = None
        person_done.messenger_followup_last_error = None
    db.commit()
    log_job_event(
        db,
        job_id=job_id,
        event_type="messenger_ai_followup_queued_send",
        outcome="ok",
        payload={"person_id": person_id, "conversation_id": conv2.id, "len": len(reply)},
    )
    db.commit()
    finish_job(db, job, status="success")
    db.commit()
    kick_messenger_send_queue_after_idle()


def process_messenger_ai_job(job_id: int) -> None:
    global _messenger_ai_active_job_id

    with _messenger_ai_lock:
        if _messenger_ai_active_job_id is not None and _messenger_ai_active_job_id != job_id:
            dbx = SessionLocal()
            try:
                j = dbx.get(Job, job_id)
                if j and j.job_type == JOB_TYPE:
                    finish_job(
                        dbx,
                        j,
                        status="cancelled",
                        error="Уже выполняется другая задача AI мессенджера",
                    )
            finally:
                dbx.close()
            return
        _messenger_ai_active_job_id = job_id

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                return
            snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
            pro_raw = snap.get("proactive_person_id")
            if pro_raw is not None and str(pro_raw).strip().isdigit():
                start_job(db, job)
                _process_messenger_followup_job(db, job, job_id, int(pro_raw))
                return

            conv_id = snap.get("conversation_id")
            fp_expected = str(snap.get("transcript_fingerprint") or "").strip()
            if conv_id is None or not str(conv_id).isdigit() or not fp_expected:
                finish_job(db, job, status="failed", error="Некорректный config задачи AI")
                db.commit()
                return
            start_job(db, job)

            conv = db.get(Conversation, int(conv_id))
            if not conv or not conv.ai_assistant_enabled:
                finish_job(db, job, status="cancelled", error="AI отключён для чата")
                db.commit()
                return
            ai_cfg0 = load_messenger_ai_settings(db)
            if messenger_ai_steps_exceeded(conv.ai_reply_steps_used or 0, ai_cfg0["max_reply_steps"]):
                finish_job(
                    db,
                    job,
                    status="cancelled",
                    error="Достигнут лимит ответов AI в этом чате",
                )
                db.commit()
                return

            msgs = (
                db.query(Message)
                .filter(Message.conversation_id == conv.id)
                .order_by(Message.id.asc())
                .all()
            )
            if not msgs or msgs[-1].direction != "in":
                finish_job(db, job, status="cancelled", error="Нет входящей реплики для ответа")
                db.commit()
                return
            fp_now = transcript_fingerprint_from_messages(msgs)
            if fp_now != fp_expected:
                finish_job(
                    db,
                    job,
                    status="cancelled",
                    error="Переписка изменилась до обработки AI — пропуск",
                )
                db.commit()
                return

            ai_cfg = load_messenger_ai_settings(db)
            lo = float(ai_cfg["delay_min_sec"])
            hi = float(ai_cfg["delay_max_sec"])
            if hi > lo:
                time.sleep(random.uniform(lo, hi))

            conv2 = db.get(Conversation, conv.id)
            if not conv2 or not conv2.ai_assistant_enabled:
                finish_job(db, job, status="cancelled", error="AI отключён во время паузы")
                db.commit()
                return
            ai_cfg1 = load_messenger_ai_settings(db)
            if messenger_ai_steps_exceeded(conv2.ai_reply_steps_used or 0, ai_cfg1["max_reply_steps"]):
                finish_job(db, job, status="cancelled", error="Лимит ответов AI достигнут во время паузы")
                db.commit()
                return
            msgs2 = (
                db.query(Message)
                .filter(Message.conversation_id == conv.id)
                .order_by(Message.id.asc())
                .all()
            )
            if not msgs2 or msgs2[-1].direction != "in":
                finish_job(db, job, status="cancelled", error="Состояние чата изменилось")
                db.commit()
                return
            if transcript_fingerprint_from_messages(msgs2) != fp_expected:
                finish_job(db, job, status="cancelled", error="Транскрипт изменился после паузы")
                db.commit()
                return

            try:
                reply, run, crm_stage, crm_rat = asyncio.run(
                    _run_llm_for_messenger(
                        db,
                        conv=conv2,
                        msgs=msgs2,
                    )
                )
            except LLMError as e:
                logger.warning("messenger AI LLM error conv=%s: %s", conv.id, e)
                finish_job(db, job, status="failed", error=str(e)[:900])
                db.commit()
                log_job_event(
                    db,
                    job_id=job_id,
                    event_type="messenger_ai_llm_error",
                    severity="warning",
                    outcome="fail",
                    payload={"conversation_id": conv.id, "error": str(e)[:400]},
                )
                db.commit()
                return

            if not reply:
                finish_job(db, job, status="failed", error="Пустой ответ модели")
                db.commit()
                return

            db.commit()

            acc_ai = db.get(FBAccount, conv2.fb_account_id)
            if crm_stage and acc_ai:
                detail = (crm_rat or "").strip() or None
                _apply_messenger_ai_crm_stage(
                    conversation_id=conv2.id,
                    organization_id=acc_ai.organization_id,
                    stage_slug=crm_stage,
                    activity_detail=detail,
                )

            try:
                enqueue_messenger_send(
                    db,
                    organization_id=job.organization_id,
                    conversation_id=conv.id,
                    body=reply,
                    created_by_admin_id=None,
                    ai_transcript_fingerprint=fp_expected,
                )
            except ValueError as e:
                finish_job(db, job, status="failed", error=str(e)[:900])
                db.commit()
                return

            log_job_event(
                db,
                job_id=job_id,
                event_type="messenger_ai_queued_send",
                outcome="ok",
                payload={"conversation_id": conv.id, "len": len(reply)},
            )
            db.commit()
            finish_job(db, job, status="success")
            db.commit()
            kick_messenger_send_queue_after_idle()
        finally:
            db.close()
    finally:
        with _messenger_ai_lock:
            if _messenger_ai_active_job_id == job_id:
                _messenger_ai_active_job_id = None


def spawn_messenger_ai_worker(job_id: int) -> None:
    t = threading.Thread(
        target=process_messenger_ai_job,
        args=(job_id,),
        name=f"messenger-ai-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned messenger AI job %s", job_id)
