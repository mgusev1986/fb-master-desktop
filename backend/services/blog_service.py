"""Blog (knowledge base) service — JSON file storage.

Хранит статьи в data/blog/posts/<slug>.json, категории в categories.json,
теги в tags.json. Read-only API: list_posts, get_post, list_categories, list_tags.

Для масштабирования (>500 статей) можно мигрировать на SQLAlchemy таблицу
без изменения роутера/шаблонов — нужно лишь подменить implementations.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Путь к каталогу с данными блога. data/ — single source of truth.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BLOG_DIR = _PROJECT_ROOT / "data" / "blog"
_POSTS_DIR = _BLOG_DIR / "posts"


def _read_json(path: Path) -> Any:
    """Безопасно читает JSON, возвращает None при ошибке."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("blog_service: failed to read %s: %s", path, e)
        return None


def _all_post_files() -> list[Path]:
    """Все JSON-файлы статей. Не кэшируем — пользователь может добавить
    статью без рестарта (особенно полезно для генератора)."""
    if not _POSTS_DIR.is_dir():
        return []
    return sorted(_POSTS_DIR.glob("*.json"))


def list_posts(
    *,
    category: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    limit: int | None = None,
    status: str = "published",
) -> list[dict]:
    """Возвращает отфильтрованный + отсортированный список статей.

    Сортировка: published_at desc (свежие сверху).
    Фильтры:
      - status: 'published' (default) | 'draft' | 'all'
      - category: slug категории
      - tag: slug тега
      - query: full-text по title + excerpt + content (case-insensitive)
    """
    posts: list[dict] = []
    q_lower = (query or "").strip().lower()

    for path in _all_post_files():
        post = _read_json(path)
        if not isinstance(post, dict):
            continue

        if status != "all" and post.get("status") != status:
            continue
        if category and post.get("category") != category:
            continue
        if tag and tag not in (post.get("tags") or []):
            continue
        if q_lower:
            haystack = " ".join(
                str(post.get(k, "")) for k in ("title", "excerpt", "content")
            ).lower()
            if q_lower not in haystack:
                continue

        posts.append(post)

    posts.sort(key=lambda p: p.get("published_at", ""), reverse=True)

    if limit is not None and limit > 0:
        posts = posts[:limit]

    return posts


def get_post(slug: str) -> dict | None:
    """Возвращает опубликованную статью по slug или None.

    Path-traversal guard: отвергает slug со `/` или `..`.
    """
    if not slug or "/" in slug or ".." in slug:
        return None
    post = _read_json(_POSTS_DIR / f"{slug}.json")
    if not isinstance(post, dict):
        return None
    if post.get("status") != "published":
        return None
    return post


@lru_cache(maxsize=1)
def list_categories() -> list[dict]:
    """Список категорий. Cached — категории редко меняются."""
    data = _read_json(_BLOG_DIR / "categories.json")
    if isinstance(data, list):
        return data
    return []


@lru_cache(maxsize=1)
def list_tags() -> list[dict]:
    """Список тегов. Cached."""
    data = _read_json(_BLOG_DIR / "tags.json")
    if isinstance(data, list):
        return data
    return []


def get_category(slug: str) -> dict | None:
    for cat in list_categories():
        if cat.get("slug") == slug:
            return cat
    return None


def get_tag(slug: str) -> dict | None:
    for t in list_tags():
        if t.get("slug") == slug:
            return t
    return None


def related_posts(post: dict, *, limit: int = 3) -> list[dict]:
    """Похожие статьи: сначала из той же категории, потом по совпадающим тегам.
    Исключает сам пост. Для блока «Читайте также» в конце статьи."""
    if not post:
        return []
    current_slug = post.get("slug")
    same_category = [
        p for p in list_posts(category=post.get("category"))
        if p.get("slug") != current_slug
    ]
    if len(same_category) >= limit:
        return same_category[:limit]

    seen = {p["slug"] for p in same_category}
    if current_slug:
        seen.add(current_slug)
    for tag in (post.get("tags") or []):
        if len(same_category) >= limit:
            break
        for p in list_posts(tag=tag):
            if p["slug"] not in seen:
                same_category.append(p)
                seen.add(p["slug"])
                if len(same_category) >= limit:
                    break
    return same_category[:limit]


def all_published_slugs() -> list[str]:
    """Slug'и всех опубликованных статей. Используется при генерации sitemap.xml."""
    return [p["slug"] for p in list_posts() if p.get("slug")]


# ─── i18n helpers ──────────────────────────────────────────────────────────

# Маппинг RU-поля → EN-поля внутри post["i18n"]["en"][...].
# Generator (scripts/generate_daily_article.py:300-310) пишет переводы под
# суффиксированными ключами, чтобы избежать коллизий с RU-источником.
_LOCALIZED_FIELDS: dict[str, str] = {
    "title": "title_en",
    "meta_title": "meta_title_en",
    "meta_description": "meta_description_en",
    "excerpt": "excerpt_en",
    "audience": "audience_en",
    "what_youll_learn": "what_youll_learn_en",
    "content": "content_en",
    "faq": "faq_en",
}


def localize_post(post: dict, lang: str) -> dict:
    """Возвращает «представление» post с подменёнными RU-полями на EN-версии.

    Если `lang != "en"` или EN-перевод отсутствует/пуст — возвращает исходный
    post без модификации. Не мутирует оригинал.

    Используется в blog router'е перед передачей post в шаблон, чтобы
    `{{ post.title }}` и т.п. автоматически отдавали EN-версию при
    `?lang=en`. Жёсткий fallback на RU гарантирует, что пользователь видит
    хоть что-то даже если конкретный пост ещё не переведён.
    """
    if lang != "en" or not isinstance(post, dict):
        return post
    en = (post.get("i18n") or {}).get("en") or {}
    if not isinstance(en, dict) or not en:
        return post
    overrides = {}
    for ru_key, en_key in _LOCALIZED_FIELDS.items():
        val = en.get(en_key)
        # Truthy check ловит и пустые строки, и пустые list/dict — ровно то,
        # что мы хотим: только реальные переводы подменяют RU-источник.
        if val:
            overrides[ru_key] = val
    if not overrides:
        return post
    return {**post, **overrides}
