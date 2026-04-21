"""LinkedIn readiness probe — быстрая проверка, что аккаунт жив.

Открывает feed под persistent-профилем; смотрит, не редиректит ли на login
или checkpoint, видна ли навигация. По результатам обновляет в БД:
  * `session_ok`
  * `last_login_check_at`
  * `login_blocked_at` / `login_blocked_reason`
  * `status` (`connected` | `needs_attention` | `restricted`)

Возвращает dict с диагностикой (см. контракт в runtime/__init__).
Никаких неперехваченных exceptions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import LinkedInAccount
from backend.modules.linkedin.runtime.playwright_session import LinkedInBrowser
from backend.modules.linkedin.services import accounts as accounts_svc

logger = logging.getLogger(__name__)


_FEED_URL = "https://www.linkedin.com/feed/"
_LOGIN_HINTS = (
    "/login",
    "/uas/login",
    "/checkpoint/",
    "/authwall",
)
_RESTRICTED_HINTS = (
    "/checkpoint/challenges",
    "/account/restricted",
    "verify your identity",
    "we restricted your account",
)


def probe_account(db: Session, account_id: int, *, headless: bool = True) -> dict[str, Any]:
    acc = accounts_svc.get_account(db, account_id)
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    org_id = acc.organization_id
    now = datetime.now(timezone.utc)

    if not (acc.cookies_json or []):
        acc.session_ok = False
        acc.last_login_check_at = now
        acc.login_blocked_at = now
        acc.login_blocked_reason = "Нет cookies — импортируйте аккаунт заново"
        acc.status = "needs_attention"
        accounts_svc.log_event(db, org_id, acc.id, "readiness_probe", "warn", {"reason": "no_cookies"})
        db.commit()
        return {"ok": False, "reason_code": "no_cookies"}

    result: dict[str, Any] = {"ok": False, "reason_code": None, "url": None}
    final_url = ""
    body_excerpt = ""
    try:
        with LinkedInBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                accounts_svc.log_event(
                    db, org_id, acc.id, "readiness_probe", "error",
                    {"phase": "goto", "error": str(e)[:300]},
                )
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

            logged_in = session.is_logged_in(page)
            result["url"] = final_url

            # Restricted/checkpoint detection.
            if any(h in final_url for h in _RESTRICTED_HINTS) or any(h in body_excerpt for h in _RESTRICTED_HINTS):
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = "LinkedIn ограничил аккаунт (checkpoint/verify identity). Откройте профиль вручную и подтвердите."
                acc.status = "restricted"
                accounts_svc.log_event(
                    db, org_id, acc.id, "readiness_probe", "error",
                    {"reason": "restricted", "url": final_url},
                )
                db.commit()
                return {"ok": False, "reason_code": "restricted", "url": final_url}

            # Login redirect detection.
            if any(h in final_url for h in _LOGIN_HINTS):
                acc.session_ok = False
                acc.last_login_check_at = now
                acc.login_blocked_at = now
                acc.login_blocked_reason = "LinkedIn просит залогиниться (cookie li_at истёк или сменился IP/UA)."
                acc.status = "needs_attention"
                accounts_svc.log_event(
                    db, org_id, acc.id, "readiness_probe", "warn",
                    {"reason": "login_redirect", "url": final_url},
                )
                db.commit()
                return {"ok": False, "reason_code": "login_required", "url": final_url}

            # Successful feed.
            if logged_in:
                acc.session_ok = True
                acc.last_login_check_at = now
                acc.login_blocked_at = None
                acc.login_blocked_reason = None
                acc.status = "connected"
                accounts_svc.log_event(
                    db, org_id, acc.id, "readiness_probe", "info",
                    {"reason": "logged_in", "url": final_url},
                )
                db.commit()
                return {"ok": True, "reason_code": None, "url": final_url}

            # Неопределённое состояние.
            acc.session_ok = False
            acc.last_login_check_at = now
            acc.login_blocked_at = now
            acc.login_blocked_reason = "Feed открылся, но навигация LinkedIn не обнаружена. Возможно, requires re-auth."
            acc.status = "needs_attention"
            accounts_svc.log_event(
                db, org_id, acc.id, "readiness_probe", "warn",
                {"reason": "no_feed_nav", "url": final_url},
            )
            db.commit()
            return {"ok": False, "reason_code": "unknown_state", "url": final_url}

    except Exception as e:  # noqa: BLE001
        logger.exception("readiness_probe failed for account_id=%s", account_id)
        accounts_svc.log_event(
            db, org_id, acc.id, "readiness_probe", "error",
            {"phase": "session", "error": str(e)[:300]},
        )
        acc.session_ok = False
        acc.last_login_check_at = now
        acc.login_blocked_at = now
        acc.login_blocked_reason = f"Сбой playwright-сессии: {str(e)[:200]}"
        acc.status = "needs_attention"
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["probe_account"]
