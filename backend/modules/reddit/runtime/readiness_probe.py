"""Reddit readiness probe (browser-mode) — открыть feed, проверить логин."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.reddit.models import RedditAccount
from backend.modules.reddit.runtime.playwright_session import RedditBrowser

logger = logging.getLogger(__name__)


_FEED_URL = "https://www.reddit.com/"
from backend.modules.reddit.runtime.dom_resilience import (  # noqa: E402
    LOCALIZED_LOGIN_HINTS as _LOGIN_HINTS,  # noqa: N812
    LOCALIZED_SUSPEND_HINTS as _RESTRICTED_HINTS,  # noqa: N812
)


def probe_account(db: Session, account_id: int, *, headless: bool = True) -> dict[str, Any]:
    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if acc.auth_mode != "browser_profile":
        return {"ok": False, "reason_code": "not_browser_mode", "details": f"auth_mode={acc.auth_mode}"}
    if not (acc.cookies_json or []):
        now = datetime.now(timezone.utc)
        acc.session_ok = False
        acc.last_login_check_at = now
        acc.login_blocked_at = now
        acc.login_blocked_reason = "Нет cookies — импортируйте аккаунт заново"
        acc.status = "needs_attention"
        db.commit()
        return {"ok": False, "reason_code": "no_cookies"}

    now = datetime.now(timezone.utc)
    final_url = ""
    body_excerpt = ""
    try:
        with RedditBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = f"Не удалось открыть feed: {str(e)[:200]}"
                acc.status = "needs_attention"
                db.commit()
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            try:
                final_url = (page.url or "").lower()
            except Exception:  # noqa: BLE001
                final_url = ""
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in body_excerpt for h in _RESTRICTED_HINTS) or any(h in final_url for h in _RESTRICTED_HINTS):
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = "Reddit пометил аккаунт как suspended — нужен новый аккаунт."
                acc.status = "restricted"
                db.commit()
                return {"ok": False, "reason_code": "suspended", "url": final_url}

            if any(h in final_url for h in _LOGIN_HINTS):
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = "Reddit просит залогиниться (cookies истекли или не приняты)."
                acc.status = "needs_attention"
                db.commit()
                return {"ok": False, "reason_code": "login_required", "url": final_url}

            if session.is_logged_in(page):
                acc.session_ok = True
                acc.last_login_check_at = now
                acc.login_blocked_at = None
                acc.login_blocked_reason = None
                acc.status = "connected"
                db.commit()
                return {"ok": True, "reason_code": None, "url": final_url}

            acc.session_ok = False
            acc.last_login_check_at = now
            acc.login_blocked_at = now
            acc.login_blocked_reason = "Feed открылся, но навигация Reddit не обнаружена. Возможно, requires re-auth."
            acc.status = "needs_attention"
            db.commit()
            return {"ok": False, "reason_code": "unknown_state", "url": final_url}
    except Exception as e:  # noqa: BLE001
        logger.exception("reddit readiness_probe failed for account_id=%s", account_id)
        try:
            acc.session_ok = False
            acc.last_login_check_at = now
            acc.login_blocked_at = now
            acc.login_blocked_reason = f"Сбой playwright-сессии: {str(e)[:200]}"
            acc.status = "needs_attention"
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["probe_account"]
