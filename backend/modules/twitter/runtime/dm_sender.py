"""Twitter / X DM sender (browser-mode).

Алгоритм: открыть `/messages/compose?recipient=<handle>` (новый flow X),
если не получилось — открыть профиль `/{handle}` → клик «Message». Ввести
текст в `[data-testid="dmComposerTextInput"]`, кликнуть Send.
"""

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
    DM_SEND_SELECTORS,
    DM_TEXTBOX_SELECTORS,
    LOCALIZED_DM_DISABLED_HINTS,
    LOCALIZED_LOGIN_HINTS,
    LOCALIZED_RATE_LIMIT_HINTS,
    LOCALIZED_RESTRICTED_HINTS,
    PROFILE_MESSAGE_BUTTON_SELECTORS,
    page_has_any_hint,
)
from backend.modules.twitter.runtime.playwright_session import TwitterBrowser

logger = logging.getLogger(__name__)


def _try_open_dm_composer(page, handle: str) -> str:
    """Попробовать прямую compose-ссылку, при неуспехе — профиль и клик Message.

    Возвращает финальный URL.
    """
    handle = (handle or "").strip().lstrip("@").lstrip("/")
    # На X compose принимает recipient_id (числовой) или handle.
    # Прямой URL handle: новый flow открывается через переход с профиля,
    # но `/messages/compose?recipient=username` обычно тоже работает.
    direct_url = f"https://x.com/messages/compose?recipient={handle}"
    try:
        page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.0 + random.random())
        cur = (page.url or "").lower()
        if "/messages/compose" in cur or "/messages" in cur:
            return cur
    except Exception:  # noqa: BLE001
        pass
    # Fallback — профиль.
    try:
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.0 + random.random())
        msg_btn = try_locator(page, PROFILE_MESSAGE_BUTTON_SELECTORS, timeout_ms=4500)
        if msg_btn is not None:
            try:
                msg_btn.click(timeout=4000)
                time.sleep(1.0 + random.random())
            except Exception:  # noqa: BLE001
                pass
        return (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return (page.url or "").lower()


def send_dm(
    db: Session,
    account_id: int,
    to_handle: str,
    body: str,
    *,
    headless: bool = True,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(TwitterAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    handle = (to_handle or "").strip().lstrip("@").lstrip("/")
    body = (body or "").strip()
    if not handle:
        return {"ok": False, "reason_code": "empty_to_handle"}
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_dms_per_day or 30)
    if cap > 0 and not check_cap(db, acc, "dms_sent", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    try:
        with TwitterBrowser(acc, headless=headless) as session:
            page = session.new_page()

            cur = _try_open_dm_composer(page, handle)

            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                mark_account_login_required(db, acc, "DM aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                mark_account_restricted(db, acc, "DM aborted: account restricted")
                return {"ok": False, "reason_code": "account_restricted"}
            if page_has_any_hint(body_excerpt, LOCALIZED_DM_DISABLED_HINTS):
                return {"ok": False, "reason_code": "dm_disabled"}

            # Anti-detect.
            try:
                from backend.modules.twitter.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(80, 200))
                    mouse_jitter(page, steps=random.randint(2, 4))
                    random_idle(0.4, 1.0)
            except Exception:  # noqa: BLE001
                pass

            textbox = try_locator(page, DM_TEXTBOX_SELECTORS, timeout_ms=8000)
            if textbox is None:
                return {"ok": False, "reason_code": "composer_not_found"}

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
            send_btn = try_locator(page, DM_SEND_SELECTORS, timeout_ms=4000)
            if send_btn is None:
                # Иногда X-DM отправляется по Enter (без shift).
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
                body2 = (page.locator("body").inner_text(timeout=2000) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body2 = ""
            if page_has_any_hint(body2, LOCALIZED_RATE_LIMIT_HINTS):
                return {"ok": False, "reason_code": "rate_limited"}

            sent = False
            try:
                left = (textbox.inner_text(timeout=1000) or "").strip()
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
            bump_usage(db, acc, "dms_sent")
            db.commit()
            return {"ok": True, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("twitter dm_sender failed for account_id=%s to=%s", account_id, handle)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["send_dm"]
