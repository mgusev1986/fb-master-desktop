"""Раздел «Прогрев аккаунтов»: многодневный мягкий автопрогрев FB-профиля."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.database import get_db
from backend.models import FBAccount
from backend.services.account_activity_guard import account_automation_conflict_message
from backend.services.fb_account_slots import account_in_active_slot, fb_accounts_for_jobs
from backend.services.natural_warmup_presets import WARMUP_PRESETS_UI, WARMUP_PRESETS_UI_CLIENT
from backend.services.natural_warmup_scheduler import try_natural_warmup_scheduler_tick
from backend.services.natural_warmup_stopwords import (
    get_effective_topic_stopwords,
    parse_stopwords_form_text,
    save_topic_stopwords,
)
from backend.services.platform_owner import is_platform_owner_user
from backend.services.preview_as_user import preview_as_user_active
from backend.services.tenancy import require_org_id

router = APIRouter(prefix="/account-warming", tags=["account_natural_warmup"])

_READINESS_LABELS = {
    "cold": "Не прогрет",
    "warming": "Идёт прогрев",
    "ready": "Готов к работе",
}


def _warmup_stopwords_editable(request: Request) -> bool:
    """Совпадает с логикой cabinet_client_mode: клиентский кабинет не меняет стоп-слова."""
    if app_config.fb_master_force_cabinet_client_ui():
        return False
    try:
        sess_user = request.session.get("user")
    except Exception:
        sess_user = None
    if sess_user and not is_platform_owner_user(sess_user):
        return False
    return not (app_config.fb_master_hide_infra_settings() or preview_as_user_active(request))


def _redirect(
    msg: str | None = None,
    *,
    err: str | None = None,
    why: str | None = None,
) -> RedirectResponse:
    q: list[str] = []
    if msg:
        q.append("msg=" + quote(msg[:500]))
    if err:
        q.append("err=" + quote(err[:200]))
    if why:
        q.append("why=" + quote(why[:500]))
    suf = ("?" + "&".join(q)) if q else ""
    return RedirectResponse(f"/account-warming{suf}", status_code=303)


@router.get("")
async def account_warming_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(FBAccount.organization_id == org_id)
        .filter(FBAccount.natural_warmup_page_hidden.is_(False))
        .order_by(FBAccount.active_slot.asc().nullslast(), FBAccount.label)
        .all()
    )
    hidden_warmup_accounts = (
        db.query(FBAccount)
        .filter(FBAccount.organization_id == org_id)
        .filter(FBAccount.natural_warmup_page_hidden.is_(True))
        .order_by(FBAccount.label)
        .all()
    )
    slotted = {a.id for a in fb_accounts_for_jobs(db, org_id)}
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "account_natural_warmup/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "account_natural_warmup",
            "accounts": accounts,
            "slotted_ids": slotted,
            "readiness_labels": _READINESS_LABELS,
            "warmup_presets_ui": WARMUP_PRESETS_UI,
            "warmup_presets_ui_client": WARMUP_PRESETS_UI_CLIENT,
            "topic_stopwords_text": "\n".join(get_effective_topic_stopwords(db)),
            "warmup_stopwords_editable": _warmup_stopwords_editable(request),
            "hidden_warmup_accounts": hidden_warmup_accounts,
        },
    )


@router.post("/topic-stopwords")
async def account_warming_topic_stopwords_save(request: Request, db: Session = Depends(get_db)):
    require_org_id(request, db)
    if not _warmup_stopwords_editable(request):
        return _redirect(err="stopwords_forbidden")
    form = await request.form()
    raw = form.get("topic_stopwords") or ""
    words = parse_stopwords_form_text(str(raw))
    save_topic_stopwords(db, words)
    return _redirect(msg="Стоп-слова для нейрокомментариев сохранены в базе.")


@router.post("/start")
async def account_warming_start(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    days_raw = (form.get("duration_days") or "3").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    try:
        days = int(days_raw)
    except ValueError:
        days = 3
    days = max(1, min(days, 7))
    aid = int(raw)
    acc = (
        db.query(FBAccount)
        .filter(FBAccount.id == aid, FBAccount.organization_id == org_id)
        .first()
    )
    if not acc:
        return _redirect(err="bad_account")
    if not account_in_active_slot(acc):
        return _redirect(err="no_slot")
    if acc.readiness_status == "warming":
        return _redirect(err="already")
    busy_msg = account_automation_conflict_message(db, [aid])
    if busy_msg:
        return _redirect(err="account_busy", why=busy_msg)
    now = datetime.now(timezone.utc)
    acc.readiness_status = "warming"
    acc.natural_warmup_json = {
        "duration_days": days,
        "started_at": now.isoformat(),
        "ends_at": (now + timedelta(days=days)).isoformat(),
        "last_tick_at": None,
        "ticks_total": 0,
        "day_key": "",
        "ticks_calendar_day": 0,
        "comments_today": 0,
        "comments_day_key": "",
        "last_comment_at": None,
        "friends_today": 0,
        "friends_day_key": "",
        "last_friend_at": None,
        "activity_totals": {},
    }
    db.commit()

    def _kick_scheduler() -> None:
        try:
            try_natural_warmup_scheduler_tick()
        except Exception:
            pass

    threading.Thread(target=_kick_scheduler, daemon=True).start()
    return _redirect(msg="Прогрев запущен. Сессии пойдут автоматически каждые 2–3 часа в дневное время (8:00–22:00).")


@router.post("/stop")
async def account_warming_stop(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    acc = (
        db.query(FBAccount)
        .filter(FBAccount.id == int(raw), FBAccount.organization_id == org_id)
        .first()
    )
    if not acc:
        return _redirect(err="bad_account")
    acc.readiness_status = "cold"
    acc.natural_warmup_json = None
    db.commit()
    return _redirect(msg="Прогрев остановлен. Статус: «Не прогрет».")


@router.post("/hide-from-page")
async def account_warming_hide_from_page(request: Request, db: Session = Depends(get_db)):
    """Убрать карточку со страницы прогрева: остановить план и скрыть (аккаунт в «Аккаунтах» не удаляется)."""
    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    acc = (
        db.query(FBAccount)
        .filter(FBAccount.id == int(raw), FBAccount.organization_id == org_id)
        .first()
    )
    if not acc:
        return _redirect(err="bad_account")
    acc.natural_warmup_page_hidden = True
    acc.readiness_status = "cold"
    acc.natural_warmup_json = None
    db.commit()
    return _redirect(
        msg="Карточка скрыта, автопрогрев остановлен. Вернуть в список — блок «Скрытые аккаунты» внизу страницы."
    )


@router.post("/show-on-page")
async def account_warming_show_on_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    acc = (
        db.query(FBAccount)
        .filter(FBAccount.id == int(raw), FBAccount.organization_id == org_id)
        .first()
    )
    if not acc:
        return _redirect(err="bad_account")
    acc.natural_warmup_page_hidden = False
    db.commit()
    return _redirect(msg="Аккаунт снова показывается в списке прогрева.")


@router.post("/mark-ready")
async def account_warming_mark_ready(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    form = await request.form()
    raw = (form.get("fb_account_id") or "").strip()
    if not raw.isdigit():
        return _redirect(err="bad_account")
    acc = (
        db.query(FBAccount)
        .filter(FBAccount.id == int(raw), FBAccount.organization_id == org_id)
        .first()
    )
    if not acc:
        return _redirect(err="bad_account")
    acc.readiness_status = "ready"
    acc.natural_warmup_json = None
    db.commit()
    return _redirect(msg="Отмечено как «Готов к работе».")
