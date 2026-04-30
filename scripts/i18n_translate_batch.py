#!/usr/bin/env python3
"""Batch-перевод UI-строк через Gemini: messages.json → messages_en.json.

Использование (на VPS, где есть GOOGLE_API_KEY в .env)::

    cd /opt/fb-master
    .venv/bin/python3 scripts/i18n_translate_batch.py

Опции::

    --input data/i18n/messages.json      исходный список (output i18n_extract.py)
    --output data/i18n/messages_en.json  куда писать переводы
    --chunk-size 50                       строк за один Gemini-вызов
    --resume                              пропустить уже переведённые (если перевод прерывался)
    --dry-run                             не вызывать API, показать только план

Workflow:
1. python3 scripts/i18n_extract.py  → messages.json (3222 строк)
2. python3 scripts/i18n_translate_batch.py  → messages_en.json (через Gemini)
3. python3 scripts/i18n_apply.py  → обновляет backend/services/i18n_dict.py

Идемпотентен: при повторном запуске с --resume использует уже переведённые
строки из existing messages_en.json и переводит только новые.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

_LOG_DIR = _PROJECT_ROOT / "data" / "i18n"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "translate_batch.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("i18n_batch")


_SYSTEM_PROMPT = """You are a professional translator of UI strings for SOCMASTER, a B2B SaaS app for social-media outreach automation (Facebook, Instagram, LinkedIn, Telegram, Reddit, Twitter/X). Translate each Russian string to natural, idiomatic American English suitable for marketers, sales teams, entrepreneurs, and agency operators.

Critical rules:
1. Preserve ALL Jinja2 placeholders EXACTLY: {{ var }}, {{ var.attr }}, {% if x %}, {% endif %}, {n}, {name}, {until}, {days}, {days_word}, etc. Do NOT translate, rename, or reformat them.
2. Preserve HTML tags <strong>, <em>, <code>, <kbd>, <br>, <a>, <span> exactly. Translate text content INSIDE tags only.
3. Keep technical terms in English: CRM, AI, B2B, SaaS, ICP, SDR, API, URL, FB, Facebook, Instagram, LinkedIn, Telegram, Reddit, Messenger, Chromium, cookies, proxy, browser-profile, follow-up, outreach, etc.
4. Punctuation: Russian uses «», em-dash «—», ellipsis «…». In English: "" or '', em-dash —, ellipsis …. Match natural English style.
5. UI tone: concise, action-oriented (e.g. "Сохранить" → "Save", not "Save it"). Match Apple/Stripe/Linear-style conciseness for buttons.
6. For long descriptive paragraphs (instructions, banners): natural marketing English, not literal translation.
7. Don't add quotation marks where there were none in the original.
8. Translation must be deterministic: same Russian input → same English output. Be consistent across strings.

Return STRICT JSON only — no markdown wrapper, no commentary, no preamble. Format:
{"<russian-string>": "<english-translation>", ...}

Every input key must appear in the output with a non-empty translation. If a string is just punctuation, English brand name, or already-English text → return it unchanged."""


async def translate_chunk(strings: list[str]) -> dict[str, str]:
    """Один Gemini-вызов для chunk строк. Возвращает {ru: en}."""
    from backend.database import SessionLocal
    from backend.services.ai_agent_llm import complete_gemini_conversation_with_key_rotation
    from backend.services.cabinet_settings import google_llm_api_key_list

    db = SessionLocal()
    try:
        keys = google_llm_api_key_list(db)
    finally:
        db.close()
    if not keys:
        raise RuntimeError("Нет GOOGLE_API_KEY в .env / cabinet_settings — добавьте ключ")

    user_prompt = "Translate these UI strings (Russian → English). Return strict JSON.\n\nINPUT:\n" + json.dumps(
        strings, ensure_ascii=False, indent=2
    )

    for attempt in range(3):
        try:
            text, meta = await complete_gemini_conversation_with_key_rotation(
                keys,
                "gemini-2.5-flash",
                _SYSTEM_PROMPT,
                [("user", user_prompt)],
                timeout_s=120.0,
            )
            text = text.strip()
            # Strip markdown wrapper if Gemini added it
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*\n", "", text)
                text = re.sub(r"\n?```\s*$", "", text)
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError(f"Expected JSON object, got {type(data).__name__}")
            # Sanity: все ключи должны быть в output
            missing = [k for k in strings if k not in data]
            if missing:
                logger.warning("Chunk: пропущено %d из %d строк (попробую ещё раз)", len(missing), len(strings))
                if attempt < 2:
                    continue
                # Заполняем пропущенные оригиналом
                for k in missing:
                    data[k] = k
            return data
        except Exception as e:  # noqa: BLE001
            logger.warning("Chunk attempt %d failed: %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(3)
    raise RuntimeError("3/3 chunks failed")


async def main_async(input_path: Path, output_path: Path, chunk_size: int, resume: bool, dry_run: bool) -> int:
    if not input_path.is_file():
        logger.error("Input не найден: %s. Сначала запустите scripts/i18n_extract.py", input_path)
        return 2

    src = json.loads(input_path.read_text(encoding="utf-8"))
    all_strings = [e["key"] for e in src.get("strings", [])]
    logger.info("Загружено %d строк из %s", len(all_strings), input_path)

    existing: dict[str, str] = {}
    if resume and output_path.is_file():
        try:
            prev = json.loads(output_path.read_text(encoding="utf-8"))
            translations = prev.get("translations", {})
            if isinstance(translations, dict):
                existing = {k: v for k, v in translations.items() if isinstance(v, str) and v}
            logger.info("Resume: уже есть %d переводов", len(existing))
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось прочитать существующий output: %s", e)

    todo = [s for s in all_strings if s not in existing]
    logger.info("К переводу: %d строк (chunk=%d → ~%d вызовов API)",
                len(todo), chunk_size, (len(todo) + chunk_size - 1) // chunk_size)

    if dry_run:
        logger.info("[DRY-RUN] первые 5 строк: %s", todo[:5])
        return 0

    if not todo:
        logger.info("Нечего переводить — output уже актуален")
        return 0

    translations: dict[str, str] = dict(existing)
    chunks = [todo[i:i + chunk_size] for i in range(0, len(todo), chunk_size)]
    for i, chunk in enumerate(chunks, 1):
        logger.info("[%d/%d] Перевожу %d строк...", i, len(chunks), len(chunk))
        try:
            result = await translate_chunk(chunk)
            translations.update(result)
        except Exception as e:  # noqa: BLE001
            logger.error("[%d/%d] FAIL: %s", i, len(chunks), e)
            # Сохраняем что есть и продолжаем
            translations.update({k: k for k in chunk})

        # Промежуточное сохранение каждые 5 chunks (на случай прерывания)
        if i % 5 == 0 or i == len(chunks):
            _save(output_path, translations, total_input=len(all_strings))
            logger.info("✓ Промежуточное сохранение: %d / %d", len(translations), len(all_strings))

    _save(output_path, translations, total_input=len(all_strings))
    logger.info("=== Готово: %d переводов в %s ===", len(translations), output_path)
    return 0


def _save(output_path: Path, translations: dict[str, str], total_input: int) -> None:
    payload = {
        "_meta": {
            "total_input": total_input,
            "total_translated": len(translations),
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "translations": translations,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-перевод RU→EN UI-строк через Gemini")
    parser.add_argument(
        "--input",
        type=Path,
        default=_PROJECT_ROOT / "data" / "i18n" / "messages.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_PROJECT_ROOT / "data" / "i18n" / "messages_en.json",
    )
    parser.add_argument("--chunk-size", type=int, default=50)
    parser.add_argument("--resume", action="store_true", default=True,
                        help="Пропускать уже переведённые (default: True)")
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    return asyncio.run(main_async(args.input, args.output, args.chunk_size, args.resume, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
