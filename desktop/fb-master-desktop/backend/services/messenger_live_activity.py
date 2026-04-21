"""Лёгкий in-memory мост активности для встроенного Messenger webview."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

_lock = threading.Lock()
_state: dict[int, dict[str, str]] = {}


def mark_messenger_outbound_activity(
    fb_account_id: int | None,
    *,
    source: str = "automation",
    preview: str = "",
    open_thread_url: str | None = None,
) -> str:
    """Пометить, что для аккаунта появилось новое исходящее сообщение.

    open_thread_url — открыть этот URL во встроенном Messenger (webview), чтобы сразу
    увидеть диалог; иначе Facebook часто показывает пустой список «Нет чатов».
    """
    if fb_account_id is None:
        return ""
    now = datetime.now(timezone.utc)
    token = f"{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
    ou = (open_thread_url or "").strip()[:768]
    payload = {
        "token": token,
        "updated_at": now.isoformat(),
        "source": (source or "automation").strip()[:40] or "automation",
        "preview": (preview or "").strip()[:140],
        "open_thread_url": ou,
    }
    with _lock:
        _state[int(fb_account_id)] = payload
    return token


def get_messenger_outbound_activity(
    fb_account_id: int | None,
) -> dict[str, str]:
    if fb_account_id is None:
        return {
            "token": "",
            "updated_at": "",
            "source": "",
            "preview": "",
            "open_thread_url": "",
        }
    with _lock:
        payload = dict(_state.get(int(fb_account_id), {}))
    return {
        "token": str(payload.get("token") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "source": str(payload.get("source") or ""),
        "preview": str(payload.get("preview") or ""),
        "open_thread_url": str(payload.get("open_thread_url") or ""),
    }
