"""Reddit outreach worker — обработка approved queue items в browser-mode.

Reddit-кампании работают через `RedditCampaign` + `RedditCampaignQueue`.
Берём queue items с review_state="approved" и dispatching по `mode`:
  * mode="dm" → dm_sender (нужен receiver_username из payload).
  * mode="comment" → comment_sender (нужен post_url из payload).

Запуск:
  * вручную: `process_campaign_once(db, campaign_id, max_items=N)`;
  * фоном: `start_outreach_worker_thread()` тикает каждые ~180 сек.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.reddit.models import (
    RedditCampaign,
    RedditCampaignQueue,
)

logger = logging.getLogger(__name__)


_thread_started = False
_DEFAULT_INTERVAL_SEC = 180
_INITIAL_DELAY_SEC = 60


def _interval_sec() -> int:
    raw = os.getenv("FB_MASTER_REDDIT_OUTREACH_TICK_SEC", "").strip()
    try:
        return max(45, int(raw) if raw else _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


def _payload_field(item: RedditCampaignQueue, *names: str) -> str:
    payload = getattr(item, "payload_json", None) or {}
    for n in names:
        if isinstance(payload, dict) and payload.get(n):
            return str(payload[n])
    # Fallback: некоторые поля могут быть на queue items напрямую.
    for n in names:
        if hasattr(item, n) and getattr(item, n):
            return str(getattr(item, n))
    return ""


def process_item(db: Session, item: RedditCampaignQueue, *, headless: bool = True) -> dict[str, Any]:
    camp = db.get(RedditCampaign, item.campaign_id)
    if camp is None:
        item.review_state = "failed"
        item.last_error = "campaign_missing"
        db.commit()
        return {"ok": False, "reason_code": "campaign_missing"}
    if camp.status not in ("active", "draft"):
        return {"ok": False, "reason_code": f"campaign_status_{camp.status}", "skipped": True}

    item.attempts = (item.attempts or 0) + 1
    db.commit()

    # Нужен account: queue item.account_id или fallback на kandidate из leads.
    account_id = getattr(item, "account_id", None)
    if not account_id:
        item.review_state = "failed"
        item.last_error = "no_account_assigned"
        db.commit()
        return {"ok": False, "reason_code": "no_account_assigned"}

    body = (item.draft_body or "").strip() if hasattr(item, "draft_body") else _payload_field(item, "body", "text")
    if not body:
        body = _payload_field(item, "body", "text")
    mode = (camp.mode or "").lower().strip() if hasattr(camp, "mode") else "dm"

    if mode == "dm":
        from backend.modules.reddit.runtime.dm_sender import send_dm

        to = _payload_field(item, "to_user", "to_username", "receiver", "username")
        subject = _payload_field(item, "subject") or "Hi!"
        if not to:
            item.review_state = "failed"
            item.last_error = "no_recipient"
            db.commit()
            return {"ok": False, "reason_code": "no_recipient"}
        res = send_dm(db, account_id, to, body, subject=subject, headless=headless, daily_cap=getattr(camp, "daily_cap", None))
    elif mode == "comment":
        from backend.modules.reddit.runtime.comment_sender import comment_on_post

        post_url = _payload_field(item, "post_url", "url", "target_url")
        if not post_url:
            item.review_state = "failed"
            item.last_error = "no_post_url"
            db.commit()
            return {"ok": False, "reason_code": "no_post_url"}
        res = comment_on_post(db, account_id, post_url, body, headless=headless, daily_cap=getattr(camp, "daily_cap", None))
    else:
        item.review_state = "failed"
        item.last_error = f"mode_not_implemented:{mode}"
        db.commit()
        return {"ok": False, "reason_code": "mode_not_implemented"}

    if res.get("ok"):
        item.review_state = "sent"
        if hasattr(item, "sent_at"):
            item.sent_at = datetime.now(timezone.utc)
        item.last_error = None
    else:
        rc = res.get("reason_code") or "unknown"
        if rc in ("daily_cap_reached", "rate_limited", "user_not_found", "chat_unavailable"):
            item.review_state = "skipped"
        elif rc in ("login_required", "account_suspended"):
            item.review_state = "failed"
        else:
            item.review_state = "failed"
        item.last_error = (rc + (": " + (res.get("details") or "") if res.get("details") else ""))[:512]
    db.commit()
    return res


def process_campaign_once(
    db: Session,
    campaign_id: int,
    *,
    max_items: int = 5,
    headless: bool = True,
    inter_item_pause_range: tuple[int, int] = (5, 15),
) -> dict[str, Any]:
    camp = db.get(RedditCampaign, campaign_id)
    if camp is None:
        return {"ok": False, "reason_code": "campaign_not_found"}
    items = (
        db.query(RedditCampaignQueue)
        .filter(
            RedditCampaignQueue.campaign_id == campaign_id,
            RedditCampaignQueue.review_state == "approved",
        )
        .order_by(RedditCampaignQueue.created_at.asc())
        .limit(max_items)
        .all()
    )
    sent = 0
    skipped = 0
    failed = 0
    halted: str | None = None
    for idx, it in enumerate(items):
        res = process_item(db, it, headless=headless)
        if res.get("ok"):
            sent += 1
        elif res.get("skipped") or it.review_state == "skipped":
            skipped += 1
        else:
            failed += 1
            rc = res.get("reason_code") or ""
            if rc in ("login_required", "account_suspended"):
                halted = rc
                break
        if idx < len(items) - 1:
            lo, hi = inter_item_pause_range
            time.sleep(random.uniform(lo, hi))
    return {"ok": True, "sent": sent, "skipped": skipped, "failed": failed, "halted_reason": halted}


def _tick_once(headless: bool = True) -> None:
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        actives = (
            db.query(RedditCampaign)
            .filter(RedditCampaign.status == "active")
            .all()
        )
        for camp in actives:
            try:
                process_campaign_once(db, camp.id, max_items=3, headless=headless)
            except Exception:
                logger.exception("reddit outreach_worker tick: campaign_id=%s", camp.id)
    finally:
        db.close()


def _loop() -> None:
    time.sleep(_INITIAL_DELAY_SEC)
    interval = _interval_sec()
    while True:
        try:
            _tick_once(headless=True)
        except Exception:
            logger.exception("reddit_outreach_worker tick failed")
        time.sleep(interval)


def start_outreach_worker_thread() -> None:
    global _thread_started
    if _thread_started:
        return
    if (os.getenv("FB_MASTER_REDDIT_OUTREACH_DISABLED", "") or "").strip().lower() in ("1", "true", "yes"):
        logger.info("reddit_outreach_worker отключён переменной FB_MASTER_REDDIT_OUTREACH_DISABLED")
        return
    threading.Thread(target=_loop, daemon=True, name="reddit-outreach-worker").start()
    _thread_started = True
    logger.info("reddit_outreach_worker запущен (интервал=%ss)", _interval_sec())


__all__ = ["process_campaign_once", "process_item", "start_outreach_worker_thread"]
