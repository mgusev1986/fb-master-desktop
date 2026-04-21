"""Ограничение числа параллельных Playwright-задач и блокировка одного FB-профиля."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from backend.config import PLAYWRIGHT_MAX_CONCURRENT

_slots = threading.Semaphore(max(1, PLAYWRIGHT_MAX_CONCURRENT))
_account_locks: dict[int, threading.Lock] = {}
_account_locks_guard = threading.Lock()


def _lock_for_fb_account(fb_account_id: int | None) -> threading.Lock | None:
    if fb_account_id is None:
        return None
    with _account_locks_guard:
        if fb_account_id not in _account_locks:
            _account_locks[fb_account_id] = threading.Lock()
        return _account_locks[fb_account_id]


@contextmanager
def playwright_run_slot(*, fb_account_id: int | None = None) -> Iterator[None]:
    """
    1) Не более PLAYWRIGHT_MAX_CONCURRENT одновременных тяжёлых задач.
    2) Для одного fb_account_id — не более одной фоновой задачи, чтобы не дёргать один аккаунт
       несколькими автоматизациями одновременно.

    Параллельно с мессенджером на том же аккаунте: воркеры берут сессию из БД и поднимают **отдельный**
    временный профиль Playwright, не открывая повторно account.profile_dir (его может держать Electron-webview).
    Если снимка сессии в БД нет, а папка профиля занята — см. warmup_worker._playwright_profile_dir_for_account.

    3) Если у аккаунта включён прокси и монитор пометил туннель как недоступный — слот не выдаётся
       (ProxyTunnelBlockedError), чтобы не слать трафик с «голого» IP.
    """
    if fb_account_id is not None:
        from backend.database import SessionLocal
        from backend.models import FBAccount
        from backend.services.proxy_health_guard import (
            PROXY_AUTOMATION_BLOCKED_HINT,
            ProxyTunnelBlockedError,
            account_proxy_tunnel_blocked,
        )

        db_chk = SessionLocal()
        try:
            acc = db_chk.get(FBAccount, int(fb_account_id))
            if account_proxy_tunnel_blocked(acc):
                detail = (getattr(acc, "proxy_tunnel_last_error", None) or "").strip()
                raise ProxyTunnelBlockedError(
                    detail or PROXY_AUTOMATION_BLOCKED_HINT
                )
        finally:
            db_chk.close()

    acc_lock = _lock_for_fb_account(fb_account_id)
    if acc_lock:
        acc_lock.acquire()
    try:
        _slots.acquire()
        try:
            yield
        finally:
            _slots.release()
    finally:
        if acc_lock:
            acc_lock.release()
