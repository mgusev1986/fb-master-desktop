"""Twitter / X reply sender — реплай к tweet'у."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import TwitterAccount
from backend.modules.twitter.runtime._common import (
    bump_usage,
    check_cap,
    human_pause,
    mark_account_login_required,
    mark_account_restricted,
    try_locator,
)
from backend.modules.twitter.runtime.dom_resilience import (
    LOCALIZED_LOGIN_HINTS,
    LOCALIZED_RATE_LIMIT_HINTS,
    LOCALIZED_RESTRICTED_HINTS,
    REPLY_SUBMIT_SELECTORS,
    REPLY_TEXTBOX_SELECTORS,
    page_has_any_hint,
)
from backend.modules.twitter.runtime.playwright_session import TwitterBrowser

logger = logging.getLogger(__name__)


def reply_to_tweet(
    db: Session,
    account_id: int,
    tweet_url: str,
    body: str,
    *,
    headless: bool = True,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(TwitterAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    if len(body) > 280:
        return {"ok": False, "reason_code": "too_long", "details": f"{len(body)} chars > 280"}
    tweet_url = (tweet_url or "").strip()
    if not tweet_url or "x.com" not in tweet_url.lower() and "twitter.com" not in tweet_url.lower():
        return {"ok": False, "reason_code": "invalid_tweet_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_replies_per_day or 30)
    if cap > 0 and not check_cap(db, acc, "replies_posted", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    try:
        with TwitterBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                mark_account_login_required(db, acc, "Reply aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                mark_account_restricted(db, acc, "Reply aborted: account restricted")
                return {"ok": False, "reason_code": "account_restricted"}

            try:
                from backend.modules.twitter.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(150, 350))
                    mouse_jitter(page, steps=random.randint(2, 4))
                    random_idle(0.5, 1.2)
            except Exception:  # noqa: BLE001
                pass

            textbox = try_locator(page, REPLY_TEXTBOX_SELECTORS, timeout_ms=6000)
            if textbox is None:
                # Fallback: на странице tweet'а composer может быть под кнопкой Reply.
                try:
                    page.locator("button[data-testid='reply']").first.click(timeout=2000)
                    time.sleep(0.7)
                    textbox = try_locator(page, REPLY_TEXTBOX_SELECTORS, timeout_ms=4000)
                except Exception:  # noqa: BLE001
                    pass
            if textbox is None:
                return {"ok": False, "reason_code": "textbox_not_found"}

            try:
                textbox.click(timeout=2000)
                textbox.focus(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            human_pause(200, 500)
            try:
                page.keyboard.type(body, delay=random.randint(28, 75))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "type_failed", "details": str(e)[:200]}

            human_pause(500, 1100)
            submit = try_locator(page, REPLY_SUBMIT_SELECTORS, timeout_ms=4000)
            if submit is None:
                return {"ok": False, "reason_code": "submit_not_found"}
            try:
                submit.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "submit_click_failed", "details": str(e)[:200]}

            time.sleep(2.0 + random.random())
            try:
                body2 = (page.locator("body").inner_text(timeout=2000) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body2 = ""
            if page_has_any_hint(body2, LOCALIZED_RATE_LIMIT_HINTS):
                return {"ok": False, "reason_code": "rate_limited"}

            sent = False
            try:
                still = textbox.is_visible(timeout=1000)
                if not still:
                    sent = True
            except Exception:  # noqa: BLE001
                sent = True
            if not sent:
                try:
                    sent = body[:60].lower() in body2
                except Exception:  # noqa: BLE001
                    pass
            if not sent:
                return {"ok": False, "reason_code": "send_unconfirmed"}

            now = datetime.now(timezone.utc)
            bump_usage(db, acc, "replies_posted")
            db.commit()
            return {"ok": True, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("twitter reply_sender failed account_id=%s tweet=%s", account_id, tweet_url)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["reply_to_tweet"]
