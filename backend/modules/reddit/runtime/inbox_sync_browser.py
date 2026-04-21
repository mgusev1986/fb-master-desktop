"""Reddit inbox sync через browser-mode (Playwright).

Открывает /message/inbox/ в Chromium-сессии, парсит preview каждой conversation
(counterpart username, snippet последнего сообщения, время) и upsert'ит в
RedditConversation/RedditMessage с дедупликацией.

Lightweight-sync (только preview списка, не полная история тредов) — этого
достаточно для unread-бейджей и реакции на новые входящие.

Только для аккаунтов с auth_mode="browser_profile". Для OAuth аккаунтов
работает существующий `services/inbox_poller.py` (через API).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.reddit.models import (
    RedditAccount,
    RedditConversation,
    RedditMessage,
)
from backend.modules.reddit.runtime.dm_sender import _LOGIN_HINTS, _SUSPEND_HINTS
from backend.modules.reddit.runtime.playwright_session import RedditBrowser

logger = logging.getLogger(__name__)


_INBOX_URL = "https://www.reddit.com/message/inbox/"

# Селекторы (старый редизайн + new shreddit-*).
_THREAD_ROW = (
    "table.message-parent tr.entry, "  # legacy
    "div.message, "  # legacy alt
    "shreddit-async-loader div[data-thingid], "  # new
    "[data-testid='inbox-thread']"  # new alt
)
_USERNAME_LINK = "a.author, a[href^='/user/'], a[href^='/u/']"
_SUBJECT_OR_PREVIEW = ".subject, .md, p.message-snippet, [data-testid='thread-preview']"
_TIME_TAG = "time, [data-testid='thread-time'], .live-timestamp"


def _strip_username(text: str) -> str:
    s = (text or "").strip().lstrip("@").lstrip("u/").lstrip("/u/").lstrip("/")
    s = re.sub(r"^\s*u/", "", s, flags=re.I)
    return s.split()[0] if s else ""


def _upsert_conversation(
    db: Session,
    acc: RedditAccount,
    counterpart_username: str,
    last_message_at: datetime,
) -> RedditConversation:
    q = (
        db.query(RedditConversation)
        .filter(
            RedditConversation.account_id == acc.id,
            RedditConversation.organization_id == acc.organization_id,
            RedditConversation.counterpart_username == counterpart_username,
        )
    )
    conv = q.first()
    if conv is None:
        conv = RedditConversation(
            organization_id=acc.organization_id,
            account_id=acc.id,
            counterpart_username=counterpart_username,
            thread_kind="pm",
        )
        db.add(conv)
        db.flush()
    if conv.last_message_at is None or last_message_at > conv.last_message_at:
        conv.last_message_at = last_message_at
        conv.last_message_from = "in"
    return conv


def _upsert_message(db: Session, conv: RedditConversation, body: str, sent_at: datetime) -> bool:
    body = (body or "").strip()
    if not body:
        return False
    existing = (
        db.query(RedditMessage)
        .filter(
            RedditMessage.conversation_id == conv.id,
            RedditMessage.direction == "in",
            RedditMessage.body == body,
            RedditMessage.sent_at == sent_at,
        )
        .first()
    )
    if existing is not None:
        return False
    db.add(
        RedditMessage(
            conversation_id=conv.id,
            account_id=conv.account_id,
            organization_id=conv.organization_id,
            direction="in",
            body=body,
            sent_at=sent_at,
        )
    )
    return True


def sync_inbox(
    db: Session,
    account_id: int,
    *,
    headless: bool = True,
    max_threads: int = 25,
) -> dict[str, Any]:
    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if acc.auth_mode != "browser_profile":
        return {"ok": False, "reason_code": "not_browser_mode"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    threads_seen = 0
    new_messages = 0
    try:
        with RedditBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(_INBOX_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in cur for h in _LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if any(h in body_excerpt for h in _SUSPEND_HINTS):
                return {"ok": False, "reason_code": "account_suspended"}

            # Anti-detect.
            try:
                from backend.modules.reddit.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=500)
                    mouse_jitter(page, steps=3)
                    random_idle(0.6, 1.4)
            except Exception:  # noqa: BLE001
                pass

            try:
                items = page.locator(_THREAD_ROW)
                count = items.count()
            except Exception:  # noqa: BLE001
                count = 0
            if count == 0:
                # Fallback: ищем любые ссылки на /user/<x> в основной части.
                try:
                    items = page.locator("main a[href^='/user/'], main a[href^='/u/']")
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
                    username = ""
                    preview = ""
                    try:
                        u_text = (item.locator(_USERNAME_LINK).first.inner_text(timeout=800) or "").strip()
                    except Exception:  # noqa: BLE001
                        u_text = ""
                    if not u_text:
                        try:
                            u_text = (item.inner_text(timeout=800) or "").strip().split("\n")[0]
                        except Exception:  # noqa: BLE001
                            u_text = ""
                    username = _strip_username(u_text)
                    if not username:
                        continue
                    try:
                        preview = (item.locator(_SUBJECT_OR_PREVIEW).first.inner_text(timeout=800) or "").strip()
                    except Exception:  # noqa: BLE001
                        preview = ""
                    if not preview:
                        try:
                            preview = (item.inner_text(timeout=800) or "").strip()[:240]
                        except Exception:  # noqa: BLE001
                            preview = ""
                    sent_at = now  # время thread'а парсить дорого — fallback на now
                    conv = _upsert_conversation(db, acc, username, sent_at)
                    if _upsert_message(db, conv, preview, sent_at):
                        new_messages += 1
                except Exception:  # noqa: BLE001
                    logger.exception("reddit inbox_sync row parse error")
                    continue

            db.commit()
            return {"ok": True, "threads_seen": threads_seen, "new_messages": new_messages}
    except Exception as e:  # noqa: BLE001
        logger.exception("reddit inbox_sync failed for account_id=%s", account_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["sync_inbox"]
