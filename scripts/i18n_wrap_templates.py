#!/usr/bin/env python3
"""Безопасное оборачивание RU-строк в шаблонах в {{ t("...") }}.

Использование::

    python3 scripts/i18n_wrap_templates.py            # все шаблоны
    python3 scripts/i18n_wrap_templates.py --dry-run  # только показать diff
    python3 scripts/i18n_wrap_templates.py --files templates/dashboard.html
    python3 scripts/i18n_wrap_templates.py --skip 'auth/login.html,launcher/*'

Safety-first правила (защита от сломанного Jinja2):
1. Оборачиваем ТОЛЬКО строки которые уже есть в TRANSLATIONS["en"]
   (прошли через i18n_extract.py → i18n_translate_batch.py → i18n_apply.py).
2. Skip строки которые содержат `{{` или `{%` — нельзя nested выражения.
3. Skip содержимое <script>, <style>, <!-- -->, {# #}.
4. Skip уже-обёрнутые: текст внутри `{{ t(...) }}`.
5. Атрибуты (title, placeholder, aria-label, alt) — только если value
   полностью совпадает с ключом dict.

Что НЕ покрывается (ручная работа):
- Текст в {% if %}{% else %}-блоках где RU делится по conditionals.
- Текст с {{ var }} вложениями (адаптировать через format() аргументы).
- Custom Jinja-теги.

Идемпотентен: повторный запуск не создаёт дубль-обёрток.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# Загружаем dict
from backend.services.i18n_dict import TRANSLATIONS  # noqa: E402

_KNOWN_KEYS: frozenset[str] = frozenset(TRANSLATIONS.get("en", {}).keys())


# Регулярки для поиска кандидатов на оборачивание
_TEXT_BETWEEN_TAGS = re.compile(r"(?P<lead>>)(?P<text>[^<>]*?[А-Яа-яЁё][^<>]*?)(?P<trail><)")
_ATTR_TEXT = re.compile(
    r'(?P<lead>(?:title|placeholder|aria-label|alt)\s*=\s*")(?P<text>[^"]*?[А-Яа-яЁё][^"]*?)(?P<trail>")'
)

# Защита: содержимое <script>, <style>, <!-- ... -->, {# ... #}
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _has_jinja_expr(s: str) -> bool:
    """True если строка содержит Jinja2-выражения, нельзя оборачивать."""
    return "{{" in s or "{%" in s or "}}" in s or "%}" in s


def _has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", s))


def _normalize_text(s: str) -> str:
    """Нормализация для поиска в dict: shrink whitespace."""
    return re.sub(r"\s+", " ", s).strip()


def _escape_for_jinja_string(s: str) -> str:
    """Экранирует строку для использования внутри {{ t("...") }}."""
    if '"' in s:
        if "'" in s:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return "'" + s + "'"
    return '"' + s.replace("\\", "\\\\") + '"'


class _ProtectedRegions:
    """Маскирует <script>, <style>, comments, существующие {{ t() }}."""

    def __init__(self, text: str):
        self.placeholders: dict[str, str] = {}
        self.text = text
        self._counter = 0

    def _stash(self, match: re.Match) -> str:
        token = f"\x00PROTECTED_{self._counter}\x00"
        self._counter += 1
        self.placeholders[token] = match.group(0)
        return token

    def mask(self) -> str:
        result = _SCRIPT_BLOCK.sub(self._stash, self.text)
        result = _STYLE_BLOCK.sub(self._stash, result)
        result = _HTML_COMMENT.sub(self._stash, result)
        result = _JINJA_COMMENT.sub(self._stash, result)
        # Целиком {{ ... }} и {% ... %} — защитим
        result = re.sub(r"\{\{[^{}]*?\}\}", self._stash, result)
        result = re.sub(r"\{%[^{%}]*?%\}", self._stash, result)
        self.text = result
        return result

    def unmask(self, text: str) -> str:
        # Сортировка по индексу (последние созданные восстанавливаются первыми —
        # это безопасно при вложенных замаскированных регионах).
        def _idx(token: str) -> int:
            m = re.search(r"PROTECTED_(\d+)", token)
            return int(m.group(1)) if m else 0
        for token in sorted(self.placeholders.keys(), key=_idx, reverse=True):
            text = text.replace(token, self.placeholders[token])
        return text


def wrap_in_html(html: str) -> tuple[str, int]:
    """Оборачивает RU-строки в одном HTML-файле. Возвращает (новый_html, число_замен)."""
    protector = _ProtectedRegions(html)
    masked = protector.mask()
    count = 0

    def _wrap_text(match: re.Match) -> str:
        nonlocal count
        text = match.group("text")
        normalized = _normalize_text(text)
        if not _has_cyrillic(normalized):
            return match.group(0)
        if _has_jinja_expr(text):
            return match.group(0)
        if normalized not in _KNOWN_KEYS:
            return match.group(0)
        leading_ws = re.match(r"^\s*", text).group(0)
        trailing_ws = re.search(r"\s*$", text).group(0)
        wrapped = f"{leading_ws}{{{{ t({_escape_for_jinja_string(normalized)}) }}}}{trailing_ws}"
        count += 1
        return f"{match.group('lead')}{wrapped}{match.group('trail')}"

    def _wrap_attr(match: re.Match) -> str:
        nonlocal count
        text = match.group("text")
        normalized = _normalize_text(text)
        if not _has_cyrillic(normalized):
            return match.group(0)
        if _has_jinja_expr(text):
            return match.group(0)
        if normalized not in _KNOWN_KEYS:
            return match.group(0)
        wrapped = f"{{{{ t({_escape_for_jinja_string(normalized)}) }}}}"
        count += 1
        return f"{match.group('lead')}{wrapped}{match.group('trail')}"

    masked = _TEXT_BETWEEN_TAGS.sub(_wrap_text, masked)
    masked = _ATTR_TEXT.sub(_wrap_attr, masked)
    result = protector.unmask(masked)
    return result, count


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-wrap RU UI-строк в Jinja2 шаблонах в {{ t() }}")
    parser.add_argument("--templates-dir", type=Path, default=_PROJECT_ROOT / "templates")
    parser.add_argument("--files", type=str, default="",
                        help="Список конкретных файлов через запятую (relative to templates-dir)")
    parser.add_argument("--skip", type=str, default="",
                        help="Список файлов/глобов через запятую — пропустить")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.templates_dir.is_dir():
        print(f"❌ {args.templates_dir} не существует", file=sys.stderr)
        return 2

    targets: list[Path] = []
    if args.files:
        for f in args.files.split(","):
            p = (args.templates_dir / f.strip()).resolve()
            if p.is_file():
                targets.append(p)
    else:
        targets = sorted(args.templates_dir.rglob("*.html"))

    skip_globs: list[str] = [g.strip() for g in args.skip.split(",") if g.strip()]

    print(f"Кандидатов: {len(targets)} файлов")
    print(f"Известных ключей в dict: {len(_KNOWN_KEYS)}")
    print()

    total_files_changed = 0
    total_wraps = 0
    for path in targets:
        rel = path.relative_to(args.templates_dir)
        if any(rel.match(g) for g in skip_globs):
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            print(f"  SKIP {rel}: {e}")
            continue

        new_src, count = wrap_in_html(src)
        if count == 0:
            continue

        total_wraps += count
        total_files_changed += 1
        if args.dry_run:
            print(f"  [DRY-RUN] {rel}: +{count} обёрток")
        else:
            path.write_text(new_src, encoding="utf-8")
            print(f"  ✓ {rel}: +{count} обёрток")

    print()
    print(f"Всего: {total_wraps} обёрток в {total_files_changed} файлах")
    if args.dry_run:
        print("(dry-run — ничего не записано)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
