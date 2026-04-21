"""Модерация поста и генерация комментария для натурального прогрева (короткие вызовы LLM)."""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from backend.services.ai_agent_llm import LLMError, complete_llm
from backend.services.ai_agent_service import cabinet_llm_ready_for_workspace
from backend.services.natural_warmup_stopwords import (
    get_effective_topic_stopwords,
    post_text_matches_topic_stopwords,
)
from backend.services.sequence_ai_comment import (
    _clean_model_comment,
    _resolve_llm_request,
    _run_awaitable_sync,
    _SYSTEM_PROMPT,
    _build_user_prompt,
)

logger = logging.getLogger(__name__)

# Сначала всегда эта проверка (до LLM): насилие, смерть, война, трагедия, агрессия и близкие темы.
_UNSAFE_RE = re.compile(
    r"\b("
    # English
    r"kill|killed|killing|killer|murder|murdered|suicide|raped?|rape\b|terror(ism|ist)?\b|terror\b|"
    r"genocide|gore|tortur|atrocity|massacre|war crime|warfare|invasion|shelling|bombing|"
    r"death|dead|dying|fatal|casualt|violence|abusive|abuse\b|tragedy|tragic|"
    r"holocaust|lynch|self[- ]harm|execution|behead|mass[- ]shoot|school[- ]shoot|"
    r"assault(ed)?\b|bloodshed|martyr"
    r"|"
    # Russian / shared roots (склонения: войне, трагедии, агрессии…)
    r"смерт\w*|убий\w*|убит\w*|зарезал\w*|застрел\w*|расстрел\w*|погиб\w*|жертв\w*|труп\w*|кровопролит\w*|"
    r"насили\w*|изнасил\w*|домогатель\w*|пытк\w*|казн\w*|эшафот\w*|"
    r"террор\w*|теракт\w*|экстремиз\w*|радикализ\w*|фашизм\w*|нацизм\w*|ксенофоб\w*|ненависть\s+к\s+\w+"
    r"|"
    r"войн\w*|военн\w*[\s\-]?операц\w*|оккупац\w*|нападен\w*|вторжен\w*|обстрел\w*|бомбард\w*|"
    r"ракетн\w*\s+удар\w*|снаряд\w*|миномёт\w*|"
    r"трагед\w*|трагич\w*|катастроф\w*|авиакатастроф\w*|чп\b|"
    r"агресси(я|и|ю|ей|ею|ёй)\b|агрессивн\w*\s+напад\w*|развязал\w*\s+войн\w*|"
    r"самоубий|суицид|повесил|сбросил\w*\s+с|выбросил\w*\s+с\s+балкон|"
    r"похорон|крематор|гроб|"
    r"взрыв|детон|заложник|захват\w*\s+заложник|"
    r"стрельб|перестрел|ножев|избил|избиен|драк[аеи]\s+с\s+кров|"
    r"кровь\b|искалеч|увеч|"
    r"насильственн|принудительн\w*\s+к\s+секс|"
    r"геноцид|этноцид|линчев|расправ"
    r")\b",
    re.I | re.UNICODE,
)

_MODERATION_SYSTEM = """Ты модератор коротких фрагментов постов из ленты Facebook.
Ответь РОВНО одним словом: UNSAFE или SAFE.

UNSAFE — если фрагмент про: насилие, убийство, смерть, трагедию, войну и жертв, агрессивные призывы, ненависть к группам людей, самоповреждение, сексуальное насилие, теракты, катастрофы с жертвами, жестокость к людям/животным.

SAFE — обычный контент: быт, юмор, еда, хобби, спорт без жести, питомцы, мнения без призывов к насилию, реклама, праздники.

Если сомневаешься — UNSAFE."""


def quick_keyword_unsafe(post_text: str) -> bool:
    t = (post_text or "").strip()
    if len(t) < 4:
        return False
    return bool(_UNSAFE_RE.search(t))


async def _moderate_async(
    *,
    provider: str,
    openai_key: str,
    google_key: str,
    model: str,
    excerpt: str,
) -> bool:
    """True = безопасно комментировать."""
    user = f"Фрагмент поста (до 4000 символов):\n---\n{excerpt[:4000]}\n---"
    text, _meta = await complete_llm(
        provider,
        openai_key=openai_key,
        google_key=google_key,
        model=model,
        system=_MODERATION_SYSTEM,
        user=user,
    )
    ans = (text or "").strip().upper()
    if re.search(r"\bUNSAFE\b", ans):
        return False
    if re.search(r"\bSAFE\b", ans):
        return True
    return False


def natural_warmup_commenting_llm_configured(db: Session) -> bool:
    """Нейрокомментарии: ключи OpenAI и/или Google (Gemini) — см. cabinet_llm_ready_for_workspace."""
    return cabinet_llm_ready_for_workspace(db)


def moderate_post_for_natural_warmup_sync(db: Session, post_text: str) -> tuple[bool, str]:
    """
    Можно ли оставлять дружелюбный комментарий под этим постом.

    Порядок: (1) стоп-слова насилие/смерть/война… (2) стоп-темы из БД (секс, эротика, религия…),
    (3) LLM. Второй элемент кортежа — код причины отказа (пустая строка, если можно комментировать).
    """
    if quick_keyword_unsafe(post_text):
        return False, "keyword"
    try:
        topic_words = get_effective_topic_stopwords(db)
        if post_text_matches_topic_stopwords(post_text, topic_words):
            return False, "topic_stopword"
    except Exception:
        logger.exception("natural_warmup topic stopwords")
        return False, "topic_stopword"
    excerpt = (post_text or "").strip()
    if len(excerpt) < 12:
        return False, "short_text"
    try:
        provider, openai_key, google_key, model = _resolve_llm_request(db, provider_override="")
    except LLMError as e:
        logger.warning("natural_warmup moderation: no LLM: %s", e)
        return False, "no_llm"

    async def _run() -> bool:
        return await _moderate_async(
            provider=provider,
            openai_key=openai_key,
            google_key=google_key,
            model=model,
            excerpt=excerpt,
        )

    try:
        ok = _run_awaitable_sync(_run())
        if ok:
            return True, ""
        return False, "llm_unsafe"
    except Exception:
        logger.exception("natural_warmup moderation LLM")
        return False, "llm_error"


def generate_natural_warmup_comment_sync(db: Session, post_text: str) -> str:
    """Короткий нейрокомментарий в контексте поста (1–2 фразы)."""
    hint = (
        "Очень коротко: 1–2 короткие фразы, дружелюбно и по делу, без пафоса и без хештегов. "
        "Не упоминай политику и спорные темы."
    )
    user_content = _build_user_prompt(
        post_text,
        display_name="",
        first_name="",
        hint=hint,
    )
    provider, openai_key, google_key, model = _resolve_llm_request(db, provider_override="")

    async def _complete() -> str:
        text, _meta = await complete_llm(
            provider,
            openai_key=openai_key,
            google_key=google_key,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user_content,
        )
        return _clean_model_comment(text)

    raw = _run_awaitable_sync(_complete())
    out = (raw or "").strip()
    if len(out) < 2:
        raise LLMError("Пустой ответ модели")
    if len(out) > 400:
        out = out[:400].rsplit(".", 1)[0] + "."
    return out


__all__ = [
    "generate_natural_warmup_comment_sync",
    "moderate_post_for_natural_warmup_sync",
    "natural_warmup_commenting_llm_configured",
    "quick_keyword_unsafe",
]
