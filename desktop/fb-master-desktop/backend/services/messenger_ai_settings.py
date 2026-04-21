"""Глобальные настройки AI-ассистента Messenger (таблица settings)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from backend.models import Setting

SK_PROMPT_ADDON = "messenger_ai_prompt_addon"
SK_MATERIALS = "messenger_ai_materials"
SK_DELAY_MIN = "messenger_ai_delay_min_sec"
SK_DELAY_MAX = "messenger_ai_delay_max_sec"
SK_TEMPERATURE = "messenger_ai_temperature"
SK_MAX_TOKENS = "messenger_ai_max_tokens"
SK_MAX_REPLY_STEPS = "messenger_ai_max_reply_steps"
SK_RAG_ENABLED = "messenger_ai_rag_enabled"

DEFAULT_DELAY_MIN_SEC = 12.0
DEFAULT_DELAY_MAX_SEC = 55.0
DEFAULT_TEMPERATURE = 0.82
DEFAULT_MAX_TOKENS = 720
# 0 = без лимита (сколько угодно ответов AI в одном чате)
DEFAULT_MAX_REPLY_STEPS = 25

MESSENGER_AI_BASE_SYSTEM = """Ты ведёшь личную переписку в Facebook Messenger от имени живого человека — предпринимателя с большим опытом в продажах, переговорах, сетевом маркетинге, инвестициях и маркетинге. Твой стиль: спокойный профессионал, без канцелярита и без шаблонных фраз бота. Короткие абзацы, естественные связки, иногда одно уточняющее вопросение — как в обычном мессенджере.

Задачи по приоритету:
1) Поддерживать доверие: слушать, отражать смысл слов собеседника, не давить.
2) Мягко выяснять контекст: чем человек занимается, что сейчас важно, открыт ли к новой информации или к диалогу о сотрудничестве (без допроса — через живой интерес).
3) Когда уместно — предложить логичный следующий шаг: короткий созвон (Zoom / Google Meet) или «посмотреть материал» (ссылки ниже в блоке «Ресурсы»), если человек сам проявляет интерес или задаёт вопросы.
4) Не обещать того, чего нет; не выдумывать факты о продукте; не обходить возражения агрессией.

По тону текста в reply_text: как у человека в мессенджере — чаще 1–4 коротких абзаца, без «воды»; эмодзи редко.
Если собеседник явно не заинтересован или просит не писать — вежливо согласись и заверши без новых предложений.

Структура ответа задаётся отдельным блоком ниже (JSON) — следуй ему буквально."""

MESSENGER_AI_JSON_SCHEMA = """Формат ответа: строго один JSON-объект (без markdown, без текста до или после).

Поля:
- "reply_text" (string): текст сообщения, которое уйдёт в чат; без префиксов «Ассистент:», без обёртки в кавычки всего сообщения.
- "crm_stage" (string или null): если по смыслу переписки нужно обновить стадию контакта в CRM — укажи ровно один slug из списка; иначе null.
- "crm_rationale" (string): кратко для журнала CRM, почему выбрана стадия (1–2 предложения); если crm_stage null — можно пустая строка.

Допустимые slug для crm_stage (только они, без выдумок): {stage_slugs}

Пример: {{"reply_text": "…", "crm_stage": null, "crm_rationale": ""}}"""

MESSENGER_AI_PROACTIVE_SYSTEM_APPEND = """Сейчас особый случай: отложенное напоминание из CRM («написать позже»).
Тебе нужно сформулировать одно новое сообщение от нас — это может быть первое сообщение после паузы или продолжение диалога, даже если последняя строка в истории была от нас.
Не извиняйся чрезмерно; не обвиняй собеседника; пиши естественно, по-человечески. Соблюдай блок «Задание от владельца» в пользовательском сообщении."""


def _get(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(Setting, key)
    return row.value if row else default


def _upsert(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def load_messenger_ai_settings(db: Session) -> dict[str, Any]:
    addon = _get(db, SK_PROMPT_ADDON, "") or ""
    materials_raw = _get(db, SK_MATERIALS, "")
    if isinstance(materials_raw, list):
        materials_lines = [str(x).strip() for x in materials_raw if str(x).strip()]
    elif isinstance(materials_raw, str) and materials_raw.strip():
        try:
            data = json.loads(materials_raw)
            materials_lines = (
                [str(x).strip() for x in data if str(x).strip()]
                if isinstance(data, list)
                else [materials_raw.strip()]
            )
        except json.JSONDecodeError:
            materials_lines = [
                ln.strip() for ln in materials_raw.splitlines() if ln.strip()
            ]
    else:
        materials_lines = []

    dmin = _get(db, SK_DELAY_MIN, DEFAULT_DELAY_MIN_SEC)
    dmax = _get(db, SK_DELAY_MAX, DEFAULT_DELAY_MAX_SEC)
    try:
        delay_min = float(dmin)
    except (TypeError, ValueError):
        delay_min = DEFAULT_DELAY_MIN_SEC
    try:
        delay_max = float(dmax)
    except (TypeError, ValueError):
        delay_max = DEFAULT_DELAY_MAX_SEC
    if delay_min < 0:
        delay_min = 0.0
    if delay_max < delay_min:
        delay_max = delay_min

    temp = _get(db, SK_TEMPERATURE, DEFAULT_TEMPERATURE)
    try:
        temperature = float(temp)
    except (TypeError, ValueError):
        temperature = DEFAULT_TEMPERATURE
    temperature = max(0.0, min(2.0, temperature))

    mt = _get(db, SK_MAX_TOKENS, DEFAULT_MAX_TOKENS)
    try:
        max_tokens = int(mt)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    max_tokens = max(80, min(4096, max_tokens))

    mrs = _get(db, SK_MAX_REPLY_STEPS, DEFAULT_MAX_REPLY_STEPS)
    try:
        max_reply_steps = int(mrs)
    except (TypeError, ValueError):
        max_reply_steps = DEFAULT_MAX_REPLY_STEPS
    max_reply_steps = max(0, min(500, max_reply_steps))

    rag_raw = _get(db, SK_RAG_ENABLED, False)
    if isinstance(rag_raw, str):
        rag_enabled = rag_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        rag_enabled = bool(rag_raw)

    return {
        "prompt_addon": str(addon).strip(),
        "materials_lines": materials_lines,
        "delay_min_sec": delay_min,
        "delay_max_sec": delay_max,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "max_reply_steps": max_reply_steps,
        "rag_enabled": rag_enabled,
    }


def save_messenger_ai_settings(
    db: Session,
    *,
    prompt_addon: str = "",
    materials_text: str = "",
    delay_min_sec: float | None = None,
    delay_max_sec: float | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_reply_steps: int | None = None,
    rag_enabled: bool | None = None,
) -> None:
    _upsert(db, SK_PROMPT_ADDON, (prompt_addon or "").strip()[:12000])
    lines = [ln.strip() for ln in (materials_text or "").splitlines() if ln.strip()][:80]
    _upsert(db, SK_MATERIALS, lines)
    if delay_min_sec is not None:
        _upsert(db, SK_DELAY_MIN, float(delay_min_sec))
    if delay_max_sec is not None:
        _upsert(db, SK_DELAY_MAX, float(delay_max_sec))
    if temperature is not None:
        _upsert(db, SK_TEMPERATURE, float(temperature))
    if max_tokens is not None:
        _upsert(db, SK_MAX_TOKENS, int(max_tokens))
    if max_reply_steps is not None:
        _upsert(db, SK_MAX_REPLY_STEPS, int(max_reply_steps))
    if rag_enabled is not None:
        _upsert(db, SK_RAG_ENABLED, bool(rag_enabled))


def build_messenger_ai_system_prompt(
    db: Session,
    *,
    peer_name: str,
    extra_per_chat: str,
    memory_context: str | None = None,
) -> str:
    from backend.services.crm_stages_registry import stage_slugs_ordered

    ui = load_messenger_ai_settings(db)
    slugs = stage_slugs_ordered(db)
    slug_line = ", ".join(slugs) if slugs else "(нет стадий в CRM — всегда crm_stage: null)"
    parts = [MESSENGER_AI_BASE_SYSTEM]
    parts.append(MESSENGER_AI_JSON_SCHEMA.format(stage_slugs=slug_line))
    if ui["materials_lines"]:
        parts.append(
            "Ресурсы и ссылки (используй только когда уместно, по одной за раз, без спама):\n"
            + "\n".join(f"— {ln}" for ln in ui["materials_lines"])
        )
    if ui["prompt_addon"]:
        parts.append("Контекст бизнеса и твои указания владельца кабинета:\n" + ui["prompt_addon"])
    mem = (memory_context or "").strip()
    if mem:
        parts.append(mem[:12000])
    hint = (peer_name or "").strip() or "Собеседник"
    parts.append(f"Имя/подпись собеседника в чате (для тона, не выдумывай факты): {hint}")
    per = (extra_per_chat or "").strip()
    if per:
        parts.append("Доп. инструкции только для этого чата:\n" + per[:4000])
    return "\n\n".join(parts)


def build_messenger_ai_proactive_user_payload(msgs: list, *, owner_prompt: str) -> str:
    """История + текст задачи для отложенного первого/возобновляющего сообщения."""
    lines: list[str] = [
        "Ниже история переписки (Собеседник / Мы). Последняя строка может быть от нас — всё равно нужно ОДНО новое исходящее сообщение.",
        "Верни один JSON-объект по схеме из системной инструкции (поля reply_text, crm_stage, crm_rationale).",
        "",
    ]
    for m in msgs[-80:]:
        role = "Собеседник" if m.direction == "in" else "Мы"
        body = (m.body or "").strip()
        if not body:
            continue
        lines.append(f"{role}: {body}")
    lines.append("")
    op = (owner_prompt or "").strip()
    if not op:
        op = "Коротко возобнови контакт: напомни о договорённости списаться и мягко предложи продолжить разговор."
    lines.append("Задание от владельца к этому напоминанию (соблюдай смысл и тон):")
    lines.append(op[:4000])
    lines.append("")
    lines.append("reply_text — только готовый текст для отправки в Messenger, без преамбул для модели.")
    return "\n".join(lines)


def build_messenger_ai_user_payload(msgs: list) -> str:
    """msgs: ORM Message ordered by id asc."""
    lines: list[str] = [
        "Ниже история переписки (Собеседник / Мы). Последняя реплика — от собеседника.",
        "Верни один JSON-объект по схеме из системной инструкции (поля reply_text, crm_stage, crm_rationale).",
        "",
    ]
    for m in msgs[-80:]:
        role = "Собеседник" if m.direction == "in" else "Мы"
        body = (m.body or "").strip()
        if not body:
            continue
        lines.append(f"{role}: {body}")
    lines.append("")
    lines.append("Не повторяй дословно последнюю реплику собеседника. reply_text — только готовый текст для отправки.")
    return "\n".join(lines)


def messenger_ai_steps_exceeded(steps_used: int, max_steps: int) -> bool:
    if int(max_steps) <= 0:
        return False
    return int(steps_used or 0) >= int(max_steps)


def transcript_fingerprint_from_messages(msgs: list) -> str:
    parts: list[str] = []
    for m in msgs:
        parts.append(f"{m.direction}\t{(m.body or '').strip()}")
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:64]
