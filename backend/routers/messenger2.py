"""Мессенджер (маршрут /messenger2): настольное приложение и запуск Chromium с профилем FB Master."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.config import (
    AUTOSTART_MESSENGER2_FRAME,
    MESSENGER2_FRAME_HEALTH_PORT,
    effective_fb_cdp_endpoint,
)
from backend.database import get_db
from backend.models import Conversation, FBAccount, Job, Person
from backend.routers.fb_accounts import persist_fb_session_snapshot
from backend.services.ai_agent_service import cabinet_llm_ready_for_workspace
from backend.services.cabinet_settings import (
    effective_embedded_login_enabled,
    effective_messenger_sync_display_name_from_messenger,
)
from backend.services.fb_account_profile_paths import playwright_profile_dir_for_account
from backend.services.active_fb_account import (
    get_active_fb_account_id,
    resolve_active_fb_account_id,
    set_active_fb_account_id,
)
from backend.services.fb_account_slots import account_in_active_slot
from backend.services.desktop_client import user_agent_is_fb_master_desktop
from backend.services.electron_proxy import electron_partition_proxy_for_webview
from backend.services.fb_session_state import session_state_is_usable
from backend.services.fb_playwright import (
    login_window_busy,
    proxy_dict_for_playwright,
    try_start_login_window,
)
from backend.services.fb_stealth_profile import stealth_playwright_options_from_account
from backend.services.tenancy import require_org_id
from backend.services.crm_stages_registry import (
    get_ordered_stages,
    icon_class_for_crm_stage_slug,
    workflow_label_for_stage,
)
from backend.services.messenger_person_link import (
    apply_messenger_crm_stage_to_conversation,
    apply_messenger_peer_display_name_to_person,
    parse_messenger_thread_location,
)
from backend.services.messenger_deep_link import (
    is_safe_facebook_messenger_nav_url,
    resolve_facebook_messenger_thread_url_for_person,
)
from backend.services.messenger_ai_learning import dismiss_draft, list_pending_drafts, merge_draft_into_prompt_addon
from backend.services.messenger_ai_memory import rebuild_messenger_ai_memory_for_conversation
from backend.services.messenger_ai_reflect_worker import (
    JOB_TYPE as MESSENGER_REFLECT_JOB_TYPE,
    messenger_ai_reflect_worker_busy,
    schedule_messenger_reflect_analysis,
)
from backend.services.messenger_ai_settings import load_messenger_ai_settings, save_messenger_ai_settings
from backend.services.messenger_ai_worker import messenger_ai_worker_busy
from backend.services.messenger_live_activity import get_messenger_outbound_activity
from backend.services.messenger_worker import (
    lookup_conversation_person_for_messenger_webview_url,
    upsert_conversation_from_messenger_webview_url,
)
from backend.services.outreach_shared_profile import (
    clear_stale_outreach_shared_profile_locks,
    messenger_blocked_by_outreach,
)

logger = logging.getLogger(__name__)


class Messenger2DesktopCrmBody(BaseModel):
    """Текущий URL webview + стадия CRM; имя собеседника подставляется с клиента по возможности."""

    current_url: str = Field("", max_length=4096)
    crm_stage: str = Field(..., min_length=1, max_length=50)
    peer_display_name: str | None = Field(None, max_length=255)


class Messenger2ThreadUrlBody(BaseModel):
    """Текущий URL webview для подтягивания стадии CRM; опционально имя из UI для синхронизации display_name."""

    url: str = Field("", max_length=4096)
    peer_display_name: str | None = Field(None, max_length=255)


class Messenger2AISettingsBody(BaseModel):
    prompt_addon: str = ""
    materials_text: str = ""
    delay_min_sec: float | None = None
    delay_max_sec: float | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_reply_steps: int | None = None
    rag_enabled: bool | None = None


class Messenger2LearningAnalyzeBody(BaseModel):
    conversation_ids: list[int] = Field(default_factory=list, max_length=5)


class Messenger2LearningReindexBody(BaseModel):
    conversation_id: int = Field(..., ge=1)


class Messenger2ThreadAssistantBody(BaseModel):
    enabled: bool | None = None
    extra_instructions: str | None = None
    reset_transcript_fingerprint: bool = False
    reset_ai_reply_steps: bool = False


class Messenger2ThreadAiFromUrlBody(BaseModel):
    """Тот же чат, что в webview: создать/найти Conversation и обновить настройки AI."""

    url: str = Field("", max_length=4096)
    peer_display_name: str | None = Field(None, max_length=255)
    enabled: bool | None = None
    extra_instructions: str | None = None
    reset_transcript_fingerprint: bool = False
    reset_ai_reply_steps: bool = False


router = APIRouter(prefix="/messenger2", tags=["messenger2"])

FB_MESSENGER_E2EE_URL = "https://www.facebook.com/messages/"


@router.post("/set-active-account")
async def messenger2_set_active_account(
    request: Request,
    db: Session = Depends(get_db),
    account_id: int = Form(...),
):
    """Текущий аккаунт для Мессенджера (сессия): только из аккаунтов со слотом 1–3."""
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc or not account_in_active_slot(acc):
        return RedirectResponse(
            "/messenger2?"
            + urlencode(
                {"flash": "Аккаунт не найден или не в активном слоте 1–3", "flash_type": "error"}
            ),
            status_code=303,
        )
    prev = get_active_fb_account_id(request, db, org_id)
    set_active_fb_account_id(request, account_id)
    if prev == account_id:
        return RedirectResponse("/messenger2", status_code=303)
    return RedirectResponse(
        "/messenger2?"
        + urlencode({"flash": "Аккаунт для Messenger переключён", "flash_type": "ok"}),
        status_code=303,
    )


@router.get("/open-person/{person_id:int}")
async def messenger2_open_person(
    request: Request, person_id: int, db: Session = Depends(get_db)
):
    """Из воронки / сценариев: открыть встроенный Messenger на переписке с контактом."""
    org_id = require_org_id(request, db)
    person = (
        db.query(Person)
        .filter(Person.id == person_id, Person.organization_id == org_id)
        .first()
    )
    if not person:
        return RedirectResponse(
            "/messenger2?"
            + urlencode(
                {"flash": "Контакт не найден", "flash_type": "error"},
                safe="",
            ),
            status_code=303,
        )
    active_aid = get_active_fb_account_id(request, db, org_id)
    if active_aid and messenger_blocked_by_outreach(db, active_aid):
        return RedirectResponse(
            "/messenger2?"
            + urlencode(
                {
                    "flash": (
                        "Сейчас выполняется рассылка с этого аккаунта — встроенный Messenger временно недоступен. "
                        "Дождитесь окончания задачи или переключите активный аккаунт в списке «Аккаунт»."
                    )[:500],
                    "flash_type": "warning",
                },
                safe="",
            ),
            status_code=303,
        )
    thread_url, hint = resolve_facebook_messenger_thread_url_for_person(db, org_id, person)
    if thread_url:
        return RedirectResponse(
            "/messenger2?" + urlencode({"thread_url": thread_url}, safe=":/?&=%"),
            status_code=303,
        )
    msg = (
        "Диалог с этим контактом ещё не привязан к списку чатов. "
        f"Найдите в Messenger «{hint}» или синхронизируйте список диалогов "
        "(раздел «Messenger» — ручная синхронизация)."
    )
    return RedirectResponse(
        "/messenger2?"
        + urlencode({"flash": msg[:500], "flash_type": "info"}, safe=""),
        status_code=303,
    )


def _fb_account_scoped(db: Session, account_id: int, org_id: int) -> FBAccount | None:
    acc = db.get(FBAccount, account_id)
    if not acc or acc.organization_id != org_id:
        return None
    return acc


def _messenger_playwright_profile_abs(db: Session, acc: FBAccount) -> str:
    """Тот же каталог user-data, что у окна «Войти в Facebook» (Playwright persistent context)."""
    return str(playwright_profile_dir_for_account(db, acc.id, acc.profile_dir))


def _session_state_from_account(acc: FBAccount) -> dict[str, Any] | None:
    raw = acc.session_state_json
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _redirect_flash(msg: str, kind: str = "info") -> RedirectResponse:
    q = quote(msg[:500], safe="")
    return RedirectResponse(f"/messenger2?flash={q}&flash_type={kind}", status_code=303)


@router.get("/desktop-session-state")
async def messenger2_desktop_session_state(request: Request, db: Session = Depends(get_db)):
    """Свежий storage_state текущего аккаунта для Electron webview (кнопка «Обновить» в Desktop)."""
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    selected_id = resolve_active_fb_account_id(request, db, org_id, accounts)
    if selected_id is None:
        return JSONResponse({"ok": False, "error": "no_active_account"}, status_code=400)
    acc = _fb_account_scoped(db, selected_id, org_id)
    if not acc:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, "storage_state": _session_state_from_account(acc)})


@router.get("/api/outbound-activity")
async def messenger2_api_outbound_activity(
    request: Request, db: Session = Depends(get_db)
):
    """Токен новой исходящей активности для текущего активного аккаунта webview."""
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    selected_id = resolve_active_fb_account_id(request, db, org_id, accounts)
    if selected_id is None:
        return JSONResponse({"ok": False, "error": "no_active_account"}, status_code=400)
    acc = _fb_account_scoped(db, selected_id, org_id)
    if not acc:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    activity = get_messenger_outbound_activity(selected_id)
    # Таймстамп последнего сохранения cookies worker'ом (рассылка / парсер / прогрев /
    # messenger_worker). Фронт детектирует изменение, чтобы автоматически обновить
    # Electron webview свежими cookies без ручного клика «Обновить».
    session_saved_at_iso = ""
    session_saved_at_ts = 0
    if acc.session_saved_at is not None:
        try:
            session_saved_at_iso = acc.session_saved_at.isoformat()
            session_saved_at_ts = int(acc.session_saved_at.timestamp() * 1000)
        except Exception:
            session_saved_at_iso = ""
            session_saved_at_ts = 0
    return JSONResponse(
        {
            "ok": True,
            "fb_account_id": int(selected_id),
            "token": activity["token"],
            "updated_at": activity["updated_at"],
            "source": activity["source"],
            "preview": activity["preview"],
            "open_thread_url": activity.get("open_thread_url") or "",
            "session_saved_at": session_saved_at_iso,
            "session_saved_at_ms": session_saved_at_ts,
        }
    )


@router.get("/api/sync-diagnostics")
async def messenger2_api_sync_diagnostics(
    request: Request, db: Session = Depends(get_db)
):
    """Серверная диагностика режима синхронизации встроенного Messenger для текущего аккаунта."""
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    selected_id = resolve_active_fb_account_id(request, db, org_id, accounts)
    if selected_id is None:
        return JSONResponse(
            {"ok": False, "error": "no_active_account", "detail": "Нет активного аккаунта"},
            status_code=400,
        )
    acc = _fb_account_scoped(db, selected_id, org_id)
    if not acc:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    selected_storage_state = _session_state_from_account(acc) or {}
    session_usable, session_reason = session_state_is_usable(
        selected_storage_state,
        expected_login=(acc.fb_login_username or "").strip() or None,
    )
    cookie_count = len(selected_storage_state.get("cookies") or [])
    proxy = electron_partition_proxy_for_webview(acc)
    profile_abs = _messenger_playwright_profile_abs(db, acc)
    profile_exists = bool(profile_abs and Path(profile_abs).is_dir())
    proxy_enabled = bool(acc.proxy_enabled and (acc.proxy_url or "").strip())
    proxy_kind = ""
    if proxy and proxy.get("rules_url"):
        proxy_kind = str(proxy.get("rules_url") or "").split("://", 1)[0].lower()
    socks_block = bool(proxy and proxy.get("socks_with_auth"))
    m2_use_disk_profile = bool(profile_exists and not socks_block)
    activity = get_messenger_outbound_activity(selected_id)

    checks: list[dict[str, str | bool | int]] = []
    recommendations: list[str] = []

    checks.append(
        {
            "key": "session",
            "label": "Сохранённая сессия Facebook",
            "status": "ok" if cookie_count > 0 else "warn",
            "value": f"cookies: {cookie_count}",
        }
    )
    checks.append(
        {
            "key": "mode",
            "label": "Режим Messenger 2",
            "status": "ok" if m2_use_disk_profile else "warn",
            "value": "общий профиль Chromium" if m2_use_disk_profile else "изолированная Electron-сессия",
        }
    )
    checks.append(
        {
            "key": "profile_dir",
            "label": "Папка браузерного профиля",
            "status": "ok" if profile_exists else "warn",
            "value": "найдена" if profile_exists else "не найдена",
        }
    )
    checks.append(
        {
            "key": "proxy",
            "label": "Прокси",
            "status": "warn" if socks_block else "ok",
            "value": (
                "SOCKS с авторизацией — webview работает изолированно"
                if socks_block
                else (proxy_kind or ("нет" if not proxy_enabled else "включён"))
            ),
        }
    )
    checks.append(
        {
            "key": "login_window",
            "label": "Окно входа Chromium",
            "status": "warn" if login_window_busy(selected_id) else "ok",
            "value": "открыто" if login_window_busy(selected_id) else "не занято",
        }
    )

    likely_issue = ""
    if cookie_count <= 0:
        likely_issue = "Для аккаунта нет сохранённой Facebook-сессии."
        recommendations.append("Войдите в Facebook заново в разделе «Аккаунты», затем обновите Messenger.")
    elif socks_block:
        likely_issue = (
            "Этот аккаунт открыт в изолированной webview-сессии, потому что у прокси SOCKS есть авторизация. "
            "Facebook на таких сессиях может показывать историю чатов с задержкой."
        )
        recommendations.append(
            "Если нужна максимально живая синхронизация, переключите прокси аккаунта на HTTP(S) или временно отключите прокси для проверки."
        )
    elif not profile_exists:
        likely_issue = (
            "Для аккаунта не найден общий Chromium-профиль, поэтому Messenger работает в изолированной сессии."
        )
        recommendations.append(
            "Откройте вход Chromium для этого аккаунта через «Аккаунты», убедитесь, что профиль создался, затем обновите Messenger."
        )
    else:
        likely_issue = (
            "Серверная часть выглядит нормально. Если сообщения всё равно не видны, скорее всего Facebook ограничивает историю чатов для этого аккаунта или конкретного диалога."
        )
        recommendations.append(
            "Если увидите предупреждение про PIN / историю чатов в шапке Messenger, это уже ограничение Meta, а не вашей программы."
        )

    if activity.get("token"):
        recommendations.append(
            "После новой отправки из автоматизации можно нажать «Обновить», если Facebook не подтянул чат сам."
        )

    return {
        "ok": True,
        "fb_account_id": int(selected_id),
        "account_label": acc.label,
        "session_usable": bool(session_usable),
        "session_reason": str(session_reason or "").strip(),
        "session_cookie_count": int(cookie_count),
        "use_disk_profile": bool(m2_use_disk_profile),
        "profile_exists": bool(profile_exists),
        "proxy_enabled": bool(proxy_enabled),
        "proxy_kind": proxy_kind or "",
        "proxy_requires_isolated_session": bool(socks_block),
        "login_window_busy": bool(login_window_busy(selected_id)),
        "recent_outbound_activity": activity,
        "likely_issue": likely_issue,
        "checks": checks,
        "recommendations": recommendations,
    }


@router.post("/api/crm-stage")
async def messenger2_api_desktop_crm_stage(
    request: Request,
    db: Session = Depends(get_db),
    body: Messenger2DesktopCrmBody = Body(...),
):
    """
    Встроенный Messenger: по URL открытого чата в webview создать/найти Conversation и выставить стадию CRM.
    """
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    selected_id = resolve_active_fb_account_id(request, db, org_id, accounts)
    if selected_id is None:
        return JSONResponse({"ok": False, "error": "no_account"}, status_code=400)
    if not _fb_account_scoped(db, selected_id, org_id):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    conv = upsert_conversation_from_messenger_webview_url(
        db,
        organization_id=org_id,
        fb_account_id=selected_id,
        location_url=body.current_url,
        peer_display_name=body.peer_display_name,
    )
    if conv is None:
        return JSONResponse(
            {
                "ok": False,
                "error": "no_thread",
                "detail": (
                    "Откройте переписку с человеком: в адресной строке webview должен быть путь "
                    "…/messages/t/… или messenger.com/t/…"
                ),
            },
            status_code=422,
        )

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


@router.post("/api/thread-crm-context")
async def messenger2_api_thread_crm_context(
    request: Request,
    db: Session = Depends(get_db),
    body: Messenger2ThreadUrlBody = Body(...),
):
    """
    По URL открытого чата в webview вернуть стадию CRM и подписи для подсветки кнопок в шапке.
    Не создаёт Conversation — только читает существующие данные.
    """
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    selected_id = resolve_active_fb_account_id(request, db, org_id, accounts)
    if selected_id is None:
        return JSONResponse({"ok": False, "error": "no_account"}, status_code=400)
    if not _fb_account_scoped(db, selected_id, org_id):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    raw = (body.url or "").strip()
    peer_url, _tid = parse_messenger_thread_location(raw)
    has_open_thread = bool(peer_url)

    conv, person = lookup_conversation_person_for_messenger_webview_url(
        db,
        organization_id=org_id,
        fb_account_id=selected_id,
        location_url=raw,
    )

    if (
        effective_messenger_sync_display_name_from_messenger(db)
        and person is not None
        and conv is not None
    ):
        touched = apply_messenger_peer_display_name_to_person(
            db,
            person=person,
            conversation=conv,
            messenger_display_name=body.peer_display_name,
        )
        if touched:
            db.commit()
            db.refresh(person)
            db.refresh(conv)

    stage_slug = (person.crm_stage or "").strip() if person else ""
    stage_label = ""
    if stage_slug:
        for s in get_ordered_stages(db):
            if s.slug == stage_slug:
                stage_label = workflow_label_for_stage(s.slug, s.label)
                break

    ms = load_messenger_ai_settings(db)
    llm_ready = cabinet_llm_ready_for_workspace(db)

    return {
        "ok": True,
        "has_open_thread": has_open_thread,
        "has_contact": person is not None,
        "conversation_id": conv.id if conv else None,
        "person_id": person.id if person else None,
        "crm_stage": stage_slug or None,
        "stage_label": stage_label or None,
        "person_name": (person.display_name or "").strip() if person else "",
        "peer_name": (conv.peer_name or "").strip() if conv else "",
        "ai_assistant_enabled": bool(conv.ai_assistant_enabled) if conv else False,
        "ai_extra_instructions": (conv.ai_extra_instructions or "") if conv else "",
        "ai_reply_steps_used": int(conv.ai_reply_steps_used or 0) if conv else 0,
        "ai_reply_steps_max": ms["max_reply_steps"],
        "messenger_ai_llm_ready": llm_ready,
        "messenger_ai_worker_busy": messenger_ai_worker_busy(),
        "messenger_ai_reflect_worker_busy": messenger_ai_reflect_worker_busy(),
    }


@router.get("/api/ai-settings")
async def messenger2_api_ai_settings_get(request: Request, db: Session = Depends(get_db)):
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
        "rag_enabled": bool(s.get("rag_enabled")),
    }


@router.post("/api/ai-settings")
async def messenger2_api_ai_settings_post(
    request: Request,
    body: Messenger2AISettingsBody,
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
        rag_enabled=body.rag_enabled,
    )
    db.commit()
    return {"ok": True}


@router.get("/api/learning/drafts")
async def messenger2_api_learning_drafts_list(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    rows = list_pending_drafts(db, organization_id=org_id, limit=50)
    return {
        "ok": True,
        "drafts": [
            {
                "id": d.id,
                "title": d.title,
                "body": d.body,
                "rationale": d.rationale or "",
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ],
    }


@router.post("/api/learning/drafts/{draft_id:int}/merge")
async def messenger2_api_learning_draft_merge(
    request: Request, draft_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    ok, err = merge_draft_into_prompt_addon(db, organization_id=org_id, draft_id=draft_id)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=404 if err == "not_found" else 400)
    return {"ok": True}


@router.post("/api/learning/drafts/{draft_id:int}/dismiss")
async def messenger2_api_learning_draft_dismiss(
    request: Request, draft_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    if not dismiss_draft(db, organization_id=org_id, draft_id=draft_id):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {"ok": True}


@router.post("/api/learning/analyze")
async def messenger2_api_learning_analyze(
    request: Request, body: Messenger2LearningAnalyzeBody, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    ids: list[int] = []
    for x in body.conversation_ids or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i > 0:
            ids.append(i)
    ids = sorted(set(ids))[:5]
    if not ids:
        return JSONResponse({"ok": False, "error": "no_conversation_ids"}, status_code=422)
    if messenger_ai_reflect_worker_busy():
        return JSONResponse({"ok": False, "error": "reflect_busy"}, status_code=409)
    for cid in ids:
        conv = db.get(Conversation, cid)
        if not conv:
            return JSONResponse({"ok": False, "error": "not_found", "conversation_id": cid}, status_code=404)
        if not (
            db.query(FBAccount)
            .filter(
                FBAccount.id == conv.fb_account_id,
                FBAccount.organization_id == org_id,
            )
            .first()
        ):
            return JSONResponse({"ok": False, "error": "not_found", "conversation_id": cid}, status_code=404)
    job_id = schedule_messenger_reflect_analysis(db, organization_id=org_id, conversation_ids=ids)
    return {"ok": True, "job_id": job_id}


@router.get("/api/learning/jobs/{job_id:int}")
async def messenger2_api_learning_job_status(
    request: Request, job_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    job = db.get(Job, job_id)
    if not job or job.organization_id != org_id or job.job_type != MESSENGER_REFLECT_JOB_TYPE:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {
        "ok": True,
        "status": job.status,
        "error": (job.error_summary or "")[:900] if job.error_summary else None,
    }


@router.post("/api/learning/reindex")
async def messenger2_api_learning_reindex(
    request: Request, body: Messenger2LearningReindexBody, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    conv = db.get(Conversation, int(body.conversation_id))
    if not conv:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if not (
        db.query(FBAccount)
        .filter(FBAccount.id == conv.fb_account_id, FBAccount.organization_id == org_id)
        .first()
    ):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    rebuild_messenger_ai_memory_for_conversation(db, conv.id)
    db.commit()
    return {"ok": True}


@router.get("/api/thread/{conversation_id:int}/ai-assistant")
async def messenger2_api_thread_ai_get(
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


@router.post("/api/thread-ai-from-url")
async def messenger2_api_thread_ai_from_url(
    request: Request,
    body: Messenger2ThreadAiFromUrlBody,
    db: Session = Depends(get_db),
):
    """Встроенный Messenger: upsert чата по URL webview и те же поля, что у thread ai-assistant."""
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    selected_id = resolve_active_fb_account_id(request, db, org_id, accounts)
    if selected_id is None:
        return JSONResponse({"ok": False, "error": "no_account"}, status_code=400)
    if not _fb_account_scoped(db, selected_id, org_id):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    conv = upsert_conversation_from_messenger_webview_url(
        db,
        organization_id=org_id,
        fb_account_id=selected_id,
        location_url=body.url,
        peer_display_name=body.peer_display_name,
    )
    if conv is None:
        return JSONResponse(
            {
                "ok": False,
                "error": "no_thread",
                "detail": (
                    "Откройте переписку с человеком: в адресе webview должен быть путь "
                    "…/messages/t/… или messenger.com/t/…"
                ),
            },
            status_code=422,
        )

    if body.enabled is not None:
        conv.ai_assistant_enabled = bool(body.enabled)
    if body.extra_instructions is not None:
        conv.ai_extra_instructions = (body.extra_instructions or "").strip()[:8000] or None
    if body.reset_transcript_fingerprint:
        conv.ai_last_transcript_fingerprint = None
    if body.reset_ai_reply_steps:
        conv.ai_reply_steps_used = 0
    autosync_was_enabled = False
    # Если клиент только что включил IA-асистента — автоматически поднимаем
    # фоновую автосинхронизацию inbox. Без неё AI никогда не увидит новое
    # входящее сообщение и не ответит (было частой UX-ловушкой).
    if body.enabled is True:
        from backend.services.messenger_settings import (
            get_messenger_auto_inbox_sync_enabled,
            set_messenger_auto_inbox_sync_enabled,
        )
        if not get_messenger_auto_inbox_sync_enabled(db):
            set_messenger_auto_inbox_sync_enabled(db, True)
            autosync_was_enabled = True
    db.commit()
    s = load_messenger_ai_settings(db)
    return {
        "ok": True,
        "conversation_id": conv.id,
        "enabled": conv.ai_assistant_enabled,
        "ai_reply_steps_used": int(conv.ai_reply_steps_used or 0),
        "ai_reply_steps_max": s["max_reply_steps"],
        "autosync_enabled_now": autosync_was_enabled,
    }


@router.post("/api/thread/{conversation_id:int}/ai-assistant")
async def messenger2_api_thread_ai_post(
    request: Request,
    conversation_id: int,
    body: Messenger2ThreadAssistantBody,
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
    autosync_was_enabled = False
    if body.enabled is True:
        from backend.services.messenger_settings import (
            get_messenger_auto_inbox_sync_enabled,
            set_messenger_auto_inbox_sync_enabled,
        )
        if not get_messenger_auto_inbox_sync_enabled(db):
            set_messenger_auto_inbox_sync_enabled(db, True)
            autosync_was_enabled = True
    db.commit()
    return {
        "ok": True,
        "enabled": conv.ai_assistant_enabled,
        "ai_reply_steps_used": int(conv.ai_reply_steps_used or 0),
        "autosync_enabled_now": autosync_was_enabled,
    }


@router.get("")
async def messenger2_page(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    # Снять залипшие outreach shared_profile блокировки (упавший job не освободил lock_job_id).
    try:
        clear_stale_outreach_shared_profile_locks(db)
    except Exception:
        logger.exception("messenger2: clear stale outreach shared_profile locks failed")
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    selected_id = resolve_active_fb_account_id(request, db, org_id, accounts)
    selected_label = ""
    selected_storage_state: dict[str, Any] | None = None
    selected_acc: FBAccount | None = None
    if selected_id is not None:
        acc = _fb_account_scoped(db, selected_id, org_id)
        if acc:
            selected_acc = acc
            selected_label = acc.label
            selected_storage_state = _session_state_from_account(acc)

    flash = request.query_params.get("flash", "")
    flash_type = request.query_params.get("flash_type", "info")
    raw_thread = (request.query_params.get("thread_url") or "").strip()
    m2_initial_nav_url = ""
    if raw_thread and is_safe_facebook_messenger_nav_url(raw_thread):
        m2_initial_nav_url = raw_thread.split("?")[0].split("#")[0][:2048]
    ua = request.headers.get("user-agent")
    is_fb_master_desktop = user_agent_is_fb_master_desktop(ua)
    m2_desktop_partition_proxy: dict[str, Any] | None = None
    m2_webview_user_agent: str | None = None
    m2_chromium_profile_abs: str | None = None
    m2_messenger_use_disk_profile = False
    # При включённом «встроенном логине» Messenger должен использовать ТОТ ЖЕ
    # Electron partition, что и embedded-login webview. Тогда IndexedDB с E2EE
    # ключами (их туда пишет Facebook после ввода PIN) переиспользуется — PIN не
    # спрашивается повторно. Дисковый профиль в этом режиме ОТКЛЮЧАЕТСЯ: ключи
    # Playwright-Chromium и Electron-Chromium несовместимы по IndexedDB, и
    # смешивать их бесполезно.
    m2_webview_partition_override: str | None = None
    if (
        is_fb_master_desktop
        and selected_acc
        and effective_embedded_login_enabled(db)
    ):
        m2_webview_partition_override = f"persist:fbm-login-{int(selected_acc.id)}"
    if is_fb_master_desktop and selected_acc:
        m2_desktop_partition_proxy = electron_partition_proxy_for_webview(selected_acc)
        ua = (selected_acc.stealth_user_agent or "").strip()
        if ua:
            m2_webview_user_agent = ua[:512]
        # Embedded login режим → всегда partition, не disk profile.
        if m2_webview_partition_override:
            m2_chromium_profile_abs = None
            m2_messenger_use_disk_profile = False
        else:
            prof = _messenger_playwright_profile_abs(db, selected_acc)
            socks_block = bool(
                m2_desktop_partition_proxy and m2_desktop_partition_proxy.get("socks_with_auth")
            )
            if prof and not socks_block:
                m2_chromium_profile_abs = prof
                m2_messenger_use_disk_profile = True
    # Устаревшие сообщения про WebSocket/предпросмотр (функция удалена) — не показывать.
    if flash and (
        "потоком предпросмотра" in flash
        or ("Стоп просмотра" in flash and "предпросмотра" in flash)
    ):
        flash = ""
    if not flash:
        flash_type = "info"
    login_busy_ids = {a.id for a in accounts if login_window_busy(a.id)}

    crm_stages_messenger = [
        {
            "slug": s.slug,
            "label": workflow_label_for_stage(s.slug, s.label),
            "icon": icon_class_for_crm_stage_slug(s.slug),
        }
        for s in get_ordered_stages(db)
    ]
    messenger_ai_llm_ready = cabinet_llm_ready_for_workspace(db)

    m2_messenger_outreach_blocked = bool(
        is_fb_master_desktop
        and selected_id is not None
        and messenger_blocked_by_outreach(db, int(selected_id))
    )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "messenger2/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "messenger2",
            "accounts": accounts,
            "selected_account_id": selected_id,
            "active_fb_account_label": selected_label,
            "selected_account_storage_state": selected_storage_state,
            "fb_messenger_e2ee_url": FB_MESSENGER_E2EE_URL,
            "flash": flash,
            "flash_type": flash_type,
            "login_busy_ids": login_busy_ids,
            "messenger2_frame_health_port": MESSENGER2_FRAME_HEALTH_PORT,
            "messenger2_autostart": AUTOSTART_MESSENGER2_FRAME,
            "is_fb_master_desktop": is_fb_master_desktop,
            "m2_desktop_partition_proxy": m2_desktop_partition_proxy,
            "m2_webview_user_agent": m2_webview_user_agent,
            "m2_chromium_profile_abs": m2_chromium_profile_abs,
            "m2_messenger_use_disk_profile": m2_messenger_use_disk_profile,
            "m2_webview_partition_override": m2_webview_partition_override,
            "m2_messenger_outreach_blocked": m2_messenger_outreach_blocked,
            "crm_stages_messenger": crm_stages_messenger,
            "m2_sync_display_name_from_messenger": effective_messenger_sync_display_name_from_messenger(
                db
            ),
            "m2_initial_nav_url": m2_initial_nav_url,
            "messenger_ai_llm_ready": messenger_ai_llm_ready,
        },
    )


@router.get("/sidecar-health")
async def messenger2_sidecar_health(request: Request, db: Session = Depends(get_db)):
    """Проверка, отвечает ли локальное Electron-приложение messenger2-frame (тот же хост)."""
    require_org_id(request, db)
    url = f"http://127.0.0.1:{MESSENGER2_FRAME_HEALTH_PORT}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("ok") is True:
            return JSONResponse(data)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError):
        pass
    return JSONResponse({"ok": False, "error": "sidecar_unavailable"}, status_code=503)


@router.post("/open-chromium")
async def messenger2_open_chromium(
    request: Request,
    db: Session = Depends(get_db),
):
    org_id = require_org_id(request, db)
    accounts = (
        db.query(FBAccount)
        .filter(
            FBAccount.organization_id == org_id,
            FBAccount.active_slot.isnot(None),
        )
        .order_by(FBAccount.active_slot.asc(), FBAccount.label)
        .all()
    )
    aid = resolve_active_fb_account_id(request, db, org_id, accounts)
    if aid is None:
        return _redirect_flash("Добавьте аккаунт в «Аккаунты»", "error")
    acc = _fb_account_scoped(db, aid, org_id)
    if not acc:
        raise HTTPException(404)
    if not account_in_active_slot(acc):
        return _redirect_flash(
            "Сначала назначьте аккаунту слот (1–3) в разделе «Аккаунты».",
            "error",
        )

    cdp = effective_fb_cdp_endpoint(acc.playwright_cdp_url)
    path = playwright_profile_dir_for_account(db, aid, acc.profile_dir)

    proxy = proxy_dict_for_playwright(
        enabled=bool(acc.proxy_enabled),
        url=acc.proxy_url,
        username=acc.proxy_username,
        password=acc.proxy_password,
    )
    inject = _session_state_from_account(acc)

    def _on_close(snap: dict[str, Any] | None) -> None:
        persist_fb_session_snapshot(aid, snap)

    stealth = stealth_playwright_options_from_account(acc)
    ok, err = try_start_login_window(
        aid,
        str(path),
        proxy,
        storage_state=inject,
        on_closed_storage=_on_close,
        on_interim_storage=_on_close,
        landing_after_warmup=FB_MESSENGER_E2EE_URL,
        stealth_bundle=stealth,
        cdp_endpoint=cdp,
    )
    if not ok:
        return _redirect_flash(err, "error")
    return _redirect_flash(
        "Открылось окно Chromium с вашим профилем аккаунта. "
        "После закрытия окна сессия при необходимости сохранится на диск и в базу (как при «Войти в Facebook»).",
        "ok",
    )
