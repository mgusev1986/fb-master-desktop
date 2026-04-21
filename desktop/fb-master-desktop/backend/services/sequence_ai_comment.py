"""ИИ-комментарий к первому посту в ленте профиля (режим агента / сценарии)."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import threading
from typing import Awaitable, TypeVar

from playwright.sync_api import Page
from sqlalchemy.orm import Session

from backend.services.ai_agent_llm import LLMError, complete_llm
from backend.services.ai_agent_service import load_ui_settings
from backend.services.cabinet_settings import effective_google_llm_api_key, effective_openai_api_key

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

_EXTRACT_POST_TEXT_JS = """({ mode, poolSize, pickIndex }) => {
  const main = document.querySelector('[role="main"]');
  if (!main) return '';
  function isTopLevelArticle(art) {
    let p = art.parentElement;
    while (p) {
      if (p.getAttribute && p.getAttribute('role') === 'article') return false;
      p = p.parentElement;
    }
    return true;
  }
  function isMeaningfulPost(text) {
    const head = text.slice(0, 160).toLowerCase();
    return !(
      head.includes('people you may know') ||
      head.includes('people you know') ||
      head.includes('возможные друзья') ||
      head.includes('рекомендуемые друзья') ||
      head.includes('знакомые вам') ||
      head.includes('suggested for you')
    );
  }
  const articles = Array.from(main.querySelectorAll('[role="article"]'));
  const blocks = [];
  for (const art of articles) {
    if (!isTopLevelArticle(art)) continue;
    const r = art.getBoundingClientRect();
    if (r.height < 22) continue;
    let t = (art.innerText || '').trim();
    t = t.replace(/\\s*\\n\\s*/g, '\\n').replace(/\\n{3,}/g, '\\n\\n');
    if (t.length < 8 || !isMeaningfulPost(t)) continue;
    blocks.push({ top: r.top + window.scrollY, text: t.slice(0, 12000), len: t.length });
  }
  blocks.sort((a, b) => a.top - b.top);
  const skipIntro = blocks.filter((b) => b.top > 64);
  const ordered = skipIntro.length ? skipIntro : blocks;
  if (!ordered.length) return '';
  const meaningful = ordered.filter((b) => b.len >= 12);
  const candidates = meaningful.length ? meaningful : ordered;
  const m = (mode || 'first').toLowerCase();
  if (m === 'random') {
    const cap = Math.max(1, Math.min(poolSize | 0, candidates.length));
    const pool = candidates.slice(0, cap);
    const idx = Math.max(0, Math.min(pickIndex | 0, pool.length - 1));
    const pick = pool[idx];
    return pick ? pick.text : '';
  }
  const pick = candidates[0];
  return pick ? pick.text : '';
}"""

_SYSTEM_PROMPT = """Ты помогаешь писать короткие комментарии под постами в Facebook (Meta).
Правила:
- 1–3 предложения, естественный разговорный тон, без канцелярита и рекламы.
- Ориентируйся на смысл и тон поста; если текста мало (например одно фото) — нейтральный доброжелательный отклик.
- Язык комментария совпадай с языком поста (русский пост → русский комментарий).
- Не используй хештеги. Не выдумывай личные факты об авторе.
- Верни ТОЛЬКО текст комментария, без кавычек вокруг и без префиксов вроде «Комментарий:»."""


def extract_timeline_post_text(
    page: Page,
    *,
    mode: str = "first",
    pool_size: int = 5,
    pick_index: int = 0,
    timeout_ms: int = 20_000,
) -> str:
    """Текст выбранного поста в ленте: первый сверху или случайный в пуле из верхних N."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=min(25_000, timeout_ms))
    except Exception:
        pass
    page.wait_for_timeout(900)
    pick_mode = (mode or "first").strip().lower()
    if pick_mode not in ("first", "random"):
        pick_mode = "first"
    pick_pool = max(1, min(int(pool_size or 5), 10))
    pick_idx = max(0, int(pick_index or 0))
    best = ""
    for _ in range(4):
        page.mouse.wheel(0, 420)
        page.wait_for_timeout(500)
        try:
            raw = page.evaluate(
                _EXTRACT_POST_TEXT_JS,
                {"mode": pick_mode, "poolSize": pick_pool, "pickIndex": pick_idx},
            )
        except Exception:
            logger.exception("extract_timeline_post_text")
            raw = ""
        chunk = (raw or "").strip() if isinstance(raw, str) else ""
        if len(chunk) > len(best):
            best = chunk
        if len(best) >= 12:
            break
    return best


_DETECT_FB_PROFILE_UI_LOCALE_JS = r"""() => {
  const L = (s) => (s || '').trim().toLowerCase();
  const htmlLang = L(document.documentElement && document.documentElement.getAttribute('lang'));
  if (htmlLang.startsWith('ru')) return 'ru';
  const og = document.querySelector('meta[property="og:locale"]');
  const ogLc = L(og && og.getAttribute('content'));
  if (ogLc.startsWith('ru')) return 'ru';
  if (htmlLang.length >= 2 && !htmlLang.startsWith('ru')) return 'non_ru';
  let t = '';
  try { t = (document.body && document.body.innerText) || ''; } catch (e) {}
  t = t.slice(0, 16000).toLowerCase();
  if (/\bдрузья\b/.test(t) && (/\bсообщени/.test(t) || /\bзаявк/.test(t) || /\bподписчик/.test(t) || /\bглавная\b/.test(t)))
    return 'ru';
  if (/\bfriends\b/.test(t) && /\bmessages\b/.test(t)) return 'non_ru';
  return 'unknown';
}"""


def infer_fb_profile_ui_dm_locale(page: Page) -> str:
    """
    Эвристика языка интерфейса Facebook на открытой странице профиля.

    Возвращает ``\"ru\"``, если похоже на русскоязычный UI, иначе ``\"en\"``
    (международный режим: генерировать ЛС на английском).
    """
    try:
        raw = page.evaluate(_DETECT_FB_PROFILE_UI_LOCALE_JS)
    except Exception:
        logger.debug("infer_fb_profile_ui_dm_locale evaluate failed", exc_info=True)
        return "en"
    tag = (raw or "").strip().lower()
    if tag == "ru":
        return "ru"
    return "en"


@functools.lru_cache(maxsize=1)
def _lingua_spoken_detector():
    """Детектор языка Lingua (все разговорные языки); None если пакет недоступен."""
    try:
        from lingua import LanguageDetectorBuilder

        return LanguageDetectorBuilder.from_all_spoken_languages().build()
    except Exception:
        logger.debug("lingua-language-detector unavailable", exc_info=True)
        return None


def _dm_locale_hint_lingua(blob: str, *, cyr_count: int, alpha: int) -> str | None:
    """
    Точное определение языка текста (русский vs украинский vs латиница/CJK и т.д.).

    Только русский → ``\"ru\"``; украинский, сербский, болгарский и прочие → ``\"en\"``
    (международное ЛС на английском по правилам продукта).
    """
    detector = _lingua_spoken_detector()
    if detector is None:
        return None
    t = (blob or "").strip()
    min_len = 26 if cyr_count >= 14 else 38
    if len(t) < min_len:
        return None
    try:
        from lingua import Language

        vals = detector.compute_language_confidence_values(t)
    except Exception:
        return None
    if not vals:
        return None
    top = vals[0]
    lang = top.language
    val = float(top.value)
    cyr_ratio = (cyr_count / alpha) if alpha else 0.0

    slavic_non_ru = frozenset(
        {
            Language.UKRAINIAN,
            Language.BELARUSIAN,
            Language.BULGARIAN,
            Language.SERBIAN,
            Language.MACEDONIAN,
            Language.BOSNIAN,
        }
    )

    if lang == Language.RUSSIAN:
        if val < 0.36:
            return None
        if cyr_ratio < 0.05 and alpha > 22:
            return None
        return "ru"

    if lang in slavic_non_ru and val >= 0.34:
        return "en"

    if val < 0.41:
        return None

    return "en"


def _script_letter_counts(text: str) -> tuple[int, int, int]:
    """Число букв: кириллица, CJK, остальное (латиница с диакритикой и т.д.)."""
    cyr = cjk = lat = 0
    for ch in text:
        if not ch.isalpha():
            continue
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            cyr += 1
        elif 0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF:
            cjk += 1
        else:
            lat += 1
    return cyr, cjk, lat


def _dm_locale_hint_langdetect(blob: str, *, cyr_count: int, alpha: int) -> str | None:
    """Определение языка по тексту (вьетнамский, англ., русский и т.д.)."""
    try:
        from langdetect import detect_langs
    except ImportError:
        logger.debug("langdetect not installed; skip content-based locale hint")
        return None
    t = (blob or "").strip()
    if len(t) < 55:
        return None
    try:
        langs = detect_langs(t)
    except Exception:
        return None
    if not langs:
        return None
    top = langs[0]
    if top.prob < 0.62:
        return None
    cyr_ratio = (cyr_count / alpha) if alpha else 0.0
    if top.lang == "ru":
        if cyr_ratio < 0.06:
            return None
        return "ru"
    return "en"


def _dm_locale_hint_script_counts(cyr: int, cjk: int, lat: int) -> str | None:
    """Запасной канал без langdetect: только скрипты."""
    alpha = cyr + cjk + lat
    if alpha < 28:
        return None
    if cyr >= 12 and cyr / alpha >= 0.28:
        return "ru"
    if cjk / alpha >= 0.07:
        return "en"
    if cyr / alpha <= 0.025 and lat >= 36:
        return "en"
    return None


def infer_dm_output_locale_for_ai_dm(
    page: Page,
    *,
    profile_content_sample: str = "",
    recipient_label: str = "",
) -> str:
    """
    Язык исходящего ИИ-ЛС: комбинирует (1) текст профиля/имени и (2) язык UI Facebook.

    Порядок сигналов по **контенту** (от более точного к запасному):

    1. **Lingua** — хорошо отделяет русский от украинского, сербского и др. кириллицы.
    2. **langdetect** — лёгкий fallback по всему тексту.
    3. **Скрипты** (кириллица / CJK / латиница) — если библиотеки недоступны или текст короткий.

    Если по контенту решить нельзя — эвристика UI (:func:`infer_fb_profile_ui_dm_locale`).
    """
    ui = infer_fb_profile_ui_dm_locale(page)
    parts: list[str] = []
    rl = (recipient_label or "").strip()
    if rl:
        parts.append(rl[:280])
    pc = (profile_content_sample or "").strip()
    if pc:
        parts.append(pc[:12000])
    blob = "\n".join(parts).strip()
    if len(blob) < 22:
        return ui
    cyr, cjk, lat = _script_letter_counts(blob)
    alpha = cyr + cjk + lat
    if alpha < 18:
        return ui
    lg = _dm_locale_hint_lingua(blob, cyr_count=cyr, alpha=alpha)
    if lg is not None:
        return lg
    ld = _dm_locale_hint_langdetect(blob, cyr_count=cyr, alpha=alpha)
    if ld is not None:
        return ld
    scr = _dm_locale_hint_script_counts(cyr, cjk, lat)
    if scr is not None:
        return scr
    return ui


def _clean_model_comment(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^Комментарий\s*:\s*", "", s, flags=re.I)
    if len(s) >= 2 and s[0] in '"«' and s[-1] in '"»':
        s = s[1:-1].strip()
    return s[:2000]


def _build_user_prompt(
    post_text: str,
    *,
    display_name: str,
    first_name: str,
    hint: str,
) -> str:
    lines = [
        f"Автор профиля (для тона, без фамильярности если неуместно): {display_name or first_name or 'человек'}",
        "",
        "Содержимое поста (фрагмент со страницы, может быть обрезано или с артефактами вёрстки):",
        "---",
        post_text[:8000],
        "---",
    ]
    h = (hint or "").strip()
    if h:
        lines.extend(["", "Пожелания к стилю комментария от оператора сценария:", h])
    lines.extend(["", "Напиши один готовый комментарий для публикации под этим постом."])
    return "\n".join(lines)


def _resolve_llm_request(
    db: Session, *, provider_override: str = ""
) -> tuple[str, str, str, str]:
    ui = load_ui_settings(db)
    prov = str(provider_override or ui.get("default_provider") or "gemini").lower().strip()
    if prov == "ollama":
        prov = "gemini"
    if prov not in ("openai", "gemini"):
        prov = "gemini"
    oa = effective_openai_api_key(db)
    gk = effective_google_llm_api_key(db)
    if prov == "openai" and not oa.strip():
        prov = "gemini"
    if prov == "gemini" and not gk.strip():
        prov = "openai"
    if prov == "openai" and not oa.strip():
        raise LLMError("Нет ключа OpenAI: задайте OPENAI_API_KEY или ключ в настройках кабинета")
    if prov == "gemini" and not gk.strip():
        raise LLMError("Нет ключа Google AI: задайте GOOGLE_API_KEY или ключ в настройках кабинета")
    if prov == "openai":
        model = ui.get("model_openai") or "gpt-4o-mini"
    else:
        model = ui.get("model_gemini") or "gemini-2.5-flash"
    return prov, oa, gk, str(model).strip()


async def _run_llm_completion(
    *,
    provider: str,
    openai_key: str,
    google_key: str,
    model: str,
    system: str,
    user_content: str,
) -> str:
    text, _meta = await complete_llm(
        provider,
        openai_key=openai_key,
        google_key=google_key,
        model=model,
        system=system,
        user=user_content,
    )
    return text


def _run_awaitable_sync(awaitable: Awaitable[_T]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    box: dict[str, _T] = {}
    errors: list[BaseException] = []

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(awaitable)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_runner, name="llm-sync-bridge", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return box["value"]


async def _complete_async(
    *,
    provider: str,
    openai_key: str,
    google_key: str,
    model: str,
    user_content: str,
) -> str:
    text = await _run_llm_completion(
        provider=provider,
        openai_key=openai_key,
        google_key=google_key,
        model=model,
        system=_SYSTEM_PROMPT,
        user_content=user_content,
    )
    return _clean_model_comment(text)


_DM_SYSTEM_PROMPT_BASE = """Ты помогаешь писать короткие личные сообщения в Facebook Messenger.
Правила:
- 1–4 предложения, естественный тон, без спама и без агрессивных продаж.
- Каждый раз формулируй по-новому: не копируй дословно опорный шаблон, сохраняй смысл, цель и уважительный стиль.
- Меняй структуру: порядок мыслей, первые слова, союзы и длину предложений — чтобы сообщения для разных людей не выглядели одним шаблоном.
- Обязательно закончи текст одним понятным вопросом к собеседнику. Последний значимый символ всего сообщения — знак вопроса ? (не точка и не многоточие).
- Не используй хештеги. Не выдумывай личные факты о человеке, которых нет во входных данных.
- Верни ТОЛЬКО текст сообщения, без кавычек вокруг и без префиксов вроде «Сообщение:» или Message:."""

_DM_SYSTEM_LOCALE_RU = """
- ЯЗЫК (режим русскоязычного Facebook у получателя): итоговое сообщение MUST match the language of the reference template / опорный шаблон. Если шаблон на русском — строго русский. Если на английском — английский. Если на другом языке — тот же язык. Не переводи смысл шаблона на другой язык без необходимости."""

_DM_SYSTEM_LOCALE_EN = """
- LANGUAGE (Facebook UI for this recipient is NOT Russian): write the ENTIRE message ONLY in natural English. If the reference template or operator hints are in Russian (or another non-English language), translate the intent, tone and goal into fluent conversational English. Do NOT leave Russian (or other non-English) sentences in the final message."""


def _dm_system_prompt(dm_output_locale: str) -> str:
    loc = (dm_output_locale or "ru").strip().lower()
    if loc == "en":
        return _DM_SYSTEM_PROMPT_BASE + _DM_SYSTEM_LOCALE_EN
    return _DM_SYSTEM_PROMPT_BASE + _DM_SYSTEM_LOCALE_RU


def _clean_dm_text(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^(Сообщение|Message)\s*:\s*", "", s, flags=re.I)
    if len(s) >= 2 and s[0] in '"«' and s[-1] in '"»':
        s = s[1:-1].strip()
    return s[:4000]


def _build_dm_user_prompt(
    *,
    display_name: str,
    first_name: str,
    hint: str,
    reference_message: str,
    profile_context: str,
    dm_output_locale: str = "ru",
) -> str:
    lines = [
        f"Получатель (для обращения): {display_name or first_name or 'человек'}",
        f"Имя для приветствия: {first_name or display_name or '—'}",
    ]
    loc = (dm_output_locale or "ru").strip().lower()
    ref = (reference_message or "").strip()
    if ref:
        if loc == "en":
            ref_note = (
                "^^^ The reference above may be in Russian or another language — translate its meaning into English "
                "for the final DM (natural English only)."
            )
        else:
            ref_note = (
                "^^^ IMPORTANT: Write the message in the SAME language as this reference template above! "
                "Do NOT switch to a different language. / Пиши сообщение СТРОГО на том же языке, что и опорный шаблон выше! "
                "НЕ переключайся на другой язык."
            )
        lines.extend(
            [
                "",
                "Опорный шаблон / Reference template (перефразируй свободно, та же цель и контекст):",
                "---",
                ref[:6000],
                "---",
                ref_note,
            ]
        )
    ctx = (profile_context or "").strip()
    if ctx:
        lines.extend(
            [
                "",
                "Фрагмент с публичной ленты профиля (для тона; не цитируй дословно длинными кусками):",
                "---",
                ctx[:4000],
                "---",
            ]
        )
    h = (hint or "").strip()
    if h:
        lines.extend(["", "Пожелания оператора к стилю и содержанию:", h])
    if loc == "en":
        tail = [
            "",
            "Напиши одно новое личное сообщение — уникальную формулировку для этого человека.",
            "Проверь: финальная фраза — вопрос, и сам текст заканчивается символом ?",
            "НАПОМИНАНИЕ: интерфейс Facebook у получателя не русский — итоговый текст ТОЛЬКО на английском.",
        ]
    else:
        tail = [
            "",
            "Напиши одно новое личное сообщение — уникальную формулировку для этого человека.",
            "Проверь: финальная фраза — вопрос, и сам текст заканчивается символом ?",
            "НАПОМИНАНИЕ: язык сообщения = язык опорного шаблона. / REMINDER: output language = reference template language.",
        ]
    lines.extend(tail)
    return "\n".join(lines)


async def _complete_dm_async(
    *,
    provider: str,
    openai_key: str,
    google_key: str,
    model: str,
    user_content: str,
    system: str,
) -> str:
    text = await _run_llm_completion(
        provider=provider,
        openai_key=openai_key,
        google_key=google_key,
        model=model,
        system=system,
        user_content=user_content,
    )
    return _clean_dm_text(text)


def generate_profile_post_comment_sync(
    db: Session,
    *,
    post_text: str,
    display_name: str,
    first_name: str,
    hint: str = "",
    provider_override: str = "",
) -> str:
    """Синхронная обёртка для воркера сценариев (Playwright sync)."""
    user_content = _build_user_prompt(
        post_text,
        display_name=display_name,
        first_name=first_name,
        hint=hint,
    )
    provider, openai_key, google_key, model = _resolve_llm_request(
        db,
        provider_override=provider_override,
    )
    return _run_awaitable_sync(
        _complete_async(
            provider=provider,
            openai_key=openai_key,
            google_key=google_key,
            model=model,
            user_content=user_content,
        )
    )


def generate_dm_message_sync(
    db: Session,
    *,
    display_name: str,
    first_name: str,
    hint: str = "",
    reference_message: str = "",
    profile_context: str = "",
    provider_override: str = "",
    dm_output_locale: str = "ru",
) -> str:
    """Синхронная генерация текста личного сообщения для шага сценария send_dm.

    ``dm_output_locale``: ``\"ru\"`` — язык как у опорного шаблона (русский Facebook у получателя);
    ``\"en\"`` — только английский (интерфейс FB не русский), русский шаблон переводится по смыслу.
    """
    loc = (dm_output_locale or "ru").strip().lower()
    if loc not in ("ru", "en"):
        loc = "ru"
    user_content = _build_dm_user_prompt(
        display_name=display_name,
        first_name=first_name,
        hint=hint,
        reference_message=reference_message,
        profile_context=profile_context,
        dm_output_locale=loc,
    )
    provider, openai_key, google_key, model = _resolve_llm_request(
        db,
        provider_override=provider_override,
    )
    return _run_awaitable_sync(
        _complete_dm_async(
            provider=provider,
            openai_key=openai_key,
            google_key=google_key,
            model=model,
            user_content=user_content,
            system=_dm_system_prompt(loc),
        )
    )


__all__ = [
    "extract_timeline_post_text",
    "infer_fb_profile_ui_dm_locale",
    "infer_dm_output_locale_for_ai_dm",
    "generate_dm_message_sync",
    "generate_profile_post_comment_sync",
]
