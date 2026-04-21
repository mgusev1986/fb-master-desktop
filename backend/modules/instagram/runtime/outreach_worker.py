"""Instagram outreach worker (DM / comment / story_reply)."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import (
    InstagramLead, InstagramOutreachCampaign, InstagramOutreachQueueItem,
)

logger = logging.getLogger(__name__)


_thread_started = False
_DEFAULT_INTERVAL_SEC = 240  # реже чем у других — IG агрессивнее
_INITIAL_DELAY_SEC = 75


def _interval_sec() -> int:
    raw = os.getenv("FB_MASTER_INSTAGRAM_OUTREACH_TICK_SEC", "").strip()
    try:
        return max(60, int(raw) if raw else _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


def process_item(db: Session, item: InstagramOutreachQueueItem, *, headless: bool = True) -> dict[str, Any]:
    camp = db.get(InstagramOutreachCampaign, item.campaign_id)
    if camp is None:
        item.review_state = "failed"
        item.last_error = "campaign_missing"
        db.commit()
        return {"ok": False, "reason_code": "campaign_missing"}
    if camp.status not in ("active", "draft"):
        return {"ok": False, "reason_code": f"campaign_status_{camp.status}", "skipped": True}

    item.attempts = (item.attempts or 0) + 1
    db.commit()

    if not camp.account_id:
        item.review_state = "failed"
        item.last_error = "no_account_assigned"
        db.commit()
        return {"ok": False, "reason_code": "no_account_assigned"}

    lead = db.get(InstagramLead, item.lead_id)
    if lead is None:
        item.review_state = "failed"
        item.last_error = "lead_missing"
        db.commit()
        return {"ok": False, "reason_code": "lead_missing"}

    body = item.draft_body or ""
    mode = (camp.mode or "dm").lower().strip()

    if mode == "dm":
        from backend.modules.instagram.runtime.dm_sender import send_dm
        res = send_dm(db, camp.account_id, lead.handle, body, headless=headless, daily_cap=camp.daily_cap)
    elif mode == "comment":
        from backend.modules.instagram.runtime.comment_sender import comment_on_post
        if not item.target_post_url:
            item.review_state = "failed"
            item.last_error = "no_target_post_url"
            db.commit()
            return {"ok": False, "reason_code": "no_target_post_url"}
        res = comment_on_post(db, camp.account_id, item.target_post_url, body, headless=headless, daily_cap=camp.daily_cap)
    elif mode == "story_reply":
        # story_reply пока работает как DM (Instagram-логика sendDM-к-story аналогична DM с context).
        from backend.modules.instagram.runtime.dm_sender import send_dm
        res = send_dm(db, camp.account_id, lead.handle, body, headless=headless, daily_cap=camp.daily_cap)
    else:
        item.review_state = "failed"
        item.last_error = f"mode_not_implemented:{mode}"
        db.commit()
        return {"ok": False, "reason_code": "mode_not_implemented"}

    if res.get("ok"):
        item.review_state = "sent"
        item.sent_at = datetime.now(timezone.utc)
        item.last_error = None
        if mode == "dm":
            lead.outreach_stage = "dm_sent"
            lead.last_outreach_at = item.sent_at
            db.commit()
    else:
        rc = res.get("reason_code") or "unknown"
        if rc in ("daily_cap_reached", "user_not_found"):
            item.review_state = "skipped"
        elif rc in ("login_required", "account_restricted"):
            item.review_state = "failed"
        else:
            item.review_state = "failed"
        item.last_error = (rc + (": " + (res.get("details") or "") if res.get("details") else ""))[:512]
    db.commit()
    return res


def process_campaign_once(
    db: Session, campaign_id: int, *, max_items: int = 5,
    headless: bool = True, inter_item_pause_range: tuple[int, int] = (8, 18),
) -> dict[str, Any]:
    camp = db.get(InstagramOutreachCampaign, campaign_id)
    if camp is None:
        return {"ok": False, "reason_code": "campaign_not_found"}
    items = (
        db.query(InstagramOutreachQueueItem)
        .filter(
            InstagramOutreachQueueItem.campaign_id == campaign_id,
            InstagramOutreachQueueItem.review_state == "approved",
        )
        .order_by(InstagramOutreachQueueItem.created_at.asc())
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
            if rc in ("login_required", "account_restricted"):
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
            db.query(InstagramOutreachCampaign)
            .filter(InstagramOutreachCampaign.status == "active").all()
        )
        for camp in actives:
            try:
                process_campaign_once(db, camp.id, max_items=2, headless=headless)
            except Exception:
                logger.exception("instagram outreach_worker tick: campaign_id=%s", camp.id)
    finally:
        db.close()


def _loop() -> None:
    time.sleep(_INITIAL_DELAY_SEC)
    interval = _interval_sec()
    while True:
        try:
            _tick_once(headless=True)
        except Exception:
            logger.exception("instagram_outreach_worker tick failed")
        time.sleep(interval)


def start_outreach_worker_thread() -> None:
    global _thread_started
    if _thread_started:
        return
    if (os.getenv("FB_MASTER_INSTAGRAM_OUTREACH_DISABLED", "") or "").strip().lower() in ("1", "true", "yes"):
        logger.info("instagram_outreach_worker отключён переменной FB_MASTER_INSTAGRAM_OUTREACH_DISABLED")
        return
    threading.Thread(target=_loop, daemon=True, name="instagram-outreach-worker").start()
    _thread_started = True
    logger.info("instagram_outreach_worker запущен (интервал=%ss)", _interval_sec())


__all__ = ["process_campaign_once", "process_item", "start_outreach_worker_thread"]
