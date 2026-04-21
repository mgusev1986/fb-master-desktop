"""Вызовы OpenAI и Gemini по HTTP (httpx, без отдельных SDK)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from backend.services.cabinet_settings import split_google_llm_api_keys

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Порядок: сначала выбранная в настройках модель, затем запасные (2.0 — устаревает в API).
GEMINI_FALLBACK_MODELS: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]


def gemini_model_chain(preferred: str) -> list[str]:
    pref = (preferred or "").strip() or GEMINI_FALLBACK_MODELS[0]
    out: list[str] = []
    for m in [pref] + GEMINI_FALLBACK_MODELS:
        if m and m not in out:
            out.append(m)
    return out


def _gemini_error_allows_model_fallback(msg: str) -> bool:
    """Переключиться на другую модель Gemini (квота, 404 имени, перегруз)."""
    m = (msg or "").lower()
    return any(
        k in m
        for k in (
            "quota",
            "free_tier",
            "exceeded",
            "resource exhausted",
            "resource_exhausted",
            "429",
            "rate limit",
            "too many requests",
            "unavailable",
            "503",
            "not found",
            "not_found",
            "404",
            "invalid model",
            "unknown model",
            "high demand",
            "spikes in demand",
            "overload",
        )
    )


def _gemini_error_allows_api_key_rotation(msg: str) -> bool:
    """
    Следующий API-ключ (другая квота / лимит проекта).
    Не переключаем ключ при блокировках по содержанию — это не лечится сменой ключа.
    """
    m = (msg or "").lower()
    if any(
        k in m
        for k in (
            "safety",
            "blocked",
            "harm",
            "content policy",
            "was blocked",
            "promptfeedback",
        )
    ):
        return False
    return any(
        k in m
        for k in (
            "quota",
            "free_tier",
            "exceeded",
            "resource exhausted",
            "resource_exhausted",
            "429",
            "rate limit",
            "too many requests",
            "billing",
            "permission denied",
            "consumer_suspended",
            "api key not valid",
            "invalid api key",
            "api_key_invalid",
            "unauthorized",
            "401",
            "403",
            "high demand",
            "spikes in demand",
            "overload",
            "unavailable",
            "503",
        )
    )


async def complete_gemini_with_key_rotation(
    api_keys: list[str],
    preferred_model: str,
    system: str,
    user: str,
    *,
    timeout_s: float = 120.0,
    response_mime_json: bool = False,
) -> tuple[str, dict[str, Any]]:
    keys = [k.strip() for k in api_keys if k and str(k).strip()]
    if not keys:
        raise LLMError("Не задан GOOGLE_API_KEY в .env или ключи в настройках")
    last_err: LLMError | None = None
    for ki, api_key in enumerate(keys):
        try:
            text, meta = await complete_gemini_with_fallback(
                api_key,
                preferred_model,
                system,
                user,
                timeout_s=timeout_s,
                response_mime_json=response_mime_json,
            )
            meta = dict(meta)
            meta["gemini_api_key_index"] = ki
            meta["gemini_api_key_count"] = len(keys)
            return text, meta
        except LLMError as e:
            last_err = e
            if ki < len(keys) - 1 and _gemini_error_allows_api_key_rotation(str(e)):
                continue
            raise
    if last_err:
        raise last_err
    raise LLMError("Gemini: нет доступных ключей")


async def complete_gemini_with_fallback(
    api_key: str,
    preferred_model: str,
    system: str,
    user: str,
    *,
    timeout_s: float = 120.0,
    response_mime_json: bool = False,
) -> tuple[str, dict[str, Any]]:
    models = gemini_model_chain(preferred_model)
    last_err: LLMError | None = None
    for i, model in enumerate(models):
        try:
            text, meta = await complete_gemini(
                api_key,
                model,
                system,
                user,
                timeout_s=timeout_s,
                response_mime_json=response_mime_json,
            )
            meta = dict(meta)
            meta["gemini_model_used"] = model
            return text, meta
        except LLMError as e:
            last_err = e
            if i < len(models) - 1 and _gemini_error_allows_model_fallback(str(e)):
                continue
            raise
    if last_err:
        raise last_err
    raise LLMError("Gemini: нет моделей для запроса")


class LLMError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


async def complete_openai(
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    timeout_s: float = 120.0,
) -> tuple[str, dict[str, Any]]:
    if not api_key.strip():
        raise LLMError("Не задан OPENAI_API_KEY в .env")
    payload = {
        "model": model.strip(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", {})
            msg = detail.get("message") or r.text
        except Exception:
            msg = r.text or r.reason_phrase
        raise LLMError(f"OpenAI: {msg}", status_code=r.status_code)
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("OpenAI: пустой ответ (нет choices)")
    content = (choices[0].get("message") or {}).get("content")
    if not content or not str(content).strip():
        raise LLMError("OpenAI: пустой текст ответа")
    usage = data.get("usage") or {}
    meta = {
        "tokens_prompt": usage.get("prompt_tokens"),
        "tokens_completion": usage.get("completion_tokens"),
    }
    return str(content).strip(), meta


async def complete_gemini(
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    timeout_s: float = 120.0,
    response_mime_json: bool = False,
) -> tuple[str, dict[str, Any]]:
    if not api_key.strip():
        raise LLMError("Не задан GOOGLE_API_KEY в .env")
    m = model.strip()
    url = GEMINI_URL_TMPL.format(model=m)
    body: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user}],
            }
        ],
    }
    if system.strip():
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if response_mime_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            url,
            params={"key": api_key.strip()},
            headers={"Content-Type": "application/json"},
            json=body,
        )
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
            msg = err.get("message") or r.text
        except Exception:
            msg = r.text or r.reason_phrase
        raise LLMError(f"Gemini: {msg}", status_code=r.status_code)
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        prompt_fb = data.get("promptFeedback")
        block = (prompt_fb or {}).get("blockReason")
        raise LLMError(
            "Gemini: нет ответа"
            + (f" (блокировка: {block})" if block else "")
            + ". Попробуйте сменить формулировку."
        )
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise LLMError("Gemini: пустой текст ответа")
    um = data.get("usageMetadata") or {}
    meta = {
        "tokens_prompt": um.get("promptTokenCount"),
        "tokens_completion": um.get("candidatesTokenCount"),
    }
    return text, meta


async def complete_gemini_conversation(
    api_key: str,
    model: str,
    system: str,
    turns: list[tuple[str, str]],
    *,
    timeout_s: float = 90.0,
) -> tuple[str, dict[str, Any]]:
    """
    Мультитуровый диалог Gemini: turns — пары (роль, текст), роль «user» или «model».
    Последняя реплика должна быть от пользователя (user).
    """
    if not api_key.strip():
        raise LLMError("Не задан GOOGLE_API_KEY в .env")
    if not turns or turns[-1][0] != "user":
        raise LLMError("Gemini: последнее сообщение должно быть от пользователя")
    m = model.strip()
    url = GEMINI_URL_TMPL.format(model=m)
    contents: list[dict[str, Any]] = []
    for role, text in turns:
        r = role.strip().lower()
        if r not in ("user", "model"):
            raise LLMError(f"Gemini: неподдерживаемая роль {role!r}")
        t = (text or "").strip()
        if not t:
            continue
        contents.append({"role": r, "parts": [{"text": t}]})
    if not contents or contents[-1]["role"] != "user":
        raise LLMError("Gemini: нет сообщения пользователя в истории")
    body: dict[str, Any] = {"contents": contents}
    if system.strip():
        body["systemInstruction"] = {"parts": [{"text": system.strip()}]}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            url,
            params={"key": api_key.strip()},
            headers={"Content-Type": "application/json"},
            json=body,
        )
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
            msg = err.get("message") or r.text
        except Exception:
            msg = r.text or r.reason_phrase
        raise LLMError(f"Gemini: {msg}", status_code=r.status_code)
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        prompt_fb = data.get("promptFeedback")
        block = (prompt_fb or {}).get("blockReason")
        raise LLMError(
            "Gemini: нет ответа"
            + (f" (блокировка: {block})" if block else "")
            + ". Попробуйте сменить формулировку."
        )
    parts = (cands[0].get("content") or {}).get("parts") or []
    text_out = "".join(p.get("text", "") for p in parts).strip()
    if not text_out:
        raise LLMError("Gemini: пустой текст ответа")
    um = data.get("usageMetadata") or {}
    meta = {
        "tokens_prompt": um.get("promptTokenCount"),
        "tokens_completion": um.get("candidatesTokenCount"),
    }
    return text_out, meta


async def complete_gemini_conversation_with_fallback(
    api_key: str,
    preferred_model: str,
    system: str,
    turns: list[tuple[str, str]],
    *,
    timeout_s: float = 90.0,
) -> tuple[str, dict[str, Any]]:
    models = gemini_model_chain(preferred_model)
    last_err: LLMError | None = None
    for i, model in enumerate(models):
        try:
            text, meta = await complete_gemini_conversation(
                api_key, model, system, turns, timeout_s=timeout_s
            )
            meta = dict(meta)
            meta["gemini_model_used"] = model
            return text, meta
        except LLMError as e:
            last_err = e
            if i < len(models) - 1 and _gemini_error_allows_model_fallback(str(e)):
                continue
            raise
    if last_err:
        raise last_err
    raise LLMError("Gemini: нет моделей для запроса")


async def complete_gemini_conversation_with_key_rotation(
    api_keys: list[str],
    preferred_model: str,
    system: str,
    turns: list[tuple[str, str]],
    *,
    timeout_s: float = 90.0,
) -> tuple[str, dict[str, Any]]:
    keys = [k.strip() for k in api_keys if k and str(k).strip()]
    if not keys:
        raise LLMError("Не задан GOOGLE_API_KEY в .env или ключи в настройках")
    last_err: LLMError | None = None
    for ki, api_key in enumerate(keys):
        try:
            text, meta = await complete_gemini_conversation_with_fallback(
                api_key, preferred_model, system, turns, timeout_s=timeout_s
            )
            meta = dict(meta)
            meta["gemini_api_key_index"] = ki
            meta["gemini_api_key_count"] = len(keys)
            return text, meta
        except LLMError as e:
            last_err = e
            if ki < len(keys) - 1 and _gemini_error_allows_api_key_rotation(str(e)):
                continue
            raise
    if last_err:
        raise last_err
    raise LLMError("Gemini: нет доступных ключей")


async def complete_llm(
    provider: str,
    *,
    openai_key: str,
    google_key: str,
    model: str,
    system: str,
    user: str,
    response_mime_json: bool = False,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    p = (provider or "gemini").lower().strip()
    if p == "gemini":
        keys = split_google_llm_api_keys(google_key)
        if not keys:
            raise LLMError("Не задан GOOGLE_API_KEY в .env или ключи в настройках")
        if len(keys) == 1:
            return await complete_gemini_with_fallback(
                keys[0], model, system, user, response_mime_json=response_mime_json
            )
        return await complete_gemini_with_key_rotation(
            keys,
            model,
            system,
            user,
            response_mime_json=response_mime_json,
        )
    if p == "openai":
        return await complete_openai(openai_key, model, system, user)
    raise LLMError(f"Неизвестный провайдер: {provider}")


PREVIEW_LIMIT = 6000

__all__ = [
    "LLMError",
    "complete_llm",
    "complete_openai",
    "complete_gemini",
    "complete_gemini_conversation",
    "complete_gemini_conversation_with_fallback",
    "complete_gemini_conversation_with_key_rotation",
    "complete_gemini_with_fallback",
    "complete_gemini_with_key_rotation",
    "gemini_model_chain",
    "GEMINI_FALLBACK_MODELS",
    "PREVIEW_LIMIT",
]
