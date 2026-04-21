"""Twitter AI autoresponder — core logic.

Алгоритм:
  1. Worker периодически проходит по `TwitterAIDialogSession` со state="active".
  2. Для каждой — берёт последние сообщения conversation, сравнивает с
     last_inbound_at: если есть НОВОЕ входящее (in после last_action_at) — генерим ответ.
  3. LLM получает: persona-промпт, цель сессии, scratchpad (memory),
     последние ~12 сообщений, и должен:
       - сгенерировать body ответа,
       - оценить статус ('continue' | 'goal_reached' | 'refused' | 'silent'),
       - вернуть rationale (для оператора).
  4. По approval_mode:
       - 'manual' → создать TwitterAIDialogDraft, ждать approve.
       - 'auto' → сразу send (через dm_sender), записать в TwitterMessage(out_source='ai_autoresponder').
  5. Обновить scratchpad, last_action_at, msg_count_*.
  6. Если status='goal_reached' → state='goal_reached', final_outcome.
     Если 'refused' → state='stopped', final_outcome='refused'.
     Если 'silent' (нет ответа > 7 дней) → state='stopped', final_outcome='silent'.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import (
    TwitterAIDialogDraft,
    TwitterAIDialogSession,
    TwitterAIPersona,
    TwitterAccount,
    TwitterConversation,
    TwitterLead,
    TwitterMessage,
)
from backend.services.ai_agent_llm import LLMError, complete_llm

logger = logging.getLogger(__name__)


_GOAL_DESCRIPTIONS = {
    "book_call": "Подвести собеседника к согласию на короткий созвон. Когда он согласен — отправь ссылку cta_call_link и спроси удобное время.",
    "share_info": "Подвести к запросу материалов / pdf / описания. Когда запросит — отправь cta_info_link.",
    "qualify_lead": "Узнать роль, компанию, размер, текущую боль/задачу. После 3-4 вопросов — пометь как qualified и предложи следующий шаг.",
    "nurture": "Поддерживать тёплый диалог, не давить. Цель — оставить позитивное впечатление и держать дверь открытой.",
}


def _build_persona_block(persona: TwitterAIPersona | None) -> str:
    if persona is None:
        return "Ты — представитель компании, отвечаешь на DM в X. Пиши коротко, по делу, без шаблонных приветствий."
    parts: list[str] = []
    parts.append(f"Тебя зовут {persona.name}.")
    if persona.role:
        parts.append(f"Роль: {persona.role}.")
    if persona.company:
        parts.append(f"Компания: {persona.company}.")
    if persona.offer_summary:
        parts.append(f"Что предлагаешь:\n{persona.offer_summary}")
    if persona.tone:
        parts.append(f"Тон общения: {persona.tone}.")
    if persona.languages:
        parts.append(f"Языки общения: {', '.join(persona.languages)}.")
    if persona.knowledge_base:
        parts.append(f"Что ты знаешь и можешь использовать в ответах:\n{persona.knowledge_base}")
    if persona.cta_call_link:
        parts.append(f"Когда уместно — ссылка на созвон: {persona.cta_call_link}")
    if persona.cta_info_link:
        parts.append(f"Когда уместно — ссылка на материалы: {persona.cta_info_link}")
    if persona.forbidden_topics:
        parts.append(f"НЕ обсуждай: {persona.forbidden_topics}")
    parts.append(
        "Стиль: пиши как живой человек, без эмодзи-мусора, без формальных приветствий после первого "
        "сообщения, без шаблонных «уважаемый». Каждый ответ — короткий (1-3 предложения), кроме случаев "
        "когда нужен ответ на конкретный вопрос. Не повторяй один и тот же вопрос дважды."
    )
    return "\n\n".join(parts)


def _build_history_block(messages: list[TwitterMessage], limit: int = 12) -> str:
    last = messages[-limit:]
    rows = []
    for m in last:
        who = "ИХ:" if m.direction == "in" else "Я:"
        rows.append(f"{who} {m.body}")
    return "\n".join(rows) if rows else "(переписки ещё нет)"


def _build_lead_block(lead: TwitterLead | None) -> str:
    if lead is None:
        return ""
    parts = [f"Собеседник: @{lead.handle}"]
    if lead.full_name:
        parts.append(lead.full_name)
    if lead.bio:
        parts.append(f"bio: {lead.bio}")
    if lead.location:
        parts.append(f"locality: {lead.location}")
    return " · ".join(parts)


_OUTPUT_INSTRUCTION = (
    "Ответь СТРОГО JSON-объектом БЕЗ markdown-блока, БЕЗ пояснений, со схемой:\n"
    "{\n"
    "  \"body\": \"твой ответ собеседнику в 1-3 предложениях\",\n"
    "  \"status\": \"continue|goal_reached|refused|silent\",\n"
    "  \"rationale\": \"краткое объяснение для оператора (1 предложение, на русском)\"\n"
    "}\n"
    "status='goal_reached' — если по сути достиг цели сессии (отправил cta-ссылку и собеседник согласился, или собеседник сам запросил материалы/созвон).\n"
    "status='refused' — если собеседник явно отказался / попросил не писать / выразил агрессию.\n"
    "status='silent' — если собеседник давно молчит и нечего сказать.\n"
    "status='continue' — продолжаем диалог."
)


def _resolve_llm_keys_and_model(db: Session) -> tuple[str, str, str, str]:
    from backend.services.ai_agent_service import load_ui_settings
    from backend.services.cabinet_settings import (
        effective_google_llm_api_key,
        effective_openai_api_key,
    )

    ui = load_ui_settings(db)
    provider = str(ui.get("default_provider") or "gemini").strip().lower()
    if provider not in ("gemini", "openai"):
        provider = "gemini"
    oa = (effective_openai_api_key(db) or "").strip()
    gk = (effective_google_llm_api_key(db) or "").strip()
    if provider == "gemini":
        model = str(ui.get("model_gemini") or "gemini-2.5-flash").strip()
    else:
        model = str(ui.get("model_openai") or "gpt-4o-mini").strip()
    return provider, oa, gk, model


async def _ask_llm(db: Session, system: str, user: str) -> dict[str, Any]:
    provider, oa, gk, model = _resolve_llm_keys_and_model(db)
    order = [provider]
    if provider == "openai" and "gemini" not in order:
        order.append("gemini")
    elif provider == "gemini" and "openai" not in order:
        order.append("openai")
    last_err = ""
    for p in order:
        if p == "openai" and not oa.strip():
            continue
        if p == "gemini" and not gk.strip():
            continue
        m = (
            "gemini-2.5-flash" if p == "gemini" and provider != "gemini" else
            ("gpt-4o-mini" if p == "openai" and provider != "openai" else model)
        )
        try:
            text, _meta = await complete_llm(
                provider=p, openai_key=oa, google_key=gk, model=m,
                system=system, user=user,
                response_mime_json=(p == "gemini"),
            )
            return _parse_llm_json(text or "")
        except LLMError as e:
            last_err = str(e)
            continue
    raise LLMError(last_err or "Нет доступного LLM-ключа")


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Пытаемся распарсить JSON-ответ LLM с разными форматами."""
    import re

    s = (raw or "").strip().lstrip("\ufeff")
    # Markdown fence?
    m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", s, re.I)
    if m:
        s = m.group(1).strip()
    # Прямой JSON
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except Exception:  # noqa: BLE001
        pass
    # Найти первый {...} в тексте.
    start = s.find("{")
    if start >= 0:
        depth = 0
        for i, ch in enumerate(s[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = s[start:i + 1]
                    try:
                        v = json.loads(chunk)
                        if isinstance(v, dict):
                            return v
                    except Exception:  # noqa: BLE001
                        break
    return {}


# ── Public API ─────────────────────────────────────────────


def create_session(
    db: Session, organization_id: int, *,
    account_id: int, lead_id: int, persona_id: int | None,
    goal: str = "qualify_lead", approval_mode: str = "manual",
    conversation_id: int | None = None,
) -> TwitterAIDialogSession:
    if goal not in _GOAL_DESCRIPTIONS:
        goal = "qualify_lead"
    if approval_mode not in ("manual", "auto"):
        approval_mode = "manual"
    sess = TwitterAIDialogSession(
        organization_id=organization_id,
        account_id=account_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        persona_id=persona_id,
        goal=goal,
        approval_mode=approval_mode,
        scratchpad={},
        next_check_at=datetime.now(timezone.utc),
    )
    db.add(sess)
    db.commit()
    return sess


def list_sessions(db: Session, organization_id: int) -> list[TwitterAIDialogSession]:
    return (
        db.query(TwitterAIDialogSession)
        .filter(TwitterAIDialogSession.organization_id == organization_id)
        .order_by(TwitterAIDialogSession.created_at.desc())
        .limit(500)
        .all()
    )


def get_session(db: Session, session_id: int) -> TwitterAIDialogSession | None:
    return db.get(TwitterAIDialogSession, int(session_id))


def update_session_state(db: Session, session_id: int, *, state: str | None = None, approval_mode: str | None = None) -> TwitterAIDialogSession | None:
    sess = get_session(db, session_id)
    if sess is None:
        return None
    if state in ("active", "paused", "stopped", "goal_reached", "failed"):
        sess.state = state
    if approval_mode in ("manual", "auto"):
        sess.approval_mode = approval_mode
    db.commit()
    return sess


async def think_next_response(db: Session, session_id: int) -> dict[str, Any]:
    """Сгенерировать черновик следующего ответа через LLM.

    Возвращает {ok, draft_id?, body?, status?, rationale?, reason_code?}.
    Не отправляет — только кладёт в TwitterAIDialogDraft (review_state='pending_review').
    Auto-send и реальный send_dm — в worker'е.
    """
    sess = get_session(db, session_id)
    if sess is None:
        return {"ok": False, "reason_code": "session_not_found"}
    if sess.state != "active":
        return {"ok": False, "reason_code": f"session_state_{sess.state}"}

    persona = db.get(TwitterAIPersona, sess.persona_id) if sess.persona_id else None
    lead = db.get(TwitterLead, sess.lead_id)
    if lead is None:
        return {"ok": False, "reason_code": "lead_missing"}

    # Берём messages: или по conversation_id, или по lead handle (fallback).
    messages: list[TwitterMessage] = []
    if sess.conversation_id:
        conv = db.get(TwitterConversation, sess.conversation_id)
        if conv is not None:
            messages = (
                db.query(TwitterMessage)
                .filter(TwitterMessage.conversation_id == conv.id)
                .order_by(TwitterMessage.sent_at.asc(), TwitterMessage.id.asc())
                .all()
            )

    system = _build_persona_block(persona)
    user = (
        f"ЦЕЛЬ СЕССИИ: {sess.goal}\n"
        f"ОПИСАНИЕ ЦЕЛИ: {_GOAL_DESCRIPTIONS.get(sess.goal, '')}\n\n"
        f"{_build_lead_block(lead)}\n\n"
        f"ПЕРЕПИСКА (последние сообщения):\n{_build_history_block(messages)}\n\n"
        f"{_OUTPUT_INSTRUCTION}"
    )

    try:
        parsed = await _ask_llm(db, system=system, user=user)
    except LLMError as e:
        return {"ok": False, "reason_code": "llm_error", "details": str(e)[:200]}

    body = (parsed.get("body") or "").strip()
    status = (parsed.get("status") or "continue").strip().lower()
    rationale = (parsed.get("rationale") or "").strip()

    if not body:
        return {"ok": False, "reason_code": "empty_response"}
    if status not in ("continue", "goal_reached", "refused", "silent"):
        status = "continue"

    # Сохраняем draft.
    draft = TwitterAIDialogDraft(
        session_id=sess.id,
        body=body,
        rationale=rationale or None,
        review_state="pending_review",
    )
    db.add(draft)

    sess.last_action_at = datetime.now(timezone.utc)
    sp = dict(sess.scratchpad or {})
    sp.setdefault("rationales", []).append({"at": sess.last_action_at.isoformat(), "rationale": rationale, "status": status})
    sess.scratchpad = sp

    if status == "goal_reached":
        sess.state = "goal_reached"
        sess.final_outcome = "qualified" if sess.goal == "qualify_lead" else (
            "call_booked" if sess.goal == "book_call" else (
                "info_shared" if sess.goal == "share_info" else "nurtured"
            )
        )
    elif status == "refused":
        sess.state = "stopped"
        sess.final_outcome = "refused"

    db.commit()
    return {"ok": True, "draft_id": draft.id, "body": body, "status": status, "rationale": rationale}


__all__ = [
    "_GOAL_DESCRIPTIONS",
    "create_session",
    "get_session",
    "list_sessions",
    "think_next_response",
    "update_session_state",
]
