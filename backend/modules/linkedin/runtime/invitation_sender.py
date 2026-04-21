"""LinkedIn invitation sender — connection invitation с personalized note.

Алгоритм:
  1. Открываем `lead.profile_url`.
  2. Если редирект login/restricted — возврат `login_required`/`account_restricted`.
  3. Если кнопка «Pending» (invitation уже отправлено) — `already_sent`.
  4. Если кнопка «Message» (но нет Connect) — лид уже в connections → `already_connected`.
  5. Иначе ищем «Connect» (или «More» → «Connect» в overflow). Если её нет — `connect_button_not_found`.
  6. Кликаем Connect → modal «Send invitation».
  7. Если note <= 200 chars — кликаем «Add a note» → вводим текст.
  8. Кликаем «Send».
  9. Ждём, пока modal изчезнет.

Note limit: 200 chars (LinkedIn). Если длиннее — обрезаем + warning.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import LinkedInAccount, LinkedInLead
from backend.modules.linkedin.runtime.dm_sender import (
    _LOGIN_HINTS,
    _RESTRICTED_HINTS,
    _bump_usage_counter,
    _check_caps,
    _human_pause,
    _mark_account_login_required,
    _mark_account_restricted,
    _try_locator,
)
from backend.modules.linkedin.runtime.playwright_session import LinkedInBrowser
from backend.modules.linkedin.services import accounts as accounts_svc

logger = logging.getLogger(__name__)


_NOTE_LIMIT = 200

_CONNECT_BTN = (
    "button[aria-label^='Invite']",
    "button[aria-label^='Connect']",
    "button:has-text('Connect')",
)
_MORE_BTN = (
    "button[aria-label^='More']",
    "button:has-text('More')",
)
_PENDING_BTN = (
    "button[aria-label^='Pending']",
    "button:has-text('Pending')",
)
_MESSAGE_BTN = (
    "button[aria-label^='Message']",
    "a[aria-label^='Message']",
)
_ADD_NOTE_BTN = (
    "button[aria-label='Add a note']",
    "button:has-text('Add a note')",
)
_NOTE_TEXTAREA = (
    "textarea[name='message']",
    "textarea#custom-message",
    "div[role='textbox'][contenteditable='true']",
)
_SEND_INVITE_BTN = (
    "button[aria-label='Send invitation']",
    "button[aria-label='Send now']",
    "button:has-text('Send invitation')",
    "button:has-text('Send now')",
    "button:has-text('Send')",
)


def send_invitation(
    db: Session,
    account_id: int,
    lead_id: int,
    note: str,
    *,
    headless: bool = True,
    daily_cap: int | None = None,
    weekly_cap: int | None = None,
) -> dict[str, Any]:
    acc = accounts_svc.get_account(db, account_id)
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    lead = db.get(LinkedInLead, int(lead_id))
    if lead is None or lead.organization_id != acc.organization_id:
        return {"ok": False, "reason_code": "lead_not_found"}
    if not lead.profile_url or "linkedin.com/in/" not in lead.profile_url.lower():
        return {"ok": False, "reason_code": "invalid_lead_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    # Note: пустая допустима (можно отправить «без сообщения»), но если есть — обрезаем до лимита.
    note = (note or "").strip()
    truncated = False
    if len(note) > _NOTE_LIMIT:
        note = note[: _NOTE_LIMIT - 1].rstrip() + "…"
        truncated = True

    cap = int(daily_cap or acc.cap_invitations_per_day or 15)
    if cap > 0 and not _check_caps(db, acc, "invitations_sent", cap):
        return {"ok": False, "reason_code": "daily_cap_reached", "details": f"cap={cap}"}

    org_id = acc.organization_id
    try:
        with LinkedInBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(lead.profile_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                accounts_svc.log_event(db, org_id, acc.id, "invitation_send", "error", {"phase": "goto", "error": str(e)[:300], "lead_id": lead.id})
                db.commit()
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            url = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in url for h in _RESTRICTED_HINTS) or any(h in body_excerpt for h in _RESTRICTED_HINTS):
                _mark_account_restricted(db, acc, "Invitation aborted: account restricted (checkpoint).")
                return {"ok": False, "reason_code": "account_restricted"}
            if any(h in url for h in _LOGIN_HINTS):
                _mark_account_login_required(db, acc, "Invitation aborted: login required (cookies expired).")
                return {"ok": False, "reason_code": "login_required"}

            # Already pending?
            if _try_locator(page, _PENDING_BTN, timeout_ms=1500):
                accounts_svc.log_event(db, org_id, acc.id, "invitation_send", "warn", {"reason": "already_sent", "lead_id": lead.id})
                db.commit()
                return {"ok": False, "reason_code": "already_sent"}

            # Connect button: ищем в actions → если нет, открываем «More» dropdown.
            connect_btn = _try_locator(page, _CONNECT_BTN, timeout_ms=2500)
            if connect_btn is None:
                # Может быть в overflow → жмём More.
                more = _try_locator(page, _MORE_BTN, timeout_ms=1500)
                if more is not None:
                    try:
                        more.click(timeout=2000)
                        _human_pause(300, 700)
                        connect_btn = _try_locator(page, _CONNECT_BTN, timeout_ms=2500)
                    except Exception:  # noqa: BLE001
                        pass
            if connect_btn is None:
                # Если есть Message — значит уже connected.
                if _try_locator(page, _MESSAGE_BTN, timeout_ms=1000):
                    accounts_svc.log_event(db, org_id, acc.id, "invitation_send", "warn", {"reason": "already_connected", "lead_id": lead.id})
                    db.commit()
                    return {"ok": False, "reason_code": "already_connected"}
                accounts_svc.log_event(db, org_id, acc.id, "invitation_send", "warn", {"reason": "connect_button_not_found", "lead_id": lead.id, "url": url})
                db.commit()
                return {"ok": False, "reason_code": "connect_button_not_found"}

            try:
                from backend.modules.linkedin.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter,
                    mouse_to_locator_humanly, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=random.randint(120, 360))
                    mouse_jitter(page, steps=random.randint(2, 4))
                    mouse_to_locator_humanly(page, connect_btn)
                    random_idle(0.5, 1.2)
            except Exception:  # noqa: BLE001
                pass

            _human_pause(400, 900)
            try:
                connect_btn.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "connect_click_failed", "details": str(e)[:200]}

            _human_pause(500, 1200)

            # Если есть note — открываем «Add a note» modal и вводим.
            if note:
                add_note = _try_locator(page, _ADD_NOTE_BTN, timeout_ms=4000)
                if add_note is not None:
                    try:
                        add_note.click(timeout=2000)
                    except Exception:  # noqa: BLE001
                        pass
                    _human_pause(300, 700)
                    textarea = _try_locator(page, _NOTE_TEXTAREA, timeout_ms=4000)
                    if textarea is None:
                        return {"ok": False, "reason_code": "note_textarea_not_found"}
                    try:
                        textarea.click(timeout=2000)
                        textarea.focus(timeout=2000)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        page.keyboard.type(note, delay=random.randint(28, 65))
                    except Exception as e:  # noqa: BLE001
                        return {"ok": False, "reason_code": "note_type_failed", "details": str(e)[:200]}
                    _human_pause(400, 900)

            # Send invitation.
            send_btn = _try_locator(page, _SEND_INVITE_BTN, timeout_ms=4000)
            if send_btn is None:
                return {"ok": False, "reason_code": "send_button_not_found"}
            try:
                send_btn.click(timeout=4000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "send_click_failed", "details": str(e)[:200]}

            # Подтверждение: pending кнопка появилась = успех.
            time.sleep(2.0 + random.random())
            success = bool(_try_locator(page, _PENDING_BTN, timeout_ms=4000))

            if not success:
                accounts_svc.log_event(db, org_id, acc.id, "invitation_send", "warn", {"reason": "send_unconfirmed", "lead_id": lead.id})
                db.commit()
                return {"ok": False, "reason_code": "send_unconfirmed"}

            now = datetime.now(timezone.utc)
            _bump_usage_counter(db, acc, "invitations_sent")
            lead.outreach_stage = "invite_sent"
            lead.last_outreach_at = now
            payload = {"lead_id": lead.id, "note_len": len(note), "truncated": truncated}
            accounts_svc.log_event(db, org_id, acc.id, "invitation_send", "info", payload)
            db.commit()
            return {
                "ok": True,
                "reason_code": None,
                "sent_at": now.isoformat(),
                "note_truncated": truncated,
            }

    except Exception as e:  # noqa: BLE001
        logger.exception("invitation_sender failed for account_id=%s lead_id=%s", account_id, lead_id)
        try:
            accounts_svc.log_event(db, org_id, acc.id, "invitation_send", "error", {"phase": "session", "error": str(e)[:300], "lead_id": lead.id})
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["send_invitation"]
