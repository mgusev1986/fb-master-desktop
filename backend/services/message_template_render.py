"""Подстановка плейсхолдеров в текст шаблона (рассылка, прогрев)."""

from __future__ import annotations

import re
import secrets
from typing import Any

from sqlalchemy.orm import Session

# Разделитель вариантов в одном поле body (не используйте эту строку внутри текста сообщения).
VARIANT_SPLIT_LINE = "\n<<<TPL_VARIANT>>>\n"

# Строка целиком — только маркер (как в подсказке UI). Раньше требовались ещё \n до/после
# в составе VARIANT_SPLIT_LINE; без них весь body считался одним вариантом и уходил в ЛС целиком.
_VARIANT_LINE_ONLY_RE = re.compile(r"^\s*<<<TPL_VARIANT>>>\s*$", re.MULTILINE)

# Англоязычные пакеты: имя как отдельное слово NAME → плейсхолдер имени.
_NAME_PLACEHOLDER_WORD_RE = re.compile(r"\bNAME\b")


def normalize_name_placeholder_token(text: str) -> str:
    """Заменяет отдельное слово NAME на {{first_name}} (как в примерах пакетов)."""
    if not text:
        return ""
    return _NAME_PLACEHOLDER_WORD_RE.sub("{{first_name}}", text)


def split_package_paste(raw: str) -> list[str]:
    """
    Несколько сообщений в одном вставленном тексте: блоки разделены одной или несколькими пустыми строками.
    В каждом блоке слово NAME (целое слово) нормализуется в {{first_name}}.
    """
    t = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return []
    chunks = re.split(r"\n(?:\s*\n)+", t)
    out: list[str] = []
    for ch in chunks:
        s = ch.strip()
        if not s:
            continue
        out.append(normalize_name_placeholder_token(s))
    return out


def prepare_template_body_for_storage(body: str) -> str:
    """
    Перед сохранением шаблона: явные варианты (<<<TPL_VARIANT>>>) оставляем структурно;
    иначе, если в тексте несколько абзацев через пустую строку — разносим по вариантам;
    в любом случае применяем NAME → {{first_name}} по частям.
    """
    b = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not b:
        return ""
    if _VARIANT_LINE_ONLY_RE.search(b) or VARIANT_SPLIT_LINE in b:
        parts = split_template_variants(b)
        norm = [normalize_name_placeholder_token(p) for p in parts]
        return join_template_variants(norm) if len(norm) > 1 else (norm[0] if norm else "")
    pkg = split_package_paste(b)
    if len(pkg) >= 2:
        return join_template_variants(pkg)
    if len(pkg) == 1:
        return pkg[0]
    return normalize_name_placeholder_token(b)


def split_template_variants(body: str) -> list[str]:
    b = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not b:
        return []
    if _VARIANT_LINE_ONLY_RE.search(b):
        parts = _VARIANT_LINE_ONLY_RE.split(b)
        out = [p.strip() for p in parts if p.strip()]
        if out:
            return out
    if VARIANT_SPLIT_LINE in b:
        parts = b.split(VARIANT_SPLIT_LINE)
        out = [p.strip() for p in parts if p.strip()]
        return out if out else [b]
    return [b]


def join_template_variants(variants: list[str]) -> str:
    """Склеить варианты в одно значение для поля шаблона."""
    clean = [str(v).strip() for v in variants if str(v).strip()]
    if not clean:
        return ""
    return VARIANT_SPLIT_LINE.join(clean)


def count_template_variants(body: str) -> int:
    return len(split_template_variants(body or ""))


def pick_random_template_variant(body: str) -> str:
    """
    Всегда выбирает один из вариантов в body (через VARIANT_SPLIT_LINE) случайно.
    Не трогает variant_cursor — для рассылки и максимальной рандомизации при каждой отправке.
    """
    variants = split_template_variants(body)
    if not variants:
        return ""
    if len(variants) == 1:
        return variants[0]
    return secrets.choice(variants)


def resolve_template_variant(
    body: str,
    *,
    db: Session | None = None,
    template_row: Any | None = None,
) -> str:
    """
    Если в body несколько вариантов (через VARIANT_SPLIT_LINE), возвращает один:
    random — случайно; sequential — по кругу (нужны db и template_row с variant_cursor).
    """
    variants = split_template_variants(body)
    if len(variants) <= 1:
        return variants[0] if variants else ""
    mode = "random"
    if template_row is not None:
        mode = (getattr(template_row, "variant_pick_mode", None) or "random").strip().lower()
    if mode == "sequential" and db is not None and template_row is not None:
        idx = int(getattr(template_row, "variant_cursor", 0) or 0)
        pick = variants[idx % len(variants)]
        template_row.variant_cursor = idx + 1
        db.add(template_row)
        return pick
    return pick_random_template_variant(body)


def placeholder_kwargs_from_person(person: Any) -> dict[str, str]:
    """Словарь для apply_template_placeholders из модели Person (или объекта с display_name / first_name)."""
    display = (getattr(person, "display_name", None) or getattr(person, "first_name", None) or "")
    display = str(display).strip()
    first = (getattr(person, "first_name", None) or "")
    first = str(first).strip()
    if not first and display:
        first = display.split()[0]
    fb = display or "друг"
    ff = first or fb
    return {"full_name": fb, "first_name": ff}


def apply_template_placeholders(text: str, **kwargs: Any) -> str:
    """
    Подстановка плейсхолдеров в текстах рассылки и прогрева.
    Маркеры (допускаются пробелы внутри скобок):
      {{full_name}} — имя и фамилия целиком (как в «Базе контактов»);
      {{first_name}} — только имя (поле «имя» или первое слово из полного).
    Значения берутся из базы и при выполнении на странице контакта.
    Фильтры вроде {{full_name|upper}} не поддерживаются. Неизвестные плейсхолдеры не меняются.
    """
    if not text:
        return ""

    def _norm_key(raw: str) -> str:
        return raw.strip().lower().replace(" ", "_")

    mapping = {_norm_key(k): str(v) if v is not None else "" for k, v in kwargs.items()}
    # Старые шаблоны могли содержать {{name}} — подставляем из full_name
    if "name" in mapping and "full_name" not in mapping:
        mapping["full_name"] = mapping["name"]
    if "full_name" in mapping and "name" not in mapping:
        mapping["name"] = mapping["full_name"]

    def repl(m: re.Match[str]) -> str:
        key = _norm_key(m.group(1))
        return mapping.get(key, m.group(0))

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, text)


def preview_sample(text: str) -> str:
    """Демо-подстановка для предпросмотра в UI (первый вариант, если их несколько)."""
    parts = split_template_variants(text or "")
    base = parts[0] if parts else ""
    return apply_template_placeholders(
        base,
        full_name="Иван Петров",
        first_name="Иван",
    )


def render_outreach_message_for_person(db: Session, template_id: int | None, person: Any) -> str:
    """Текст ЛС для рассылки: случайный вариант шаблона + плейсхолдеры (каждый вызов — новый выбор)."""
    if not template_id:
        return ""
    from backend.models import Template

    t = db.get(Template, int(template_id))
    if not t:
        return ""
    single = pick_random_template_variant(t.body)
    return apply_template_placeholders(single, **placeholder_kwargs_from_person(person))
