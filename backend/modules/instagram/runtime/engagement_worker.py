"""Instagram engagement worker (likes/comments/follows/story-likes)."""

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
    InstagramEngagementQueueItem, InstagramEngagementTask, InstagramTemplate,
)

logger = logging.getLogger(__name__)


_thread_started = False
_DEFAULT_INTERVAL_SEC = 300
_INITIAL_DELAY_SEC = 90


def _interval_sec() -> int:
    raw = os.getenv("FB_MASTER_INSTAGRAM_ENGAGEMENT_TICK_SEC", "").strip()
    try:
        return max(60, int(raw) if raw else _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


def _render_comment_body(template: InstagramTemplate | None) -> str:
    """Для comment-task: берём body или случайный variant из variants_json."""
    if template is None:
        return ""
    variants = template.body_variants or []
    if variants:
        return random.choice(list(variants) + [template.body])
    return template.body or ""


def process_queue_item(db: Session, item: InstagramEngagementQueueItem, *, headless: bool = True) -> dict[str, Any]:
    task = db.get(InstagramEngagementTask, item.task_id)
    if task is None:
        item.review_state = "failed"; item.last_error = "task_missing"; db.commit()
        return {"ok": False, "reason_code": "task_missing"}
    if task.status not in ("active", "draft"):
        return {"ok": False, "reason_code": f"task_status_{task.status}", "skipped": True}
    if not task.account_id:
        item.review_state = "failed"; item.last_error = "no_account"; db.commit()
        return {"ok": False, "reason_code": "no_account"}

    item.attempts = (item.attempts or 0) + 1
    db.commit()

    if task.kind == "like_post":
        if item.target_kind != "post":
            item.review_state = "failed"; item.last_error = "wrong_target_kind"; db.commit()
            return {"ok": False, "reason_code": "wrong_target_kind"}
        from backend.modules.instagram.runtime.like_sender import like_post
        res = like_post(db, task.account_id, item.target_ref, headless=headless, daily_cap=task.daily_cap)
    elif task.kind == "comment_post":
        if item.target_kind != "post":
            item.review_state = "failed"; item.last_error = "wrong_target_kind"; db.commit()
            return {"ok": False, "reason_code": "wrong_target_kind"}
        tpl = db.get(InstagramTemplate, task.template_id) if task.template_id else None
        body = _render_comment_body(tpl)
        if not body:
            item.review_state = "failed"; item.last_error = "no_template_body"; db.commit()
            return {"ok": False, "reason_code": "no_template_body"}
        from backend.modules.instagram.runtime.comment_sender import comment_on_post
        res = comment_on_post(db, task.account_id, item.target_ref, body, headless=headless, daily_cap=task.daily_cap)
    elif task.kind == "follow_user":
        if item.target_kind != "user":
            item.review_state = "failed"; item.last_error = "wrong_target_kind"; db.commit()
            return {"ok": False, "reason_code": "wrong_target_kind"}
        from backend.modules.instagram.runtime.follow_sender import follow_user
        res = follow_user(db, task.account_id, item.target_ref, headless=headless, daily_cap=task.daily_cap)
    elif task.kind == "story_like":
        if item.target_kind != "user":
            item.review_state = "failed"; item.last_error = "wrong_target_kind"; db.commit()
            return {"ok": False, "reason_code": "wrong_target_kind"}
        from backend.modules.instagram.runtime.story_like_sender import story_like_user
        res = story_like_user(db, task.account_id, item.target_ref, headless=headless, daily_cap=task.daily_cap)
    else:
        item.review_state = "failed"; item.last_error = f"unknown_kind:{task.kind}"; db.commit()
        return {"ok": False, "reason_code": "unknown_kind"}

    if res.get("ok"):
        item.review_state = "done"
        item.done_at = datetime.now(timezone.utc)
        item.last_error = None
    else:
        rc = res.get("reason_code") or "unknown"
        if rc in ("daily_cap_reached", "already_liked", "already_following", "user_not_found",
                  "no_active_stories"):
            item.review_state = "skipped"
        elif rc in ("login_required", "account_restricted"):
            item.review_state = "failed"
        else:
            item.review_state = "failed"
        item.last_error = (rc + (": " + (res.get("details") or "") if res.get("details") else ""))[:512]
    db.commit()
    return res


def process_task_once(
    db: Session, task_id: int, *, max_items: int = 5, headless: bool = True,
    inter_item_pause_range: tuple[int, int] = (4, 12),
) -> dict[str, Any]:
    task = db.get(InstagramEngagementTask, task_id)
    if task is None:
        return {"ok": False, "reason_code": "task_not_found"}
    items = (
        db.query(InstagramEngagementQueueItem)
        .filter(
            InstagramEngagementQueueItem.task_id == task_id,
            InstagramEngagementQueueItem.review_state == "pending",
        )
        .order_by(InstagramEngagementQueueItem.created_at.asc())
        .limit(max_items)
        .all()
    )
    done = 0
    skipped = 0
    failed = 0
    halted: str | None = None
    for idx, it in enumerate(items):
        res = process_queue_item(db, it, headless=headless)
        if res.get("ok"):
            done += 1
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
    return {"ok": True, "done": done, "skipped": skipped, "failed": failed, "halted_reason": halted}


def _tick_once() -> None:
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        actives = (
            db.query(InstagramEngagementTask)
            .filter(InstagramEngagementTask.status == "active").all()
        )
        for t in actives:
            try:
                process_task_once(db, t.id, max_items=3, headless=True)
            except Exception:
                logger.exception("instagram engagement_worker tick: task_id=%s", t.id)
    finally:
        db.close()


def _loop() -> None:
    time.sleep(_INITIAL_DELAY_SEC)
    interval = _interval_sec()
    while True:
        try:
            _tick_once()
        except Exception:
            logger.exception("instagram_engagement_worker tick failed")
        time.sleep(interval)


def start_engagement_worker_thread() -> None:
    global _thread_started
    if _thread_started:
        return
    if (os.getenv("FB_MASTER_INSTAGRAM_ENGAGEMENT_DISABLED", "") or "").strip().lower() in ("1", "true", "yes"):
        logger.info("instagram_engagement_worker отключён переменной FB_MASTER_INSTAGRAM_ENGAGEMENT_DISABLED")
        return
    threading.Thread(target=_loop, daemon=True, name="instagram-engagement-worker").start()
    _thread_started = True
    logger.info("instagram_engagement_worker запущен (интервал=%ss)", _interval_sec())


__all__ = ["process_queue_item", "process_task_once", "start_engagement_worker_thread"]
