"""LinkedIn DM sender — отправка direct message 1st-degree connection.

Алгоритм:
  1. Открываем профиль `lead.profile_url`.
  2. Если редирект на /login или /authwall → возвращаем `login_required`.
  3. Если на странице есть кнопка «Connect» (не «Message») → лид НЕ 1st degree
     → возвращаем `not_first_degree` (для outreach к не-connections нужен InMail).
  4. Кликаем «Message» → дожидаемся overlay'а composer.
  5. Заполняем textarea (`role=textbox`) человеко-подобно (по символам, рандомные паузы).
  6. Кликаем «Send» (`button[aria-label*='Send']`).
  7. Ждём подтверждения отправки (composer закрывается, или появляется success-toast).
  8. Возвращаем `{ok:True, sent_at, conversation_url}`.

Selectors LinkedIn часто меняются — для устойчивости каждый шаг имеет
несколько fallback'ов (по `aria-label` / тексту / data-attrs).

Контракт: dict
  {ok: bool, reason_code: str|None, details: str|None, conversation_url?: str}
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import (
    LinkedInAccount,
    LinkedInAccountDayUsage,
    LinkedInLead,
)
from backend.modules.linkedin.runtime.playwright_session import LinkedInBrowser
from backend.modules.linkedin.services import accounts as accounts_svc

logger = logging.getLogger(__name__)


_LOGIN_HINTS = ("/login", "/uas/login", "/checkpoint/", "/authwall")
_RESTRICTED_HINTS = ("/checkpoint/challenges", "/account/restricted", "verify your identity", "we restricted your account")


# Селекторы (упорядочены: data-test → aria-label → text fallback).
_MESSAGE_BUTTON_SELECTORS = (
    "button[aria-label^='Message']",
    "a[aria-label^='Message']",
    "button:has-text('Message')",
    "a:has-text('Message')",
    "[data-control-name='message']",
)
_CONNECT_BUTTON_SELECTORS = (
    "button[aria-label^='Invite']",
    "button[aria-label^='Connect']",
    "button:has-text('Connect')",
)
_COMPOSER_TEXTBOX_SELECTORS = (
    "div[role='textbox'][contenteditable='true']",
    "div.msg-form__contenteditable[contenteditable='true']",
    "div[contenteditable='true'][aria-label*='message' i]",
)
_SEND_BUTTON_SELECTORS = (
    "button[aria-label^='Send'][type='submit']",
    "button[aria-label='Send']",
    "button.msg-form__send-button:not([disabled])",
    "button:has-text('Send')",
)


def _human_pause(min_ms: int = 350, max_ms: int = 1100) -> None:
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _try_locator(page, selectors: tuple[str, ...], timeout_ms: int = 4000):
    """Найти первый видимый элемент по списку селекторов; вернуть Locator или None."""
    deadline = time.monotonic() + timeout_ms / 1000
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            remaining = max(200, int((deadline - time.monotonic()) * 1000))
            if loc.is_visible(timeout=remaining):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _bump_usage_counter(db: Session, account: LinkedInAccount, field: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    row = (
        db.query(LinkedInAccountDayUsage)
        .filter(LinkedInAccountDayUsage.account_id == account.id, LinkedInAccountDayUsage.usage_date == today)
        .one_or_none()
    )
    if row is None:
        row = LinkedInAccountDayUsage(account_id=account.id, usage_date=today)
        db.add(row)
        db.flush()
    setattr(row, field, (getattr(row, field, 0) or 0) + 1)


def _check_caps(db: Session, account: LinkedInAccount, field: str, soft_cap: int) -> bool:
    """True если лимит не достигнут."""
    today = datetime.now(timezone.utc).date().isoformat()
    row = (
        db.query(LinkedInAccountDayUsage)
        .filter(LinkedInAccountDayUsage.account_id == account.id, LinkedInAccountDayUsage.usage_date == today)
        .one_or_none()
    )
    if row is None:
        return True
    used = int(getattr(row, field, 0) or 0)
    return used < soft_cap


def send_dm(
    db: Session,
    account_id: int,
    lead_id: int,
    body: str,
    *,
    headless: bool = True,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    acc = accounts_svc.get_account(db, account_id)
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    lead = db.get(LinkedInLead, int(lead_id))
    if lead is None or lead.organization_id != acc.organization_id:
        return {"ok": False, "reason_code": "lead_not_found"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "reason_code": "empty_body"}
    if not lead.profile_url or "linkedin.com/in/" not in lead.profile_url.lower():
        return {"ok": False, "reason_code": "invalid_lead_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    cap = int(daily_cap or acc.cap_messages_per_day or 30)
    if cap > 0 and not _check_caps(db, acc, "messages_sent", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    org_id = acc.organization_id
    try:
        with LinkedInBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(lead.profile_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                accounts_svc.log_event(db, org_id, acc.id, "dm_send", "error", {"phase": "goto", "error": str(e)[:300], "lead_id": lead.id})
                db.commit()
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            url = (page.url or "").lower()
            body_excerpt = ""
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                pass

            if any(h in url for h in _RESTRICTED_HINTS) or any(h in body_excerpt for h in _RESTRICTED_HINTS):
                _mark_account_restricted(db, acc, "DM aborted: account restricted (checkpoint).")
                return {"ok": False, "reason_code": "account_restricted"}
            if any(h in url for h in _LOGIN_HINTS):
                _mark_account_login_required(db, acc, "DM aborted: login required (cookies expired).")
                return {"ok": False, "reason_code": "login_required"}

            # Если нет «Message», но есть «Connect» → не 1st degree.
            msg_btn = _try_locator(page, _MESSAGE_BUTTON_SELECTORS, timeout_ms=4500)
            if msg_btn is None:
                connect_btn = _try_locator(page, _CONNECT_BUTTON_SELECTORS, timeout_ms=1500)
                if connect_btn is not None:
                    accounts_svc.log_event(db, org_id, acc.id, "dm_send", "warn", {"reason": "not_first_degree", "lead_id": lead.id})
                    db.commit()
                    return {"ok": False, "reason_code": "not_first_degree"}
                accounts_svc.log_event(db, org_id, acc.id, "dm_send", "warn", {"reason": "no_message_button", "lead_id": lead.id, "url": url})
                db.commit()
                return {"ok": False, "reason_code": "no_message_button"}

            # Anti-detect: лёгкий scroll+jitter перед кликом, navigate-as-human.
            try:
                from backend.modules.linkedin.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter,
                    mouse_to_locator_humanly, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(150, 400))
                    mouse_jitter(page, steps=random.randint(2, 4))
                    mouse_to_locator_humanly(page, msg_btn)
                    random_idle(0.4, 1.0)
            except Exception:  # noqa: BLE001
                pass

            _human_pause(400, 900)
            try:
                msg_btn.click(timeout=5000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "message_click_failed", "details": str(e)[:200]}

            composer = _try_locator(page, _COMPOSER_TEXTBOX_SELECTORS, timeout_ms=8000)
            if composer is None:
                return {"ok": False, "reason_code": "composer_not_found"}

            try:
                composer.click(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            _human_pause(200, 600)

            # Печатаем по символам с рандомными паузами (defeats simple bot-detection).
            try:
                # focus
                composer.focus(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            try:
                page.keyboard.type(body, delay=random.randint(28, 75))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "type_failed", "details": str(e)[:200]}

            _human_pause(500, 1200)

            send_btn = _try_locator(page, _SEND_BUTTON_SELECTORS, timeout_ms=4000)
            if send_btn is None:
                return {"ok": False, "reason_code": "send_button_not_found"}
            try:
                send_btn.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "send_click_failed", "details": str(e)[:200]}

            # Ждём, пока composer изчезнет (= успех) или останется (= ошибка).
            sent = False
            try:
                composer.wait_for(state="hidden", timeout=8000)
                sent = True
            except Exception:  # noqa: BLE001
                # fallback: если кнопка Send стала disabled или textarea опустела — считаем отправленным.
                try:
                    text_left = composer.inner_text(timeout=1000) or ""
                    sent = len(text_left.strip()) == 0
                except Exception:  # noqa: BLE001
                    sent = False

            if not sent:
                accounts_svc.log_event(db, org_id, acc.id, "dm_send", "warn", {"reason": "send_unconfirmed", "lead_id": lead.id})
                db.commit()
                return {"ok": False, "reason_code": "send_unconfirmed"}

            now = datetime.now(timezone.utc)
            _bump_usage_counter(db, acc, "messages_sent")
            lead.outreach_stage = "dm_sent"
            lead.last_outreach_at = now
            accounts_svc.log_event(db, org_id, acc.id, "dm_send", "info", {"lead_id": lead.id, "url": (page.url or "")[:200]})
            db.commit()
            return {"ok": True, "reason_code": None, "sent_at": now.isoformat(), "url": page.url}

    except Exception as e:  # noqa: BLE001
        logger.exception("dm_sender failed for account_id=%s lead_id=%s", account_id, lead_id)
        try:
            accounts_svc.log_event(db, org_id, acc.id, "dm_send", "error", {"phase": "session", "error": str(e)[:300], "lead_id": lead.id})
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def _mark_account_login_required(db: Session, acc: LinkedInAccount, reason: str) -> None:
    now = datetime.now(timezone.utc)
    acc.session_ok = False
    acc.last_login_check_at = now
    acc.login_blocked_at = now
    acc.login_blocked_reason = reason[:480]
    acc.status = "needs_attention"
    accounts_svc.log_event(db, acc.organization_id, acc.id, "dm_send", "warn", {"reason": "login_required"})
    db.commit()


def _mark_account_restricted(db: Session, acc: LinkedInAccount, reason: str) -> None:
    now = datetime.now(timezone.utc)
    acc.session_ok = False
    acc.last_login_check_at = now
    acc.login_blocked_at = now
    acc.login_blocked_reason = reason[:480]
    acc.status = "restricted"
    accounts_svc.log_event(db, acc.organization_id, acc.id, "dm_send", "error", {"reason": "restricted"})
    db.commit()


__all__ = ["send_dm"]
