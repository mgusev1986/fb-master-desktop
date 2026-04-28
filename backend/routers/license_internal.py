"""Internal license trigger endpoint (v3.0+).

Вызывается ТОЛЬКО локальным Electron-процессом (loopback): при `online`-event
и сразу после старта приложения. Дёргает license_watcher, чтобы проверить
свежесть лицензии немедленно вместо ожидания 30-секундного цикла.

Защита: разрешён только loopback IP. Внешние запросы → 403.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.services.license_watcher import (
    current_state,
    is_active_on_this_machine,
    trigger_recheck_now,
)

router = APIRouter(prefix="/internal/license", tags=["internal"])
logger = logging.getLogger(__name__)


_LOOPBACK = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def _is_loopback(request: Request) -> bool:
    client = request.client
    if not client:
        return False
    return (client.host or "").lower() in _LOOPBACK


@router.post("/recheck-now")
async def license_recheck_now(request: Request):
    """Триггер немедленной проверки лицензии. Вызов из Electron при online-event."""
    if not _is_loopback(request):
        return JSONResponse({"ok": False, "error": "loopback_only"}, status_code=403)
    if not is_active_on_this_machine():
        return JSONResponse({"ok": True, "triggered": False, "reason": "watcher_inactive"})
    trigger_recheck_now()
    state = current_state()
    return JSONResponse({
        "ok": True,
        "triggered": True,
        "current_status": state.get("status") or "init",
    })


@router.get("/state")
async def license_state(request: Request):
    """Текущий снимок состояния лицензии (debug). Loopback only."""
    if not _is_loopback(request):
        return JSONResponse({"ok": False, "error": "loopback_only"}, status_code=403)
    state = current_state()
    return JSONResponse({
        "active": is_active_on_this_machine(),
        "status": state.get("status"),
        "reason": state.get("reason"),
        "verified_at": state.get("verified_at"),
        "expires_at": state.get("expires_at"),
        "last_vps_success_at": state.get("last_vps_success_at"),
        "suspect_count": state.get("suspect_count"),
        "probes_count": len(state.get("probes") or []),
        "probes_valid_until": state.get("probes_valid_until"),
    })


__all__ = ["router"]
