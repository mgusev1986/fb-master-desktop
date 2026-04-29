"""Facebook-аккаунты: CRUD, профиль Chromium, вход, проверка сессии, прокси, резерв сессии в БД."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections import Counter
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.config import (
    BASE_DIR,
    effective_fb_cdp_endpoint,
    effective_fb_local_chrome_cookies,
    proxy_health_check_interval_sec,
)
from backend.database import SessionLocal, get_db
from backend.models import FBAccount
from backend.services.contacted_registry import preserve_contacted_registry_before_fb_account_delete
from backend.services.cabinet_settings import (
    effective_browser_profiles_dir,
    effective_embedded_login_enabled,
)
from backend.services.electron_proxy import electron_partition_proxy_for_webview
from backend.services.desktop_client import user_agent_is_fb_master_desktop
from backend.services.fb_account_profile_paths import (
    playwright_profile_dir_for_account,
    resolved_profile_under_base,
)
from backend.services.fb_account_import_parse import (
    ParsedCookieMarketplaceLine,
    extract_all_cookie_marketplace_lines,
    extract_fb_import_line_from_text,
    parse_account_line,
    parse_cookie_json_storage_blob,
    parse_cookie_marketplace_line,
    parse_cookie_pipe_token_line,
    parse_cookie_semicolon_meta_pipe_line,
    normalize_fb_account_proxy_form,
    parse_proxy_line,
    proxy_url_for_playwright,
)
from backend.services.fb_credentials_crypto import decrypt_secret, encrypt_secret
from backend.services.proxy_geo_lookup import (
    lookup_geo_through_proxy,
    parsed_proxy_line_from_account_fields,
)
from backend.services.proxy_lease import parse_proxy_lease_ends_at_from_form
from backend.services.fb_playwright import (
    check_facebook_session,
    compute_totp_for_display,
    evaluate_fb_storage_session,
    login_window_busy,
    playwright_chromium_precheck,
    proxy_dict_for_playwright,
    test_facebook_credentials,
    test_proxy,
    try_start_auto_login_window,
    try_start_login_window,
)
from backend.services.fb_session_state import (
    clear_account_login_blocked,
    persist_account_login_blocked,
    persist_account_session_snapshot as persist_fb_session_snapshot,
    refresh_account_session_flags as _refresh_session_flags,
    session_state_from_account as _session_state_from_account,
    session_state_is_usable,
    usable_session_state_from_account,
)
from backend.services.fb_stealth_profile import (
    DEFAULT_STEALTH_REGION,
    apply_custom_stealth,
    apply_region_preset_to_account,
    stealth_playwright_options_from_account,
    stealth_region_choices,
)
from backend.services.active_fb_account import (
    clear_active_fb_account_id,
    get_active_fb_account_id,
    resolve_active_fb_account_id,
    set_active_fb_account_id,
)
from backend.services.fb_account_slots import (
    MAX_FB_ACTIVE_SLOTS,
    account_in_active_slot,
    assign_active_slot,
    assign_first_free_active_slot_if_unslotted,
    count_filled_slots,
    slot_label,
)
from backend.services.tenancy import require_org_id
from backend.services.account_health_snapshot import build_account_health_map
from backend.services.proxy_health_guard import sync_proxy_lease_slot_and_label_for_account

logger = logging.getLogger(__name__)


def _fb_account_scoped(db: Session, account_id: int, org_id: int) -> FBAccount | None:
    acc = db.get(FBAccount, account_id)
    if not acc or acc.organization_id != org_id:
        return None
    return acc


def _auto_slot_after_account_saved(db: Session, account_id: int) -> None:
    """Назначить первый свободный слот 1–3, если есть; иначе аккаунт остаётся без слота (очередь)."""
    acc = db.get(FBAccount, int(account_id))
    if not acc:
        return
    assign_first_free_active_slot_if_unslotted(db, acc)
    db.commit()

router = APIRouter(prefix="/fb-accounts", tags=["fb_accounts"])


def _profiles_base(db: Session) -> Path:
    p = effective_browser_profiles_dir(db)
    return p.resolve()


def _profiles_paths_for_ui(db: Session) -> tuple[str, str]:
    """Короткий путь от корня проекта (если внутри) и полный resolved — для подсказок в UI."""
    full_p = _profiles_base(db)
    full = str(full_p)
    try:
        short = str(full_p.relative_to(BASE_DIR.resolve()))
    except ValueError:
        short = full
    return short, full


def _redirect_flash(msg: str, kind: str = "info") -> RedirectResponse:
    q = quote(msg[:500], safe="")
    return RedirectResponse(f"/fb-accounts?flash={q}&flash_type={kind}", status_code=303)


@router.get("")
async def fb_accounts_list(request: Request, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    q = request.query_params.get("q", "").strip()
    query = (
        db.query(FBAccount)
        .filter(FBAccount.organization_id == org_id)
        .order_by(FBAccount.created_at.desc())
    )
    if q:
        query = query.filter(FBAccount.label.ilike(f"%{q}%"))
    accounts = query.all()
    lease_sync_changed = False
    for acc_row in accounts:
        if sync_proxy_lease_slot_and_label_for_account(db, acc_row):
            lease_sync_changed = True
    if lease_sync_changed:
        db.commit()
    dup_logins = {
        u
        for u, c in Counter(
            (a.fb_login_username or "").strip() for a in accounts if (a.fb_login_username or "").strip()
        ).items()
        if c > 1
    }
    resolve_active_fb_account_id(request, db, org_id, accounts)
    flash = request.query_params.get("flash", "")
    flash_type = request.query_params.get("flash_type", "info")
    login_busy_ids = {a.id for a in accounts if login_window_busy(a.id)}
    active_fb_id = get_active_fb_account_id(request, db, org_id)
    templates = request.app.state.templates
    prof_short, prof_full = _profiles_paths_for_ui(db)
    account_health = build_account_health_map(
        db,
        accounts,
        active_messenger_account_id=active_fb_id,
    )
    proxy_health_interval_sec = int(proxy_health_check_interval_sec())
    return templates.TemplateResponse(
        "fb_accounts/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "accounts": accounts,
            "search": q,
            "flash": flash,
            "flash_type": flash_type,
            "login_busy_ids": login_busy_ids,
            "active_fb_account_id": active_fb_id,
            "page_id": "fb_accounts",
            "profiles_base": prof_short,
            "profiles_base_full": prof_full,
            "fb_local_chrome_cookies": effective_fb_local_chrome_cookies(),
            "stealth_region_choices": stealth_region_choices(),
            "duplicate_fb_logins": dup_logins,
            "max_fb_active_slots": MAX_FB_ACTIVE_SLOTS,
            "fb_slots_filled": count_filled_slots(db, org_id),
            "slot_label": slot_label,
            "account_health": account_health,
            "proxy_health_interval_sec": proxy_health_interval_sec,
            "embedded_login_enabled": effective_embedded_login_enabled(db),
        },
    )


@router.post("/create")
async def fb_account_create(
    request: Request,
    db: Session = Depends(get_db),
    label: str = Form(...),
):
    label = label.strip()
    if not label:
        return _redirect_flash("Введите название аккаунта", "error")

    org_id = require_org_id(request, db)
    acc = FBAccount(
        organization_id=org_id,
        label=label,
        profile_dir=str(_profiles_base(db) / "__new__"),
    )
    db.add(acc)
    db.flush()

    final = _profiles_base(db) / f"fb_account_{acc.id}"
    final.mkdir(parents=True, exist_ok=True)
    acc.profile_dir = str(final)
    db.commit()
    logger.info("FB account created id=%s path=%s", acc.id, acc.profile_dir)
    _auto_slot_after_account_saved(db, acc.id)
    return _redirect_flash(
        f"Аккаунт «{label}» добавлен. Если был свободный слот 1–{MAX_FB_ACTIVE_SLOTS}, он назначен автоматически; "
        f"если все слоты заняты — аккаунт в очереди (без слота), назначьте место вручную, когда освободится. "
        f"Далее «Войти в Facebook» и при необходимости «Использовать в Мессенджере».",
        "ok",
    )


@router.post("/proxy-geo")
async def fb_proxy_geo_from_line(
    request: Request,
    db: Session = Depends(get_db),
    proxy_line: str = Form(""),
):
    """JSON: страна выхода по прокси и рекомендуемый пресет stealth (запрос идёт через прокси)."""
    require_org_id(request, db)
    parsed = parse_proxy_line((proxy_line or "").strip())
    if not parsed:
        return JSONResponse(
            {
                "ok": False,
                "error": "Не удалось разобрать прокси (host:port:user:pass или socks5://…)",
            },
            status_code=400,
        )
    data = await asyncio.to_thread(lookup_geo_through_proxy, parsed)
    return JSONResponse(data)


@router.post("/{account_id}/set-active")
async def fb_account_set_active(request: Request, account_id: int, db: Session = Depends(get_db)):
    """Текущий аккаунт для встроенного Мессенджера — только из занятых слотов 1–3."""
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    if not account_in_active_slot(acc):
        return _redirect_flash(
            "Сначала переведите аккаунт в активный слот (1–3): без слота Мессенджер и парсер недоступны.",
            "error",
        )
    set_active_fb_account_id(request, account_id)
    return _redirect_flash(
        f"Сейчас используется аккаунт «{acc.label}» (Мессенджер).",
        "ok",
    )


@router.post("/{account_id}/active-slot")
async def fb_account_set_active_slot(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
    slot: str = Form(""),
):
    """
    slot пустой — в ожидание; 1, 2 или 3 — в активный слот (предыдущий обладатель слота уходит в ожидание).
    """
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    raw = (slot or "").strip()
    if not raw:
        acc.active_slot = None
        db.commit()
        if get_active_fb_account_id(request, db, org_id) == account_id:
            clear_active_fb_account_id(request)
        return _redirect_flash(f"Аккаунт «{acc.label}» переведён в ожидание.", "ok")
    try:
        n = int(raw)
    except ValueError:
        return _redirect_flash("Номер слота: 1, 2 или 3", "error")
    err = assign_active_slot(db, acc, n)
    if err:
        return _redirect_flash(err, "error")
    db.commit()
    return _redirect_flash(
        f"Аккаунт «{acc.label}» в слоте {n} — доступен для Мессенджера, парсера, прогрева и рассылок.",
        "ok",
    )


@router.post("/{account_id}/update")
async def fb_account_update(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
    label: str = Form(...),
    proxy_enabled: str = Form(""),
    proxy_url: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form(""),
    playwright_cdp_url: str = Form(""),
    proxy_lease_ends_at: str = Form(""),
):
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)

    acc.label = label.strip() or acc.label
    acc.proxy_enabled = proxy_enabled in ("on", "true", "1", "yes")
    p_url, p_user, p_pw, set_pw = normalize_fb_account_proxy_form(
        proxy_url, proxy_username, proxy_password
    )
    acc.proxy_url = p_url
    acc.proxy_username = p_user
    if set_pw and p_pw:
        acc.proxy_password = p_pw
    acc.playwright_cdp_url = (playwright_cdp_url or "").strip() or None

    if not acc.proxy_enabled or not (acc.proxy_url or "").strip():
        acc.proxy_lease_ends_at = None
        acc.proxy_lease_purchased_at = None
        acc.proxy_lease_days = None
    else:
        ends_raw = (proxy_lease_ends_at or "").strip()
        if not ends_raw:
            acc.proxy_lease_ends_at = None
            acc.proxy_lease_purchased_at = None
            acc.proxy_lease_days = None
        else:
            p = parse_proxy_lease_ends_at_from_form(ends_raw)
            if not p:
                return _redirect_flash(
                    "Некорректная дата и время окончания прокси. Проверьте поле «окончание у провайдера».",
                    "error",
                )
            acc.proxy_lease_ends_at = p
            acc.proxy_lease_purchased_at = None
            acc.proxy_lease_days = None

    db.commit()
    from backend.services.proxy_health_guard import on_fb_account_proxy_settings_saved

    on_fb_account_proxy_settings_saved(db, account_id)
    return _redirect_flash("Настройки сохранены", "ok")


@router.post("/{account_id}/update-label")
async def fb_account_update_label(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
    label: str = Form(...),
):
    """Только название в списке — без открытия блока «Прокси»."""
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    new_label = (label or "").strip()
    if not new_label:
        return _redirect_flash("Название не может быть пустым.", "error")
    if len(new_label) > 255:
        return _redirect_flash("Название не длиннее 255 символов.", "error")
    acc.label = new_label
    db.commit()
    return _redirect_flash("Название сохранено", "ok")


@router.post("/{account_id}/delete")
async def fb_account_delete(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
    confirm: str = Form(""),
):
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    if confirm != "yes":
        return _redirect_flash("Для удаления подтвердите галочкой в форме", "error")

    to_remove: list[Path] = []
    p = resolved_profile_under_base(db, acc.profile_dir)
    if p and p.is_dir():
        to_remove.append(p)
    canon = _profiles_base(db) / f"fb_account_{account_id}"
    if canon.is_dir() and canon not in to_remove:
        to_remove.append(canon)
    if get_active_fb_account_id(request, db, org_id) == account_id:
        clear_active_fb_account_id(request)
    preserve_contacted_registry_before_fb_account_delete(
        db, fb_account_id=int(acc.id), organization_id=int(acc.organization_id)
    )
    db.delete(acc)
    db.commit()
    for rm in to_remove:
        try:
            shutil.rmtree(rm)
        except OSError as e:
            logger.warning("rmtree profile %s: %s", rm, e)
            return _redirect_flash("Запись удалена из БД; папку профиля удалите вручную", "error")
    return _redirect_flash("Аккаунт удалён", "ok")


@router.post("/import-automation")
async def fb_account_import_automation(
    request: Request,
    db: Session = Depends(get_db),
    account_file: UploadFile | None = File(None),
    account_paste: str = Form(""),
    proxy_line: str = Form(""),
    proxy_lease_ends_at: str = Form(""),
    label: str = Form(""),
    stealth_region: str = Form(DEFAULT_STEALTH_REGION),
    custom_locale: str = Form(""),
    custom_timezone: str = Form(""),
    run_auto_login: str | None = Form(default=None),
):
    """
    Импорт: классика логин:пароль:TOTP:дата + прокси, либо cookie-строка
    телефон:пароль:User-Agent|[{cookies}] (прокси необязателен).
    """
    org_id = require_org_id(request, db)
    raw_line = ""
    cookie_parsed = None
    # 3.0.6+: batch-импорт. Если в файле/paste нашлось >1 cookie-строки —
    # импортируем ВСЕ как отдельные FBAccount. Первая идёт через основной флоу
    # (cookie_parsed), остальные — через extra_cookie_lines после первого commit.
    extra_cookie_lines: list[ParsedCookieMarketplaceLine] = []
    raw_blob_for_batch: str = ""

    def _cookie_from_text(blob: str) -> tuple[ParsedCookieMarketplaceLine | None, str]:
        """
        Сначала строка из шапки магазина (uid|pass|EAAB|[…], email;pass|…|[…], UA|[…]),
        иначе целый файл как чистый JSON cookies — чтобы не терять пароль из строки перед массивом.
        """
        rl = extract_fb_import_line_from_text(blob)
        cp: ParsedCookieMarketplaceLine | None = None
        if rl:
            cp = (
                parse_cookie_marketplace_line(rl)
                or parse_cookie_pipe_token_line(rl)
                or parse_cookie_semicolon_meta_pipe_line(rl)
            )
        if not cp:
            cp = parse_cookie_json_storage_blob(blob)
        return cp, rl

    if account_file and getattr(account_file, "filename", None):
        body = await account_file.read()
        text = body.decode("utf-8", errors="replace")
        cookie_parsed, raw_line = _cookie_from_text(text)
        raw_blob_for_batch = text
    if not cookie_parsed and not raw_line and (account_paste or "").strip():
        cookie_parsed, raw_line = _cookie_from_text((account_paste or "").strip())
        raw_blob_for_batch = (account_paste or "").strip()
    if not cookie_parsed and not raw_line:
        return _redirect_flash("Загрузите .txt или вставьте строку аккаунта", "error")

    # 3.0.6+: ищем дополнительные cookie-строки в том же blob (батч).
    # Применимо только когда первый аккаунт распарсился через cookie-формат
    # (для классики логин:пароль:TOTP — единичный импорт, как было).
    if cookie_parsed and raw_blob_for_batch:
        all_parsed = extract_all_cookie_marketplace_lines(raw_blob_for_batch)
        # Первый из all_parsed — это уже cookie_parsed (или эквивалентный по c_user).
        # Остальные — extra. Дедуп по c_user уже сделан в extract_all_cookie_marketplace_lines.
        primary_uid = cookie_parsed.fb_user_id or ""
        for cp in all_parsed:
            if cp.fb_user_id and cp.fb_user_id == primary_uid:
                continue
            # Если у обоих нет c_user — сравниваем по login (phone/email)
            if not primary_uid and cp.login == cookie_parsed.login:
                continue
            extra_cookie_lines.append(cp)

    parsed = None if cookie_parsed else parse_account_line(raw_line)
    if not cookie_parsed and not parsed:
        return _redirect_flash(
            "Не удалось разобрать строку. Поддерживаются: "
            "логин:пароль:TOTP:дата; телефон:пароль:User-Agent|[JSON cookies]; "
            "или целый .txt с JSON-массивом cookies / Playwright storage_state {\"cookies\":[...]}",
            "error",
        )

    pline = (proxy_line or "").strip()
    pp = parse_proxy_line(pline) if pline else None
    if pline and not pp:
        return _redirect_flash("Не удалось разобрать прокси (host:port:user:pass)", "error")
    if not cookie_parsed and not pp:
        return _redirect_flash("Для формата с TOTP укажите прокси (host:port:user:pass)", "error")

    lease_ends_raw = (proxy_lease_ends_at or "").strip()
    if lease_ends_raw:
        if not pp:
            return _redirect_flash(
                "Чтобы указать срок прокси, сначала заполните поле «Прокси».",
                "error",
            )
        e_lease = parse_proxy_lease_ends_at_from_form(lease_ends_raw)
        if not e_lease:
            return _redirect_flash(
                "Срок прокси: некорректная дата окончания (или очистите поле).",
                "error",
            )
    else:
        e_lease = None

    if cookie_parsed:
        acc = FBAccount(
            organization_id=org_id,
            label=(label or "").strip() or cookie_parsed.login[:80],
            profile_dir=str(_profiles_base(db) / "__new__"),
            # Логин (phone/email) имеет приоритет над user_id — нужен именно login для
            # формы Facebook при auto-login. FB API обычно НЕ принимает user_id (c_user)
            # в поле username, только phone или email.
            fb_login_username=(cookie_parsed.login or cookie_parsed.fb_user_id or "").strip(),
            enc_password=encrypt_secret(cookie_parsed.password),
            enc_totp_secret=None,
            birth_hint=None,
            proxy_enabled=bool(pp),
            proxy_url=proxy_url_for_playwright(pp) if pp else None,
            proxy_username=pp.username if pp else None,
            proxy_password=pp.password if pp else None,
            proxy_lease_ends_at=e_lease,
            proxy_lease_purchased_at=None,
            proxy_lease_days=None,
        )
    else:
        assert parsed is not None and pp is not None
        acc = FBAccount(
            organization_id=org_id,
            label=(label or "").strip() or parsed.login[:80],
            profile_dir=str(_profiles_base(db) / "__new__"),
            fb_login_username=parsed.login.strip(),
            enc_password=encrypt_secret(parsed.password),
            enc_totp_secret=encrypt_secret(parsed.totp_secret) if parsed.totp_secret else None,
            birth_hint=(parsed.birth_hint or None),
            proxy_enabled=True,
            proxy_url=proxy_url_for_playwright(pp),
            proxy_username=pp.username,
            proxy_password=pp.password,
            proxy_lease_ends_at=e_lease,
            proxy_lease_purchased_at=None,
            proxy_lease_days=None,
        )
    db.add(acc)
    db.flush()
    final = _profiles_base(db) / f"fb_account_{acc.id}"
    final.mkdir(parents=True, exist_ok=True)
    acc.profile_dir = str(final)

    reg = (stealth_region or DEFAULT_STEALTH_REGION).strip().lower()
    if reg == "custom":
        apply_custom_stealth(acc, custom_locale, custom_timezone)
    else:
        apply_region_preset_to_account(acc, reg)

    if cookie_parsed:
        ua = (cookie_parsed.user_agent or "").strip()
        if ua:
            acc.stealth_user_agent = ua[:512]
        acc.session_state_json = json.dumps(cookie_parsed.storage_state, ensure_ascii=False)
        acc.session_saved_at = datetime.now(timezone.utc)

    db.commit()
    from backend.services.proxy_health_guard import on_fb_account_proxy_settings_saved

    on_fb_account_proxy_settings_saved(db, acc.id)
    _auto_slot_after_account_saved(db, acc.id)

    if cookie_parsed:
        _refresh_session_flags(acc.id)
        logger.info(
            "FB import cookie id=%s login=%s c_user=%s proxy=%s",
            acc.id,
            cookie_parsed.login,
            cookie_parsed.fb_user_id,
            "yes" if pp else "no",
        )
    else:
        assert pp is not None
        logger.info("FB import-automation id=%s login=%s proxy=%s:%s", acc.id, acc.fb_login_username, pp.host, pp.port)

    # 3.0.6+: batch-импорт. Если в файле/paste было >1 cookie-строки — создаём
    # дополнительные FBAccount с тем же proxy/region/lease (label берём от login
    # каждого аккаунта, чтобы они не сливались по подписи).
    extra_imported_count = 0
    extra_failed_count = 0
    for extra_cp in extra_cookie_lines:
        try:
            extra_acc = FBAccount(
                organization_id=org_id,
                label=extra_cp.login[:80],
                profile_dir=str(_profiles_base(db) / "__new__"),
                fb_login_username=(extra_cp.login or extra_cp.fb_user_id or "").strip(),
                enc_password=encrypt_secret(extra_cp.password),
                enc_totp_secret=None,
                birth_hint=None,
                proxy_enabled=bool(pp),
                proxy_url=proxy_url_for_playwright(pp) if pp else None,
                proxy_username=pp.username if pp else None,
                proxy_password=pp.password if pp else None,
                proxy_lease_ends_at=e_lease,
                proxy_lease_purchased_at=None,
                proxy_lease_days=None,
            )
            db.add(extra_acc)
            db.flush()
            extra_final = _profiles_base(db) / f"fb_account_{extra_acc.id}"
            extra_final.mkdir(parents=True, exist_ok=True)
            extra_acc.profile_dir = str(extra_final)
            if reg == "custom":
                apply_custom_stealth(extra_acc, custom_locale, custom_timezone)
            else:
                apply_region_preset_to_account(extra_acc, reg)
            ua = (extra_cp.user_agent or "").strip()
            if ua:
                extra_acc.stealth_user_agent = ua[:512]
            extra_acc.session_state_json = json.dumps(extra_cp.storage_state, ensure_ascii=False)
            extra_acc.session_saved_at = datetime.now(timezone.utc)
            db.commit()
            on_fb_account_proxy_settings_saved(db, extra_acc.id)
            _auto_slot_after_account_saved(db, extra_acc.id)
            _refresh_session_flags(extra_acc.id)
            extra_imported_count += 1
            logger.info(
                "FB import cookie batch id=%s login=%s c_user=%s",
                extra_acc.id,
                extra_cp.login,
                extra_cp.fb_user_id,
            )
        except Exception as e:  # noqa: BLE001
            db.rollback()
            extra_failed_count += 1
            logger.exception(
                "FB import cookie batch FAIL login=%s c_user=%s: %s",
                extra_cp.login,
                extra_cp.fb_user_id,
                e,
            )

    if cookie_parsed:
        # Сразу оценим, что внутри snapshot — есть ли c_user/xs. Это даёт клиенту понимание,
        # что Facebook может встретить страницей «Продолжить как …» (Continue-gate),
        # если xs отозван/IP сменился. Полная проверка через Playwright — отдельной кнопкой.
        snap_after = cookie_parsed.storage_state if isinstance(cookie_parsed.storage_state, dict) else {}
        snap_cookies = snap_after.get("cookies") if isinstance(snap_after.get("cookies"), list) else []
        names = {str(c.get("name") or "") for c in snap_cookies if isinstance(c, dict)}
        has_c_user = "c_user" in names
        has_xs = "xs" in names

        # 3.0.6+: префикс с количеством для batch-импорта (когда в файле >1 аккаунт)
        batch_prefix = ""
        if extra_imported_count > 0:
            total = 1 + extra_imported_count
            batch_prefix = f"Импортировано {total} аккаунта(ов) из файла. "
            if extra_failed_count > 0:
                batch_prefix += f"⚠️ {extra_failed_count} строка(и) не прошли — см. логи. "

        if has_c_user and has_xs:
            tip = (
                batch_prefix +
                "Аккаунт импортирован: cookies c_user и xs найдены, сохранены в базе. "
                "Если при открытии Мессенджера Facebook покажет «Продолжить как …» — "
                "это значит, что xs отозван продавцом или сменился IP. "
                "Один раз введите пароль в окне Мессенджера — программа сохранит свежий вход и больше не будет затирать его. "
                "Дальше можно нажать «Проверить сессию» для финальной валидации."
            )
            level = "ok"
        elif has_c_user and not has_xs:
            tip = (
                batch_prefix +
                "Аккаунт импортирован: cookie c_user найден, но xs (auth-токен) отсутствует — "
                "это неполная сессия, Facebook потребует пароль при первом открытии Мессенджера. "
                "Если пароль из TXT-файла верный, вход сохранится автоматически после ручного входа в Мессенджере."
            )
            level = "error"
        else:
            tip = (
                batch_prefix +
                "Аккаунт сохранён, но в импортированных cookies нет c_user — это не полноценная сессия. "
                "Скорее всего понадобится войти по логину и паролю вручную через окно Мессенджера. "
                "Проверьте формат TXT-файла у продавца."
            )
            level = "error"
        return _redirect_flash(tip, level)

    do_auto = (run_auto_login or "").strip().lower() in ("on", "true", "1", "yes")
    if do_auto:
        cdp_auto = effective_fb_cdp_endpoint(acc.playwright_cdp_url)
        if not cdp_auto:
            pre = await asyncio.to_thread(playwright_chromium_precheck)
            if pre:
                return _redirect_flash(pre, "error")
        stealth = stealth_playwright_options_from_account(acc)
        proxy = proxy_dict_for_playwright(
            enabled=True,
            url=acc.proxy_url,
            username=acc.proxy_username,
            password=acc.proxy_password,
        )

        def _on_close(snap: dict[str, Any] | None) -> None:
            persist_fb_session_snapshot(acc.id, snap)

        ok, err = try_start_auto_login_window(
            acc.id,
            str(final),
            proxy,
            parsed.login.strip(),
            parsed.password,
            parsed.totp_secret,
            stealth,
            on_closed_storage=_on_close,
            cdp_endpoint=cdp_auto,
        )
        if not ok:
            return _redirect_flash(f"Аккаунт создан, но автологин не запущен: {err}", "error")
        return _redirect_flash(
            "Аккаунт импортирован. Откроется окно Chromium — автоматический вход и 2FA (TOTP). "
            "При чекпоинте Meta завершите вручную в этом окне. После закрытия сессия сохранится.",
            "ok",
        )

    return _redirect_flash(
        "Аккаунт и прокси сохранены. Свободный слот 1–3 назначен автоматически, если был; иначе аккаунт в очереди. "
        "Нажмите «Автовход (TOTP)» у карточки или «Войти в Facebook».",
        "ok",
    )


@router.post("/{account_id}/auto-login-stored")
async def fb_account_auto_login_stored(request: Request, account_id: int, db: Session = Depends(get_db)):
    """Автовход по сохранённым (зашифрованным) логину/паролю/TOTP."""
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    if not (acc.fb_login_username or "").strip() or not acc.enc_password:
        return _redirect_flash("Нет сохранённых учётных данных — сначала импортируйте аккаунт или укажите логин/пароль", "error")

    cdp = effective_fb_cdp_endpoint(acc.playwright_cdp_url)
    path = playwright_profile_dir_for_account(db, account_id, acc.profile_dir)

    pw = decrypt_secret(acc.enc_password)
    if not pw:
        return _redirect_flash("Не удалось расшифровать пароль (проверьте SECRET_KEY в .env)", "error")
    totp = decrypt_secret(acc.enc_totp_secret) if acc.enc_totp_secret else None

    proxy = proxy_dict_for_playwright(
        enabled=bool(acc.proxy_enabled),
        url=acc.proxy_url,
        username=acc.proxy_username,
        password=acc.proxy_password,
    )
    stealth = stealth_playwright_options_from_account(acc)

    def _on_close(snap: dict[str, Any] | None) -> None:
        persist_fb_session_snapshot(account_id, snap)
        usable, _reason = session_state_is_usable(
            snap,
            expected_login=(acc.fb_login_username or "").strip() or None,
        )
        if usable:
            clear_account_login_blocked(account_id)

    def _on_login_blocked(reason: str) -> None:
        persist_account_login_blocked(account_id, reason)

    if not cdp:
        pre_al = await asyncio.to_thread(playwright_chromium_precheck)
        if pre_al:
            return _redirect_flash(pre_al, "error")

    ok, err = try_start_auto_login_window(
        account_id,
        str(path),
        proxy,
        (acc.fb_login_username or "").strip(),
        pw,
        totp,
        stealth,
        on_closed_storage=_on_close,
        on_login_blocked=_on_login_blocked,
        cdp_endpoint=cdp,
    )
    if not ok:
        return _redirect_flash(err, "error")
    if account_in_active_slot(acc):
        set_active_fb_account_id(request, account_id)
    return _redirect_flash("Запущен автоматический вход (TOTP). Дождитесь закрытия окна Chromium.", "ok")


@router.post("/{account_id}/login")
async def fb_account_login(request: Request, account_id: int, db: Session = Depends(get_db)):
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    cdp = effective_fb_cdp_endpoint(acc.playwright_cdp_url)
    path = playwright_profile_dir_for_account(db, account_id, acc.profile_dir)

    proxy = proxy_dict_for_playwright(
        enabled=bool(acc.proxy_enabled),
        url=acc.proxy_url,
        username=acc.proxy_username,
        password=acc.proxy_password,
    )
    # Всегда подмешиваем снимок из БД, если он есть (импорт по cookies и т.д.):
    # иначе при уже непустом каталоге профиля cookies из базы не попадали в контекст.
    inject = usable_session_state_from_account(acc)

    def _on_close(snap: dict[str, Any] | None) -> None:
        persist_fb_session_snapshot(account_id, snap)

    stealth = stealth_playwright_options_from_account(acc)

    if not cdp:
        pre_lg = await asyncio.to_thread(playwright_chromium_precheck)
        if pre_lg:
            return _redirect_flash(pre_lg, "error")

    ok, err = try_start_login_window(
        account_id,
        str(path),
        proxy,
        storage_state=inject,
        on_closed_storage=_on_close,
        on_interim_storage=_on_close,
        stealth_bundle=stealth,
        cdp_endpoint=cdp,
    )
    if not ok:
        return _redirect_flash(err, "error")
    if account_in_active_slot(acc):
        set_active_fb_account_id(request, account_id)
    return _redirect_flash(
        "Запускается отдельное окно Chromium (проверьте Dock и другие рабочие столы). "
        "После успешного входа сессия каждые ~30 с дублируется в базу — можно нажать «Проверить вход», не закрывая окно. "
        "При закрытии Chromium снимок сохранится ещё раз. Если открыт мессенджер с тем же профилем, закройте его перед проверкой или парсером.",
        "ok",
    )


@router.get("/{account_id}/embedded-login")
async def fb_account_embedded_login_page(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    """
    Встроенный логин (бета): открывает webview внутри Electron на facebook.com/login.
    После успешного логина preload JS делает export + POST на /api/embedded-login-capture.
    Работает только когда включён флаг «Встроенный логин» в «Настройках» И это Desktop.
    Web-кабинет fallback'ается на старый POST /{id}/login с Playwright-окном.
    """
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    if not effective_embedded_login_enabled(db):
        return _redirect_flash(
            "Встроенный логин выключен. Включите его в «Настройках → Окно браузера».",
            "warning",
        )
    ua = request.headers.get("user-agent")
    is_desktop = user_agent_is_fb_master_desktop(ua)
    if not is_desktop:
        return _redirect_flash(
            "Встроенный логин доступен только в десктопной версии SOCMASTER.",
            "warning",
        )
    partition = f"persist:fbm-login-{int(account_id)}"
    proxy_rules_info = electron_partition_proxy_for_webview(acc)
    # Отдаём существующий snapshot cookies, чтобы фронт ин-джектнул их в webview
    # до первой навигации — тогда FB логинит пользователя автоматически и просит
    # только PIN E2EE (без ввода пароля повторно).
    existing_storage = usable_session_state_from_account(acc)
    webview_user_agent = (acc.stealth_user_agent or "").strip()[:512]
    # Сохранённые логин + пароль — для авто-заполнения формы FB, если cookies
    # просрочены и появился «Saved login» gate. Пароль расшифровывается здесь и
    # уходит в webview только в рамках локального приложения (owner — клиент).
    stored_login = (acc.fb_login_username or "").strip()
    stored_password = ""
    if acc.enc_password:
        try:
            stored_password = decrypt_secret(acc.enc_password) or ""
        except Exception:
            stored_password = ""
    # Стартуем с home — FB логинит по cookies и не показывает «Saved login» gate.
    # После того как пользователь зайдёт во вкладку «Мессенджер» внутри SOCMASTER,
    # FB покажет PIN-диалог для E2EE; ключи сохраняются в тот же partition, что и
    # embedded login — повторно PIN не спрашивается.
    initial_url = "https://www.facebook.com/" if existing_storage else "https://www.facebook.com/login/"
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "fb_accounts/embedded_login.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "fb_accounts",
            "acc": acc,
            "partition": partition,
            "login_url": initial_url,
            "proxy_info": proxy_rules_info or {},
            "existing_storage_state": existing_storage or {},
            "has_existing_session": bool(existing_storage),
            "webview_user_agent": webview_user_agent,
            "stored_login": stored_login,
            "stored_password": stored_password,
        },
    )


@router.post("/{account_id}/api/embedded-login-capture")
async def fb_account_embedded_login_capture(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    """
    Принимает storage_state из Electron webview после успешного логина.
    Структура body: { storage_state: { cookies: [...], origins: [...] } }
    Валидирует наличие c_user, сохраняет в FBAccount.session_state_json.
    """
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    storage = body.get("storage_state") if isinstance(body, dict) else None
    if not isinstance(storage, dict):
        return JSONResponse({"ok": False, "error": "missing_storage_state"}, status_code=400)
    cookies = storage.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return JSONResponse({"ok": False, "error": "empty_cookies"}, status_code=400)
    c_user = ""
    xs_cookie = ""
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        value = str(c.get("value") or "").strip()
        if name == "c_user" and value:
            c_user = value
        elif name == "xs" and value:
            xs_cookie = value
    if not c_user:
        return JSONResponse(
            {"ok": False, "error": "missing_c_user", "hint": "Логин не завершён"},
            status_code=400,
        )
    if not xs_cookie:
        return JSONResponse(
            {
                "ok": False,
                "error": "missing_xs",
                "hint": "Facebook ещё не выдал auth-cookie xs. Нажмите «Продолжить» или завершите вход паролем/2FA.",
            },
            status_code=400,
        )
    try:
        persist_fb_session_snapshot(int(account_id), storage)
    except Exception:
        logger.exception("embedded_login_capture: persist failed account=%s", account_id)
        return JSONResponse({"ok": False, "error": "persist_failed"}, status_code=500)
    try:
        clear_account_login_blocked(db, acc)
    except Exception:
        logger.debug("clear_account_login_blocked failed account=%s", account_id, exc_info=True)
    logger.info(
        "embedded_login_capture: OK account=%s cookies=%s c_user=%s",
        account_id,
        len(cookies),
        c_user[:6] + "…" if len(c_user) > 6 else c_user,
    )
    return JSONResponse({"ok": True, "cookies": len(cookies), "c_user": c_user})


@router.get("/{account_id}/totp-now")
async def fb_account_totp_now(request: Request, account_id: int, db: Session = Depends(get_db)):
    """
    Текущий 6-значный TOTP из зашифрованного секрета в карточке (для ручного ввода при входе в Facebook).
    Секрет в ответ не попадает.
    """
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(status_code=404)
    if not (acc.enc_totp_secret or "").strip():
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "Секрет TOTP не сохранён — импортируйте строку аккаунта с полем 2FA.",
            },
        )
    plain = decrypt_secret(acc.enc_totp_secret)
    if not plain:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "Не удалось расшифровать секрет (проверьте SECRET_KEY в .env).",
            },
        )
    code, interval, rem, err = compute_totp_for_display(plain)
    if err:
        return JSONResponse(status_code=400, content={"ok": False, "error": err})
    return JSONResponse(
        {
            "ok": True,
            "code": code,
            "interval": interval,
            "seconds_remaining": rem,
        }
    )


@router.post("/{account_id}/check-session")
async def fb_account_check_session(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    cdp = effective_fb_cdp_endpoint(acc.playwright_cdp_url)
    path = playwright_profile_dir_for_account(db, account_id, acc.profile_dir)
    path_s = str(path)

    proxy = proxy_dict_for_playwright(
        enabled=bool(acc.proxy_enabled),
        url=acc.proxy_url,
        username=acc.proxy_username,
        password=acc.proxy_password,
    )
    inject = usable_session_state_from_account(acc)

    stealth = stealth_playwright_options_from_account(acc)
    fn = partial(
        check_facebook_session,
        path_s,
        proxy,
        storage_state=inject,
        stealth_bundle=stealth,
        cdp_endpoint=cdp,
        expected_login=(acc.fb_login_username or "").strip() or None,
        account_id=account_id,
    )
    ok, msg, snap = await asyncio.to_thread(fn)

    acc.last_login_check_at = datetime.now(timezone.utc)
    acc.session_ok = ok
    if ok is True and snap:
        acc.session_state_json = json.dumps(snap, ensure_ascii=False)
        acc.session_saved_at = datetime.now(timezone.utc)
    if ok is True:
        assign_first_free_active_slot_if_unslotted(db, acc)
        acc.login_blocked_at = None
        acc.login_blocked_reason = None
    elif ok is False:
        # Сохраняем причину провала, чтобы клиент видел её на карточке (а не только во flash-toast).
        # «Continue-gate» (FB помнит профиль, xs истёк) — отдельная категория, требует пароля,
        # а не повторного импорта cookies.
        low = (msg or "").lower()
        if "помнит профиль" in low or "remembered" in low or "продолжить" in low:
            reason_short = "FB узнал аккаунт, но просит пароль (xs истёк или сменился IP/прокси)"
        else:
            reason_short = (msg or "сессия не активна")[:480]
        acc.login_blocked_at = datetime.now(timezone.utc)
        acc.login_blocked_reason = reason_short
    db.commit()

    if ok is True:
        return _redirect_flash(f"Сессия: ок — {msg}. Копия cookies сохранена в базе.", "ok")
    if ok is False:
        return _redirect_flash(f"Сессия не активна — {msg}", "error")
    return _redirect_flash(f"Проверка не удалась — {msg}", "error")


@router.post("/{account_id}/test-credentials")
async def fb_account_test_credentials(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
    fb_email: str = Form(""),
    fb_password: str = Form(""),
):
    """
    Одноразовая проверка логина/пароля Meta во временном браузере.
    Учётные данные в БД не сохраняются.
    """
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)

    proxy = proxy_dict_for_playwright(
        enabled=bool(acc.proxy_enabled),
        url=acc.proxy_url,
        username=acc.proxy_username,
        password=acc.proxy_password,
    )
    ok, msg = await asyncio.to_thread(
        test_facebook_credentials,
        fb_email,
        fb_password,
        proxy,
    )
    if ok is True:
        return _redirect_flash(msg, "ok")
    if ok is False:
        return _redirect_flash(msg, "error")
    return _redirect_flash(msg, "error")


@router.post("/{account_id}/proxy-geo")
async def fb_account_proxy_geo(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    """JSON: гео выхода по сохранённому у аккаунта прокси."""
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    parsed = parsed_proxy_line_from_account_fields(
        acc.proxy_url, acc.proxy_username, acc.proxy_password
    )
    if not parsed:
        return JSONResponse(
            {"ok": False, "error": "У аккаунта не заполнен URL прокси"},
            status_code=400,
        )
    data = await asyncio.to_thread(lookup_geo_through_proxy, parsed)
    return JSONResponse(data)


@router.post("/{account_id}/proxy-geo-apply")
async def fb_account_proxy_geo_apply(
    request: Request, account_id: int, db: Session = Depends(get_db)
):
    """Гео через сохранённый прокси + запись пресета stealth в БД (locale/TZ/UA/viewport)."""
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    parsed = parsed_proxy_line_from_account_fields(
        acc.proxy_url, acc.proxy_username, acc.proxy_password
    )
    if not parsed:
        return JSONResponse(
            {"ok": False, "error": "У аккаунта не заполнен URL прокси"},
            status_code=400,
        )
    data = await asyncio.to_thread(lookup_geo_through_proxy, parsed)
    if not data.get("ok"):
        return JSONResponse(data, status_code=400)
    region_key = (data.get("stealth_region") or DEFAULT_STEALTH_REGION).strip().lower()
    apply_region_preset_to_account(acc, region_key)
    db.commit()
    out = dict(data)
    out["applied"] = True
    out["applied_region"] = region_key
    return JSONResponse(out)


@router.post("/{account_id}/test-proxy")
async def fb_account_test_proxy(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
    proxy_url: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form(""),
):
    org_id = require_org_id(request, db)
    acc = _fb_account_scoped(db, account_id, org_id)
    if not acc:
        raise HTTPException(404)
    if not acc.proxy_enabled:
        return _redirect_flash("Включите «Использовать прокси» и сохраните настройки", "error")
    url, user, pw_new, set_pw = normalize_fb_account_proxy_form(
        proxy_url, proxy_username, proxy_password
    )
    if not (url or "").strip():
        return _redirect_flash("Укажите адрес прокси в форме или сохраните настройки.", "error")
    pw_final = ""
    if set_pw and pw_new:
        pw_final = pw_new
    elif acc.proxy_password:
        plain = decrypt_secret(acc.proxy_password)
        if plain:
            pw_final = plain
    proxy = proxy_dict_for_playwright(
        enabled=True,
        url=url,
        username=user,
        password=pw_final or None,
    )
    ok, msg = await asyncio.to_thread(test_proxy, proxy)
    if ok:
        return _redirect_flash(f"Прокси отвечает: {msg}", "ok")
    return _redirect_flash(f"Прокси: {msg}", "error")
