"""Нейро-варианты текста шаблона (сохранение плейсхолдеров {{full_name}}, {{first_name}})."""

from __future__ import annotations

import ast
import json
import logging
import re

from backend.services.ai_agent_llm import LLMError, complete_llm

logger = logging.getLogger(__name__)

SYSTEM = """Ты помощник для шаблонов сообщений (мессенджер, рассылка).
Пользователь пришлёт текст шаблона. В нём могут быть плейсхолдеры ровно в виде {{full_name}} и {{first_name}} — их нужно сохранить в каждом варианте буква в букву, без изменений и без «улучшений».
Сгенерируй N разных формулировок с тем же смыслом и тоном, на том же языке, что и исходник. Варианты должны заметно отличаться друг от друга по формулировкам.
Ответь ТОЛЬКО одним JSON-массивом из N строк: строки в двойных кавычках по правилам JSON. Без текста до или после массива, без markdown-блоков ```, без пояснений, без объекта-обёртки — только массив.
Пример: ["вариант 1", "вариант 2"]"""


def _unwrap_markdown_fence(raw: str) -> str:
    s = (raw or "").strip().lstrip("\ufeff")
    m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", s, re.I)
    if m:
        inner = m.group(1).strip()
        if inner:
            return inner
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _normalize_typographic_quotes(s: str) -> str:
    return (
        s.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u00ab", '"')
        .replace("\u00bb", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


def _extract_balanced_array(s: str) -> str | None:
    """Первый [...] с учётом строк в \" или ' (глубина скобок)."""
    start = s.find("[")
    if start < 0:
        return None
    depth = 0
    i = start
    in_str = False
    esc = False
    q = '"'
    while i < len(s):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str = True
            q = c
            i += 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
        i += 1
    return None


def _coerce_to_str_list(data: object) -> list[str]:
    if isinstance(data, dict):
        for k in ("variants", "items", "messages", "texts", "data", "result"):
            if k in data and isinstance(data[k], list):
                data = data[k]
                break
        else:
            raise ValueError("expected list or object with variants")
    if not isinstance(data, list):
        raise ValueError("not a json array")
    out: list[str] = []
    for x in data:
        t = str(x).strip()
        if t:
            out.append(t)
    return out


def _loads_json_or_python_list(s: str) -> object:
    s = _normalize_typographic_quotes(s.strip())
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return ast.literal_eval(s)


def _parse_variants_array(raw: str) -> list[str]:
    s = _unwrap_markdown_fence(raw)
    s = _normalize_typographic_quotes(s)

    candidates: list[str] = []
    ext = _extract_balanced_array(s)
    if ext:
        candidates.append(ext)
    if s not in candidates:
        candidates.append(s)

    last_err: Exception | None = None
    for chunk in candidates:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = _loads_json_or_python_list(chunk)
            return _coerce_to_str_list(data)
        except (json.JSONDecodeError, ValueError, SyntaxError, TypeError, MemoryError) as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise ValueError("empty or unparseable response")


async def generate_template_variations(
    *,
    provider: str,
    openai_key: str,
    google_key: str,
    model: str,
    source_body: str,
    count: int,
) -> list[str]:
    body = (source_body or "").strip()
    if not body:
        raise LLMError("Пустой текст шаблона")
    n = max(1, min(int(count), 40))
    user = (
        f"N={n}\n\nИСХОДНЫЙ ШАБЛОН (сохрани все {{full_name}} и {{first_name}} как есть):\n\n{body[:12000]}"
    )
    prov = (provider or "").lower().strip()
    text, _meta = await complete_llm(
        provider,
        openai_key=openai_key,
        google_key=google_key,
        model=model,
        system=SYSTEM,
        user=user,
        response_mime_json=(prov == "gemini"),
    )
    try:
        variants = _parse_variants_array(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("neuro variants parse fail: %s raw_head=%s", e, text[:400])
        raise LLMError("Модель вернула не JSON. Попробуйте ещё раз или смените провайдера.") from e
    if len(variants) < 1:
        raise LLMError("Модель не вернула ни одного варианта. Повторите запрос.")
    if n >= 3 and len(variants) < min(3, n):
        raise LLMError(
            f"Слишком мало вариантов ({len(variants)} из {n}). Повторите запрос или уменьшите число."
        )
    return variants[:n] if len(variants) > n else variants
