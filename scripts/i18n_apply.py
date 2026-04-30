#!/usr/bin/env python3
"""Применяет batch-переводы из messages_en.json в backend/services/i18n_dict.py.

Использование::

    python3 scripts/i18n_apply.py
    python3 scripts/i18n_apply.py --dry-run

Поведение:
- Читает messages_en.json (output i18n_translate_batch.py).
- Перезаписывает auto-generated секцию в i18n_dict.py (всё после маркера
  `# === AUTO-GENERATED START ===` и до `# === AUTO-GENERATED END ===`).
- Manually-curated ключи (выше START-маркера) сохраняются — их можно
  использовать чтобы переопределить машинный перевод (manual override).

Order of precedence в TRANSLATIONS["en"]:
1. Manual section (выше AUTO-GENERATED) — наивысший приоритет (Python dict
   при дубликатах ключей оставляет последнее значение, но мы помещаем
   manual-ключи ВЫШЕ, а auto-секция дописывается ПОСЛЕ → manual получают
   приоритет если совпадают).
2. Auto-generated section — fallback на Gemini-перевод.

Workflow:
    python3 scripts/i18n_extract.py          → messages.json
    python3 scripts/i18n_translate_batch.py  → messages_en.json
    python3 scripts/i18n_apply.py            → обновляет i18n_dict.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_DICT_FILE = _PROJECT_ROOT / "backend" / "services" / "i18n_dict.py"
_INPUT = _PROJECT_ROOT / "data" / "i18n" / "messages_en.json"

_AUTO_START = "        # === AUTO-GENERATED START === (do not edit manually; use scripts/i18n_apply.py)"
_AUTO_END = "        # === AUTO-GENERATED END ==="


def _python_repr(s: str) -> str:
    """Безопасное представление строки для Python source."""
    return json.dumps(s, ensure_ascii=False)


def _build_auto_block(translations: dict[str, str], manual_keys: set[str]) -> str:
    """Строит Python-код auto-секции внутри TRANSLATIONS['en'].

    Manual-ключи (которые уже есть в manual-секции выше) пропускаются —
    auto-секция не должна затирать manual override.
    """
    lines = [_AUTO_START]
    for ru in sorted(translations.keys()):
        en = translations[ru]
        if ru == en:  # identity → не записываем (i18n.translate fallback на RU)
            continue
        if ru in manual_keys:  # уже в manual-секции — не дублируем
            continue
        lines.append(f"        {_python_repr(ru)}: {_python_repr(en)},")
    lines.append(_AUTO_END)
    return "\n".join(lines)


def _extract_manual_keys(src: str, auto_start_marker: str) -> set[str]:
    """Извлечь все ключи из manual-секции (всё перед auto-маркером).

    Простой regex: `"<key>": "<val>",` — для нашего dict достаточно.
    """
    import re

    if auto_start_marker in src:
        manual_part = src.split(auto_start_marker, 1)[0]
    else:
        manual_part = src
    keys = re.findall(r'^\s+"([^"\\]+(?:\\.[^"\\]*)*)":\s*"', manual_part, re.MULTILINE)
    return set(keys)


def apply_translations(input_path: Path, dict_path: Path, dry_run: bool) -> int:
    if not input_path.is_file():
        print(f"❌ Input не найден: {input_path}", file=sys.stderr)
        return 2

    if not dict_path.is_file():
        print(f"❌ Dict не найден: {dict_path}", file=sys.stderr)
        return 2

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    translations = payload.get("translations", {})
    if not isinstance(translations, dict) or not translations:
        print("❌ messages_en.json пуст или не содержит 'translations'", file=sys.stderr)
        return 3

    src = dict_path.read_text(encoding="utf-8")
    manual_keys = _extract_manual_keys(src, _AUTO_START)

    auto_block = _build_auto_block(translations, manual_keys)

    # Если маркеры уже есть — заменяем содержимое между ними
    if _AUTO_START in src and _AUTO_END in src:
        start = src.index(_AUTO_START)
        end = src.index(_AUTO_END) + len(_AUTO_END)
        new_src = src[:start] + auto_block + src[end:]
    else:
        # Первая инсталляция: вставляем перед закрытием TRANSLATIONS["en"] dict.
        # Точка вставки: последняя строка `}` в файле перед `def all_keys`.
        marker = "    }\n}\n"
        if marker not in src:
            print(f"❌ Не нашли точку вставки в {dict_path} (нет `    }}\\n}}`)", file=sys.stderr)
            return 4
        new_src = src.replace(
            marker,
            "\n" + auto_block + "\n" + marker,
            1,
        )

    if new_src == src:
        print("Нечего обновлять — i18n_dict.py уже актуален")
        return 0

    added_count = sum(
        1 for k, v in translations.items()
        if k != v and k not in manual_keys
    )

    if dry_run:
        print(f"[DRY-RUN] обновим i18n_dict.py: ~{added_count} ключей в auto-секции")
        print(f"[DRY-RUN] manual-секция: {len(manual_keys)} ключей сохранено как есть")
        return 0

    dict_path.write_text(new_src, encoding="utf-8")
    print(f"✓ Обновлено: {dict_path}")
    print(f"✓ Auto-секция: {added_count} переводов (identity пропущены, manual-overrides сохранены)")
    print(f"✓ Manual-секция: {len(manual_keys)} ключей оставлено без изменений")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Применить переводы из messages_en.json в i18n_dict.py")
    parser.add_argument("--input", type=Path, default=_INPUT)
    parser.add_argument("--dict-file", type=Path, default=_DICT_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return apply_translations(args.input, args.dict_file, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
