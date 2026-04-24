"""Синхронизация списка чатов (и опционально текста) с Facebook Messenger."""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import sync_playwright
from sqlalchemy import or_

from backend.database import SessionLocal
from backend.models import Conversation, FBAccount, Job, Message, MessengerSendQueue, Person, Setting
from backend.services.contacted_registry import upsert_contacted
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.job_logging import finish_job, log_job_event, start_job
from backend.services.messenger_person_link import (
    build_messenger_person_id_index,
    ensure_conversation_person,
    relink_conversations_missing_person,
    resolve_messenger_conversation_person_id,
)
from backend.services.messenger_scrape import (
    MessengerUIBlockedError,
    normalize_peer_url,
    open_messages_inbox,
    open_thread,
    scrape_inbox_threads_with_scroll,
    scrape_thread_messages,
    wait_until_messenger_unblocked_or_raise,
)
from backend.services.playwright_resource import playwright_run_slot
from backend.services.proxy_health_guard import (
    ProxyTunnelBlockedError,
    pause_all_automation_for_fb_account,
)
from backend.services.sequence_actions import (
    _messages_new_search_term_from_canonical,
    dm_direct_thread_url_for_person,
    try_send_dm_on_open_thread_page,
)
from backend.services.throttle import is_valid_throttle_preset
from backend.services.warmup_actions import parse_storage_state
from backend.services.warmup_worker import fb_account_playwright_profile_ephemeral
from backend.services.messenger_ai_worker import schedule_messenger_ai_if_needed
from backend.services.messenger_live_activity import mark_messenger_outbound_activity

logger = logging.getLogger(__name__)

JOB_TYPE = "messenger_sync"
INBOX_RECENT_THREAD_PULL_LIMIT = 5
# Чаты с непрочитанным + включённым AI — подтягиваем тред, чтобы сработал автоответ
INBOX_AI_UNREAD_SCAN_LIMIT = 20
MAX_INBOX_THREAD_REFRESH = 25

_messenger_lock = threading.Lock()
_messenger_active_job_id: int | None = None


def messenger_worker_busy() -> bool:
    with _messenger_lock:
        return _messenger_active_job_id is not None


def messenger_worker_blocks_other_playwright() -> bool:
    """
    Устарело: параллелизм ограничивает playwright_run_slot (семафор + блокировка профиля).
    Оставлено для совместимости импортов — всегда False.
    """
    return False


def _upsert_conversation(
    db,
    *,
    fb_account_id: int,
    thread_id: str,
    peer_url: str,
    peer_name: str,
    snippet: str,
    id_index: dict[str, int] | None = None,
) -> Conversation:
    url = normalize_peer_url(thread_id) if thread_id else peer_url[:512]
    tid_clean = str(thread_id).strip() if thread_id else ""
    tid_arg = tid_clean if tid_clean.isdigit() else None
    pid = resolve_messenger_conversation_person_id(
        db, tid_arg, peer_name, id_index=id_index
    )
    row = (
        db.query(Conversation)
        .filter(
            Conversation.fb_account_id == fb_account_id,
            Conversation.peer_url == url,
        )
        .first()
    )
    now = datetime.now(timezone.utc)
    new_name = (peer_name or "Диалог")[:255]
    new_snippet = (snippet or "")[:2000]
    if row:
        changed = False
        if new_name and new_name != (row.peer_name or ""):
            row.peer_name = new_name
            changed = True
        if snippet and new_snippet != (row.last_snippet or ""):
            row.last_snippet = new_snippet
            changed = True
        if row.last_at is None or changed:
            row.last_at = now
        if pid:
            row.person_id = pid
        return row
    c = Conversation(
        fb_account_id=fb_account_id,
        peer_url=url,
        peer_name=new_name,
        last_snippet=new_snippet,
        last_at=now,
        unread_count=0,
        person_id=pid,
    )
    db.add(c)
    return c


def upsert_conversation_from_messenger_webview_url(
    db,
    *,
    organization_id: int,
    fb_account_id: int,
    location_url: str,
    peer_display_name: str | None = None,
) -> Conversation | None:
    """
    Встроенный Messenger (webview): по URL открытого треда и FB-аккаунту создать/обновить Conversation.
    """
    from backend.services.messenger_person_link import (
        build_messenger_person_id_index,
        parse_messenger_thread_location,
    )

    acc = db.get(FBAccount, fb_account_id)
    if (
        acc is None
        or acc.organization_id != organization_id
        or not account_in_active_slot(acc)
    ):
        return None

    peer_url, digit_tid = parse_messenger_thread_location(location_url)
    if not peer_url:
        return None

    hint = (peer_display_name or "").strip()[:255]
    id_index = build_messenger_person_id_index(db, organization_id=organization_id)
    tid_arg = digit_tid or ""
    row = _upsert_conversation(
        db,
        fb_account_id=fb_account_id,
        thread_id=tid_arg,
        peer_url=peer_url,
        peer_name=hint or "Диалог",
        snippet="",
        id_index=id_index,
    )
    if hint:
        row.peer_name = hint
        db.flush()
    return row


def lookup_conversation_person_for_messenger_webview_url(
    db,
    *,
    organization_id: int,
    fb_account_id: int,
    location_url: str,
) -> tuple[Conversation | None, Person | None]:
    """
    Только чтение: найти Conversation и связанного Person по URL webview и аккаунту
    (без создания строк в БД).
    """
    from backend.services.messenger_person_link import parse_messenger_thread_location

    acc = db.get(FBAccount, fb_account_id)
    if (
        acc is None
        or acc.organization_id != organization_id
        or not account_in_active_slot(acc)
    ):
        return None, None

    peer_url, _digit = parse_messenger_thread_location(location_url)
    if not peer_url:
        return None, None

    peer_variants = {peer_url}
    if "/messages/t/" in peer_url and "/e2ee/" not in peer_url:
        peer_variants.add(peer_url.replace("/messages/t/", "/messages/e2ee/t/", 1))

    conv = (
        db.query(Conversation)
        .filter(
            Conversation.fb_account_id == fb_account_id,
            Conversation.peer_url.in_(list(peer_variants)),
        )
        .first()
    )
    if not conv:
        return None, None
    if not conv.person_id:
        return conv, None
    person = db.get(Person, int(conv.person_id))
    if person is None or person.organization_id != organization_id:
        return conv, None
    return conv, person


def record_outbound_dm_in_cabinet(
    db,
    *,
    fb_account_id: int,
    person: Person,
    message_text: str,
) -> None:
    """
    После успешной отправки ЛС из сценария или рассылки: создать/обновить диалог в кабинете,
    чтобы он сразу появился в списке без ожидания синхронизации с Facebook.
    URL треда выводится из canonical_url контакта (как при отправке из профиля).
    """
    snippet = (message_text or "").strip()[:2000] or "Сообщение отправлено"

    peer_full = dm_direct_thread_url_for_person(
        (person.canonical_url or "").strip(),
        person.raw_meta if isinstance(person.raw_meta, dict) else None,
    )
    if not peer_full:
        hint = _messages_new_search_term_from_canonical((person.canonical_url or "").strip())
        if hint.isdigit() and len(hint) >= 5:
            peer_full = f"https://www.facebook.com/messages/t/{hint}"
        elif hint and re.match(r"^[A-Za-z0-9._-]{1,80}$", hint, re.I):
            low = hint.lower()
            if low not in (
                "www",
                "web",
                "m",
                "people",
                "messages",
                "profile",
                "story",
                "stories",
                "reel",
                "watch",
            ):
                peer_full = f"https://www.facebook.com/messages/t/{hint}"
    if not peer_full:
        # Всё равно дергаем токен — встроенный Messenger опросит /api/outbound-activity и может перезагрузить webview.
        mark_messenger_outbound_activity(
            fb_account_id,
            source="outreach",
            preview=snippet[:140],
            open_thread_url=None,
        )
        return
    peer_url = peer_full.split("?")[0].split("#")[0][:512]
    mt = re.search(r"/messages/t/([^/?#]+)/?$", peer_url)
    segment = (mt.group(1) if mt else "").strip()
    digit_tid = segment if segment.isdigit() else ""
    name = (person.display_name or person.first_name or "Диалог")[:255]
    id_index = build_messenger_person_id_index(
        db, organization_id=person.organization_id
    )
    row = _upsert_conversation(
        db,
        fb_account_id=fb_account_id,
        thread_id=digit_tid,
        peer_url=peer_url,
        peer_name=name,
        snippet=snippet,
        id_index=id_index,
    )
    row.person_id = person.id
    row.last_at = datetime.now(timezone.utc)
    row.unread_count = 0
    row.local_unread = False
    text_full = (message_text or "").strip()[:8000]
    db.flush()
    if text_full and row.id:
        last_msg = (
            db.query(Message)
            .filter(Message.conversation_id == row.id)
            .order_by(Message.id.desc())
            .first()
        )
        is_same_tail = bool(
            last_msg
            and str(last_msg.direction or "").strip().lower() == "out"
            and str(last_msg.body or "").strip() == text_full
        )
        if not is_same_tail:
            db.add(
                Message(
                    conversation_id=row.id,
                    direction="out",
                    body=text_full,
                )
            )
    mark_messenger_outbound_activity(
        fb_account_id,
        source="outreach",
        preview=snippet,
        open_thread_url=peer_url or None,
    )


def _replace_conversation_messages(
    db,
    *,
    conv: Conversation,
    msgs: list[dict[str, str]],
    fallback_snippet: str = "",
    touch_last_at: bool = True,
) -> int:
    valid: list[dict[str, str]] = []
    for m in msgs or []:
        body = str((m or {}).get("body") or "").strip()[:8000]
        if not body:
            continue
        direction = str((m or {}).get("direction") or "in").strip().lower()
        if direction not in ("in", "out"):
            direction = "in"
        valid.append({"body": body, "direction": direction})
    if not valid:
        return 0
    db.query(Message).filter(Message.conversation_id == conv.id).delete()
    for m in valid:
        db.add(
            Message(
                conversation_id=conv.id,
                direction=m["direction"],
                body=m["body"],
            )
        )
    last_body = str(valid[-1]["body"] or "").strip()
    if last_body:
        conv.last_snippet = last_body[:2000]
    elif fallback_snippet.strip():
        conv.last_snippet = fallback_snippet.strip()[:2000]
    if touch_last_at:
        conv.last_at = datetime.now(timezone.utc)
    try:
        from backend.services.messenger_ai_memory import rebuild_messenger_ai_memory_for_conversation

        rebuild_messenger_ai_memory_for_conversation(db, conv.id)
    except Exception:
        logger.exception("messenger_ai_memory rebuild failed conv=%s", conv.id)
    return len(valid)


def _refresh_recent_inbox_threads(
    db,
    *,
    page,
    job_id: int,
    account: FBAccount,
    conversations: list[Conversation],
    max_threads: int | None = None,
) -> int:
    refreshed = 0
    cap = int(max_threads) if max_threads is not None else INBOX_RECENT_THREAD_PULL_LIMIT
    if cap < 1:
        return 0
    for conv in conversations[:cap]:
        if not conv or not (conv.peer_url or "").strip():
            continue
        try:
            open_thread(page, conv.peer_url)
            wait_until_messenger_unblocked_or_raise(page)
            stored_n = _replace_conversation_messages(
                db,
                conv=conv,
                msgs=scrape_thread_messages(page),
            )
            if stored_n < 1:
                continue
            db.commit()
            log_job_event(
                db,
                job_id=job_id,
                event_type="messenger_inbox_thread_refreshed",
                fb_account_id=account.id,
                outcome="ok",
                payload={"conversation_id": conv.id, "n": stored_n},
            )
            db.commit()
            try:
                schedule_messenger_ai_if_needed(db, conversation_id=conv.id)
            except Exception:
                logger.exception(
                    "messenger AI schedule after inbox refresh conv=%s", conv.id
                )
            refreshed += 1
        except MessengerUIBlockedError:
            raise
        except Exception:
            logger.exception("messenger inbox thread refresh account=%s conv=%s", account.id, conv.id)
            db.rollback()
    return refreshed


def process_messenger_sync_job(job_id: int) -> None:
    global _messenger_active_job_id

    dbx = SessionLocal()
    try:
        job_early = dbx.get(Job, job_id)
        snap_early = (
            job_early.config_snapshot if isinstance(job_early.config_snapshot, dict) else {}
        )
        if job_early and job_early.job_type == JOB_TYPE:
            inbox_autosync = bool(snap_early.get("inbox_autosync"))
        else:
            inbox_autosync = False
    finally:
        dbx.close()

    with _messenger_lock:
        if _messenger_active_job_id is not None:
            if _messenger_active_job_id != job_id:
                dbx = SessionLocal()
                try:
                    j = dbx.get(Job, job_id)
                    if j and j.job_type == JOB_TYPE:
                        finish_job(
                            dbx,
                            j,
                            status="cancelled",
                            error="Уже идёт синхронизация мессенджера",
                        )
                finally:
                    dbx.close()
                return
            return
        _messenger_active_job_id = job_id

    try:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if not job or job.job_type != JOB_TYPE:
                return
            snap = job.config_snapshot if isinstance(job.config_snapshot, dict) else {}
            inbox_autosync = bool(snap.get("inbox_autosync"))
            inbox_autosync_light = bool(snap.get("inbox_autosync_light"))
            pull_background = bool(snap.get("pull_background"))
            account_ids = snap.get("account_ids")
            load_raw = snap.get("load_thread_conversation_id")
            load_thread_id: int | None = (
                int(load_raw) if load_raw is not None and str(load_raw).strip().isdigit() else None
            )

            start_job(db, job)

            send_block = snap.get("send_message") if isinstance(snap, dict) else None
            send_queue_row_id: int | None = None
            if isinstance(send_block, dict):
                qraw = send_block.get("send_queue_row_id")
                if qraw is not None and str(qraw).strip().isdigit():
                    send_queue_row_id = int(qraw)
            if isinstance(send_block, dict) and (str(send_block.get("text") or "")).strip():
                from backend.services.messenger_send_queue import finalize_messenger_send_queue_row

                def _qfin(ok: bool, err: str | None = None) -> None:
                    finalize_messenger_send_queue_row(
                        db, send_queue_row_id, success=ok, error=err
                    )

                cid_raw = send_block.get("conversation_id")
                body = str(send_block.get("text") or "").strip()[:4000]
                if cid_raw is None or not str(cid_raw).strip().isdigit():
                    finish_job(db, job, status="failed", error="Неверный идентификатор чата")
                    db.commit()
                    _qfin(False, "Неверный идентификатор чата")
                    return
                conv_send = db.get(Conversation, int(cid_raw))
                if not conv_send or not (conv_send.peer_url or "").strip():
                    finish_job(db, job, status="failed", error="Чат не найден или нет ссылки на тред")
                    db.commit()
                    _qfin(False, "Чат не найден или нет ссылки на тред")
                    return
                acc_send = db.get(FBAccount, conv_send.fb_account_id)
                if not acc_send or not account_in_active_slot(acc_send):
                    finish_job(
                        db,
                        job,
                        status="failed",
                        error="Аккаунт не в активном слоте (1–3) или не найден",
                    )
                    db.commit()
                    _qfin(False, "Аккаунт не в активном слоте (1–3) или не найден")
                    return
                send_tp = "medium"
                row_tp = db.get(Setting, "throttle_preset")
                if row_tp and is_valid_throttle_preset(str(row_tp.value or "").strip().lower()):
                    send_tp = str(row_tp.value).strip().lower()
                with sync_playwright() as p:
                    try:
                        with playwright_run_slot(fb_account_id=acc_send.id):
                            with fb_account_playwright_profile_ephemeral(
                                p,
                                acc_send,
                                storage_state=parse_storage_state(acc_send.session_state_json),
                                db=db,
                            ) as ctx:
                                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                                open_thread(page, conv_send.peer_url)
                                wait_until_messenger_unblocked_or_raise(page)
                                ok_send, err_send = try_send_dm_on_open_thread_page(
                                    page,
                                    body,
                                    peer_url=conv_send.peer_url,
                                    throttle_preset=send_tp,
                                )
                                if not ok_send:
                                    if "facebook_message_request_limit" in (err_send or ""):
                                        pause_all_automation_for_fb_account(db, acc_send.id)
                                        logger.warning(
                                            "Messenger send: лимит запросов — пауза "
                                            "автоматизации fb_account_id=%s",
                                            acc_send.id,
                                        )
                                    finish_job(db, job, status="failed", error=err_send[:900])
                                    db.commit()
                                    log_job_event(
                                        db,
                                        job_id=job_id,
                                        event_type="messenger_send_failed",
                                        fb_account_id=acc_send.id,
                                        outcome="fail",
                                        payload={"conversation_id": conv_send.id, "error": err_send[:400]},
                                    )
                                    db.commit()
                                    _qfin(False, err_send[:900])
                                else:
                                    msgs = scrape_thread_messages(page)
                                    conv_send.last_at = datetime.now(timezone.utc)
                                    conv_send.last_snippet = body[:2000]
                                    conv_send.unread_count = 0
                                    conv_send.local_unread = False
                                    stored_n = _replace_conversation_messages(
                                        db,
                                        conv=conv_send,
                                        msgs=msgs,
                                        fallback_snippet=body,
                                        touch_last_at=False,
                                    )
                                    if stored_n < 1:
                                        db.add(
                                            Message(
                                                conversation_id=conv_send.id,
                                                direction="out",
                                                body=body,
                                            )
                                        )
                                        try:
                                            from backend.services.messenger_ai_memory import (
                                                rebuild_messenger_ai_memory_for_conversation,
                                            )

                                            rebuild_messenger_ai_memory_for_conversation(
                                                db, conv_send.id
                                            )
                                        except Exception:
                                            logger.exception(
                                                "messenger_ai_memory rebuild after out conv=%s",
                                                conv_send.id,
                                            )
                                    person_send, _, _ = ensure_conversation_person(db, conv_send)
                                    if person_send is not None:
                                        upsert_contacted(
                                            db,
                                            person_id=person_send.id,
                                            fb_account_id=acc_send.id,
                                            canonical_url=person_send.canonical_url,
                                        )
                                        person_send.crm_stage = "new"
                                        person_send.crm_stage_changed_at = datetime.now(timezone.utc)
                                    db.commit()
                                    log_job_event(
                                        db,
                                        job_id=job_id,
                                        event_type="messenger_message_sent",
                                        fb_account_id=acc_send.id,
                                        outcome="ok",
                                        payload={"conversation_id": conv_send.id, "n": stored_n or 1},
                                    )
                                    db.commit()
                                    finish_job(db, job, status="success")
                                    db.commit()
                                    if send_queue_row_id:
                                        qrow = db.get(MessengerSendQueue, send_queue_row_id)
                                        if qrow and qrow.ai_transcript_fingerprint:
                                            conv_send.ai_last_transcript_fingerprint = (
                                                qrow.ai_transcript_fingerprint
                                            )
                                            conv_send.ai_reply_steps_used = int(
                                                conv_send.ai_reply_steps_used or 0
                                            ) + 1
                                            db.commit()
                                    _qfin(True)
                    except ProxyTunnelBlockedError as e:
                        msg = str(e)[:2000]
                        logger.warning("messenger send proxy blocked account %s: %s", acc_send.id, msg)
                        finish_job(db, job, status="failed", error=msg)
                        db.commit()
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="messenger_send_proxy_blocked",
                            severity="warning",
                            fb_account_id=acc_send.id,
                            outcome="fail",
                            payload={"error": msg[:400]},
                        )
                        db.commit()
                        _qfin(False, msg)
                    except MessengerUIBlockedError as e:
                        logger.warning("messenger send blocked (E2EE/UI): %s", e)
                        finish_job(db, job, status="failed", error=str(e)[:2000])
                        db.commit()
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="messenger_send_blocked",
                            severity="warning",
                            fb_account_id=acc_send.id,
                            outcome="fail",
                            payload={"error": str(e)[:400]},
                        )
                        db.commit()
                        _qfin(False, str(e)[:2000])
                    except Exception as e:
                        logger.exception("messenger send account %s", acc_send.id)
                        finish_job(db, job, status="failed", error=str(e)[:900])
                        db.commit()
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="messenger_send_error",
                            severity="error",
                            fb_account_id=acc_send.id,
                            outcome="fail",
                            payload={"error": str(e)[:400]},
                        )
                        db.commit()
                        _qfin(False, str(e)[:900])
                return

            accounts: list[FBAccount] = []
            if load_thread_id:
                conv = db.get(Conversation, int(load_thread_id))
                if not conv:
                    finish_job(db, job, status="failed", error="Диалог не найден")
                    db.commit()
                    return
                acc = db.get(FBAccount, conv.fb_account_id)
                if not acc or not account_in_active_slot(acc):
                    finish_job(
                        db,
                        job,
                        status="failed",
                        error="Аккаунт не в активном слоте (1–3) или не найден",
                    )
                    db.commit()
                    return
                accounts = [acc]
            else:
                if inbox_autosync:
                    q = (
                        db.query(FBAccount)
                        .filter(FBAccount.active_slot.isnot(None))
                        .order_by(FBAccount.id)
                    )
                else:
                    q = (
                        db.query(FBAccount)
                        .filter(
                            FBAccount.organization_id == job.organization_id,
                            FBAccount.active_slot.isnot(None),
                        )
                        .order_by(FBAccount.id)
                    )
                if isinstance(account_ids, list) and account_ids:
                    ids = [int(x) for x in account_ids if str(x).isdigit()]
                    if ids:
                        q = q.filter(FBAccount.id.in_(ids))
                accounts = q.all()

            if not accounts:
                finish_job(db, job, status="failed", error="Нет аккаунтов")
                db.commit()
                return

            batch_any_ok = False
            last_sync_error: str | None = None
            # Один индекс «id в URL профиля → person» на всю синхронизацию списка чатов
            messenger_person_id_index: dict[str, int] | None = None
            if not load_thread_id:
                messenger_person_id_index = build_messenger_person_id_index(
                    db,
                    organization_id=None
                    if inbox_autosync
                    else job.organization_id,
                )

            with sync_playwright() as p:
                for account in accounts:
                    try:
                        with playwright_run_slot(fb_account_id=account.id):
                            with fb_account_playwright_profile_ephemeral(
                                p,
                                account,
                                storage_state=parse_storage_state(account.session_state_json),
                                headless=True if (inbox_autosync or pull_background) else None,
                                db=db,
                            ) as ctx:
                                page = ctx.pages[0] if ctx.pages else ctx.new_page()

                                if load_thread_id:
                                    conv = db.get(Conversation, int(load_thread_id))
                                    if not conv or conv.fb_account_id != account.id:
                                        continue
                                    if not conv.peer_url:
                                        continue
                                    open_thread(page, conv.peer_url)
                                    wait_until_messenger_unblocked_or_raise(page)
                                    msgs = scrape_thread_messages(page)
                                    stored_n = _replace_conversation_messages(
                                        db,
                                        conv=conv,
                                        msgs=msgs,
                                    )
                                    idx_one = build_messenger_person_id_index(
                                        db, organization_id=job.organization_id
                                    )
                                    mt = re.search(r"/messages/t/(\d+)", conv.peer_url or "")
                                    if mt:
                                        p_link = resolve_messenger_conversation_person_id(
                                            db,
                                            mt.group(1),
                                            conv.peer_name,
                                            id_index=idx_one,
                                        )
                                        if p_link:
                                            conv.person_id = p_link
                                    db.commit()
                                    log_job_event(
                                        db,
                                        job_id=job_id,
                                        event_type="messenger_thread_loaded",
                                        fb_account_id=account.id,
                                        outcome="ok",
                                        payload={"conversation_id": conv.id, "n": stored_n},
                                    )
                                    try:
                                        schedule_messenger_ai_if_needed(
                                            db, conversation_id=conv.id
                                        )
                                    except Exception:
                                        logger.exception(
                                            "messenger AI schedule after thread load conv=%s",
                                            conv.id,
                                        )
                                    batch_any_ok = True
                                else:
                                    open_messages_inbox(page)
                                    wait_until_messenger_unblocked_or_raise(page)
                                    threads = scrape_inbox_threads_with_scroll(page)
                                    idx = messenger_person_id_index or {}
                                    recent_conversations: list[Conversation] = []
                                    n = 0
                                    for t in threads:
                                        tid = str(t.get("thread_id") or "").strip()
                                        peer_u = str(t.get("peer_url") or "").strip()
                                        if not peer_u:
                                            continue
                                        if not tid and not peer_u.startswith("http"):
                                            continue
                                        conv_row = _upsert_conversation(
                                            db,
                                            fb_account_id=account.id,
                                            thread_id=tid if tid.isdigit() else "",
                                            peer_url=peer_u,
                                            peer_name=str(t.get("peer_name") or ""),
                                            snippet=str(t.get("snippet") or ""),
                                            id_index=idx,
                                        )
                                        if len(recent_conversations) < INBOX_RECENT_THREAD_PULL_LIMIT:
                                            recent_conversations.append(conv_row)
                                        n += 1
                                    relink_conversations_missing_person(db, idx)
                                    db.commit()
                                    if inbox_autosync and inbox_autosync_light:
                                        refreshed_threads = 0
                                    else:
                                        ai_unread = (
                                            db.query(Conversation)
                                            .filter(
                                                Conversation.fb_account_id == account.id,
                                                Conversation.ai_assistant_enabled.is_(True),
                                                or_(
                                                    Conversation.unread_count > 0,
                                                    Conversation.local_unread.is_(True),
                                                ),
                                                Conversation.peer_url.isnot(None),
                                            )
                                            .order_by(
                                                Conversation.last_at.desc().nullslast(),
                                                Conversation.id.desc(),
                                            )
                                            .limit(INBOX_AI_UNREAD_SCAN_LIMIT)
                                            .all()
                                        )
                                        merged: list[Conversation] = []
                                        seen_ids: set[int] = set()
                                        for c in ai_unread:
                                            if c.id not in seen_ids and (c.peer_url or "").strip():
                                                seen_ids.add(c.id)
                                                merged.append(c)
                                        for c in recent_conversations:
                                            if c.id not in seen_ids and (c.peer_url or "").strip():
                                                seen_ids.add(c.id)
                                                merged.append(c)
                                        merged = merged[:MAX_INBOX_THREAD_REFRESH]
                                        refreshed_threads = _refresh_recent_inbox_threads(
                                            db,
                                            page=page,
                                            job_id=job_id,
                                            account=account,
                                            conversations=merged,
                                            max_threads=len(merged),
                                        )
                                    log_job_event(
                                        db,
                                        job_id=job_id,
                                        event_type="messenger_inbox_synced",
                                        fb_account_id=account.id,
                                        outcome="ok",
                                        payload={
                                            "threads": n,
                                            "thread_messages_refreshed": refreshed_threads,
                                            "inbox_autosync_light": bool(
                                                inbox_autosync and inbox_autosync_light
                                            ),
                                        },
                                    )
                                    batch_any_ok = True
                    except MessengerUIBlockedError as e:
                        logger.warning(
                            "messenger sync blocked (E2EE/UI) account %s: %s", account.id, e
                        )
                        last_sync_error = str(e)[:2000]
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="messenger_sync_blocked",
                            severity="warning",
                            fb_account_id=account.id,
                            outcome="fail",
                            payload={"error": str(e)[:400]},
                        )
                        db.commit()
                    except ProxyTunnelBlockedError as e:
                        logger.warning(
                            "messenger sync proxy blocked account %s: %s", account.id, e
                        )
                        last_sync_error = str(e)[:900]
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="messenger_sync_proxy_blocked",
                            severity="warning",
                            fb_account_id=account.id,
                            outcome="fail",
                            payload={"error": str(e)[:400]},
                        )
                        db.commit()
                    except Exception as e:
                        logger.exception("messenger sync account %s", account.id)
                        last_sync_error = str(e)[:900]
                        log_job_event(
                            db,
                            job_id=job_id,
                            event_type="messenger_sync_error",
                            severity="error",
                            fb_account_id=account.id,
                            outcome="fail",
                            payload={"error": str(e)[:400]},
                        )
                        db.commit()
            if batch_any_ok:
                finish_job(db, job, status="success")
            else:
                finish_job(
                    db,
                    job,
                    status="failed",
                    error=last_sync_error
                    or (
                        "Ни один аккаунт не синхронизирован. Если в Messenger открыто окно PIN "
                        "или восстановления зашифрованных чатов — введите PIN в обычном Chrome с тем же аккаунтом, "
                        "затем повторите синхронизацию. Автообновление — один фоновый цикл по расписанию."
                    ),
                )
            db.commit()
        finally:
            db.close()
    finally:
        with _messenger_lock:
            if _messenger_active_job_id == job_id:
                _messenger_active_job_id = None
        try:
            from backend.services.messenger_send_queue import kick_messenger_send_queue_after_idle

            kick_messenger_send_queue_after_idle()
        except Exception:
            logger.exception("kick messenger send queue after job")


def spawn_messenger_sync_worker(job_id: int) -> None:
    from backend.services.automation_switch import playwright_workers_enabled

    if not playwright_workers_enabled():
        logger.warning("spawn_messenger_sync_worker пропущен (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
        return
    t = threading.Thread(
        target=process_messenger_sync_job,
        args=(job_id,),
        name=f"messenger-sync-{job_id}",
        daemon=True,
    )
    t.start()
    logger.info("Spawned messenger sync job %s", job_id)
