"""Instagram story-like sender.

Алгоритм:
  1. Открыть профиль `/{handle}/`.
  2. Если есть story — кликнуть на аватарку.
  3. На viewer'е — клик на сердечко (Like reaction).
  4. Закрыть viewer.
"""

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
    LOCALIZED_LOGIN_HINTS, LOCALIZED_RESTRICTED_HINTS,
    STORY_AVATAR_SELECTORS, STORY_LIKE_BUTTON_SELECTORS, page_has_any_hint,
)
from backend.modules.instagram.runtime.playwright_session import InstagramBrowser

logger = logging.getLogger(__name__)


def story_like_user(
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

    cap = int(daily_cap or acc.cap_story_likes_per_day or 100)
    if cap > 0 and not check_cap(db, acc, "story_likes_done", cap):
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
                mark_login_required(db, acc, "Story-like aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                mark_restricted(db, acc, "Story-like aborted: account restricted")
                return {"ok": False, "reason_code": "account_restricted"}

            avatar = try_locator(page, STORY_AVATAR_SELECTORS, timeout_ms=4000)
            if avatar is None:
                return {"ok": False, "reason_code": "story_avatar_not_found"}
            try:
                avatar.click(timeout=4000)
                time.sleep(2.5 + random.random())
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "avatar_click_failed", "details": str(e)[:200]}

            # У пользователя без активных stories клик по аватарке откроет zoomed photo viewer.
            # Проверим что мы перешли на /stories/.
            try:
                cur2 = (page.url or "").lower()
            except Exception:  # noqa: BLE001
                cur2 = ""
            if "/stories/" not in cur2:
                return {"ok": False, "reason_code": "no_active_stories"}

            try:
                from backend.modules.instagram.runtime.human_behavior import random_idle
                random_idle(1.0, 2.5)  # имитация просмотра
            except Exception:  # noqa: BLE001
                pass

            like_btn = try_locator(page, STORY_LIKE_BUTTON_SELECTORS, timeout_ms=3000)
            if like_btn is None:
                return {"ok": False, "reason_code": "story_like_button_not_found"}
            try:
                like_btn.click(timeout=3000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "story_like_click_failed", "details": str(e)[:200]}

            time.sleep(1.0 + random.random())

            now = datetime.now(timezone.utc)
            bump_usage(db, acc, "story_likes_done")
            db.commit()
            return {"ok": True, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram story_like_sender failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["story_like_user"]
