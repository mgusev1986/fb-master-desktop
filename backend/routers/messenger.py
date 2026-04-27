"""Мессенджер: список диалогов с аккаунтов, синхронизация с Facebook."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Conversation, FBAccount, Job, Message, MessengerSendQueue, Person
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.job_logging import create_job
from backend.services.messenger_send_queue import (
    enqueue_messenger_send,
    kick_messenger_send_queue_after_idle,
)
from backend.services.cabinet_settings import effective_messenger_sync_display_name_from_messenger
from backend.services.messenger_person_link import (
    apply_messenger_crm_stage_to_conversation,
    ensure_conversation_person,
)
from backend.services.messenger_settings import (
    MESSENGER_AWAY_INTERVAL_FLOOR_SEC,
    MESSENGER_FOCUS_STALE_SEC,
    MIN_INBOX_AUTOSYNC_INTERVAL_SEC,
    get_messenger_auto_inbox_sync_enabled,
    get_messenger_auto_inbox_sync_interval_sec,
    get_messenger_auto_pull_enabled,
    get_messenger_effective_autosync_interval_sec,
    is_messenger_page_focus_recent,
    normalize_messenger_auto_sync_flags,
    set_messenger_auto_sync_enabled,
    touch_messenger_focus_ping,
)
from backend.services.messenger_scrape import is_messenger_ui_blocked_message
from backend.services.crm_stages_registry import (
    get_ordered_stages,
    icon_class_for_crm_stage_slug,
    workflow_label_for_stage,
)
from backend.services.ai_agent_service import cabinet_llm_ready_for_workspace
from backend.services.messenger_ai_settings import load_messenger_ai_settings, save_messenger_ai_settings
from backend.services.messenger_ai_worker import messenger_ai_worker_busy
from backend.services.messenger_worker import JOB_TYPE, messenger_worker_busy, spawn_messenger_sync_worker
from backend.services.tenancy import require_org_id

def build_messenger_router(url_prefix: str, page_id: str) -> APIRouter:
    router = APIRouter(prefix=url_prefix, tags=["messenger"])

    def _mr(qs: str = "") -> str:
        return url_prefix + qs


    _MADRID_TZ = ZoneInfo("Europe/Madrid")


    _MONTHS_VK = (
        "янв",
        "фев",
        "мар",
        "апр",
        "мая",
        "июн",
        "июл",
        "авг",
        "сен",
        "окт",
        "ноя",
        "дек",
    )


    def _normalize_dt_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


    def _messenger_msg_max_subquery(db: Session):
        return (
            db.query(
                Message.conversation_id.label("_mcid"),
                func.max(Message.created_at).label("_mmsg"),
            )
            .group_by(Message.conversation_id)
            .subquery()
        )


    def _messenger_activity_order_columns(msg_max_subq):
        """Самая поздняя активность: max(conversation.last_at, последнее сообщение в threads)."""
        _la = Conversation.last_at
        _ma = msg_max_subq.c._mmsg
        _activity = case(
            (_la.is_(None), _ma),
            (_ma.is_(None), _la),
            (_la > _ma, _la),
            else_=_ma,
        )
        return _activity


    def _latest_message_times_for_conversations(
        db: Session, conv_ids: list[int]
    ) -> dict[int, datetime]:
        if not conv_ids:
            return {}
        rows = (
            db.query(Message.conversation_id, func.max(Message.created_at))
            .filter(Message.conversation_id.in_(conv_ids))
            .group_by(Message.conversation_id)
            .all()
        )
        return {int(cid): mx for cid, mx in rows if mx is not None}


    def _effective_messenger_activity_at(
        c: Conversation, latest_msg_by_conv: dict[int, datetime]
    ) -> datetime | None:
        la = _normalize_dt_utc(c.last_at)
        ma = _normalize_dt_utc(latest_msg_by_conv.get(c.id))
        if la is None:
            return ma
        if ma is None:
            return la
        return la if la >= ma else ma


    def _fmt_vk_time(dt: datetime | None) -> str:
        """
        Короткая метка времени как в VK: сейчас / Nм / Nч / вчера / Nд / Nн / «3 фев» / «29 дек 2023».
        Время в Europe/Madrid (как в остальном кабинете).
        """
        if not dt:
            return ""
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(_MADRID_TZ)
            now = datetime.now(timezone.utc).astimezone(_MADRID_TZ)
            if local > now:
                return local.strftime("%H:%M")

            delta = now - local
            total_sec = int(delta.total_seconds())
            if total_sec < 60:
                return "сейчас"
            if total_sec < 3600:
                m = max(1, total_sec // 60)
                return f"{m}м"
            if local.date() == now.date():
                h = max(1, total_sec // 3600)
                return f"{h}ч"

            days = (now.date() - local.date()).days
            if days == 1:
                return "вчера"
            if days < 7:
                return f"{days}д"
            if days < 30:
                w = max(1, days // 7)
                return f"{w}н"
            if local.year == now.year:
                return f"{local.day} {_MONTHS_VK[local.month - 1]}"
            return f"{local.day} {_MONTHS_VK[local.month - 1]} {local.year}"
        except Exception:
            return dt.strftime("%d.%m %H:%M") if dt else ""


    def _format_msg_ts(dt: datetime | None) -> str:
        if not dt:
            return "—"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_MADRID_TZ).strftime("%d.%m.%Y %H:%M")


    def _last_thread_messenger_sync_ended_at(
        db: Session, conversation_id: int, org_id: int
    ) -> datetime | None:
        """Время завершения последней успешной задачи pull/send по этому чату."""
        cid = int(conversation_id)
        q = (
            db.query(Job)
            .filter(
                Job.job_type == JOB_TYPE,
                Job.status == "success",
                Job.ended_at.isnot(None),
                Job.organization_id == org_id,
            )
            .order_by(Job.ended_at.desc())
            .limit(150)
        )
        best: datetime | None = None
        for j in q.all():
            snap = j.config_snapshot if isinstance(j.config_snapshot, dict) else {}
            load_raw = snap.get("load_thread_conversation_id")
            if load_raw is not None and str(load_raw).strip().isdigit():
                if int(load_raw) == cid:
                    if best is None or j.ended_at > best:
                        best = j.ended_at
            sm = snap.get("send_message")
            if isinstance(sm, dict):
                sc = sm.get("conversation_id")
                if sc is not None and str(sc).strip().isdigit() and int(sc) == cid:
                    if best is None or j.ended_at > best:
                        best = j.ended_at
        return best


    def _last_thread_messenger_terminal_job(
        db: Session, conversation_id: int, org_id: int
    ) -> Job | None:
        """Последняя завершённая задача мессенджера, привязанная к этому чату (pull или send)."""
        cid = int(conversation_id)
        q = (
            db.query(Job)
            .filter(
                Job.job_type == JOB_TYPE,
                Job.ended_at.isnot(None),
                Job.status.in_(("success", "failed", "cancelled")),
                Job.organization_id == org_id,
            )
            .order_by(Job.ended_at.desc())
            .limit(120)
        )
        for j in q.all():
            snap = j.config_snapshot if isinstance(j.config_snapshot, dict) else {}
            load_raw = snap.get("load_thread_conversation_id")
            if load_raw is not None and str(load_raw).strip().isdigit():
                if int(load_raw) == cid:
                    return j
            sm = snap.get("send_message")
            if isinstance(sm, dict):
                sc = sm.get("conversation_id")
                if sc is not None and str(sc).strip().isdigit() and int(sc) == cid:
                    return j
        return None


    def _is_full_inbox_sync_snapshot(snap: dict | None) -> bool:
        if not isinstance(snap, dict):
            return True
        if snap.get("load_thread_conversation_id") is not None:
            return False
        if isinstance(snap.get("send_message"), dict):
            return False
        return True


    def _last_inbox_messenger_sync_ended_at(db: Session, org_id: int) -> datetime | None:
        q = (
            db.query(Job)
            .filter(
                Job.job_type == JOB_TYPE,
                Job.status == "success",
                Job.ended_at.isnot(None),
                Job.organization_id == org_id,
            )
            .order_by(Job.ended_at.desc())
            .limit(120)
        )
        for j in q.all():
            snap = j.config_snapshot if isinstance(j.config_snapshot, dict) else {}
            if _is_full_inbox_sync_snapshot(snap):
                return j.ended_at
        return None


    def _conv_notif_muted(c: Conversation, now: datetime) -> bool:
        if getattr(c, "notif_muted_forever", False):
            return True
        u = getattr(c, "notif_muted_until", None)
        if not u:
            return False
        if u.tzinfo is None:
            u = u.replace(tzinfo=timezone.utc)
        return u > now


    def _unread_badge_n(c: Conversation) -> int:
        n = int(c.unread_count or 0)
        if n > 0:
            return n
        if getattr(c, "local_unread", False):
            return 1
        return 0


    def _messenger_list_params(
        *,
        q: str,
        account_id: str,
        folder: str,
        thread: str,
    ) -> dict[str, str]:
        p: dict[str, str] = {}
        if (q or "").strip():
            p["q"] = q.strip()
        if (account_id or "").strip():
            p["account_id"] = str(account_id).strip()
        if folder and folder != "all":
            p["folder"] = folder
        if (thread or "").strip().isdigit():
            p["thread"] = str(int(thread))
        return p


    def _messenger_nav_qs(
        *,
        q: str,
        account_id: str,
        folder: str,
        thread: str = "",
    ) -> str:
        p = _messenger_list_params(q=q, account_id=account_id, folder=folder, thread=thread)
        return "?" + urlencode(p) if p else ""


    def _person_messenger_search_query(person: Person | None) -> str:
        if person is None:
            return ""
        display = (person.display_name or "").strip()
        if display:
            return display
        canonical = (person.canonical_url or "").strip()
        if canonical:
            return canonical.replace("https://www.facebook.com/", "").strip("/")
        return str(person.id)


    @router.get("")
    async def messenger_index(
        request: Request,
        db: Session = Depends(get_db),
        q: str = "",
        account_id: str = "",
        folder: str = "all",
        thread: str = "",
    ):
        folder = (folder or "all").strip().lower()
        if folder not in ("all", "unread", "archive"):
            folder = "all"

        org_id = require_org_id(request, db)
        normalize_messenger_auto_sync_flags(db)

        thread_id: int | None = None
        if (thread or "").strip().isdigit():
            thread_id = int(thread.strip())

        msg_max_subq = _messenger_msg_max_subquery(db)
        _activity = _messenger_activity_order_columns(msg_max_subq)

        qry = (
            db.query(Conversation)
            .join(FBAccount, Conversation.fb_account_id == FBAccount.id)
            .outerjoin(msg_max_subq, msg_max_subq.c._mcid == Conversation.id)
            .filter(
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
        )
        if folder == "archive":
            qry = qry.filter(Conversation.is_archived.is_(True))
        else:
            qry = qry.filter(Conversation.is_archived.is_(False))
            if folder == "unread":
                qry = qry.filter(
                    or_(Conversation.unread_count > 0, Conversation.local_unread.is_(True))
                )

        if q.strip():
            like = f"%{q.strip()}%"
            qry = qry.filter(
                (Conversation.peer_name.ilike(like)) | (Conversation.last_snippet.ilike(like))
            )
        if account_id.strip().isdigit():
            qry = qry.filter(Conversation.fb_account_id == int(account_id))

        qry = qry.order_by(
            Conversation.is_pinned.desc(),
            _activity.desc().nullslast(),
            Conversation.id.desc(),
        )

        convs = qry.limit(400).all()
        acc_ids = list({c.fb_account_id for c in convs})
        if acc_ids:
            labels = {
                r.id: r.label
                for r in db.query(FBAccount).filter(FBAccount.id.in_(acc_ids)).all()
            }
        else:
            labels = {}
        rows = [(c, labels.get(c.fb_account_id, "?")) for c in convs]
        pids = list({c.person_id for c, _ in rows if c.person_id})
        people = (
            {
                p.id: p
                for p in db.query(Person)
                .filter(Person.id.in_(pids), Person.organization_id == org_id)
                .all()
            }
            if pids
            else {}
        )
        accounts = (
            db.query(FBAccount)
            .filter(
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .order_by(FBAccount.active_slot.asc(), FBAccount.label)
            .all()
        )

        now_utc = datetime.now(timezone.utc)
        latest_msg_at = _latest_message_times_for_conversations(db, [c.id for c, _ in rows])
        vk_times = {
            c.id: _fmt_vk_time(_effective_messenger_activity_at(c, latest_msg_at))
            for c, _ in rows
        }
        chat_rows = [
            {
                "c": c,
                "acc_label": lbl,
                "time_s": vk_times[c.id],
                "time_title": _format_msg_ts(
                    _effective_messenger_activity_at(c, latest_msg_at)
                ),
                "unread_n": _unread_badge_n(c),
                "notif_muted": _conv_notif_muted(c, now_utc),
            }
            for c, lbl in rows
        ]

        active_conv = None
        active_account = None
        active_person = None
        active_notif_muted = False
        messages: list[Message] = []
        if thread_id is not None:
            active_conv = db.get(Conversation, thread_id)
            if active_conv:
                active_account = (
                    db.query(FBAccount)
                    .filter(
                        FBAccount.id == active_conv.fb_account_id,
                        FBAccount.organization_id == org_id,
                    )
                    .first()
                )
                if not active_account or not account_in_active_slot(active_account):
                    active_conv = None
                    thread_id = None
            if active_conv:
                active_notif_muted = _conv_notif_muted(active_conv, now_utc)
                if active_conv.person_id:
                    active_person = (
                        db.query(Person)
                        .filter(
                            Person.id == active_conv.person_id,
                            Person.organization_id == org_id,
                        )
                        .first()
                    )
                messages = list(reversed(
                    db.query(Message)
                    .filter(Message.conversation_id == active_conv.id)
                    .order_by(Message.id.desc())
                    .limit(200)
                    .all()
                ))
                if active_conv.local_unread:
                    active_conv.local_unread = False
                    db.commit()
            else:
                thread_id = None

        last_job = (
            db.query(Job)
            .filter(Job.job_type == JOB_TYPE, Job.organization_id == org_id)
            .order_by(Job.id.desc())
            .first()
        )

        nav_qs = {
            "all": _messenger_nav_qs(q=q, account_id=account_id, folder="all"),
            "unread": _messenger_nav_qs(q=q, account_id=account_id, folder="unread"),
            "archive": _messenger_nav_qs(q=q, account_id=account_id, folder="archive"),
        }
        all_accounts_qs = _messenger_nav_qs(q=q, account_id="", folder=folder)
        account_dock = [
            (a, _messenger_nav_qs(q=q, account_id=str(a.id), folder=folder)) for a in accounts
        ]

        def _thread_qs(tid: int) -> str:
            return _messenger_nav_qs(q=q, account_id=account_id, folder=folder, thread=str(tid))

        # После normalize флаги совпадают — один источник для UI.
        messenger_auto_sync_on = get_messenger_auto_pull_enabled(db)
        messenger_auto_inbox_sync_interval_sec = get_messenger_auto_inbox_sync_interval_sec(db)
        messenger_effective_autosync_interval_sec = get_messenger_effective_autosync_interval_sec(
            db, org_id
        )
        last_sync_ended: datetime | None = None
        last_inbox_sync_ended = _last_inbox_messenger_sync_ended_at(db, org_id)
        messenger_pause_auto_pull = False
        messenger_thread_error_hint = ""
        if thread_id is not None:
            last_sync_ended = _last_thread_messenger_sync_ended_at(db, thread_id, org_id)
            if active_conv is not None:
                lj = _last_thread_messenger_terminal_job(db, thread_id, org_id)
                if (
                    lj is not None
                    and lj.status == "failed"
                    and is_messenger_ui_blocked_message(lj.error_summary)
                ):
                    messenger_pause_auto_pull = True
                    messenger_thread_error_hint = (lj.error_summary or "")[:900]
        last_sync_label = _format_msg_ts(last_sync_ended) if last_sync_ended else ""
        last_inbox_sync_label = _format_msg_ts(last_inbox_sync_ended) if last_inbox_sync_ended else ""
        last_inbox_sync_iso = ""
        if last_inbox_sync_ended is not None:
            ended = last_inbox_sync_ended
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            last_inbox_sync_iso = ended.isoformat()

        crm_stages_messenger = [
            {
                "slug": s.slug,
                "label": workflow_label_for_stage(s.slug, s.label),
                "icon": icon_class_for_crm_stage_slug(s.slug),
            }
            for s in get_ordered_stages(db)
        ]

        templates = request.app.state.templates
        return templates.TemplateResponse(
            "messenger/index.html",
            {
                "request": request,
                "user": request.session.get("user"),
                "page_id": page_id,
                "messenger_url_base": url_prefix,
                "rows": rows,
                "chat_rows": chat_rows,
                "people": people,
                "accounts": accounts,
                "search": q,
                "filter_account_id": account_id,
                "folder": folder,
                "thread_id": thread_id,
                "active_conv": active_conv,
                "active_account": active_account,
                "active_person": active_person,
                "active_notif_muted": active_notif_muted,
                "messages": messages,
                "vk_times": vk_times,
                "nav_qs": nav_qs,
                "all_accounts_qs": all_accounts_qs,
                "account_dock": account_dock,
                "thread_qs": _thread_qs,
                "sync_busy": messenger_worker_busy(),
                "last_sync_job": last_job,
                "messenger_last_sync_label": last_sync_label,
                "messenger_pause_auto_pull": messenger_pause_auto_pull,
                "messenger_thread_error_hint": messenger_thread_error_hint,
                "messenger_auto_sync_on": messenger_auto_sync_on,
                "messenger_auto_inbox_sync_interval_sec": messenger_auto_inbox_sync_interval_sec,
                "messenger_effective_autosync_interval_sec": messenger_effective_autosync_interval_sec,
                "messenger_inbox_last_sync_label": last_inbox_sync_label,
                "messenger_inbox_last_sync_iso": last_inbox_sync_iso,
                "messenger_away_sync_floor_sec": MESSENGER_AWAY_INTERVAL_FLOOR_SEC,
                "messenger_focus_stale_sec": MESSENGER_FOCUS_STALE_SEC,
                "messenger_inbox_autosync_min_sec": MIN_INBOX_AUTOSYNC_INTERVAL_SEC,
                "crm_stages_messenger": crm_stages_messenger,
                "messenger_ai_llm_ready": cabinet_llm_ready_for_workspace(db),
                "messenger_ai_worker_busy": messenger_ai_worker_busy(),
            },
        )


    @router.get("/open-person/{person_id:int}")
    async def messenger_open_person(
        request: Request, person_id: int, db: Session = Depends(get_db)
    ):
        org_id = require_org_id(request, db)
        person = (
            db.query(Person)
            .filter(Person.id == person_id, Person.organization_id == org_id)
            .first()
        )
        if not person:
            return RedirectResponse(_mr(), status_code=303)

        msg_max_subq = _messenger_msg_max_subquery(db)
        _activity = _messenger_activity_order_columns(msg_max_subq)
        conv = (
            db.query(Conversation)
            .join(FBAccount, Conversation.fb_account_id == FBAccount.id)
            .outerjoin(msg_max_subq, msg_max_subq.c._mcid == Conversation.id)
            .filter(
                Conversation.person_id == person.id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .order_by(
                _activity.desc().nullslast(),
                Conversation.id.desc(),
            )
            .first()
        )
        if conv:
            params: dict[str, str] = {"thread": str(conv.id)}
            if getattr(conv, "is_archived", False):
                params["folder"] = "archive"
            return RedirectResponse(_mr("?" + urlencode(params)), status_code=303)

        q = _person_messenger_search_query(person)
        if q:
            return RedirectResponse(_mr("?" + urlencode({"q": q})), status_code=303)
        return RedirectResponse(_mr(), status_code=303)


    @router.post("/sync")
    async def messenger_sync(request: Request, db: Session = Depends(get_db)):
        if messenger_worker_busy():
            return RedirectResponse(_mr("?err=busy"), status_code=303)

        form = await request.form()
        sel = [int(x) for x in form.getlist("fb_account_id") if str(x).strip().isdigit()]
        org_id = require_org_id(request, db)
        if sel:
            n_ok = (
                db.query(FBAccount.id)
                .filter(
                    FBAccount.id.in_(sel),
                    FBAccount.organization_id == org_id,
                    FBAccount.active_slot.isnot(None),
                )
                .count()
            )
            if n_ok != len(set(sel)):
                return RedirectResponse(_mr("?err=form"), status_code=303)
        admin_id = (request.session.get("user") or {}).get("id")
        snap: dict = {}
        if sel:
            snap["account_ids"] = sel
        job = create_job(
            db,
            organization_id=org_id,
            job_type=JOB_TYPE,
            config_snapshot=snap,
            admin_id=admin_id,
        )
        db.commit()
        spawn_messenger_sync_worker(job.id)
        return RedirectResponse(_mr("?sync_started=1"), status_code=303)


    @router.get("/thread/{conversation_id:int}")
    async def messenger_thread_legacy(request: Request, conversation_id: int, db: Session = Depends(get_db)):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return RedirectResponse(_mr(), status_code=303)
        acc = (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        )
        if not acc:
            return RedirectResponse(_mr(), status_code=303)
        qp = dict(request.query_params)
        qp["thread"] = str(conversation_id)
        return RedirectResponse(_mr("?" + urlencode(qp)), status_code=303)


    @router.post("/thread/{conversation_id:int}/load-messages")
    async def messenger_load_messages(
        request: Request,
        conversation_id: int,
        db: Session = Depends(get_db),
    ):
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return RedirectResponse(_mr(), status_code=303)
        org_id = require_org_id(request, db)
        acc = db.get(FBAccount, conv.fb_account_id)
        if not acc or acc.organization_id != org_id or not account_in_active_slot(acc):
            return RedirectResponse(_mr(), status_code=303)
        if messenger_worker_busy():
            return RedirectResponse(_mr(f"?thread={conversation_id}&err=busy"), status_code=303)

        admin_id = (request.session.get("user") or {}).get("id")
        job = create_job(
            db,
            organization_id=org_id,
            job_type=JOB_TYPE,
            config_snapshot={"load_thread_conversation_id": conv.id},
            admin_id=admin_id,
        )
        db.commit()
        spawn_messenger_sync_worker(job.id)
        return RedirectResponse(_mr(f"?thread={conversation_id}&load_started=1"), status_code=303)


    @router.post("/conversation/{conversation_id:int}/action")
    async def messenger_conversation_action(
        request: Request,
        conversation_id: int,
        db: Session = Depends(get_db),
        action: str = Form(""),
        ret_q: str = Form(""),
        ret_account_id: str = Form(""),
        ret_folder: str = Form("all"),
        ret_thread: str = Form(""),
    ):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return RedirectResponse(_mr(), status_code=303)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return RedirectResponse(_mr(), status_code=303)
        act = (action or "").strip().lower()
        now = datetime.now(timezone.utc)
        if act == "mark_unread":
            conv.local_unread = True
        elif act == "mark_read":
            conv.local_unread = False
        elif act == "pin_toggle":
            conv.is_pinned = not bool(conv.is_pinned)
        elif act == "mute_off":
            conv.notif_muted_forever = False
            conv.notif_muted_until = None
        elif act == "mute_1h":
            conv.notif_muted_forever = False
            conv.notif_muted_until = now + timedelta(hours=1)
        elif act == "mute_8h":
            conv.notif_muted_forever = False
            conv.notif_muted_until = now + timedelta(hours=8)
        elif act == "mute_forever":
            conv.notif_muted_forever = True
            conv.notif_muted_until = None
        elif act == "clear_history":
            db.query(Message).filter(Message.conversation_id == conv.id).delete(synchronize_session=False)
            conv.last_snippet = None
        else:
            return RedirectResponse(_mr(), status_code=303)
        db.commit()
        params = _messenger_list_params(
            q=ret_q,
            account_id=ret_account_id,
            folder=ret_folder or "all",
            thread=ret_thread if ret_thread.strip().isdigit() else "",
        )
        qs = "?" + urlencode(params) if params else ""
        return RedirectResponse(url_prefix + qs, status_code=303)


    @router.post("/conversation/{conversation_id:int}/archive")
    async def messenger_toggle_archive(
        request: Request,
        conversation_id: int,
        db: Session = Depends(get_db),
        ret_q: str = Form(""),
        ret_account_id: str = Form(""),
        ret_folder: str = Form("all"),
        ret_thread: str = Form(""),
    ):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return RedirectResponse(_mr(), status_code=303)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return RedirectResponse(_mr(), status_code=303)
        conv.is_archived = not bool(conv.is_archived)
        db.commit()
        params = _messenger_list_params(
            q=ret_q,
            account_id=ret_account_id,
            folder=ret_folder or "all",
            thread=ret_thread if ret_thread.strip().isdigit() else "",
        )
        if ret_thread.strip().isdigit() and int(ret_thread) == conversation_id and conv.is_archived:
            params.pop("thread", None)
        qs = "?" + urlencode(params) if params else ""
        return RedirectResponse(url_prefix + qs, status_code=303)


    class MessengerAutoPullBody(BaseModel):
        enabled: bool


    class MessengerThreadPullBody(BaseModel):
        """background=true — подтяжка по таймеру без окна Chromium (headless Playwright)."""

        background: bool = False


    class MessengerSendBody(BaseModel):
        text: str = Field(..., min_length=1, max_length=4000)


    class MessengerCRMStageBody(BaseModel):
        crm_stage: str = Field(..., min_length=1, max_length=50)


    class MessengerAISettingsBody(BaseModel):
        prompt_addon: str = ""
        materials_text: str = ""
        delay_min_sec: float | None = None
        delay_max_sec: float | None = None
        temperature: float | None = None
        max_tokens: int | None = None
        max_reply_steps: int | None = None


    class MessengerAIAssistantBody(BaseModel):
        """Поля None — не менять (частичное обновление)."""

        enabled: bool | None = None
        extra_instructions: str | None = None
        reset_transcript_fingerprint: bool = False
        reset_ai_reply_steps: bool = False


    @router.get("/api/status")
    async def messenger_api_status():
        return {
            "messenger_busy": messenger_worker_busy(),
            "messenger_ai_busy": messenger_ai_worker_busy(),
        }


    @router.get("/api/inbox-sync-meta")
    async def messenger_api_inbox_sync_meta(
        request: Request, db: Session = Depends(get_db)
    ):
        org_id = require_org_id(request, db)
        ended = _last_inbox_messenger_sync_ended_at(db, org_id)
        label = _format_msg_ts(ended) if ended else ""
        iso: str | None = None
        if ended is not None:
            e = ended
            if e.tzinfo is None:
                e = e.replace(tzinfo=timezone.utc)
            iso = e.isoformat()
        return {
            "sync_busy": messenger_worker_busy(),
            "auto_inbox_sync_enabled": get_messenger_auto_inbox_sync_enabled(db),
            "auto_inbox_sync_interval_sec": get_messenger_auto_inbox_sync_interval_sec(db),
            "effective_autosync_interval_sec": get_messenger_effective_autosync_interval_sec(
                db, org_id
            ),
            "focus_page_recent": is_messenger_page_focus_recent(db, org_id),
            "last_sync_at": iso,
            "last_sync_label": label or None,
        }


    @router.post("/api/focus-ping")
    async def messenger_api_focus_ping(request: Request, db: Session = Depends(get_db)):
        """Сигнал, что вкладка с мессенджером открыта и видна — для реже/легче фоновой синхронизации вне раздела."""
        org_id = require_org_id(request, db)
        touch_messenger_focus_ping(db, org_id)
        return {"ok": True}


    @router.get("/api/thread/{conversation_id:int}/messages")
    async def messenger_api_thread_messages(
        request: Request, conversation_id: int, db: Session = Depends(get_db)
    ):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return JSONResponse({"error": "not_found"}, status_code=404)
        msgs = list(reversed(
            db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.id.desc())
            .limit(500)
            .all()
        ))
        return {
            "conversation_id": conv.id,
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "body": m.body,
                    "time": _format_msg_ts(m.created_at),
                }
                for m in msgs
            ],
        }


    @router.get("/api/thread/{conversation_id:int}/sync-meta")
    async def messenger_api_thread_sync_meta(
        request: Request, conversation_id: int, db: Session = Depends(get_db)
    ):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return JSONResponse({"error": "not_found"}, status_code=404)
        ended = _last_thread_messenger_sync_ended_at(db, conversation_id, org_id)
        poll_sec = get_messenger_auto_inbox_sync_interval_sec(db)
        label = _format_msg_ts(ended) if ended else ""
        iso: str | None = None
        if ended:
            e = ended
            if e.tzinfo is None:
                e = e.replace(tzinfo=timezone.utc)
            iso = e.isoformat()
        last_j = _last_thread_messenger_terminal_job(db, conversation_id, org_id)
        pause_auto_pull = (
            last_j is not None
            and last_j.status == "failed"
            and is_messenger_ui_blocked_message(last_j.error_summary)
        )
        auto_pull_enabled = get_messenger_auto_pull_enabled(db)
        auto_inbox = get_messenger_auto_inbox_sync_enabled(db)
        eff = get_messenger_effective_autosync_interval_sec(db, org_id)
        focus_recent = is_messenger_page_focus_recent(db, org_id)
        return {
            "poll_interval_sec": poll_sec,
            "effective_autosync_interval_sec": eff,
            "focus_page_recent": focus_recent,
            "last_sync_at": iso,
            "last_sync_label": label or None,
            "pause_auto_pull": pause_auto_pull,
            "auto_pull_enabled": auto_pull_enabled,
            "auto_sync_master": auto_pull_enabled and auto_inbox,
        }


    @router.post("/api/settings/auto-pull")
    async def messenger_api_set_auto_pull(body: MessengerAutoPullBody, db: Session = Depends(get_db)):
        """Включает/выключает единый режим фоновой синхронизации мессенджера (список чатов + недавние треды)."""
        set_messenger_auto_sync_enabled(db, body.enabled)
        return {"ok": True, "enabled": body.enabled}


    @router.post("/api/sync-inbox")
    async def messenger_api_sync_inbox(request: Request, db: Session = Depends(get_db)):
        """Полная ручная синхронизация списка чатов (все аккаунты организации), как старая форма «Синхронизация»."""
        if messenger_worker_busy():
            return JSONResponse({"ok": False, "busy": True}, status_code=409)
        org_id = require_org_id(request, db)
        admin_id = (request.session.get("user") or {}).get("id")
        job = create_job(
            db,
            organization_id=org_id,
            job_type=JOB_TYPE,
            config_snapshot={},
            admin_id=admin_id,
        )
        db.commit()
        spawn_messenger_sync_worker(job.id)
        return {"ok": True, "job_id": job.id}


    @router.post("/api/thread/{conversation_id:int}/crm-stage")
    async def messenger_api_thread_set_crm_stage(
        request: Request,
        conversation_id: int,
        body: MessengerCRMStageBody,
        db: Session = Depends(get_db),
    ):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        payload, status = apply_messenger_crm_stage_to_conversation(
            db,
            org_id,
            conv,
            body.crm_stage,
            sync_display_name_from_messenger=effective_messenger_sync_display_name_from_messenger(
                db
            ),
        )
        if status != 200:
            return JSONResponse(payload, status_code=status)
        return payload


    @router.get("/api/job/{job_id:int}")
    async def messenger_api_job_status(
        request: Request, job_id: int, db: Session = Depends(get_db)
    ):
        org_id = require_org_id(request, db)
        j = db.get(Job, job_id)
        if not j or j.job_type != JOB_TYPE or j.organization_id != org_id:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return {"status": j.status, "error": j.error_summary}


    @router.post("/api/thread/{conversation_id:int}/pull")
    async def messenger_api_thread_pull(
        request: Request,
        conversation_id: int,
        db: Session = Depends(get_db),
        body: MessengerThreadPullBody = Body(default=MessengerThreadPullBody()),
    ):
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        org_id = require_org_id(request, db)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if messenger_worker_busy():
            return JSONResponse({"ok": False, "busy": True}, status_code=409)
        admin_id = (request.session.get("user") or {}).get("id")
        snap: dict = {"load_thread_conversation_id": conv.id}
        if body.background:
            snap["pull_background"] = True
        job = create_job(
            db,
            organization_id=org_id,
            job_type=JOB_TYPE,
            config_snapshot=snap,
            admin_id=admin_id,
        )
        spawn_messenger_sync_worker(job.id)
        return {"ok": True, "job_id": job.id}


    @router.post("/api/thread/{conversation_id:int}/send")
    async def messenger_api_thread_send(
        request: Request,
        conversation_id: int,
        body: MessengerSendBody,
        db: Session = Depends(get_db),
    ):
        conv = db.get(Conversation, conversation_id)
        if not conv or not (conv.peer_url or "").strip():
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        org_id = require_org_id(request, db)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        admin_id = (request.session.get("user") or {}).get("id")
        try:
            row = enqueue_messenger_send(
                db,
                organization_id=org_id,
                conversation_id=conv.id,
                body=body.text,
                created_by_admin_id=admin_id,
            )
        except ValueError as e:
            code = str(e)
            if code == "queue_full":
                return JSONResponse(
                    {"ok": False, "error": "queue_full", "message": "Слишком много сообщений в очереди на этот чат"},
                    status_code=429,
                )
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        qid = row.id
        kick_messenger_send_queue_after_idle()
        row = db.get(MessengerSendQueue, qid)
        if row and row.messenger_job_id:
            return {
                "ok": True,
                "queue_id": row.id,
                "job_id": row.messenger_job_id,
                "queued": False,
            }
        return {"ok": True, "queue_id": row.id, "queued": True}


    @router.get("/api/send-queue/{queue_id:int}")
    async def messenger_api_send_queue_status(
        request: Request, queue_id: int, db: Session = Depends(get_db)
    ):
        org_id = require_org_id(request, db)
        row = db.get(MessengerSendQueue, queue_id)
        if not row or row.organization_id != org_id:
            return JSONResponse({"error": "not_found"}, status_code=404)
        ahead = (
            db.query(func.count(MessengerSendQueue.id))
            .filter(
                MessengerSendQueue.status == "queued",
                MessengerSendQueue.id < row.id,
            )
            .scalar()
            or 0
        )
        return {
            "ok": True,
            "queue_id": row.id,
            "status": row.status,
            "job_id": row.messenger_job_id,
            "error_summary": row.error_summary,
            "ahead": ahead,
        }


    @router.get("/api/ai-settings")
    async def messenger_api_ai_settings_get(request: Request, db: Session = Depends(get_db)):
        require_org_id(request, db)
        s = load_messenger_ai_settings(db)
        return {
            "ok": True,
            "prompt_addon": s["prompt_addon"],
            "materials_text": "\n".join(s["materials_lines"]),
            "delay_min_sec": s["delay_min_sec"],
            "delay_max_sec": s["delay_max_sec"],
            "temperature": s["temperature"],
            "max_tokens": s["max_tokens"],
            "max_reply_steps": s["max_reply_steps"],
            "llm_ready": cabinet_llm_ready_for_workspace(db),
        }


    @router.post("/api/ai-settings")
    async def messenger_api_ai_settings_post(
        request: Request,
        body: MessengerAISettingsBody,
        db: Session = Depends(get_db),
    ):
        require_org_id(request, db)
        save_messenger_ai_settings(
            db,
            prompt_addon=body.prompt_addon,
            materials_text=body.materials_text,
            delay_min_sec=body.delay_min_sec,
            delay_max_sec=body.delay_max_sec,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            max_reply_steps=body.max_reply_steps,
        )
        db.commit()
        return {"ok": True}


    @router.get("/api/thread/{conversation_id:int}/ai-assistant")
    async def messenger_api_thread_ai_get(
        request: Request, conversation_id: int, db: Session = Depends(get_db)
    ):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        s = load_messenger_ai_settings(db)
        return {
            "ok": True,
            "enabled": bool(conv.ai_assistant_enabled),
            "extra_instructions": (conv.ai_extra_instructions or ""),
            "ai_reply_steps_used": int(conv.ai_reply_steps_used or 0),
            "ai_reply_steps_max": s["max_reply_steps"],
        }


    @router.post("/api/thread/{conversation_id:int}/ai-assistant")
    async def messenger_api_thread_ai_post(
        request: Request,
        conversation_id: int,
        body: MessengerAIAssistantBody,
        db: Session = Depends(get_db),
    ):
        org_id = require_org_id(request, db)
        conv = db.get(Conversation, conversation_id)
        if not conv:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
                FBAccount.active_slot.isnot(None),
            )
            .first()
        ):
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if body.enabled is not None:
            conv.ai_assistant_enabled = bool(body.enabled)
        if body.extra_instructions is not None:
            conv.ai_extra_instructions = (body.extra_instructions or "").strip()[:8000] or None
        if body.reset_transcript_fingerprint:
            conv.ai_last_transcript_fingerprint = None
        if body.reset_ai_reply_steps:
            conv.ai_reply_steps_used = 0
        db.commit()
        return {
            "ok": True,
            "enabled": conv.ai_assistant_enabled,
            "ai_reply_steps_used": int(conv.ai_reply_steps_used or 0),
        }


    return router


router = build_messenger_router("/messenger", "messenger")
