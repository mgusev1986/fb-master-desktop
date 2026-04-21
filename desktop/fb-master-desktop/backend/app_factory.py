"""Фабрика FastAPI-приложения FB Master."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from backend import config as app_config
from backend.config import (
    BASE_DIR,
    BASE_FB_DIR,
    BROWSER_PROFILES_DIR,
    DATA_DIR,
    GOOGLE_CLIENT_ID,
    LOG_DIAGNOSTICS_DIR,
    LOG_DIR,
    SECRET_KEY,
    SESSION_COOKIE_SECURE,
    fb_master_access_key_required,
    fb_master_no_auth,
)
from backend.database import init_db, using_postgresql
from backend.logging_config import setup_logging

logger = logging.getLogger(__name__)

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

def _static_asset_version() -> str:
    """Версия для ?v= на CSS/JS — меняется при правке файла, снимает «залипший» кэш webview/браузера."""
    p = STATIC_DIR / "css" / "main.css"
    try:
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"

_MADRID_TZ = ZoneInfo("Europe/Madrid")


def format_dt_madrid(dt: datetime | None) -> str:
    """Отображение дат в интерфейсе (Europe/Madrid)."""
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MADRID_TZ).strftime("%d.%m.%Y %H:%M")


def messenger_fb_account_label(lbl: str | None) -> str:
    """Подпись аккаунта Facebook в UI мессенджера (имя в БД может отличаться от желаемого текста)."""
    if lbl is None:
        return ""
    s = str(lbl).strip()
    if s == "Основной профиль":
        return "Перейти в профиль"
    return str(lbl)


def _start_sequence_autotick_thread() -> None:
    import threading
    import time

    def _loop() -> None:
        time.sleep(75)
        while True:
            time.sleep(900)
            try:
                from backend.services.sequence_worker import try_sequence_autotick

                try_sequence_autotick()
            except Exception:
                logger.exception("sequence autotick")

    threading.Thread(target=_loop, daemon=True, name="sequence-autotick").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    try:
        from backend.database import SessionLocal
        from backend.services.automation_auto_resume import run_auto_resume_after_stale_recovery
        from backend.services.stale_job_recovery import recover_stale_automation_on_startup

        s = SessionLocal()
        try:
            stats = recover_stale_automation_on_startup(s)
            if sum(stats.values()):
                logger.info("Восстановление состояния задач при старте: %s", stats)
        finally:
            s.close()
        s2 = SessionLocal()
        try:
            auto = run_auto_resume_after_stale_recovery(s2)
            if auto.get("outreach_resumed") or auto.get("warmup_resumed"):
                logger.info("Автовозобновление рассылки/прогрева после перезапуска: %s", auto)
        finally:
            s2.close()
    except Exception:
        logger.exception("Восстановление зависших задач при старте")

    from backend.services.automation_switch import playwright_workers_enabled

    if playwright_workers_enabled():
        _start_sequence_autotick_thread()
    else:
        logger.info("Фоновые потоки сценариев отключены (FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS).")
    try:
        if playwright_workers_enabled():
            from backend.services.messenger_autosync_scheduler import start_messenger_inbox_autosync_thread

            start_messenger_inbox_autosync_thread()
    except Exception:
        logger.exception("start_messenger_inbox_autosync_thread")
    try:
        if playwright_workers_enabled():
            from backend.services.natural_warmup_scheduler import start_natural_warmup_scheduler_thread

            start_natural_warmup_scheduler_thread()
    except Exception:
        logger.exception("start_natural_warmup_scheduler_thread")
    try:
        if playwright_workers_enabled():
            from backend.services.proxy_health_guard import start_proxy_health_monitor_thread

            start_proxy_health_monitor_thread()
    except Exception:
        logger.exception("start_proxy_health_monitor_thread")
    try:
        from backend.services.messenger2_frame_sidecar import try_autostart_messenger2_frame

        try_autostart_messenger2_frame()
    except Exception:
        logger.exception("try_autostart_messenger2_frame")
    logger.info(
        "Back-office started: DB=%s DATA_DIR=%s LOG_DIR=%s LOG_DIAGNOSTICS_DIR=%s "
        "BROWSER_PROFILES_DIR=%s BASE_FB_DIR=%s",
        "postgresql" if using_postgresql() else "sqlite",
        DATA_DIR,
        LOG_DIR,
        LOG_DIAGNOSTICS_DIR,
        BROWSER_PROFILES_DIR,
        BASE_FB_DIR,
    )
    yield
    logger.info("Back-office stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="FB Master",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health_check():
        """Лёгкая проверка для локального бэкенда внутри Electron (без авторизации)."""
        return {"ok": True, "app": "fb-master"}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    from backend.routers.system import ACTION_TYPE_LABELS
    from backend.services.access_keys import expires_at_is_past
    from backend.services.proxy_lease import (
        proxy_lease_datetime_local_value,
        proxy_lease_ends_at_form_value,
    )
    from backend.services.platform_owner import is_platform_owner_user
    from backend.services.preview_as_user import preview_as_user_active

    def sidebar_show_logout(request, user) -> bool:
        if preview_as_user_active(request) and is_platform_owner_user(user):
            return False
        return app_config.fb_master_show_logout_in_ui()

    def cabinet_client_mode(request) -> bool:
        """
        Упрощённый кабинет без технических подсказок.
        Всегда для пользователей, которые не владелец платформы (типичные клиенты на VPS).
        Для владельца — десктоп (hide_infra), предпросмотр клиента или FB_MASTER_FORCE_CABINET_CLIENT_UI.
        """
        if app_config.fb_master_force_cabinet_client_ui():
            return True
        try:
            sess_user = request.session.get("user")
        except Exception:
            sess_user = None
        if sess_user and not is_platform_owner_user(sess_user):
            return True
        return app_config.fb_master_hide_infra_settings() or preview_as_user_active(
            request
        )

    templates.env.globals["action_type_labels"] = ACTION_TYPE_LABELS
    templates.env.globals["format_dt_madrid"] = format_dt_madrid
    templates.env.globals["proxy_lease_datetime_local_value"] = proxy_lease_datetime_local_value
    templates.env.globals["proxy_lease_ends_at_form_value"] = proxy_lease_ends_at_form_value
    templates.env.globals["access_key_expiry_expired"] = expires_at_is_past
    templates.env.globals["messenger_fb_account_label"] = messenger_fb_account_label
    templates.env.globals["is_platform_owner"] = is_platform_owner_user
    templates.env.globals["preview_as_user_active"] = preview_as_user_active
    templates.env.globals["cabinet_client_mode"] = cabinet_client_mode
    templates.env.globals["sidebar_show_logout"] = sidebar_show_logout
    templates.env.globals["static_v"] = _static_asset_version()
    templates.env.globals["show_logout_in_ui"] = app_config.fb_master_show_logout_in_ui()
    templates.env.globals["fbm_desktop_app_version"] = app_config.desktop_app_version_ui()
    templates.env.globals["fb_master_no_auth"] = fb_master_no_auth

    def fbm_client_installation_id() -> str:
        from backend.services.client_installation import get_client_installation_id

        return get_client_installation_id()

    def fbm_sidebar_access_key_license(request: Request):
        from backend.database import SessionLocal
        from backend.services.access_keys import sidebar_access_key_license_info

        db = SessionLocal()
        try:
            return sidebar_access_key_license_info(db, request)
        finally:
            db.close()

    templates.env.globals["fbm_client_installation_id"] = fbm_client_installation_id
    templates.env.globals["fbm_sidebar_access_key_license"] = fbm_sidebar_access_key_license

    app.state.templates = templates

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        path = request.url.path
        from backend.services.fbm_client_version import append_fbm_app_to_url

        from backend.database import SessionLocal
        from backend.services.access_keys import session_access_valid
        from backend.services.local_operator_session import ensure_no_auth_session_user
        from backend.services.tenancy import effective_organization_id

        if path == "/health":
            return await call_next(request)

        # /auth/unlock обходится allow_unlock_path до общего auth_guard — иначе
        # try_rehydrate ниже никогда не вызывается, cookie после обновления пустая,
        # а клиент снова видит ввод ключа при живой записи в локальной SQLite.
        if (
            fb_master_access_key_required()
            and path == "/auth/unlock"
            and request.method == "GET"
        ):
            db_unlock = SessionLocal()
            try:
                from backend.services.access_keys import try_rehydrate_desktop_access_session

                try_rehydrate_desktop_access_session(db_unlock, request)
            finally:
                db_unlock.close()

        from backend.services.web_cabinet_gate import allow_unlock_path_without_gate_session

        if allow_unlock_path_without_gate_session(request, path, request.method):
            return await call_next(request)

        if app_config.web_cabinet_gate_enabled():
            slug = app_config.web_cabinet_gate_slug()
            p_norm = path.rstrip("/") or "/"
            gate_path = f"/gate/{slug}"
            if slug and p_norm == gate_path and request.method in ("GET", "POST"):
                return await call_next(request)

        if app_config.web_cabinet_gate_enabled():
            from backend.services.web_cabinet_gate import (
                WEB_CABINET_GATE_SESSION_KEY,
                cabinet_auth_entry_requires_gate_pass,
                user_agent_desktop_cabinet_bypass,
            )

            if cabinet_auth_entry_requires_gate_pass(path, request.method):
                if not user_agent_desktop_cabinet_bypass(request):
                    if not request.session.get(WEB_CABINET_GATE_SESSION_KEY):
                        from starlette.responses import PlainTextResponse

                        return PlainTextResponse("Not Found", status_code=404)

        if request.method in ("GET", "HEAD") and path == "/download/release":
            return await call_next(request)

        if path.startswith("/static"):
            from backend.services.release_download import block_static_releases_path

            if block_static_releases_path() and path.startswith("/static/releases/"):
                from starlette.responses import Response

                return Response(status_code=404)
            return await call_next(request)

        if request.method == "POST" and path == "/api/public/desktop-license/activate":
            return await call_next(request)
        if request.method == "POST" and path == "/api/public/client-presence":
            return await call_next(request)

        # Манифест обновлений Electron — без ключа и без входа в кабинет
        if request.method == "GET" and path == "/api/public/desktop-update":
            return await call_next(request)
        if request.method == "GET" and path == "/api/public/desktop-update-dev":
            return await call_next(request)

        # NOWPayments: оплата и вебхук без ключа и без входа в кабинет
        if path == "/webhooks/nowpayments" and request.method == "POST":
            return await call_next(request)
        if path.startswith("/billing/nowpayments/"):
            return await call_next(request)
        if request.method == "GET" and path in (
            "/",
            "/buy",
            "/purchase",
            "/promo",
            "/promo2",
            "/landing",
            "/download/dev",
        ):
            return await call_next(request)

        ak_required = fb_master_access_key_required()
        auth_without_user = path == "/auth/unlock" or path == "/auth/access-key/activate"
        # Десктоп открывает /auth/unlock первым — восстановление ключа по железу должно сработать и здесь.
        if ak_required:
            db_rh = SessionLocal()
            try:
                from backend.services.access_keys import try_rehydrate_desktop_access_session

                try_rehydrate_desktop_access_session(db_rh, request)
            finally:
                db_rh.close()

        if ak_required and not auth_without_user:
            db = SessionLocal()
            try:
                if not session_access_valid(db, request):
                    from backend.models import AccessKey
                    from backend.services.access_keys import SESSION_KEY_ID, expires_at_is_past

                    q = ""
                    raw_kid = request.session.get(SESSION_KEY_ID)
                    if raw_kid is not None:
                        try:
                            row = db.get(AccessKey, int(raw_kid))
                            if (
                                row
                                and row.revoked_at is None
                                and expires_at_is_past(row.expires_at)
                            ):
                                q = "?reason=expired"
                        except (TypeError, ValueError):
                            pass
                    return RedirectResponse(
                        append_fbm_app_to_url(f"/auth/unlock{q}", request),
                        status_code=303,
                    )
            finally:
                db.close()

        user = request.session.get("user")
        if not user and fb_master_no_auth():
            db = SessionLocal()
            try:
                ensure_no_auth_session_user(request, db)
                user = request.session.get("user")
            finally:
                db.close()

        # Платный ключ в сессии → оператор без OAuth (в т.ч. VPS без AK_REQUIRED, где /auth/* иначе обходили эту логику)
        if not user:
            db = SessionLocal()
            try:
                if session_access_valid(db, request):
                    ensure_no_auth_session_user(request, db)
                    user = request.session.get("user")
            finally:
                db.close()

        if not ak_required and path.startswith("/auth"):
            return await call_next(request)

        if not user:
            if (
                path.startswith("/auth")
                or path.startswith("/billing/nowpayments/")
                or path in ("/", "/buy", "/purchase", "/promo", "/promo2", "/landing", "/download/dev")
                or (path == "/webhooks/nowpayments" and request.method == "POST")
            ):
                return await call_next(request)
            entry = (
                "/auth/unlock"
                if app_config.fb_master_web_cabinet_email_login_disabled()
                else "/auth/login"
            )
            return RedirectResponse(append_fbm_app_to_url(entry, request), status_code=303)

        db = SessionLocal()
        try:
            org_id = effective_organization_id(request, db, int(user["id"]))
            from backend.services.client_presence import maybe_record_client_presence

            maybe_record_client_presence(
                db,
                request,
                admin_user_id=int(user["id"]),
                organization_id=org_id,
            )
        finally:
            db.close()
        return await call_next(request)

    # SessionMiddleware добавляется ПОСЛЕ auth_guard, чтобы быть внешним (LIFO)
    app.add_middleware(
        SessionMiddleware,
        secret_key=SECRET_KEY,
        max_age=86400 * 7,
        same_site="lax",
        https_only=SESSION_COOKIE_SECURE,
    )

    class _PersistFbmAppCookieMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            from backend.services.fbm_client_version import set_fbm_app_cookie_on_response

            response = await call_next(request)
            set_fbm_app_cookie_on_response(request, response)
            return response

    app.add_middleware(_PersistFbmAppCookieMiddleware)

    from backend.routers import (
        account_branding,
        account_natural_warmup,
        ai_agent,
        auth,
        billing_nowpayments,
        contacted,
        crm_funnel,
        dashboard,
        desktop_license_public,
        desktop_public,
        donors,
        release_download,
        faq,
        fb_accounts,
        import_base,
        message_templates,
        messenger,
        messenger2,
        outreach,
        parser,
        people,
        placeholders,
        platform_admin,
        promo,
        scenarios,
        system,
        warmup,
        web_cabinet_gate,
    )

    app.include_router(promo.router)
    app.include_router(web_cabinet_gate.router)
    app.include_router(auth.router)
    app.include_router(release_download.router)
    app.include_router(billing_nowpayments.router)
    app.include_router(billing_nowpayments.webhook_router)
    app.include_router(dashboard.router)
    app.include_router(desktop_public.router)
    app.include_router(desktop_license_public.router)
    app.include_router(faq.router)
    app.include_router(donors.router)
    app.include_router(parser.router)
    app.include_router(people.router)
    app.include_router(import_base.router)
    app.include_router(fb_accounts.router)
    app.include_router(account_branding.router)
    app.include_router(account_natural_warmup.router)
    app.include_router(message_templates.router)
    app.include_router(warmup.router)
    app.include_router(scenarios.router)
    app.include_router(outreach.router)
    app.include_router(contacted.router)
    app.include_router(crm_funnel.router)
    app.include_router(messenger.router)
    app.include_router(messenger2.router)
    app.include_router(ai_agent.router)
    app.include_router(placeholders.router)
    app.include_router(platform_admin.router)
    app.include_router(system.router)

    return app
