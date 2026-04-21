"""Фоновый самоанализ переписок Messenger → черновики правок промпта."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import AIAgentRun, Conversation, FBAccount, Job, Message, MessengerAIPromptDraft
from backend.services.ai_agent_llm import LLMError, PREVIEW_LIMIT, complete_llm
from backend.services.ai_agent_service import (
    DEFAULT_MODEL_GEMINI,
    DEFAULT_MODEL_OPENAI,
    load_ui_settings,
)
from backend.services.cabinet_settings import effective_google_llm_api_key, effective_openai_api_key
from backend.services.job_logging import create_job, finish_job, log_job_event, start_job

logger = logging.getLogger(__name__)

JOB_TYPE = "messenger_ai_reflect"

_reflect_lock = threading.Lock()
_reflect_active_job_id: int | None = None


def messenger_ai_reflect_worker_busy() -> bool:
    with _reflect_lock:
        return _reflect_active_job_id is not None


REFLECT_SYSTEM = """Ты аналитик переписок в Facebook Messenger (B2B / сетевой маркетинг / продажи в директе).
Тебе дают один или несколько диалогов (транскрипты). Найди устойчивые паттерны: что сработало, где были ошибки тона, типовые возражения, удачные формулировки.

Верни строго один JSON-объект (без markdown, без текста до или после) вида:
{"drafts":[{"title":"краткий заголовок","body":"текст для добавления в системные указания оператору/боту (2-8 предложений, без персональных данных собеседников)","rationale":"почему это важно","risk":"low"}]}

Ограничения:
- Поле risk только: low | med | high (оценка риска вредного совета).
- Не больше 5 элементов в drafts.
- Не придумывай диалоги — только опора на переданный текст.
- В body не вставляй имена, телефоны, ссылки из переписки; обобщай.
- Пиши body на русском, если переписка на русском."""


def _parse_reflect_json(raw: str) -> list[dict[str, Any]]:
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
        return []
    if not isinstance(data, dict):
        return []
    drafts = data.get("drafts")
    if not isinstance(drafts, list):
        return []
    out: list[dict[str, Any]] = []
    for item in drafts[:5]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:255]
        body = str(item.get("body") or "").strip()[:6000]
        rationale = str(item.get("rationale") or "").strip()[:2000]
        risk = str(item.get("risk") or "low").strip().lower()
        if risk not in ("low", "med", "high"):
            risk = "low"
        if title and body:
            out.append(
                {
                    "title": title,
                    "body": body,
                    "rationale": rationale or None,
                    "risk": risk,
                }
            )
    return out


def _transcript_for_conversation(db: Session, conv: Conversation, *, max_chars: int) -> str:
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.asc())
        .all()
    )
    lines: list[str] = []
    for m in msgs:
        role = "Собеседник" if (m.direction or "").strip().lower() == "in" else "Мы"
        b = (m.body or "").strip()
        if not b:
            continue
        lines.append(f"{role}: {b}")
    blob = "\n".join(lines)
    if len(blob) <= max_chars:
        return blob
    return blob[-max_chars:]


async def _run_reflect_llm(db: Session, *, organization_id: int, user_payload: str) -> str:
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

    run = AIAgentRun(
        provider=prov,
        model=str(model)[:100],
        context_type="m2_reflect",
        person_id=None,
        conversation_id=None,
        outcome="error",
        input_preview=user_payload[:PREVIEW_LIMIT],
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
            system=REFLECT_SYSTEM,
            user=user_payload,
            response_mime_json=use_json,
            temperature=0.35,
            max_output_tokens=2048,
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

    return text


def process_messenger_reflect_job(job_id: int) -> None:
    global _reflect_active_job_id

    with _reflect_lock:
        if _reflect_active_job_id is not None and _reflect_active_job_id != job_id:
            dbx = SessionLocal()
            try:
                j = dbx.get(Job, job_id)
                if j and j.job_type == JOB_TYPE:
                    finish_job(
                        dbx,
                        j,
                        status="cancelled",
                        error="Уже выполняется другая задача самоанализа",
                    )
            finally:
                dbx.close()
            return
        _reflect_active_job_id = job_id

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                return
            snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
            raw_ids = snap.get("conversation_ids") or []
            conv_ids: list[int] = []
            for x in raw_ids:
                try:
                    conv_ids.append(int(x))
                except (TypeError, ValueError):
                    continue
            conv_ids = sorted({i for i in conv_ids if i > 0})[:5]
            if not conv_ids:
                finish_job(db, job, status="failed", error="Не указаны conversation_ids")
                db.commit()
                return

            start_job(db, job)

            org_id = int(job.organization_id)
            parts: list[str] = []
            src_ids: list[int] = []
            for cid in conv_ids:
                conv = db.get(Conversation, cid)
                if not conv:
                    continue
                acc = db.get(FBAccount, conv.fb_account_id)
                if not acc or acc.organization_id != org_id:
                    continue
                tr = _transcript_for_conversation(db, conv, max_chars=9000)
                if not tr.strip():
                    continue
                src_ids.append(conv.id)
                parts.append(f"=== Диалог conversation_id={conv.id} ===\n{tr}")

            bundle = "\n\n".join(parts).strip()
            if len(bundle) > 28000:
                bundle = bundle[-28000:]

            if not bundle:
                finish_job(db, job, status="failed", error="Нет текста для анализа")
                db.commit()
                return

            user_payload = (
                "Проанализируй следующие диалоги и верни JSON по схеме из системной инструкции.\n\n"
                + bundle
            )

            try:
                raw = asyncio.run(_run_reflect_llm(db, organization_id=org_id, user_payload=user_payload))
            except LLMError as e:
                logger.warning("messenger reflect LLM error job=%s: %s", job_id, e)
                finish_job(db, job, status="failed", error=str(e)[:900])
                db.commit()
                log_job_event(
                    db,
                    job_id=job_id,
                    event_type="messenger_reflect_llm_error",
                    severity="warning",
                    outcome="fail",
                    payload={"error": str(e)[:400]},
                )
                db.commit()
                return

            drafts = _parse_reflect_json(raw)
            if not drafts:
                finish_job(db, job, status="failed", error="Модель не вернула корректный JSON drafts")
                db.commit()
                return

            for d in drafts:
                db.add(
                    MessengerAIPromptDraft(
                        organization_id=org_id,
                        title=d["title"],
                        body=d["body"],
                        rationale=d.get("rationale"),
                        source_conversation_ids=src_ids,
                        status="pending",
                    )
                )
            db.commit()
            log_job_event(
                db,
                job_id=job_id,
                event_type="messenger_reflect_drafts_saved",
                outcome="ok",
                payload={"n": len(drafts), "conversation_ids": src_ids},
            )
            db.commit()
            finish_job(db, job, status="success")
            db.commit()
        finally:
            db.close()
    finally:
        with _reflect_lock:
            if _reflect_active_job_id == job_id:
                _reflect_active_job_id = None


def spawn_messenger_reflect_worker(job_id: int) -> None:
    t = threading.Thread(
        target=process_messenger_reflect_job,
        args=(job_id,),
        name=f"messenger-reflect-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned messenger reflect job %s", job_id)


def schedule_messenger_reflect_analysis(
    db: Session, *, organization_id: int, conversation_ids: list[int]
) -> int:
    """Создать задачу и запустить воркер. Возвращает job.id."""
    job = create_job(
        db,
        organization_id=int(organization_id),
        job_type=JOB_TYPE,
        config_snapshot={"conversation_ids": [int(x) for x in conversation_ids]},
        admin_id=None,
    )
    spawn_messenger_reflect_worker(job.id)
    return job.id
