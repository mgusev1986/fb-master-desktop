"""Instagram Direct inbox sync через browser-mode."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import (
    InstagramAccount, InstagramConversation, InstagramMessage,
)
from backend.modules.instagram.runtime.dom_resilience import (
    LOCALIZED_LOGIN_HINTS, LOCALIZED_RESTRICTED_HINTS, page_has_any_hint,
)
from backend.modules.instagram.runtime.playwright_session import InstagramBrowser

logger = logging.getLogger(__name__)


_INBOX_URL = "https://www.instagram.com/direct/inbox/"
_HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")


def _extract_handle_from_text(text: str) -> str:
    s = (text or "").strip()
    # Первая «слово-подобная» строка часто = имя или handle.
    parts = [p.strip().lstrip("@").lower() for p in s.split() if p.strip()]
    for p in parts:
        if _HANDLE_RE.match(p):
            return p
    return ""


def _upsert_conversation(db: Session, acc: InstagramAccount, handle: str, sent_at: datetime) -> InstagramConversation:
    q = (
        db.query(InstagramConversation)
        .filter(
            InstagramConversation.account_id == acc.id,
            InstagramConversation.organization_id == acc.organization_id,
            InstagramConversation.counterpart_handle == handle,
        )
    )
    conv = q.first()
    if conv is None:
        conv = InstagramConversation(
            organization_id=acc.organization_id, account_id=acc.id,
            counterpart_handle=handle,
        )
        db.add(conv)
        db.flush()
    if conv.last_message_at is None or sent_at > conv.last_message_at:
        conv.last_message_at = sent_at
        conv.last_message_from = "in"
    return conv


def _upsert_message(db: Session, conv: InstagramConversation, body: str, sent_at: datetime) -> bool:
    body = (body or "").strip()
    if not body:
        return False
    existing = (
        db.query(InstagramMessage)
        .filter(
            InstagramMessage.conversation_id == conv.id,
            InstagramMessage.direction == "in",
            InstagramMessage.body == body,
            InstagramMessage.sent_at == sent_at,
        ).first()
    )
    if existing is not None:
        return False
    db.add(InstagramMessage(
        conversation_id=conv.id, direction="in", body=body, sent_at=sent_at,
    ))
    conv.unread_count = (conv.unread_count or 0) + 1
    return True


def sync_inbox(db: Session, account_id: int, *, headless: bool = True, max_threads: int = 25) -> dict[str, Any]:
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    threads_seen = 0
    new_messages = 0
    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(_INBOX_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}

            try:
                from backend.modules.instagram.runtime.human_behavior import antidetect_enabled, gentle_scroll, random_idle
                if antidetect_enabled():
                    gentle_scroll(page, total_px=400)
                    random_idle(0.5, 1.2)
            except Exception:  # noqa: BLE001
                pass

            # Threads = div[role='listitem'] в /direct/inbox/.
            try:
                items = page.locator("div[role='listitem']")
                count = items.count()
            except Exception:  # noqa: BLE001
                count = 0
            if count == 0:
                db.commit()
                return {"ok": True, "threads_seen": 0, "new_messages": 0, "note": "empty_inbox"}

            limit = min(count, max_threads)
            now = datetime.now(timezone.utc)
            for i in range(limit):
                threads_seen += 1
                try:
                    item = items.nth(i)
                    text = ""
                    try:
                        text = (item.inner_text(timeout=800) or "").strip()
                    except Exception:  # noqa: BLE001
                        text = ""
                    handle = _extract_handle_from_text(text)
                    if not handle:
                        continue
                    preview_lines = [ln for ln in text.splitlines() if ln.strip()]
                    preview = preview_lines[-1] if len(preview_lines) > 1 else preview_lines[0] if preview_lines else ""
                    if not preview:
                        continue
                    conv = _upsert_conversation(db, acc, handle, now)
                    if _upsert_message(db, conv, preview, now):
                        new_messages += 1
                except Exception:  # noqa: BLE001
                    logger.exception("instagram inbox row parse error")
                    continue

            db.commit()
            return {"ok": True, "threads_seen": threads_seen, "new_messages": new_messages}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram inbox_sync failed account_id=%s", account_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["sync_inbox"]
