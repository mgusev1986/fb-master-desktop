"""Instagram Direct DM sender.

Алгоритм:
  1. Открыть `/direct/new/`, ввести handle получателя в search.
  2. Выбрать пользователя из dropdown.
  3. Кликнуть Next/Chat → попадаем в thread.
  4. В composer — ввод текста → Send (или Enter).

Альтернатива: открыть профиль `/{handle}/`, клик Message → попадаем в thread.
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
    DM_SEND_SELECTORS, DM_TEXTBOX_SELECTORS,
    LOCALIZED_LOGIN_HINTS, LOCALIZED_RESTRICTED_HINTS,
    PROFILE_MESSAGE_BUTTON_SELECTORS, page_has_any_hint,
)
from backend.modules.instagram.runtime.playwright_session import InstagramBrowser

logger = logging.getLogger(__name__)


def _open_dm_via_profile(page, handle: str) -> str:
    """Открыть профиль и кликнуть Message — вернёт финальный URL."""
    try:
        page.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.0 + random.random())
        msg_btn = try_locator(page, PROFILE_MESSAGE_BUTTON_SELECTORS, timeout_ms=4500)
        if msg_btn is not None:
            try:
                msg_btn.click(timeout=4000)
                time.sleep(2.0 + random.random())
            except Exception:  # noqa: BLE001
                pass
        return (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return (page.url or "").lower()


def send_dm(
    db: Session, account_id: int, to_handle: str, body: str,
    *, headless: bool = True, daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = db.get(InstagramAccount, int(account_id))
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

    cap = int(daily_cap or acc.cap_dms_per_day or 25)
    if cap > 0 and not check_cap(db, acc, "dms_sent", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            cur = _open_dm_via_profile(page, handle)

            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                mark_login_required(db, acc, "DM aborted: login required")
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                mark_restricted(db, acc, "DM aborted: account restricted")
                return {"ok": False, "reason_code": "account_restricted"}
            if "user not found" in body_excerpt or "page isn't available" in body_excerpt:
                return {"ok": False, "reason_code": "user_not_found"}

            try:
                from backend.modules.instagram.runtime.human_behavior import (
                    antidetect_enabled, mouse_jitter, random_idle,
                )
                if antidetect_enabled():
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
                # Fallback на Enter.
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
            bump_usage(db, acc, "dms_sent")
            db.commit()
            return {"ok": True, "sent_at": now.isoformat()}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram dm_sender failed account_id=%s to=%s", account_id, handle)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["send_dm"]
