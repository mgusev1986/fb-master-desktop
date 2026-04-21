"""Instagram comment sender (на пост по URL)."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import InstagramAccount
from backend.modules.instagram.runtime._common import (
    bump_usage, check_cap, human_pause,
    mark_login_required, mark_restricted, try_locator,
)
from backend.modules.instagram.runtime.dom_resilience import (
    COMMENT_FOCUS_BUTTON_SELECTORS, COMMENT_SUBMIT_SELECTORS, COMMENT_TEXTBOX_SELECTORS,
    LOCALIZED_LOGIN_HINTS, LOCALIZED_RESTRICTED_HINTS, page_has_any_hint,
)
from backend.modules.instagram.runtime.playwright_session import InstagramBrowser

logger = logging.getLogger(__name__)


def comment_on_post(
    db: Session, account_id: int, post_url: str, body: str,
    *, headless: bool = True, daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    if not post_url or "instagram.com" not in (post_url or "").lower():
        return {"ok": False, "reason_code": "invalid_post_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_comments_per_day or 30)
    if cap > 0 and not check_cap(db, acc, "comments_posted", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                mark_login_required(db, acc, "Comment aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                mark_restricted(db, acc, "Comment aborted: account restricted")
                return {"ok": False, "reason_code": "account_restricted"}

            try:
                from backend.modules.instagram.runtime.human_behavior import antidetect_enabled, gentle_scroll, mouse_jitter, random_idle
                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(120, 350))
                    mouse_jitter(page, steps=random.randint(3, 5))
                    random_idle(0.6, 1.4)
            except Exception:  # noqa: BLE001
                pass

            # Иногда нужно сначала кликнуть на иконку Comment, чтобы получить focus в textarea.
            focus_btn = try_locator(page, COMMENT_FOCUS_BUTTON_SELECTORS, timeout_ms=2500)
            if focus_btn is not None:
                try:
                    focus_btn.click(timeout=2000)
                    time.sleep(0.5)
                except Exception:  # noqa: BLE001
                    pass

            textbox = try_locator(page, COMMENT_TEXTBOX_SELECTORS, timeout_ms=6000)
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
            submit = try_locator(page, COMMENT_SUBMIT_SELECTORS, timeout_ms=4000)
            if submit is None:
                return {"ok": False, "reason_code": "submit_not_found"}
            try:
                submit.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "submit_click_failed", "details": str(e)[:200]}

            time.sleep(2.0 + random.random())
            sent = False
            try:
                left = (textbox.inner_text(timeout=1000) or "").strip()
                if not left or len(left) < 5:
                    sent = True
            except Exception:  # noqa: BLE001
                sent = True
            if not sent:
                return {"ok": False, "reason_code": "send_unconfirmed"}

            now = datetime.now(timezone.utc)
            bump_usage(db, acc, "comments_posted")
            db.commit()
            return {"ok": True, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram comment_sender failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["comment_on_post"]
