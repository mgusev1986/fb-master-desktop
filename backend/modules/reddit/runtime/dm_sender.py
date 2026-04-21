"""Reddit DM sender (browser-mode).

Использует старый «классический» путь /message/compose/?to=<username>&subject=&message=,
который работает и в новом редизайне. Этот путь стабильнее, чем новый Chat
overlay (последний завязан на WebSocket и часто меняет DOM).

Алгоритм:
  1. open `https://www.reddit.com/message/compose/?to={username}&subject={subject}&message={body}`.
  2. detect login/suspended.
  3. find «send» button → click → wait success.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.reddit.models import RedditAccount, RedditAccountDayUsage
from backend.modules.reddit.runtime.playwright_session import RedditBrowser

logger = logging.getLogger(__name__)


# Backwards-compat shortcuts: остаются как реэкспорт расширенных списков
# из dom_resilience (другие модули — chat_sender, comment_sender, inbox_sync_browser
# и audience_scraper_browser — импортируют их из этого файла).
from backend.modules.reddit.runtime.dom_resilience import (  # noqa: E402
    LOCALIZED_LOGIN_HINTS as _LOGIN_HINTS,  # noqa: N812
    LOCALIZED_RATE_LIMIT_HINTS as _RATE_LIMIT_HINTS,  # noqa: N812
    LOCALIZED_SUSPEND_HINTS as _SUSPEND_HINTS,  # noqa: N812
    SEND_BUTTON_SELECTORS_LOCALIZED as _SEND_BUTTON_SELECTORS,  # noqa: N812
)
_SUCCESS_HINTS = (
    "your message has been delivered",
    "message sent",
    "delivered",
    "/message/sent",
)


def _human_pause(min_ms: int = 350, max_ms: int = 1100) -> None:
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _try_locator(page, selectors: tuple[str, ...], timeout_ms: int = 4000):
    deadline = time.monotonic() + timeout_ms / 1000
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            remaining = max(200, int((deadline - time.monotonic()) * 1000))
            if loc.is_visible(timeout=remaining):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _bump_counter(db: Session, account: RedditAccount, field: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    row = (
        db.query(RedditAccountDayUsage)
        .filter(RedditAccountDayUsage.account_id == account.id, RedditAccountDayUsage.usage_date == today)
        .one_or_none()
    )
    if row is None:
        row = RedditAccountDayUsage(account_id=account.id, usage_date=today)
        db.add(row)
        db.flush()
    setattr(row, field, (getattr(row, field, 0) or 0) + 1)


def _check_caps(db: Session, account: RedditAccount, field: str, soft_cap: int) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    row = (
        db.query(RedditAccountDayUsage)
        .filter(RedditAccountDayUsage.account_id == account.id, RedditAccountDayUsage.usage_date == today)
        .one_or_none()
    )
    if row is None:
        return True
    used = int(getattr(row, field, 0) or 0)
    return used < soft_cap


def send_dm(
    db: Session,
    account_id: int,
    to_username: str,
    body: str,
    *,
    subject: str = "Hi!",
    headless: bool = True,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if acc.auth_mode != "browser_profile":
        return {"ok": False, "reason_code": "not_browser_mode"}
    to = (to_username or "").strip().lstrip("u/").lstrip("/")
    body = (body or "").strip()
    subject = (subject or "Hi!").strip()[:100]
    if not to:
        return {"ok": False, "reason_code": "empty_to_user"}
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_messages_per_day or 20)
    if cap > 0 and not _check_caps(db, acc, "messages_sent", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    url = (
        "https://www.reddit.com/message/compose/?"
        + urllib.parse.urlencode({"to": to, "subject": subject, "message": body})
    )
    try:
        with RedditBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in cur for h in _LOGIN_HINTS):
                _mark_login_required(db, acc, "DM aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if any(h in body_excerpt for h in _SUSPEND_HINTS) or any(h in cur for h in _SUSPEND_HINTS):
                _mark_suspended(db, acc, "DM aborted: account suspended")
                return {"ok": False, "reason_code": "account_suspended"}
            if "user does not exist" in body_excerpt or "nobody on reddit goes by that name" in body_excerpt:
                return {"ok": False, "reason_code": "user_not_found"}

            try:
                from backend.modules.reddit.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(80, 240))
                    mouse_jitter(page, steps=random.randint(2, 4))
                    random_idle(0.4, 1.0)
            except Exception:  # noqa: BLE001
                pass

            send_btn = _try_locator(page, _SEND_BUTTON_SELECTORS, timeout_ms=6000)
            if send_btn is None:
                return {"ok": False, "reason_code": "send_button_not_found"}

            _human_pause(400, 900)
            try:
                send_btn.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "send_click_failed", "details": str(e)[:200]}

            time.sleep(2.0 + random.random())
            try:
                cur2 = (page.url or "").lower()
                body2 = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                cur2, body2 = cur, body_excerpt

            if any(h in body2 for h in _RATE_LIMIT_HINTS):
                return {"ok": False, "reason_code": "rate_limited"}

            success = (
                any(h in cur2 for h in _SUCCESS_HINTS)
                or any(h in body2 for h in _SUCCESS_HINTS)
                or "/message/sent" in cur2
            )
            if not success:
                return {"ok": False, "reason_code": "send_unconfirmed"}

            now = datetime.now(timezone.utc)
            _bump_counter(db, acc, "messages_sent")
            db.commit()
            return {"ok": True, "reason_code": None, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("reddit dm_sender failed for account_id=%s to=%s", account_id, to)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def _mark_login_required(db: Session, acc: RedditAccount, reason: str) -> None:
    now = datetime.now(timezone.utc)
    acc.session_ok = False
    acc.last_login_check_at = now
    acc.login_blocked_at = now
    acc.login_blocked_reason = reason[:480]
    acc.status = "needs_attention"
    db.commit()


def _mark_suspended(db: Session, acc: RedditAccount, reason: str) -> None:
    now = datetime.now(timezone.utc)
    acc.session_ok = False
    acc.last_login_check_at = now
    acc.login_blocked_at = now
    acc.login_blocked_reason = reason[:480]
    acc.status = "restricted"
    db.commit()


__all__ = ["send_dm"]
