#!/usr/bin/env python3
"""Bulk-перевод существующих RU-статей блога на английский.

Зачем: до v3.0.7 авто-генератор писал только RU-версию. После того как в
generate_daily_article.py был добавлен второй проход через Gemini для перевода,
в новых статьях `post.i18n.en` заполняется автоматически. Старые статьи же
имеют пустые поля внутри `i18n.en` — этот скрипт их догоняет.

Использование (на проде):
    cd /opt/fb-master && .venv/bin/python3 scripts/translate_existing_articles.py
    # или для одной статьи:
    cd /opt/fb-master && .venv/bin/python3 scripts/translate_existing_articles.py --slug ai-v-prodazhah
    # dry-run (только показать какие файлы обновятся, без вызова Gemini):
    cd /opt/fb-master && .venv/bin/python3 scripts/translate_existing_articles.py --dry-run

Идемпотентен: если в `post.i18n.en.title_en` уже есть значение — статья
пропускается. Чтобы переводить заново, удалите вручную поле title_en.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в PYTHONPATH (скрипт можно запустить из любого CWD)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# Логи в тот же файл, что использует основной generator
_LOG_DIR = _PROJECT_ROOT / "data" / "blog"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "translate.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("blog_translator")


_POSTS_DIR = _PROJECT_ROOT / "data" / "blog" / "posts"


def needs_translation(post: dict) -> bool:
    """True, если у поста ещё нет EN-перевода (или он неполный)."""
    en = (post.get("i18n") or {}).get("en") or {}
    if not isinstance(en, dict):
        return True
    return not (en.get("title_en") and en.get("content_en"))


async def translate_one(path: Path, dry_run: bool) -> bool:
    """Переводит одну статью и сохраняет обратно. Возвращает True при успехе."""
    # Импортируем здесь чтобы dry-run не требовал Gemini API
    from scripts.generate_daily_article import translate_article_to_en

    try:
        post = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Не удалось прочитать %s: %s", path.name, e)
        return False

    if not needs_translation(post):
        logger.info("⏭  %s — уже переведено, пропускаем", path.name)
        return True

    logger.info("→  %s — запрос перевода у Gemini...", path.name)

    if dry_run:
        logger.info("[DRY-RUN] would translate %s (RU title='%s...')",
                    path.name, (post.get("title") or "")[:60])
        return True

    en = await translate_article_to_en(post)
    if not en:
        logger.warning("✗  %s — Gemini вернул пустой/невалидный ответ", path.name)
        return False

    post["i18n"] = {"en": en}
    path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("✓  %s — EN-перевод сохранён (title_en='%s...')",
                path.name, (en.get("title_en") or "")[:60])
    return True


async def main_async(slug: str | None, dry_run: bool, sleep_s: float) -> int:
    if not _POSTS_DIR.is_dir():
        logger.error("Каталог постов не найден: %s", _POSTS_DIR)
        return 1

    if slug:
        files = [_POSTS_DIR / f"{slug}.json"]
        if not files[0].is_file():
            logger.error("Файл не найден: %s", files[0])
            return 2
    else:
        files = sorted(_POSTS_DIR.glob("*.json"))
        # Скип служебных файлов начинающихся с _
        files = [f for f in files if not f.name.startswith("_")]

    logger.info("=== Bulk-перевод: %d файлов, dry_run=%s ===", len(files), dry_run)

    ok_count = 0
    fail_count = 0
    for i, path in enumerate(files, 1):
        logger.info("[%d/%d] %s", i, len(files), path.name)
        if await translate_one(path, dry_run):
            ok_count += 1
        else:
            fail_count += 1
        # Anti-rate-limit пауза между переводами (Gemini free tier — 15 req/min)
        if i < len(files) and not dry_run and sleep_s > 0:
            await asyncio.sleep(sleep_s)

    logger.info("=== Готово: ok=%d fail=%d ===", ok_count, fail_count)
    return 0 if fail_count == 0 else 3


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-перевод RU-статей блога на EN через Gemini")
    parser.add_argument("--slug", type=str, default=None,
                        help="Slug одной статьи (для отладки). Если не указан — обрабатываем все.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не вызывать Gemini, только показать какие файлы будут обработаны")
    parser.add_argument("--sleep", type=float, default=5.0,
                        help="Пауза между переводами, сек (по умолчанию 5; 0 = без паузы)")
    args = parser.parse_args()
    return asyncio.run(main_async(args.slug, args.dry_run, args.sleep))


if __name__ == "__main__":
    sys.exit(main())
