"""Фоновой опрос /message/inbox для всех подключённых Reddit-аккаунтов.

Запускается из app_factory.lifespan() как daemon-thread. Период — env
FB_MASTER_REDDIT_INBOX_POLL_SEC (default 120). Уважает rate-limits:
если у аккаунта в `RedditRateLimitState` остаток bucket="api" равен 0
и до сброса меньше now — пропускаем тик.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_thread_started = False
_DEFAULT_INTERVAL_SEC = 120
_INITIAL_DELAY_SEC = 30


def _interval_sec() -> int:
    raw = os.getenv("FB_MASTER_REDDIT_INBOX_POLL_SEC", "").strip()
    try:
        v = int(raw) if raw else _DEFAULT_INTERVAL_SEC
    except ValueError:
        v = _DEFAULT_INTERVAL_SEC
    return max(30, v)


def _tick_once() -> None:
    from backend.database import SessionLocal
    from backend.modules.reddit.models import RedditAccount, RedditRateLimitState
    from backend.modules.reddit.services.conversations import poll_inbox

    db = SessionLocal()
    try:
        accounts = (
            db.query(RedditAccount)
            .filter(RedditAccount.status == "connected")
            .all()
        )
        for acc in accounts:
            try:
                rl = (
                    db.query(RedditRateLimitState)
                    .filter(
                        RedditRateLimitState.account_id == acc.id,
                        RedditRateLimitState.bucket == "api",
                    )
                    .order_by(RedditRateLimitState.observed_at.desc())
                    .first()
                )
                if rl and rl.remaining is not None and rl.remaining <= 0:
                    if rl.reset_at and rl.reset_at > datetime.now(timezone.utc):
                        continue
                poll_inbox(db, acc, acc.organization_id, limit=25)
            except Exception:
                logger.exception("reddit_inbox_poll: account_id=%s", acc.id)
    finally:
        db.close()


def _loop() -> None:
    time.sleep(_INITIAL_DELAY_SEC)
    interval = _interval_sec()
    while True:
        try:
            _tick_once()
        except Exception:
            logger.exception("reddit_inbox_poller tick failed")
        time.sleep(interval)


def start_reddit_inbox_poller_thread() -> None:
    """Идемпотентный старт фонового потока опроса inbox."""
    global _thread_started
    if _thread_started:
        return
    if (os.getenv("FB_MASTER_REDDIT_INBOX_POLL_DISABLED", "") or "").strip().lower() in ("1", "true", "yes"):
        logger.info("reddit_inbox_poller отключён переменной FB_MASTER_REDDIT_INBOX_POLL_DISABLED")
        return
    threading.Thread(target=_loop, daemon=True, name="reddit-inbox-poller").start()
    _thread_started = True
    logger.info("reddit_inbox_poller запущен (интервал=%ss)", _interval_sec())


__all__ = ["start_reddit_inbox_poller_thread"]
