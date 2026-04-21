"""LinkedIn inbox sync — синхронизация входящих сообщений в БД.

Открываем `/messaging/`, проходим по списку тредов, забираем последнее
сообщение каждого треда (имя counterpart, текст, время). Upsert в
`LinkedInConversation` (по паре account_id + counterpart_public_identifier)
и в `LinkedInMessage` с дедупликацией по тексту+времени.

Это lightweight-sync (без полной истории) — главная задача: заметить
новые входящие, чтобы UI показывал бейджи unread и оператор знал, что
кто-то ответил. Полная история тредов → next-iteration.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import (
    LinkedInAccount,
    LinkedInConversation,
    LinkedInMessage,
)
from backend.modules.linkedin.runtime.dm_sender import _LOGIN_HINTS, _RESTRICTED_HINTS, _human_pause
from backend.modules.linkedin.runtime.playwright_session import LinkedInBrowser
from backend.modules.linkedin.services import accounts as accounts_svc

logger = logging.getLogger(__name__)


_INBOX_URL = "https://www.linkedin.com/messaging/"
_THREAD_ITEMS = "li.msg-conversation-listitem, [data-control-name='view_thread']"
_THREAD_NAME = "h3, .msg-conversation-listitem__participant-names"
_THREAD_PREVIEW = ".msg-conversation-card__message-snippet, p.msg-conversation-card__message-snippet"
_THREAD_TIME = "time, .msg-conversation-card__time-stamp"


def _upsert_conversation(
    db: Session,
    acc: LinkedInAccount,
    counterpart_name: str,
    counterpart_handle: str | None,
    last_message_at: datetime | None,
    last_message_from: str | None,
) -> LinkedInConversation:
    q = db.query(LinkedInConversation).filter(
        LinkedInConversation.account_id == acc.id,
        LinkedInConversation.organization_id == acc.organization_id,
    )
    if counterpart_handle:
        q = q.filter(LinkedInConversation.counterpart_public_identifier == counterpart_handle)
    else:
        q = q.filter(LinkedInConversation.counterpart_full_name == counterpart_name)
    conv = q.one_or_none()
    if conv is None:
        conv = LinkedInConversation(
            organization_id=acc.organization_id,
            account_id=acc.id,
            counterpart_public_identifier=counterpart_handle,
            counterpart_full_name=counterpart_name,
            thread_kind="dm",
        )
        db.add(conv)
        db.flush()
    if last_message_at:
        if conv.last_message_at is None or last_message_at > conv.last_message_at:
            conv.last_message_at = last_message_at
            conv.last_message_from = last_message_from
    return conv


def _upsert_message(
    db: Session,
    conv: LinkedInConversation,
    body: str,
    sent_at: datetime,
    direction: str,
) -> bool:
    """Дедупликация: тот же direction + body + sent_at в той же conversation = не добавляем."""
    body = (body or "").strip()
    if not body:
        return False
    existing = (
        db.query(LinkedInMessage)
        .filter(
            LinkedInMessage.conversation_id == conv.id,
            LinkedInMessage.direction == direction,
            LinkedInMessage.body == body,
            LinkedInMessage.sent_at == sent_at,
        )
        .first()
    )
    if existing is not None:
        return False
    db.add(
        LinkedInMessage(
            conversation_id=conv.id,
            direction=direction,
            body=body,
            sent_at=sent_at,
        )
    )
    if direction == "in":
        conv.unread_count = (conv.unread_count or 0) + 1
    return True


def sync_inbox(db: Session, account_id: int, *, headless: bool = True, max_threads: int = 25) -> dict[str, Any]:
    acc = accounts_svc.get_account(db, account_id)
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    threads_seen = 0
    new_messages = 0
    try:
        with LinkedInBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(_INBOX_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                accounts_svc.log_event(db, org_id, acc.id, "inbox_sync", "error", {"phase": "goto", "error": str(e)[:300]})
                db.commit()
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            url = (page.url or "").lower()
            if any(h in url for h in _RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}
            if any(h in url for h in _LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}

            _human_pause(700, 1500)

            try:
                items = page.locator(_THREAD_ITEMS)
                count = items.count()
            except Exception:  # noqa: BLE001
                count = 0
            if count == 0:
                accounts_svc.log_event(db, org_id, acc.id, "inbox_sync", "info", {"reason": "no_threads"})
                db.commit()
                return {"ok": True, "threads_seen": 0, "new_messages": 0}

            limit = min(count, max_threads)
            for i in range(limit):
                threads_seen += 1
                try:
                    item = items.nth(i)
                    name = ""
                    preview = ""
                    try:
                        name = (item.locator(_THREAD_NAME).first.inner_text(timeout=1000) or "").strip()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        preview = (item.locator(_THREAD_PREVIEW).first.inner_text(timeout=1000) or "").strip()
                    except Exception:  # noqa: BLE001
                        pass
                    # Время — используем сейчас как fallback (LinkedIn рендерит "1d", "Now" и пр.; парсить долго).
                    sent_at = datetime.now(timezone.utc)
                    if not name and not preview:
                        continue
                    conv = _upsert_conversation(
                        db,
                        acc,
                        counterpart_name=name or "Unknown",
                        counterpart_handle=None,
                        last_message_at=sent_at,
                        last_message_from="in",
                    )
                    if _upsert_message(db, conv, preview, sent_at, direction="in"):
                        new_messages += 1
                except Exception:  # noqa: BLE001
                    logger.exception("inbox_sync thread parse error")
                    continue

            accounts_svc.log_event(
                db, org_id, acc.id, "inbox_sync", "info",
                {"threads_seen": threads_seen, "new_messages": new_messages},
            )
            db.commit()
            return {"ok": True, "threads_seen": threads_seen, "new_messages": new_messages}
    except Exception as e:  # noqa: BLE001
        logger.exception("inbox_sync failed for account_id=%s", account_id)
        try:
            accounts_svc.log_event(db, org_id, acc.id, "inbox_sync", "error", {"phase": "session", "error": str(e)[:300]})
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["sync_inbox"]
