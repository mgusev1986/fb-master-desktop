"""Instagram follow sender — подписаться на пользователя."""

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
    FOLLOW_BUTTON_SELECTORS, FOLLOWING_INDICATOR_SELECTORS,
    LOCALIZED_LOGIN_HINTS, LOCALIZED_RESTRICTED_HINTS, page_has_any_hint,
)
from backend.modules.instagram.runtime.playwright_session import InstagramBrowser

logger = logging.getLogger(__name__)


def follow_user(
    db: Session, account_id: int, handle: str,
    *, headless: bool = True, daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    handle = (handle or "").strip().lstrip("@").lstrip("/")
    if not handle:
        return {"ok": False, "reason_code": "empty_handle"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    # Жёсткий cap для нового аккаунта = 50/день. Default 60.
    cap = int(daily_cap or acc.cap_follows_per_day or 60)
    if cap > 0 and not check_cap(db, acc, "follows_done", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                mark_login_required(db, acc, "Follow aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                mark_restricted(db, acc, "Follow aborted: account restricted")
                return {"ok": False, "reason_code": "account_restricted"}
            if "user not found" in body_excerpt or "page isn't available" in body_excerpt:
                return {"ok": False, "reason_code": "user_not_found"}

            try:
                from backend.modules.instagram.runtime.human_behavior import antidetect_enabled, mouse_jitter, random_idle
                if antidetect_enabled():
                    mouse_jitter(page, steps=random.randint(2, 4))
                    random_idle(0.4, 1.0)
            except Exception:  # noqa: BLE001
                pass

            # Already following?
            already = try_locator(page, FOLLOWING_INDICATOR_SELECTORS, timeout_ms=1500)
            if already is not None:
                return {"ok": False, "reason_code": "already_following", "skipped": True}

            follow_btn = try_locator(page, FOLLOW_BUTTON_SELECTORS, timeout_ms=4000)
            if follow_btn is None:
                return {"ok": False, "reason_code": "follow_button_not_found"}
            try:
                follow_btn.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "follow_click_failed", "details": str(e)[:200]}

            time.sleep(1.5 + random.random())
            confirmed = try_locator(page, FOLLOWING_INDICATOR_SELECTORS, timeout_ms=2500)
            if confirmed is None:
                return {"ok": False, "reason_code": "follow_unconfirmed"}

            now = datetime.now(timezone.utc)
            bump_usage(db, acc, "follows_done")
            db.commit()
            return {"ok": True, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram follow_sender failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["follow_user"]
