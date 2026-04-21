"""Reddit Chat overlay sender (новый /chat/ путь, в дополнение к legacy DM).

Reddit Chat (нативный chat) живёт по адресу `/chat/u/<username>` или
`/chat/r/<subreddit>`. В отличие от `/message/compose/` (legacy PM), это
WebSocket-overlay с другим набором селекторов.

Контракт идентичен `dm_sender.send_dm`, но используется другая навигация и
другие селекторы. Если chat overlay не загружается / chat не доступен
(пользователь отключил chat) — возвращаем `chat_unavailable`.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.parse
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


from backend.modules.reddit.runtime.dom_resilience import (  # noqa: E402
    CHAT_TEXTBOX_SELECTORS_LOCALIZED as _CHAT_TEXTBOX_SELECTORS,  # noqa: N812
    SEND_BUTTON_SELECTORS_LOCALIZED as _CHAT_SEND_BUTTON_SELECTORS,  # noqa: N812
)
_CHAT_UNAVAILABLE_HINTS = (
    "this user can't be messaged",
    "chat is unavailable",
    "chat is disabled",
    "user not found",
    "messaging is restricted",
)
_CHAT_INVITE_REQUIRED_HINTS = (
    "send chat request",
    "invite to chat",
)


def send_chat_message(
    db: Session,
    account_id: int,
    to_username: str,
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
    to = (to_username or "").strip().lstrip("u/").lstrip("/")
    body = (body or "").strip()
    if not to:
        return {"ok": False, "reason_code": "empty_to_user"}
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_messages_per_day or 20)
    if cap > 0 and not _check_caps(db, acc, "messages_sent", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    chat_url = f"https://www.reddit.com/chat/u/{urllib.parse.quote(to, safe='')}"
    try:
        with RedditBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=3500) or "").lower()[:2000]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in cur for h in _LOGIN_HINTS):
                _mark_login_required(db, acc, "Chat aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if any(h in body_excerpt for h in _SUSPEND_HINTS):
                _mark_suspended(db, acc, "Chat aborted: account suspended")
                return {"ok": False, "reason_code": "account_suspended"}
            if any(h in body_excerpt for h in _CHAT_UNAVAILABLE_HINTS):
                return {"ok": False, "reason_code": "chat_unavailable"}
            invite_required = any(h in body_excerpt for h in _CHAT_INVITE_REQUIRED_HINTS)

            try:
                from backend.modules.reddit.runtime.human_behavior import (
                    antidetect_enabled, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    mouse_jitter(page, steps=random.randint(2, 4))
                    random_idle(0.5, 1.2)
            except Exception:  # noqa: BLE001
                pass

            textbox = _try_locator(page, _CHAT_TEXTBOX_SELECTORS, timeout_ms=10000)
            if textbox is None:
                return {"ok": False, "reason_code": "chat_input_not_found"}

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
            send_btn = _try_locator(page, _CHAT_SEND_BUTTON_SELECTORS, timeout_ms=4000)
            if send_btn is None:
                try:
                    page.keyboard.press("Enter")
                except Exception:  # noqa: BLE001
                    return {"ok": False, "reason_code": "send_button_not_found"}
            else:
                try:
                    send_btn.click(timeout=4000)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "reason_code": "send_click_failed", "details": str(e)[:200]}

            time.sleep(2.0 + random.random())
            try:
                body2 = (page.locator("body").inner_text(timeout=2000) or "").lower()[:2000]
            except Exception:  # noqa: BLE001
                body2 = ""
            if any(h in body2 for h in _RATE_LIMIT_HINTS):
                return {"ok": False, "reason_code": "rate_limited"}

            sent = False
            try:
                left = (textbox.inner_text(timeout=800) or "").strip()
                if not left or len(left) < 5:
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
            _bump_counter(db, acc, "messages_sent")
            db.commit()
            return {
                "ok": True,
                "reason_code": None,
                "sent_at": now.isoformat(),
                "invite_request_used": invite_required,
            }
    except Exception as e:  # noqa: BLE001
        logger.exception("reddit chat_sender failed for account_id=%s to=%s", account_id, to)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["send_chat_message"]
