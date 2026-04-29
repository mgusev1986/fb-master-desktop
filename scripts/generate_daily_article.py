#!/usr/bin/env python3
"""Авто-генератор статей для блога SOCMASTER (Gemini API).

Запуск:
    python3 scripts/generate_daily_article.py [--dry-run] [--topic SLUG]

Берёт следующую неопубликованную тему из data/blog/_topic_queue.json,
вызывает Gemini для генерации статьи (~1800 слов с FAQ, JSON-LD, mid-CTA),
сохраняет в data/blog/posts/<slug>.json со статусом 'published'.

Запускается через systemd timer 2 раза в день (09:00 и 19:00 МСК).
Логи в data/blog/generation.log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Добавляем корень проекта в PYTHONPATH (скрипт запускается из любого CWD)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# Настройка логирования
_LOG_DIR = _PROJECT_ROOT / "data" / "blog"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "generation.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("blog_generator")


# ─── Загрузка master-промпта ──────────────────────────────────────────────

def load_master_prompt() -> str:
    """Читает scripts/blog_master_prompt.md."""
    p = _PROJECT_ROOT / "scripts" / "blog_master_prompt.md"
    if not p.is_file():
        raise FileNotFoundError(f"Master prompt не найден: {p}")
    return p.read_text(encoding="utf-8")


# ─── Очередь тем ──────────────────────────────────────────────────────────

def load_topic_queue() -> list[dict]:
    """Возвращает список тем из data/blog/_topic_queue.json."""
    p = _PROJECT_ROOT / "data" / "blog" / "_topic_queue.json"
    if not p.is_file():
        raise FileNotFoundError(f"Topic queue не найден: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("topics", [])


def published_slugs() -> set[str]:
    """Set всех slug'ов из data/blog/posts/*.json."""
    posts_dir = _PROJECT_ROOT / "data" / "blog" / "posts"
    if not posts_dir.is_dir():
        return set()
    return {p.stem for p in posts_dir.glob("*.json")}


def pick_next_topic(force_slug: str | None = None) -> dict | None:
    """Выбирает следующую тему: первая из очереди, у которой нет файла-публикации.
    Если force_slug задан — возвращает именно эту тему (для отладки).
    """
    queue = load_topic_queue()
    if force_slug:
        for t in queue:
            if t["slug"] == force_slug:
                return t
        logger.error("Тема %s не найдена в queue", force_slug)
        return None

    done = published_slugs()
    for t in queue:
        if t["slug"] not in done:
            return t
    logger.warning("Очередь тем исчерпана — все %d тем опубликованы", len(queue))
    return None


# ─── Gemini API ────────────────────────────────────────────────────────────

async def generate_article_via_gemini(topic: dict, master_prompt: str) -> dict:
    """Вызывает Gemini для генерации статьи. Возвращает dict с полями статьи.

    Raises LLMError при ошибке.
    """
    from backend.database import SessionLocal
    from backend.services.ai_agent_llm import (
        complete_gemini_conversation_with_key_rotation,
    )
    from backend.services.cabinet_settings import google_llm_api_key_list

    db = SessionLocal()
    try:
        keys = google_llm_api_key_list(db)
    finally:
        db.close()

    if not keys:
        raise RuntimeError("Не настроен GOOGLE_API_KEY в .env или в админке")

    user_prompt = f"""Напиши статью на тему «{topic['title']}» для Базы знаний SOCMASTER.

Категория: {topic['category']}
Slug (заранее задан): {topic['slug']}

Краткая инструкция к содержанию: {topic.get('brief', 'Раскрой тему практически и применимо к бизнесу.')}

Внутренние ссылки, которые рекомендуется использовать (выбери 1–2 наиболее релевантные):
- /blog/kak-poluchat-lidy-iz-socsetey-bez-reklamy
- /blog/avtomatizaciya-facebook-dlya-biznesa
- /blog/instagram-dlya-poiska-klientov
- /blog/linkedin-dlya-b2b-prodazh
- /blog/ai-v-prodazhah

ВАЖНО:
- Верни ТОЛЬКО валидный JSON, без markdown ```json``` обёртки, без пояснений до и после.
- slug в JSON должен быть точно «{topic['slug']}» (используй его как есть).
- category в JSON должна быть точно «{topic['category']}».
- content — HTML без <html>/<body> wrapper.
"""

    # Используем Gemini Pro (более качественный текст)
    text, meta = await complete_gemini_conversation_with_key_rotation(
        keys,
        "gemini-2.0-flash-exp",  # быстрая, но достаточно качественная модель
        master_prompt,
        [("user", user_prompt)],
        timeout_s=180.0,
    )
    logger.info("Gemini ответил: %d chars, key_index=%s", len(text), meta.get("gemini_api_key_index"))
    return parse_json_response(text)


def parse_json_response(text: str) -> dict:
    """Парсит JSON-ответ Gemini, очищая возможные markdown-обёртки."""
    text = text.strip()
    # Убираем ```json ... ``` если есть
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Не удалось распарсить JSON ответ: %s\nFirst 500 chars: %s", e, text[:500])
        raise


# ─── Валидация и сохранение ───────────────────────────────────────────────

REQUIRED_FIELDS = [
    "slug", "title", "meta_title", "meta_description", "category",
    "tags", "excerpt", "audience", "what_youll_learn",
    "reading_time_minutes", "content", "faq",
]


def validate_article(article: dict) -> list[str]:
    """Возвращает список ошибок валидации (пустой = OK)."""
    errors = []
    for f in REQUIRED_FIELDS:
        if f not in article:
            errors.append(f"missing field: {f}")
    if not errors:
        if not isinstance(article.get("tags"), list) or len(article["tags"]) < 3:
            errors.append("tags must be list of >=3 strings")
        if not isinstance(article.get("faq"), list) or len(article["faq"]) < 3:
            errors.append("faq must be list of >=3 items")
        for q in article.get("faq", []):
            if not isinstance(q, dict) or "q" not in q or "a" not in q:
                errors.append(f"bad faq item: {q}")
        content = article.get("content", "")
        if len(content) < 3000:
            errors.append(f"content too short: {len(content)} chars (need >=3000)")
        if "<h2>" not in content:
            errors.append("content has no <h2> headers")
    return errors




# ─── Phase 3: Trending topic generator ─────────────────────────────────────

_TRENDING_SYSTEM_PROMPT = """Ты — продакт-маркетолог B2B SaaS-платформы SOCMASTER (автоматизация привлечения клиентов из соцсетей).
Твоя задача — придумывать актуальные SEO-темы для блога, которые получают трафик в Google/Yandex в 2026 году.

Темы должны быть:
- Конкретными, не водянистыми
- Полезными предпринимателям, маркетологам, sales-командам, агентствам
- Связанными с трафиком, лидами, AI, автоматизацией, соцсетями
- Свежими (учитывают 2026 год: AI-тренды, изменения алгоритмов, новые подходы)
- Уникальными — не должны повторять уже существующие slug'и

Категории на выбор: lid-generation, traffic-socsety, facebook, instagram, linkedin, telegram, reddit, twitter-x, ai-sales, crm, cases, news.

Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{
  "slug": "<latinized-kebab-case-unique>",
  "title": "<Заголовок 50-70 chars>",
  "category": "<один из categories>",
  "brief": "<2-3 предложения о чём статья и какую пользу несёт>"
}"""


async def generate_trending_topic(used_slugs: set[str]) -> dict | None:
    """Генерирует через Gemini одну актуальную тему. Гарантирует уникальность slug."""
    from backend.database import SessionLocal
    from backend.services.ai_agent_llm import complete_gemini_conversation_with_key_rotation
    from backend.services.cabinet_settings import google_llm_api_key_list

    db = SessionLocal()
    try:
        keys = google_llm_api_key_list(db)
    finally:
        db.close()
    if not keys:
        return None

    used_sample = sorted(list(used_slugs))[-30:]  # последние 30 для контекста
    user_prompt = f"""Сгенерируй ОДНУ свежую SEO-тему для блога SOCMASTER, актуальную в 2026 году.

Уже использованные slug'и (НЕ повторять их и не делать слишком похожие):
{json.dumps(used_sample, ensure_ascii=False)}

Подумай: что сейчас (2026) обсуждает B2B-аудитория? Какие тренды? Что ищут в Google маркетологи и предприниматели?

Верни строго JSON по формату из system-промпта."""

    for attempt in range(3):
        try:
            text, _ = await complete_gemini_conversation_with_key_rotation(
                keys, "gemini-2.5-flash", _TRENDING_SYSTEM_PROMPT,
                [("user", user_prompt)], timeout_s=60.0,
            )
            topic = parse_json_response(text)
            if not isinstance(topic, dict):
                continue
            slug = topic.get("slug", "").strip()
            if not slug or slug in used_slugs:
                logger.warning("Trending topic: дубликат %s, retry", slug)
                continue
            if not topic.get("title") or not topic.get("category"):
                continue
            logger.info("Сгенерирована trending-тема: %s", slug)
            return topic
        except Exception as e:
            logger.warning("Trending generation attempt %d failed: %s", attempt + 1, e)
    return None


def append_topic_to_queue(topic: dict) -> None:
    """Дописывает новую тему в data/blog/_topic_queue.json."""
    p = _PROJECT_ROOT / "data" / "blog" / "_topic_queue.json"
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        topics = data.get("topics", [])
        # Anti-dup
        if any(t.get("slug") == topic.get("slug") for t in topics):
            return
        topics.append(topic)
        data["topics"] = topics
        data["total"] = len(topics)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Тема добавлена в queue: %s (total=%d)", topic.get("slug"), len(topics))
    except Exception as e:
        logger.warning("append_topic_to_queue failed: %s", e)




# ─── EN auto-translation ────────────────────────────────────────────────────

_EN_TRANSLATE_SYSTEM = """You are a professional translator and SEO content editor. Translate Russian B2B/SaaS blog content to natural, idiomatic English suitable for an international audience of entrepreneurs, marketers, and sales teams.

Rules:
- Preserve HTML markup exactly (tags, attributes, /blog/<slug> links).
- Translate text inside tags only. Do NOT translate slug values in href attributes (keep /blog/<slug> as is).
- Use natural marketing English, not literal translation.
- Keep CTAs persuasive but not pushy.
- Keep technical terms (CRM, AI, B2B, SaaS, ICP, SDR) in English.
- Return STRICT JSON only — no markdown wrapper, no commentary.

Output JSON structure:
{
  "title_en": "...",
  "meta_title_en": "...",
  "meta_description_en": "...",
  "excerpt_en": "...",
  "audience_en": "...",
  "what_youll_learn_en": ["...", "..."],
  "content_en": "<full HTML content translated>",
  "faq_en": [{"q": "...", "a": "..."}]
}
"""


async def translate_article_to_en(article: dict) -> dict | None:
    """Переводит RU-статью на EN через Gemini. Возвращает dict с *_en полями или None."""
    from backend.database import SessionLocal
    from backend.services.ai_agent_llm import complete_gemini_conversation_with_key_rotation
    from backend.services.cabinet_settings import google_llm_api_key_list

    db = SessionLocal()
    try:
        keys = google_llm_api_key_list(db)
    finally:
        db.close()
    if not keys:
        logger.warning("Translate: no Gemini keys")
        return None

    src_payload = {
        "title": article.get("title", ""),
        "meta_title": article.get("meta_title", ""),
        "meta_description": article.get("meta_description", ""),
        "excerpt": article.get("excerpt", ""),
        "audience": article.get("audience", ""),
        "what_youll_learn": article.get("what_youll_learn", []),
        "content": article.get("content", ""),
        "faq": article.get("faq", []),
    }
    user_prompt = "Translate this Russian blog article to English. Return strict JSON.\n\nINPUT:\n" + json.dumps(src_payload, ensure_ascii=False)

    for attempt in range(3):
        try:
            text, _ = await complete_gemini_conversation_with_key_rotation(
                keys, "gemini-2.5-flash", _EN_TRANSLATE_SYSTEM,
                [("user", user_prompt)], timeout_s=180.0,
            )
            tr = parse_json_response(text)
            if not isinstance(tr, dict) or not tr.get("title_en"):
                continue
            return tr
        except Exception as e:
            logger.warning("Translate attempt %d failed: %s", attempt + 1, e)
    return None


def fetch_pexels_cover(topic: dict) -> str:
    """Подбирает релевантную тематике обложку через Pexels API.

    Использует brand-specific queries и FILTER по alt-text фото — отфильтровывает
    нерелевантные картинки (например, Instagram-фото у Facebook-статьи).
    PEXELS_API_KEY — env-var, free tier 200 req/час.
    """
    api_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not api_key:
        return ""

    # Brand-specific queries — бренд + сопутствующий контекст для разнообразия фото
    en_query_map = {
        "lid-generation": "business team meeting laptop",
        "facebook": "facebook logo app icon blue",
        "instagram": "instagram logo pink purple smartphone",
        "linkedin": "linkedin business handshake professional suit",
        "telegram": "telegram messaging app blue",
        "reddit": "reddit orange community discussion",
        "twitter-x": "twitter x logo social network",
        "ai-sales": "artificial intelligence sales technology",
        "crm": "data dashboard analytics business",
        "traffic-socsety": "marketing growth chart digital",
        "cases": "business success entrepreneur team",
        "news": "newspaper modern technology launch",
    }

    # Negative keywords per category — отбраковываем фото содержащие название
    # ДРУГОГО бренда в alt-text (минимизирует cross-brand mismatch).
    cross_brand_filter = {
        "facebook": ("instagram", "twitter", "tiktok", "snapchat"),
        "instagram": ("facebook", "twitter", "tiktok", "snapchat"),
        "linkedin": ("instagram", "tiktok", "snapchat"),
        "telegram": ("whatsapp", "viber", "signal"),
        "reddit": ("4chan",),
        "twitter-x": ("instagram", "facebook", "tiktok"),
    }

    cat = topic.get("category", "")
    query = en_query_map.get(cat, "business technology")
    blacklist = cross_brand_filter.get(cat, ())

    import urllib.request, urllib.parse
    url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(query)}&per_page=30&orientation=landscape"
    try:
        req = urllib.request.Request(url, headers={"Authorization": api_key, "User-Agent": "SOCMASTER-blog-bot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        photos = data.get("photos") or []
        if not photos:
            return ""

        # Фильтруем фото: alt НЕ должен содержать названия чужого бренда
        def is_clean(photo: dict) -> bool:
            alt = (photo.get("alt") or "").lower()
            for word in blacklist:
                if word in alt:
                    return False
            return True

        filtered = [p for p in photos if is_clean(p)]
        if not filtered:
            logger.warning("Pexels: все %d фото содержат blacklist для %s, fallback на первое", len(photos), cat)
            filtered = photos

        # Псевдо-случайный выбор по slug — детерминированный
        idx = abs(hash(topic.get("slug", ""))) % len(filtered)
        photo = filtered[idx]
        return photo.get("src", {}).get("large", "") or photo.get("src", {}).get("original", "")
    except Exception as e:
        logger.warning("Pexels fetch failed: %s", e)
        return ""

def save_article(article: dict, *, dry_run: bool = False) -> Path:
    """Сохраняет статью в data/blog/posts/<slug>.json. Возвращает путь."""
    article["author"] = "SOCMASTER"
    article["published_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    article["status"] = "published"
    article.setdefault("cover_image", "")

    posts_dir = _PROJECT_ROOT / "data" / "blog" / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)
    path = posts_dir / f"{article['slug']}.json"

    if dry_run:
        logger.info("[DRY-RUN] would write %s (%d bytes)", path, len(json.dumps(article)))
        return path

    if path.exists():
        logger.warning("Файл %s уже существует — перезаписываем", path)
    path.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("✓ Сохранено: %s (%d bytes)", path, path.stat().st_size)
    return path


# ─── Main flow ─────────────────────────────────────────────────────────────

async def main_async(dry_run: bool, force_slug: str | None, *, mode: str = 'auto', do_translate: bool = True) -> int:
    logger.info("=== Запуск генератора статей ===")

    # Mode routing
    if mode == "trending" and not force_slug:
        logger.info("Mode=trending: генерируем актуальную trending-тему через Gemini")
        used = published_slugs() | {t.get("slug") for t in load_topic_queue() if t.get("slug")}
        topic = await generate_trending_topic(used)
        if topic:
            append_topic_to_queue(topic)
        else:
            logger.error("Не удалось сгенерировать trending-тему — fallback на queue")
            topic = pick_next_topic(None)
    else:
        topic = pick_next_topic(force_slug)
        if not topic and not force_slug and mode != "queue":
            logger.info("Очередь исчерпана — fallback на trending")
            used = published_slugs() | {t.get("slug") for t in load_topic_queue() if t.get("slug")}
            topic = await generate_trending_topic(used)
            if topic:
                append_topic_to_queue(topic)

    # Дополнительно: каждый 7-й запуск (по дню недели = воскресенье) можно подкинуть trending
    # для свежести даже когда очередь не пустая — закомментировано пока, можно включить.
    # if datetime.now(timezone.utc).weekday() == 6 and not force_slug:
    #     used = published_slugs() | {t.get("slug") for t in load_topic_queue() if t.get("slug")}
    #     trend = await generate_trending_topic(used)
    #     if trend:
    #         append_topic_to_queue(trend)
    #         topic = trend

    if not topic:
        logger.error("Нет доступной темы для генерации")
        return 2

    logger.info("Тема: %s (slug=%s, category=%s)", topic["title"], topic["slug"], topic["category"])

    master = load_master_prompt()
    article = None
    last_err = None
    for attempt in range(1, 4):
        try:
            logger.info("Попытка генерации %d/3...", attempt)
            article = await generate_article_via_gemini(topic, master)
            break
        except Exception as e:
            last_err = e
            logger.warning("Attempt %d failed: %s", attempt, e)
            if attempt < 3:
                await asyncio.sleep(3)
    if article is None:
        logger.exception("Все 3 попытки генерации провалились: %s", last_err)
        return 3

    # Принудительно фиксируем slug + category (на случай если Gemini ошибся)
    article["slug"] = topic["slug"]
    article["category"] = topic["category"]

    errors = validate_article(article)
    if errors:
        logger.error("Валидация не прошла: %s", errors)
        # Сохраним как _draft_failed_<slug>.json для разбора
        debug_path = _PROJECT_ROOT / "data" / "blog" / f"_draft_failed_{topic['slug']}.json"
        debug_path.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Черновик сохранён для разбора: %s", debug_path)
        return 4

    # Опциональная загрузка cover image с Pexels (если настроен PEXELS_API_KEY)
    if not article.get("cover_image"):
        cover = fetch_pexels_cover(topic)
        if cover:
            article["cover_image"] = cover
            logger.info("Pexels cover: %s", cover[:80])
    # Auto-translate to EN before saving
    if do_translate and not dry_run:
        logger.info("Переводим статью на EN через Gemini...")
        en = await translate_article_to_en(article)
        if en:
            article["i18n"] = {"en": en}
            logger.info("✓ EN-перевод сохранён (%d полей)", len(en))
        else:
            logger.warning("⚠ EN-перевод не получен — статья только на RU")

    save_article(article, dry_run=dry_run)
    logger.info("=== Успешно сгенерирована статья: %s ===", article["slug"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SOCMASTER blog auto-generator")
    parser.add_argument("--dry-run", action="store_true", help="Не сохранять файл, только сгенерировать")
    parser.add_argument("--topic", type=str, default=None, help="Slug темы для отладки")
    parser.add_argument("--mode", choices=["queue", "trending", "auto"], default="auto",
                        help="queue=строго из очереди, trending=форсированная trending-тема, auto=queue→trending fallback")
    parser.add_argument("--no-translate", action="store_true", help="Не делать EN-перевод (по умолчанию переводит)")
    args = parser.parse_args()
    return asyncio.run(main_async(args.dry_run, args.topic, mode=args.mode, do_translate=not args.no_translate))


if __name__ == "__main__":
    sys.exit(main())
