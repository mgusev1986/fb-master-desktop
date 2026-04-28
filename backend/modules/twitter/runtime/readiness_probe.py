"""Twitter / X readiness probe."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import TwitterAccount
from backend.modules.twitter.runtime.dom_resilience import (
    LOCALIZED_LOGIN_HINTS,
    LOCALIZED_RESTRICTED_HINTS,
    page_has_any_hint,
)
from backend.modules.twitter.runtime.playwright_session import TwitterBrowser

logger = logging.getLogger(__name__)


_HOME_URL = "https://x.com/home"


def probe_account(db: Session, account_id: int, *, headless: bool = True) -> dict[str, Any]:
    acc = db.get(TwitterAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not (acc.cookies_json or []):
        now = datetime.now(timezone.utc)
        acc.session_ok = False
        acc.last_login_check_at = now
        # 2.96+: «свой личный (логин+пароль)» — cookies появятся после первого
        # автологина. Не пугаем пользователя статусом needs_attention, а оставляем
        # needs_login (с подсказкой запустить кнопку «Войти»).
        if acc.login_username and acc.enc_password:
            acc.login_blocked_at = None
            acc.login_blocked_reason = "Нужен первый вход — нажмите «Войти», система откроет Chromium и сохранит cookies."
            acc.status = "needs_login"
        else:
            acc.login_blocked_at = now
            acc.login_blocked_reason = "Нет cookies — импортируйте аккаунт заново"
            acc.status = "needs_attention"
        db.commit()
        return {"ok": False, "reason_code": "no_cookies"}

    now = datetime.now(timezone.utc)
    final_url = ""
    try:
        with TwitterBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = f"Не удалось открыть /home: {str(e)[:200]}"
                acc.status = "needs_attention"
                db.commit()
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            try:
                final_url = (page.url or "").lower()
            except Exception:  # noqa: BLE001
                final_url = ""
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2000]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS) or page_has_any_hint(final_url, LOCALIZED_RESTRICTED_HINTS):
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = "X пометил аккаунт как suspended/restricted."
                acc.status = "restricted"
                db.commit()
                return {"ok": False, "reason_code": "suspended", "url": final_url}

            if page_has_any_hint(final_url, LOCALIZED_LOGIN_HINTS):
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = "X просит залогиниться (auth_token истёк или ct0 не принят)."
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
                return {"ok": True, "url": final_url}

            acc.session_ok = False
            acc.last_login_check_at = now
            acc.login_blocked_at = now
            acc.login_blocked_reason = "Home открылся, но навигация не обнаружена. Возможно, нужен re-auth."
            acc.status = "needs_attention"
            db.commit()
            return {"ok": False, "reason_code": "unknown_state", "url": final_url}
    except Exception as e:  # noqa: BLE001
        logger.exception("twitter readiness_probe failed for account_id=%s", account_id)
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
