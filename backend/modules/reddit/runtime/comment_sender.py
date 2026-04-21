"""Reddit comment sender (browser-mode).

Открывает пост по URL, ищет comment-textarea (новый редизайн использует
contenteditable rich-text editor, старый — обычный textarea), вводит
текст и кликает «Comment».
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.reddit.models import RedditAccount
from backend.modules.reddit.runtime.dm_sender import (
    _LOGIN_HINTS,
    _RATE_LIMIT_HINTS,
    _SUSPEND_HINTS,
    _bump_counter,
    _check_caps,
    _human_pause,
    _mark_login_required,
    _mark_suspended,
    _try_locator,
)
from backend.modules.reddit.runtime.playwright_session import RedditBrowser

logger = logging.getLogger(__name__)


# Используем расширенные locale-aware списки (см. dom_resilience).
from backend.modules.reddit.runtime.dom_resilience import (  # noqa: E402
    COMMENT_SUBMIT_SELECTORS_LOCALIZED as _COMMENT_SUBMIT_SELECTORS,  # noqa: N812
    COMMENT_TEXTBOX_SELECTORS_LOCALIZED as _COMMENT_TEXTBOX_SELECTORS,  # noqa: N812
)
_OPEN_COMPOSER_SELECTORS = (
    "button:has-text('Add a comment')",
    "div:has-text('Add a comment')",
    "shreddit-composer button",
)


def comment_on_post(
    db: Session,
    account_id: int,
    post_url: str,
    body: str,
    *,
    headless: bool = True,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if acc.auth_mode != "browser_profile":
        return {"ok": False, "reason_code": "not_browser_mode"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    if not post_url or "reddit.com" not in (post_url or "").lower():
        return {"ok": False, "reason_code": "invalid_post_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_comments_per_day or 30)
    if cap > 0 and not _check_caps(db, acc, "comments_posted", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    try:
        with RedditBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in cur for h in _LOGIN_HINTS):
                _mark_login_required(db, acc, "Comment aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if any(h in body_excerpt for h in _SUSPEND_HINTS):
                _mark_suspended(db, acc, "Comment aborted: account suspended")
                return {"ok": False, "reason_code": "account_suspended"}

            try:
                from backend.modules.reddit.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(200, 600))
                    mouse_jitter(page, steps=random.randint(3, 5))
                    random_idle(0.6, 1.4)
            except Exception:  # noqa: BLE001
                pass

            # Иногда composer надо «открыть» (на новом редизайне).
            opener = _try_locator(page, _OPEN_COMPOSER_SELECTORS, timeout_ms=2500)
            if opener is not None:
                try:
                    opener.click(timeout=2000)
                    _human_pause(300, 700)
                except Exception:  # noqa: BLE001
                    pass

            textbox = _try_locator(page, _COMMENT_TEXTBOX_SELECTORS, timeout_ms=6000)
            if textbox is None:
                return {"ok": False, "reason_code": "textbox_not_found"}

            try:
                textbox.click(timeout=2000)
                textbox.focus(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            _human_pause(200, 500)

            try:
                page.keyboard.type(body, delay=random.randint(28, 75))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "type_failed", "details": str(e)[:200]}

            _human_pause(500, 1100)
            submit = _try_locator(page, _COMMENT_SUBMIT_SELECTORS, timeout_ms=4000)
            if submit is None:
                return {"ok": False, "reason_code": "submit_not_found"}
            try:
                submit.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "submit_click_failed", "details": str(e)[:200]}

            time.sleep(2.0 + random.random())
            try:
                body2 = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                body2 = ""
            if any(h in body2 for h in _RATE_LIMIT_HINTS):
                return {"ok": False, "reason_code": "rate_limited"}

            # Проверка успеха: composer пропадает или появляется наш текст в comments.
            success = False
            try:
                still_visible = textbox.is_visible(timeout=1000)
                if not still_visible:
                    success = True
            except Exception:  # noqa: BLE001
                success = True
            # Доп. проверка: первая часть нашего body встречается в DOM.
            if not success:
                try:
                    success = body[:60].lower() in body2
                except Exception:  # noqa: BLE001
                    pass
            if not success:
                return {"ok": False, "reason_code": "send_unconfirmed"}

            now = datetime.now(timezone.utc)
            _bump_counter(db, acc, "comments_posted")
            db.commit()
            return {"ok": True, "reason_code": None, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("reddit comment_sender failed for account_id=%s post=%s", account_id, post_url)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["comment_on_post"]
