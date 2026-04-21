"""Instagram template AI."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from backend.services.ai_agent_llm import LLMError, complete_llm

logger = logging.getLogger(__name__)


_PLACEHOLDERS = ("handle", "full_name", "first_name", "bio", "topic", "recent_post")
_PLACEHOLDER_HINT = ", ".join("{" + p + "}" for p in _PLACEHOLDERS)

_SYSTEM = (
    "Ты помощник для outreach в Instagram (Direct DM, комментарии). "
    f"Плейсхолдеры в фигурных скобках: {_PLACEHOLDER_HINT}. Сохраняй их буква в букву. "
    "Сохраняй язык исходного текста. Тон Instagram — короткий, эмоциональный, "
    "избегай spam-маркеров (не используй CAPS, гарантии, скидки большими цифрами)."
)


def _unwrap_fence(raw: str) -> str:
    s = (raw or "").strip().lstrip("\ufeff")
    m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", s, re.I)
    if m:
        return m.group(1).strip()
    return s


def _normalize_quotes(s: str) -> str:
    return (
        s.replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u00ab", '"').replace("\u00bb", '"')
        .replace("\u2018", "'").replace("\u2019", "'")
    )


def _extract_array(raw: str) -> list[str] | None:
    s = _normalize_quotes(_unwrap_fence(raw))
    try:
        v = json.loads(s)
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return [x.strip() for x in v if x.strip()]
    except Exception:  # noqa: BLE001
        pass
    lines: list[str] = []
    for ln in s.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        ln = re.sub(r"^\s*(?:\d+[\.\)]|\-|\*|•|—)\s*", "", ln)
        ln = ln.strip('"').strip("'")
        if ln:
            lines.append(ln)
    return lines or None


def _missing(original: str, candidate: str) -> list[str]:
    miss: list[str] = []
    for p in _PLACEHOLDERS:
        token = "{" + p + "}"
        if token in original and token not in candidate:
            miss.append(token)
    return miss


def _filter(original: str, variants: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in variants:
        s = (v or "").strip()
        if not s or s in seen:
            continue
        if _missing(original, s):
            continue
        out.append(s)
        seen.add(s)
    return out


def _resolve(db: Session) -> tuple[str, str, str, str]:
    from backend.services.ai_agent_service import load_ui_settings
    from backend.services.cabinet_settings import (
        effective_google_llm_api_key, effective_openai_api_key,
    )

    ui = load_ui_settings(db)
    provider = str(ui.get("default_provider") or "gemini").strip().lower()
    if provider not in ("gemini", "openai"):
        provider = "gemini"
    oa = (effective_openai_api_key(db) or "").strip()
    gk = (effective_google_llm_api_key(db) or "").strip()
    if provider == "gemini":
        model = str(ui.get("model_gemini") or "gemini-2.5-flash").strip()
    else:
        model = str(ui.get("model_openai") or "gpt-4o-mini").strip()
    return provider, oa, gk, model


async def _call_llm(db: Session, *, system: str, user: str, response_mime_json: bool = False) -> str:
    provider, oa, gk, model = _resolve(db)
    order = [provider]
    if provider == "openai" and "gemini" not in order:
        order.append("gemini")
    elif provider == "gemini" and "openai" not in order:
        order.append("openai")
    last_err = ""
    for p in order:
        if p == "openai" and not oa.strip():
            continue
        if p == "gemini" and not gk.strip():
            continue
        m = (
            "gemini-2.5-flash" if p == "gemini" and provider != "gemini" else
            ("gpt-4o-mini" if p == "openai" and provider != "openai" else model)
        )
        try:
            text, _meta = await complete_llm(
                provider=p, openai_key=oa, google_key=gk, model=m,
                system=system, user=user, response_mime_json=(p == "gemini" and response_mime_json),
            )
            return text or ""
        except LLMError as e:
            last_err = str(e)
            continue
    raise LLMError(last_err or "Нет доступного LLM-ключа")


async def generate_variations(db: Session, body: str, *, n: int = 3) -> dict[str, Any]:
    body = (body or "").strip()
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    n = max(1, min(8, int(n)))
    user = (
        f"Сгенерируй {n} РАЗНЫХ формулировок для следующего Instagram outreach-сообщения. "
        "Сохрани смысл, тон и язык, плейсхолдеры обязательны. "
        "Ответь ТОЛЬКО JSON-массивом строк.\n\n"
        f"Исходный текст:\n{body}"
    )
    try:
        raw = await _call_llm(db, system=_SYSTEM, user=user, response_mime_json=True)
    except LLMError as e:
        return {"ok": False, "reason_code": "llm_error", "details": str(e)[:200]}
    variants = _extract_array(raw or "")
    if not variants:
        return {"ok": False, "reason_code": "parse_failed", "details": (raw or "")[:200]}
    filtered = _filter(body, variants)
    if not filtered:
        return {"ok": False, "reason_code": "all_dropped_placeholders"}
    return {"ok": True, "variants": filtered[:n]}


async def _single_op(db: Session, body: str, instruction: str) -> dict[str, Any]:
    body = (body or "").strip()
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    user = (
        f"{instruction} Плейсхолдеры обязательны. Ответь ТОЛЬКО переписанным текстом, без markdown.\n\n"
        f"Исходный текст:\n{body}"
    )
    try:
        raw = await _call_llm(db, system=_SYSTEM, user=user)
    except LLMError as e:
        return {"ok": False, "reason_code": "llm_error", "details": str(e)[:200]}
    text = _unwrap_fence(raw or "").strip().strip('"').strip("'")
    if not text:
        return {"ok": False, "reason_code": "empty_response"}
    miss = _missing(body, text)
    if miss:
        return {"ok": False, "reason_code": "lost_placeholders", "details": ",".join(miss)}
    return {"ok": True, "text": text}


async def rewrite(db: Session, body: str, intent: str) -> dict[str, Any]:
    intent = (intent or "more natural").strip()
    return await _single_op(db, body, f"Перепиши Instagram outreach-текст в стиле «{intent}», тот же смысл.")


async def shorten(db: Session, body: str, *, max_chars: int = 300) -> dict[str, Any]:
    return await _single_op(db, body, f"Сократи Instagram outreach-текст до {int(max_chars)} символов.")


async def soften(db: Session, body: str) -> dict[str, Any]:
    return await _single_op(
        db, body,
        "Перепиши Instagram outreach-текст: убери spam-тон, не используй CAPS, "
        "избегай гарантий и больших скидок-цифр, сделай тон дружеским и эмоциональным.",
    )


__all__ = ["generate_variations", "rewrite", "shorten", "soften"]
