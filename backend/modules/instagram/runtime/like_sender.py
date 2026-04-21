"""Instagram like sender — лайк на пост по URL."""

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
    LIKE_BUTTON_SELECTORS, UNLIKE_BUTTON_SELECTORS,
    LOCALIZED_LOGIN_HINTS, LOCALIZED_RESTRICTED_HINTS, page_has_any_hint,
)
from backend.modules.instagram.runtime.playwright_session import InstagramBrowser

logger = logging.getLogger(__name__)


def like_post(
    db: Session, account_id: int, post_url: str,
    *, headless: bool = True, daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not post_url or "instagram.com" not in (post_url or "").lower():
        return {"ok": False, "reason_code": "invalid_post_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_likes_per_day or 100)
    if cap > 0 and not check_cap(db, acc, "likes_done", cap):
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
                mark_login_required(db, acc, "Like aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                mark_restricted(db, acc, "Like aborted: account restricted")
                return {"ok": False, "reason_code": "account_restricted"}

            try:
                from backend.modules.instagram.runtime.human_behavior import antidetect_enabled, gentle_scroll, mouse_jitter, random_idle
                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(100, 280))
                    mouse_jitter(page, steps=random.randint(2, 4))
                    random_idle(0.4, 1.0)
            except Exception:  # noqa: BLE001
                pass

            # Если уже лайкнут (есть Unlike) — skip.
            unlike_btn = try_locator(page, UNLIKE_BUTTON_SELECTORS, timeout_ms=1500)
            if unlike_btn is not None:
                return {"ok": False, "reason_code": "already_liked", "skipped": True}

            like_btn = try_locator(page, LIKE_BUTTON_SELECTORS, timeout_ms=4000)
            if like_btn is None:
                return {"ok": False, "reason_code": "like_button_not_found"}
            try:
                like_btn.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "like_click_failed", "details": str(e)[:200]}

            time.sleep(1.0 + random.random())
            # Verify: появился Unlike.
            unlike_btn2 = try_locator(page, UNLIKE_BUTTON_SELECTORS, timeout_ms=2000)
            if unlike_btn2 is None:
                return {"ok": False, "reason_code": "like_unconfirmed"}

            now = datetime.now(timezone.utc)
            bump_usage(db, acc, "likes_done")
            db.commit()
            return {"ok": True, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram like_sender failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["like_post"]
