"""Пакетная генерация черновиков AI (LLM + first touch), настройки, флаг занятости."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from backend.config import LOG_LLM_DEBUG
from backend.database import SessionLocal
from backend.models import AIAgentRun, Person, Setting
from backend.services.ai_agent_llm import LLMError, PREVIEW_LIMIT, complete_llm
from backend.services.cabinet_settings import effective_google_llm_api_key, effective_openai_api_key
from backend.services.first_touch_templates import pick_first_touch_templates
from backend.services.message_template_render import (
    apply_template_placeholders,
    placeholder_kwargs_from_person,
    resolve_template_variant,
)
from backend.services.throttle import is_valid_throttle_preset, resolve_delay

logger = logging.getLogger(__name__)

SETTING_KEYS = {
    "default_provider": "ai_default_provider",
    "model_openai": "ai_model_openai",
    "model_gemini": "ai_model_gemini",
    "prompt_dm": "ai_system_prompt_dm",
    "prompt_comment": "ai_system_prompt_comment",
    "addressing_dm": "ai_prompt_addressing_dm",
    "addressing_comment": "ai_prompt_addressing_comment",
}

DEFAULT_PROMPT_DM = """Ты помощник для переписки в Facebook Messenger.
Пиши естественно, кратко, без канцелярита. Учитывай язык и тон собеседника из контекста.
Не обещай то, чего нет. Не выдавай себя за бота.
Сгенерируй один готовый черновик ответа (без пояснений до и после)."""

DEFAULT_PROMPT_COMMENT = """Ты помощник для комментариев под постами в Facebook.
Комментарий должен быть уместным, по делу, не похожим на спам, 1–4 предложения.
Учитывай язык поста. Сгенерируй один готовый текст комментария (без пояснений до и после)."""

DEFAULT_ADDRESSING_DM = """Обращение к человеку по имени:
— Если во входном тексте в скобках указано, как звать собеседника — например (имя: Анна), (полное имя: Анна Петрова) или просто (Анна) — используй это в черновике ответа, если уместно.
— Если есть блок [Контакт в базе: …] — это имя/подпись из CRM; используй для обращения, если пользователь не задал другое в скобках.
— В тексте могут быть маркеры {{full_name}} и {{first_name}} — они уже подставлены из карточки контакта; используй их смысл в черновике.
— Если ни скобок, ни подходящего имени нет — не выдумывай имя; допустимо нейтральное обращение без имени."""

DEFAULT_ADDRESSING_COMMENT = """Обращение по имени в комментарии:
— Если в задании в скобках указано (имя: …), (полное имя: …) или иначе явно дано имя — можно использовать в тексте, если это уместно для тона комментария.
— Блок [Контакт в базе: …] — подсказка из CRM.
— Маркеры {{full_name}} / {{first_name}} в тексте уже подставлены из базы.
— Не придумывай имя, если оно не задано."""

DEFAULT_MODEL_OPENAI = "gpt-4o-mini"
DEFAULT_MODEL_GEMINI = "gemini-2.5-flash"

BATCH_MAX_PEOPLE = 200

_ai_lock = threading.Lock()
_ai_running = False


def ai_agent_busy() -> bool:
    with _ai_lock:
        return _ai_running


def try_begin_ai_agent() -> bool:
    global _ai_running
    with _ai_lock:
        if _ai_running:
            return False
        _ai_running = True
        return True


def end_ai_agent() -> None:
    global _ai_running
    with _ai_lock:
        _ai_running = False


def get_setting(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def cabinet_llm_ready_for_workspace(db: Session) -> bool:
    """LLM доступен при наличии ключа OpenAI и/или Google (Gemini) в кабинете или .env."""
    return bool(
        effective_openai_api_key(db).strip() or effective_google_llm_api_key(db).strip()
    )


def load_ui_settings(db: Session) -> dict[str, Any]:
    return {
        "default_provider": get_setting(db, SETTING_KEYS["default_provider"], "gemini"),
        "model_openai": get_setting(db, SETTING_KEYS["model_openai"], DEFAULT_MODEL_OPENAI),
        "model_gemini": get_setting(db, SETTING_KEYS["model_gemini"], DEFAULT_MODEL_GEMINI),
        "prompt_dm": get_setting(db, SETTING_KEYS["prompt_dm"], DEFAULT_PROMPT_DM),
        "prompt_comment": get_setting(db, SETTING_KEYS["prompt_comment"], DEFAULT_PROMPT_COMMENT),
        "addressing_dm": get_setting(db, SETTING_KEYS["addressing_dm"], DEFAULT_ADDRESSING_DM),
        "addressing_comment": get_setting(
            db, SETTING_KEYS["addressing_comment"], DEFAULT_ADDRESSING_COMMENT
        ),
    }


def throttle_preset_for_ai(db: Session) -> str:
    p = get_setting(db, "throttle_preset", "slow")
    return p if is_valid_throttle_preset(p) else "slow"


async def run_single_llm(
    db: Session,
    *,
    context_type: str,
    provider: str,
    body_rendered: str,
    person_row: Person | None,
    address_as: str,
) -> AIAgentRun:
    ui = load_ui_settings(db)
    prov = (provider or ui["default_provider"] or "gemini").lower().strip()
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
        model = ui["model_openai"] or DEFAULT_MODEL_OPENAI
    else:
        model = ui["model_gemini"] or DEFAULT_MODEL_GEMINI

    base_system = ui["prompt_dm"] if context_type == "dm" else ui["prompt_comment"]
    addr_extra = (ui["addressing_dm"] if context_type == "dm" else ui["addressing_comment"]) or ""
    addr_extra = addr_extra.strip()
    system = f"{base_system}\n\n{addr_extra}" if addr_extra else base_system

    meta_lines: list[str] = []
    if person_row:
        crm_name = (person_row.display_name or person_row.first_name or "").strip()
        if crm_name:
            meta_lines.append(f"[Контакт в базе: {crm_name}]")
    addr_manual = (address_as or "").strip()
    if addr_manual:
        meta_lines.append(
            f"[Обращаться к собеседнику так: {addr_manual} — имя или полное имя, как вы задали]"
        )
    user_msg = ("\n".join(meta_lines) + "\n\n" + body_rendered) if meta_lines else body_rendered

    if LOG_LLM_DEBUG:
        logger.info(
            "AI generate: provider=%s model=%s ctx=%s user_len=%s",
            prov,
            model,
            context_type,
            len(user_msg),
        )

    pid: int | None = person_row.id if person_row else None

    t0 = time.perf_counter()
    run = AIAgentRun(
        provider=prov,
        model=model,
        context_type=context_type,
        person_id=pid,
        outcome="error",
        input_preview=user_msg[:PREVIEW_LIMIT],
        output_preview=None,
    )
    db.add(run)
    db.flush()

    try:
        text, meta = await complete_llm(
            prov,
            openai_key=oa,
            google_key=gk,
            model=model,
            system=system,
            user=user_msg,
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
    finally:
        run.duration_ms = int((time.perf_counter() - t0) * 1000)

    return run


def save_first_touch_run(
    db: Session,
    *,
    person_row: Person,
    output_text: str,
    input_note: str,
) -> None:
    run = AIAgentRun(
        provider="template",
        model="first-touch-en",
        context_type="first_touch",
        person_id=person_row.id,
        outcome="success",
        error=None,
        input_preview=input_note[:PREVIEW_LIMIT],
        output_preview=output_text[:PREVIEW_LIMIT],
        tokens_prompt=None,
        tokens_completion=None,
        duration_ms=0,
    )
    db.add(run)


async def ai_batch_worker(
    person_ids: list[int],
    context_type: str,
    provider_override: str,
    context_template: str,
    *,
    job_mode: str = "custom",
    variants_per_person: int = 1,
) -> None:
    """custom — LLM + шаблон; first_touch — готовые EN-шаблоны с {{first_name}}, без API."""
    db = SessionLocal()
    try:
        if context_type not in ("dm", "comment"):
            return
        preset = throttle_preset_for_ai(db)
        job_mode = (job_mode or "custom").strip().lower()
        variants_per_person = max(1, min(variants_per_person, 10))

        if job_mode == "first_touch":
            if context_type != "dm":
                return
            rng = random.Random()
            global_i = 0
            prev_pid: int | None = None
            for pid in person_ids:
                person_row = db.get(Person, pid)
                if not person_row:
                    continue
                templates_pick = pick_first_touch_templates(variants_per_person, rng=rng)
                for vi, tpl in enumerate(templates_pick):
                    if global_i > 0:
                        act = "send_dm" if prev_pid is not None and pid == prev_pid else "next_person"
                        lo, hi = resolve_delay(act, preset=preset)
                        await asyncio.sleep(random.uniform(lo, hi))
                    text = apply_template_placeholders(
                        tpl, **placeholder_kwargs_from_person(person_row)
                    ).strip()
                    if not text:
                        continue
                    note = f"[Первое касание EN, вариант {vi + 1}/{variants_per_person}]"
                    try:
                        save_first_touch_run(
                            db,
                            person_row=person_row,
                            output_text=text,
                            input_note=note,
                        )
                        db.commit()
                    except Exception:
                        logger.exception("first_touch save failed person_id=%s", pid)
                        db.rollback()
                    global_i += 1
                prev_pid = pid
            return

        prov0 = (provider_override or "").strip().lower()
        if prov0 == "ollama":
            prov0 = ""
        oa = effective_openai_api_key(db)
        gk = effective_google_llm_api_key(db)
        ui = load_ui_settings(db)
        if prov0 not in ("openai", "gemini"):
            prov0 = str(ui.get("default_provider") or "gemini").strip().lower()
        if prov0 == "ollama":
            prov0 = "gemini"
        if prov0 not in ("openai", "gemini"):
            prov0 = "gemini"
        if prov0 == "openai" and not oa.strip():
            prov0 = "gemini"
        if prov0 == "gemini" and not gk.strip():
            prov0 = "openai"
        if prov0 == "openai" and not oa.strip():
            return
        if prov0 == "gemini" and not gk.strip():
            return

        for i, pid in enumerate(person_ids):
            if i > 0:
                lo, hi = resolve_delay("next_person", preset=preset)
                await asyncio.sleep(random.uniform(lo, hi))
            person_row = db.get(Person, pid)
            if not person_row:
                continue
            single_tpl = resolve_template_variant(
                context_template, db=None, template_row=None
            )
            body_rendered = apply_template_placeholders(
                single_tpl, **placeholder_kwargs_from_person(person_row)
            )
            if not body_rendered.strip():
                continue
            try:
                await run_single_llm(
                    db,
                    context_type=context_type,
                    provider=prov0,
                    body_rendered=body_rendered,
                    person_row=person_row,
                    address_as="",
                )
                db.commit()
            except Exception:
                logger.exception("AI batch item failed person_id=%s", pid)
                db.rollback()
    finally:
        db.close()
