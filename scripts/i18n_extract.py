#!/usr/bin/env python3
"""Извлекает уникальные RU-строки из templates/*.html для batch-перевода.

Использование::

    python3 scripts/i18n_extract.py
    python3 scripts/i18n_extract.py --templates-dir templates --output data/i18n/messages.json

Как работает:
1. Сканирует templates/**/*.html.
2. Удаляет содержимое <script>, <style>, <!-- comments --> (там не UI-текст).
3. Находит все строки внутри текстовых тегов или внутри title/placeholder/aria-label
   атрибутов, которые содержат хотя бы один кириллический символ.
4. Чистит Jinja2-выражения `{{ ... }}` и `{% ... %}` (они уже обработаны на сервере).
5. Группирует уникальные строки + список файлов где они встречаются.
6. Пишет в messages.json — этот файл идёт в i18n_translate_batch.py.

Важно: скрипт извлекает только то что **видит конечный пользователь**:
- текст между `>...<` (содержимое тегов)
- значения атрибутов: `title=`, `placeholder=`, `aria-label=`, `alt=`
- {% block title %}...{% endblock %} (page title)
Не извлекает: CSS-classes, id, data-*, href значения.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# Регулярки для поиска текста.
# 1. Содержимое тега: между `>` и `<` (но не пустое, и содержит кириллицу).
_TEXT_BETWEEN_TAGS = re.compile(r">([^<>{}]*?[А-Яа-яЁё][^<>{}]*?)<")
# 2. Атрибуты: title="...", placeholder="...", aria-label="...", alt="..."
_ATTR_TEXT = re.compile(
    r'(?:title|placeholder|aria-label|alt)\s*=\s*"([^"]*?[А-Яа-яЁё][^"]*?)"'
)
# 3. Содержимое <script>, <style>, <!-- ... -->, {# ... #}
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _strip_unwanted(html: str) -> str:
    """Убрать <script>, <style>, комментарии — там нет UI-строк."""
    html = _SCRIPT_BLOCK.sub("", html)
    html = _STYLE_BLOCK.sub("", html)
    html = _HTML_COMMENT.sub("", html)
    html = _JINJA_COMMENT.sub("", html)
    return html


def _clean_text(s: str) -> str:
    """Нормализация выделенной строки: убираем переносы, лишние пробелы."""
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", s))


def extract_from_html(html: str) -> list[str]:
    """Вернуть список уникальных строк (в порядке появления) для одного файла."""
    cleaned = _strip_unwanted(html)
    found: list[str] = []
    seen: set[str] = set()

    for match in _TEXT_BETWEEN_TAGS.finditer(cleaned):
        text = _clean_text(match.group(1))
        if not text or not _has_cyrillic(text):
            continue
        if len(text) < 2:
            continue
        if text in seen:
            continue
        seen.add(text)
        found.append(text)

    for match in _ATTR_TEXT.finditer(cleaned):
        text = _clean_text(match.group(1))
        if not text or not _has_cyrillic(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        found.append(text)

    return found


def extract_all(templates_dir: Path) -> dict[str, list[str]]:
    """Сканирует templates/**/*.html, возвращает dict { rel_path: [strings] }."""
    out: dict[str, list[str]] = {}
    for path in sorted(templates_dir.rglob("*.html")):
        rel = path.relative_to(templates_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        strings = extract_from_html(text)
        if strings:
            out[rel] = strings
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Извлечь RU UI-строки из Jinja2-шаблонов")
    parser.add_argument(
        "--templates-dir",
        type=Path,
        default=_PROJECT_ROOT / "templates",
        help="Каталог с шаблонами (default: templates/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_PROJECT_ROOT / "data" / "i18n" / "messages.json",
        help="Куда писать результат (default: data/i18n/messages.json)",
    )
    args = parser.parse_args()

    if not args.templates_dir.is_dir():
        print(f"❌ Каталог не найден: {args.templates_dir}", file=sys.stderr)
        return 2

    by_file = extract_all(args.templates_dir)

    # Уникальные строки + где встречаются
    string_to_files: dict[str, list[str]] = {}
    for path, strings in by_file.items():
        for s in strings:
            string_to_files.setdefault(s, []).append(path)

    try:
        templates_label = str(args.templates_dir.relative_to(_PROJECT_ROOT))
    except ValueError:
        templates_label = str(args.templates_dir)

    payload = {
        "_meta": {
            "templates_dir": templates_label,
            "total_unique_strings": len(string_to_files),
            "total_files_with_strings": len(by_file),
        },
        "strings": [
            {"key": s, "files": files}
            for s, files in sorted(string_to_files.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"✓ Извлечено {len(string_to_files)} уникальных RU-строк из {len(by_file)} файлов")
    print(f"✓ Сохранено: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
