"""Подробные диагностические файлы по неуспешным job_events."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import LOG_DIAGNOSTICS_DIR
from backend.services.job_journal_narrative import humanize_error_summary

logger = logging.getLogger(__name__)

_MAX_DEPTH = 6
_MAX_ITEMS = 50
_MAX_STRING = 8000


def _slug(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return "event"
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw)
    cleaned = cleaned.strip("-")
    return cleaned or "event"


def _truncate_text(value: str) -> str:
    s = value if len(value) <= _MAX_STRING else value[: _MAX_STRING - 3] + "..."
    return s


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth >= _MAX_DEPTH:
        return _truncate_text(repr(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= _MAX_ITEMS:
                out["__truncated__"] = f"Only first {_MAX_ITEMS} keys are stored"
                break
            out[str(k)] = _json_safe(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [_json_safe(v, depth=depth + 1) for v in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out.append(f"... truncated after {_MAX_ITEMS} items")
        return out
    return _truncate_text(repr(value))


def _raw_error_text(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("detail", "error", "reason", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _reason_parts(raw_error: str) -> tuple[str | None, str]:
    raw = (raw_error or "").strip()
    if ":" not in raw:
        return None, raw
    code, text = raw.split(":", 1)
    code = code.strip().lower().replace("-", "_")
    return code or None, text.strip()


def _reason_summary(action: str | None, raw_error: str) -> tuple[str, str | None, str]:
    code, message = _reason_parts(raw_error)
    act = (action or "").strip().lower().replace("-", "_")
    hint, cleaned = humanize_error_summary(raw_error)
    detail = cleaned or raw_error or "Причина не передана"

    if code == "ai_llm":
        target = "комментария" if act in (
            "comment_post",
            "like_and_comment",
            "like_comment",
            "like_comment_friend_request",
            "like_comment_friend_request_send_dm",
        ) else "текста"
        return (
            f"Шаг {act or 'sequence'} остановился до выполнения действия: ИИ-генерация {target} вернула ошибку.",
            hint,
            message or detail,
        )
    if code == "ai_error":
        return (
            f"Шаг {act or 'sequence'} остановился на внутренней ошибке при обращении к ИИ.",
            hint,
            message or detail,
        )
    if code == "nav":
        return (
            f"Шаг {act or 'sequence'} не смог открыть страницу профиля перед действием.",
            hint,
            message or detail,
        )
    if code == "no_comment_text":
        return (
            "Комментарий не был отправлен: для шага не найден текст комментария.",
            hint,
            detail,
        )
    if code == "no_dm_text":
        return (
            "Сообщение не было отправлено: для шага не найден текст сообщения.",
            hint,
            detail,
        )
    if code == "ai_empty_comment":
        return (
            "Комментарий не был отправлен: ИИ вернул пустой текст.",
            hint,
            detail,
        )
    if code == "ai_empty_dm":
        return (
            "Сообщение не было отправлено: ИИ вернул пустой текст.",
            hint,
            detail,
        )
    if hint:
        return (
            f"Событие {act or 'job'} завершилось с ошибкой.",
            hint,
            detail,
        )
    return (
        f"Событие {act or 'job'} завершилось с ошибкой.",
        None,
        detail,
    )


def write_failure_diagnostic(
    *,
    job_id: int,
    event_id: int,
    event_type: str,
    severity: str,
    outcome: str | None,
    payload: dict[str, Any] | None,
    correlation_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[Path | None, str | None]:
    try:
        now = datetime.now(timezone.utc)
        action = ""
        if isinstance(payload, dict):
            action = str(payload.get("action") or "").strip()
        summary_ru, hint_ru, detail_ru = _reason_summary(action, _raw_error_text(payload))

        date_dir = LOG_DIAGNOSTICS_DIR / now.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{now.strftime('%H%M%S')}_job{job_id}_event{event_id}_"
            f"{_slug(event_type)}_{_slug(action or 'detail')}.json"
        )
        path = date_dir / filename

        body = {
            "timestamp_utc": now.isoformat(),
            "summary_ru": summary_ru,
            "hint_ru": hint_ru,
            "detail_ru": detail_ru,
            "job": {
                "id": job_id,
                "correlation_id": correlation_id,
            },
            "event": {
                "id": event_id,
                "event_type": event_type,
                "severity": severity,
                "outcome": outcome,
            },
            "payload": _json_safe(payload or {}),
            "context": _json_safe(context or {}),
        }
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.error("Failure diagnostic written: %s", path)
        return path, summary_ru
    except Exception:
        logger.exception("write_failure_diagnostic job=%s event=%s", job_id, event_id)
        return None, None
