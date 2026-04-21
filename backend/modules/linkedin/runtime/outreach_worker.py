"""LinkedIn outreach worker — обработка approved queue items.

Берёт по одному items с `review_state="approved"` и вызывает соответствующий
runtime (dm_sender или invitation_sender, в зависимости от mode кампании).
Обновляет review_state → "sent" / "failed" / "skipped". Уважает дневные капы.

Запускать можно:
  * вручную — `process_campaign_once(db, campaign_id, max_items=N)`;
  * фоном — `start_outreach_worker_thread()` (тикает каждые ~120 сек по всем
    campaigns с status="active").
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

from backend.modules.linkedin.models import (
    LinkedInOutreachCampaign,
    LinkedInOutreachQueueItem,
)

logger = logging.getLogger(__name__)


_thread_started = False
_DEFAULT_INTERVAL_SEC = 180
_INITIAL_DELAY_SEC = 45


def _interval_sec() -> int:
    raw = os.getenv("FB_MASTER_LINKEDIN_OUTREACH_TICK_SEC", "").strip()
    try:
        v = int(raw) if raw else _DEFAULT_INTERVAL_SEC
    except ValueError:
        v = _DEFAULT_INTERVAL_SEC
    return max(45, v)


def process_item(db: Session, item: LinkedInOutreachQueueItem, *, headless: bool = True) -> dict[str, Any]:
    """Обработать один queue item: вызвать соответствующий runtime."""
    camp = db.get(LinkedInOutreachCampaign, item.campaign_id)
    if camp is None:
        item.review_state = "failed"
        item.last_error = "campaign_missing"
        db.commit()
        return {"ok": False, "reason_code": "campaign_missing"}
    if camp.status not in ("active", "draft"):
        return {"ok": False, "reason_code": f"campaign_status_{camp.status}", "skipped": True}

    item.attempts = (item.attempts or 0) + 1
    db.commit()

    if camp.mode == "dm_first_degree":
        from backend.modules.linkedin.runtime.dm_sender import send_dm

        res = send_dm(
            db,
            account_id=camp.account_id,
            lead_id=item.lead_id,
            body=item.draft_body,
            headless=headless,
            daily_cap=camp.daily_cap,
        )
    elif camp.mode == "invitation_with_note":
        from backend.modules.linkedin.runtime.invitation_sender import send_invitation

        res = send_invitation(
            db,
            account_id=camp.account_id,
            lead_id=item.lead_id,
            note=item.draft_body,
            headless=headless,
            daily_cap=camp.daily_cap,
        )
    else:
        # Пока не реализованные режимы.
        item.review_state = "failed"
        item.last_error = f"mode_not_implemented:{camp.mode}"
        db.commit()
        return {"ok": False, "reason_code": "mode_not_implemented"}

    if res.get("ok"):
        item.review_state = "sent"
        item.sent_at = datetime.now(timezone.utc)
        item.last_error = None
    else:
        rc = res.get("reason_code") or "unknown"
        # daily_cap_reached / not_first_degree / login_required → не failed навсегда, помечаем skipped/failed по смыслу.
        if rc in ("daily_cap_reached", "not_first_degree"):
            item.review_state = "skipped"
        elif rc in ("login_required", "account_restricted"):
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
    inter_item_pause_range: tuple[int, int] = (4, 12),
) -> dict[str, Any]:
    """Обработать до `max_items` approved-items одной кампании.

    Между items — рандомная пауза (защита от бот-паттернов). Если попался
    `login_required` / `account_restricted` — останавливаемся (нет смысла
    дальше пытаться этим аккаунтом).
    """
    camp = db.get(LinkedInOutreachCampaign, campaign_id)
    if camp is None:
        return {"ok": False, "reason_code": "campaign_not_found"}

    items = (
        db.query(LinkedInOutreachQueueItem)
        .filter(
            LinkedInOutreachQueueItem.campaign_id == campaign_id,
            LinkedInOutreachQueueItem.review_state == "approved",
        )
        .order_by(LinkedInOutreachQueueItem.created_at.asc())
        .limit(max_items)
        .all()
    )
    sent = 0
    skipped = 0
    failed = 0
    halted_reason: str | None = None
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
                halted_reason = rc
                break
        if idx < len(items) - 1:
            lo, hi = inter_item_pause_range
            time.sleep(random.uniform(lo, hi))
    return {"ok": True, "sent": sent, "skipped": skipped, "failed": failed, "halted_reason": halted_reason}


def _tick_once(headless: bool = True) -> None:
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        active = (
            db.query(LinkedInOutreachCampaign)
            .filter(LinkedInOutreachCampaign.status == "active")
            .all()
        )
        for camp in active:
            try:
                process_campaign_once(db, camp.id, max_items=3, headless=headless)
            except Exception:
                logger.exception("outreach_worker tick: campaign_id=%s", camp.id)
    finally:
        db.close()


def _loop() -> None:
    time.sleep(_INITIAL_DELAY_SEC)
    interval = _interval_sec()
    while True:
        try:
            _tick_once(headless=True)
        except Exception:
            logger.exception("linkedin_outreach_worker tick failed")
        time.sleep(interval)


def start_outreach_worker_thread() -> None:
    """Идемпотентный старт фонового воркера."""
    global _thread_started
    if _thread_started:
        return
    if (os.getenv("FB_MASTER_LINKEDIN_OUTREACH_DISABLED", "") or "").strip().lower() in ("1", "true", "yes"):
        logger.info("linkedin_outreach_worker отключён переменной FB_MASTER_LINKEDIN_OUTREACH_DISABLED")
        return
    threading.Thread(target=_loop, daemon=True, name="linkedin-outreach-worker").start()
    _thread_started = True
    logger.info("linkedin_outreach_worker запущен (интервал=%ss)", _interval_sec())


__all__ = ["process_campaign_once", "process_item", "start_outreach_worker_thread"]
