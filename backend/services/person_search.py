"""Общая фильтрация списка Person по строке поиска (имя, URL, токены, без учёта регистра для Unicode)."""

from __future__ import annotations

import re

from sqlalchemy import func, literal, or_
from sqlalchemy.orm import Query

from backend.database import using_postgresql
from backend.models import Person


def _escape_like_fragment(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sqlite_like_case_variants(token: str) -> list[str]:
    """
    Варианты написания токена для LIKE в SQLite: встроенный lower()/LIKE не учитывают регистр кириллицы,
    поэтому перебираем типичные формы из Python (casefold, upper, title…).
    """
    t = (token or "").strip()
    if not t:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in (t, t.casefold(), t.lower(), t.upper(), t.capitalize(), t.title()):
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def apply_person_text_search_filter(query: Query, q_raw: str) -> Query:
    """
    AND по словам: каждое слово должно встречаться (без учёта регистра) в объединении
    display_name, first_name, canonical_url.

    PostgreSQL: func.lower() даёт корректную нижнюю строку для Unicode.
    SQLite: lower() не трогает кириллицу — OR из LIKE по вариантам регистра из Python.
    """
    q = (q_raw or "").strip()
    if not q:
        return query

    tokens = [t for t in re.split(r"\s+", q) if t]
    if not tokens:
        return query

    combined_plain = func.concat(
        func.coalesce(Person.display_name, ""),
        literal(" "),
        func.coalesce(Person.first_name, ""),
        literal(" "),
        func.coalesce(Person.canonical_url, ""),
    )

    if using_postgresql():
        hay = func.lower(combined_plain)
        for tok in tokens:
            frag = _escape_like_fragment(tok.casefold())
            pat = f"%{frag}%"
            query = query.filter(hay.like(pat, escape="\\"))
        return query

    for tok in tokens:
        variants = _sqlite_like_case_variants(tok)
        if not variants:
            continue
        ors = [
            combined_plain.like(f"%{_escape_like_fragment(v)}%", escape="\\") for v in variants
        ]
        query = query.filter(or_(*ors))
    return query
