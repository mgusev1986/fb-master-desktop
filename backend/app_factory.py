"""Фабрика FastAPI-приложения SOCMASTER."""

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

    # 3.0+: License watcher (только на десктопе клиента). Проверяет ключ
    # на VPS каждые 30 сек, защищён Ed25519-подписями + DRP probes.
    # На VPS — автоматический no-op (есть приватный ключ).
    try:
        from backend.services.license_watcher import start_watcher as _start_license_watcher
        _start_license_watcher()
    except Exception:
        logger.exception("license_watcher: failed to start (non-fatal)")

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
    try:
        from backend.modules.reddit.services.inbox_poller import start_reddit_inbox_poller_thread

        start_reddit_inbox_poller_thread()
    except Exception:
        logger.exception("start_reddit_inbox_poller_thread")
    try:
        from backend.core.feature_flags import (
            reddit_browser_profile_enabled,
            reddit_module_enabled,
        )

        if reddit_module_enabled() and reddit_browser_profile_enabled():
            from backend.modules.reddit.runtime.outreach_worker import (
                start_outreach_worker_thread as start_reddit_browser_outreach,
            )

            start_reddit_browser_outreach()
    except Exception:
        logger.exception("start_reddit_browser_outreach_worker")
    try:
        from backend.core.feature_flags import linkedin_module_enabled

        if linkedin_module_enabled():
            from backend.modules.linkedin.runtime.outreach_worker import (
                start_outreach_worker_thread,
            )
            from backend.modules.linkedin.runtime.sequence_worker import (
                start_sequence_worker_thread,
            )

            start_outreach_worker_thread()
            start_sequence_worker_thread()
    except Exception:
        logger.exception("start_linkedin_outreach_worker_thread")
    try:
        from backend.core.feature_flags import (
            twitter_browser_profile_enabled,
            twitter_module_enabled,
        )

        if twitter_module_enabled() and twitter_browser_profile_enabled():
            from backend.modules.twitter.runtime.outreach_worker import (
                start_outreach_worker_thread as start_twitter_outreach,
            )
            start_twitter_outreach()
            # AI autoresponder worker — стартанёт при наличии (M3.2).
            try:
                from backend.modules.twitter.runtime.autoresponder_worker import (
                    start_autoresponder_worker_thread,
                )
                start_autoresponder_worker_thread()
            except Exception:  # noqa: BLE001
                logger.exception("start_twitter_autoresponder_worker (M3.2)")
    except Exception:
        logger.exception("start_twitter_outreach_worker")
    try:
        from backend.core.feature_flags import (
            instagram_browser_profile_enabled,
            instagram_module_enabled,
        )

        if instagram_module_enabled() and instagram_browser_profile_enabled():
            from backend.modules.instagram.runtime.outreach_worker import (
                start_outreach_worker_thread as start_instagram_outreach,
            )
            from backend.modules.instagram.runtime.engagement_worker import (
                start_engagement_worker_thread as start_instagram_engagement,
            )
            start_instagram_outreach()
            start_instagram_engagement()
            try:
                from backend.modules.instagram.runtime.autoresponder_worker import (
                    start_autoresponder_worker_thread as start_instagram_autoresp,
                )
                start_instagram_autoresp()
            except Exception:  # noqa: BLE001
                logger.exception("start_instagram_autoresponder_worker (M3.2)")
    except Exception:
        logger.exception("start_instagram_workers")
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
        title="SOCMASTER",
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
    # 2.96+: stealth_region_choices доступен в шаблонах IG/X для селекта региона
    # на форме «Свой личный (логин+пароль)» (как в FB Master).
    from backend.services.fb_stealth_profile import stealth_region_choices as _stealth_region_choices
    templates.env.globals["stealth_region_choices"] = _stealth_region_choices
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

    # ── Multi-workspace shell (dormant при FB_MASTER_MULTI_WORKSPACE_ENABLED=0) ──
    from backend.core.feature_flags import multi_workspace_enabled
    from backend.core.workspace.launcher_view import build_switcher_view
    from backend.core.workspace.session import get_current_workspace_id

    def fbm_multi_workspace_enabled_fn() -> bool:
        return multi_workspace_enabled()

    def fbm_available_workspaces(request: Request):
        if not multi_workspace_enabled():
            return None
        # v3.0.5+: клиентам показываем только Facebook как активный, остальные
        # модули — disabled (build_switcher_view ставит .openable=False).
        return build_switcher_view(
            get_current_workspace_id(request),
            is_client_mode=cabinet_client_mode(request),
        )

    def fbm_current_workspace_id(request: Request) -> str | None:
        if not multi_workspace_enabled():
            return None
        return get_current_workspace_id(request)

    # Переменные вида "fbm_*" — вызываемые из Jinja; базовый шаблон
    # читает их через {% if fbm_multi_workspace_enabled %} и т.д.
    # Чтобы base.html мог ссылаться на них как на переменные, а не только
    # функции, предоставляем оба варианта.
    templates.env.globals["fbm_multi_workspace_enabled"] = multi_workspace_enabled()
    templates.env.globals["fbm_available_workspaces_for"] = fbm_available_workspaces
    templates.env.globals["fbm_current_workspace_id_for"] = fbm_current_workspace_id

    def fbm_workspace_nav(module_id: str):
        """Navigation manifest модуля для рендера sidebar в per-workspace шаблонах."""
        from backend.core.modules import module_registry as _registry

        mod = _registry.get(module_id)
        if mod is None:
            return None
        try:
            return mod.navigation()
        except Exception:
            return None

    templates.env.globals["fbm_workspace_nav"] = fbm_workspace_nav

    # ─── i18n: Jinja2 global `t()` для перевода UI-строк ──────────────────
    # Использование в шаблонах::
    #     {{ t("Главная") }}
    #     {{ t("Привет, {name}", name=user.name) }}
    # Язык берётся из `request` (query ?lang=en или cookie `lang`).
    # Словарь переводов: `backend/services/i18n_dict.py::TRANSLATIONS["en"]`.
    # Если перевода нет — возвращается оригинал (RU) — graceful fallback.
    from jinja2 import pass_context as _jinja_pass_context
    from backend.services.i18n import get_lang as _get_lang, translate as _translate

    @_jinja_pass_context
    def _jinja_t(ctx, key: str, **kwargs):
        request = ctx.get("request")
        lang = _get_lang(request) if request is not None else "ru"
        return _translate(key, lang, **kwargs)

    templates.env.globals["t"] = _jinja_t

    # Cookie persistence для языка: при ?lang=en/ru сохраняем выбор на 1 год,
    # чтобы пользователю не приходилось каждый раз кликать переключатель.
    # Cookie читается helper'ом get_lang() (services/i18n.py).
    @app.middleware("http")
    async def _i18n_cookie_middleware(request, call_next):
        response = await call_next(request)
        try:
            qlang = (request.query_params.get("lang") or "").strip().lower()
            if qlang in ("ru", "en"):
                # Перезаписываем только если query param действительно был
                response.set_cookie(
                    key="lang",
                    value=qlang,
                    max_age=60 * 60 * 24 * 365,  # 1 год
                    httponly=False,  # JS может читать (на будущее для UI)
                    samesite="lax",
                    path="/",
                )
        except Exception:  # noqa: BLE001
            # Безопасный fallback — не ломаем response
            pass
        return response

    # Reddit Master — локализация технических статусов/enum'ов для Jinja.
    _REDDIT_RU_LABELS = {
        "connected": "Подключён", "disconnected": "Отключён",
        "needs_reauth": "Нужен повторный вход", "limited": "Ограничен",
        "restricted": "Ограничен", "cooldown": "Пауза",
        "pending_review": "Ожидает подтверждения", "approved": "Одобрено",
        "sent": "Отправлено", "published": "Опубликовано", "failed": "Ошибка",
        "rejected": "Отклонено", "skipped": "Пропущено",
        "draft": "Черновик", "running": "Выполняется", "active": "Активно",
        "paused": "Пауза", "archived": "Архив", "finished": "Завершено",
        "cancelled": "Отменено",
        "scheduled": "Запланировано", "waiting": "Ожидает",
        "awaiting_approval": "Ждёт подтверждения", "completed": "Завершено",
        "new": "Новый", "contacted": "Связались", "replied": "Ответил",
        "blocked": "Заблокирован", "dnd": "Не беспокоить",
        "ok": "Успех", "pending": "Ожидает", "rate_limited": "Лимит превышен",
        "dm": "ЛС", "comment": "Комментарий", "chat_opener": "Открытие чата",
        "follow_up": "Повторное касание",
        "post": "Пост",
        "manual": "Ручной", "semi": "Полу-ручной", "auto": "Авто",
    }

    def reddit_ru_status(value):
        if value is None:
            return ""
        key = str(value).strip().lower()
        return _REDDIT_RU_LABELS.get(key, str(value))

    templates.env.filters["ru_status"] = reddit_ru_status

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
        # 3.0+: атомарная проверка валидности ключа с подписями Ed25519.
        if request.method == "POST" and path == "/api/public/desktop-license/check":
            return await call_next(request)
        if request.method == "POST" and path == "/api/public/client-presence":
            return await call_next(request)
        if request.method == "POST" and path == "/api/public/promo-chat":
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

        # LavaTop: оплата и вебхук без ключа и без входа в кабинет
        if path == "/webhooks/lavatop" and request.method == "POST":
            return await call_next(request)
        if path.startswith("/billing/lavatop/"):
            return await call_next(request)
        if request.method == "GET" and path in (
            "/",
            "/buy",
            "/purchase",
            "/promo",
            "/promo2",
            "/promo3",
            "/landing",
            "/download/dev",
            "/blog",
            "/robots.txt",
            "/sitemap.xml",
            "/favicon.ico",
        ):
            return await call_next(request)

        # Blog post pages (/blog/<slug>) и категории/теги (/blog/category/<slug>, /blog/tag/<slug>)
        if request.method == "GET" and path.startswith("/blog/"):
            return await call_next(request)

        ak_required = fb_master_access_key_required()
        auth_without_user = path == "/auth/unlock" or path == "/auth/access-key/activate"

        # 3.0+: License watcher (DRP + Ed25519). Если на этой машине активен
        # watcher и он сказал invalid/blocked — редиректим на /auth/unlock.
        # Whitelist путей — пропускаем БЕЗ редиректа (иначе redirect-loop):
        #   /auth/* — страница ввода ключа сама должна работать.
        #   /static/* — CSS/JS/шрифты.
        #   /api/public/* — публичные API (включая /desktop-license/*).
        #   /internal/* — loopback от Electron.
        #   /webhooks/*, /billing/*, /download/* — публичные внешние интеграции.
        #   /, /buy, /promo, /promo2, /landing — публичные страницы.
        _LICENSE_WATCHER_WHITELIST_PREFIXES = (
            "/auth/", "/static/", "/api/public/", "/internal/",
            "/webhooks/", "/billing/", "/download/", "/blog/",
        )
        _LICENSE_WATCHER_WHITELIST_EXACT = (
            "/", "/buy", "/purchase", "/promo", "/promo2", "/promo3", "/landing",
            "/blog", "/robots.txt", "/sitemap.xml", "/favicon.ico",
        )
        if (
            not path.startswith(_LICENSE_WATCHER_WHITELIST_PREFIXES)
            and path not in _LICENSE_WATCHER_WHITELIST_EXACT
        ):
            try:
                from backend.services.license_watcher import (
                    block_reason as _lic_reason,
                    is_active_on_this_machine as _lic_active,
                    is_valid as _lic_valid,
                )
                if _lic_active() and not _lic_valid():
                    reason = _lic_reason() or "invalid"
                    # state=init/checking — первая проверка watcher ещё в полёте,
                    # не блокируем (даём 5-30 сек на установку).
                    if reason in ("init", "checking"):
                        return await call_next(request)
                    return RedirectResponse(
                        append_fbm_app_to_url(f"/auth/unlock?reason={reason}", request),
                        status_code=303,
                    )
            except Exception:
                logger.exception("license_watcher: middleware check failed (non-fatal)")

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
                or path.startswith("/billing/lavatop/")
                or path.startswith("/blog/")
                or path in (
                    "/", "/buy", "/purchase", "/promo", "/promo2", "/promo3",
                    "/landing", "/download/dev",
                    "/blog", "/robots.txt", "/sitemap.xml", "/favicon.ico",
                )
                or (path == "/webhooks/nowpayments" and request.method == "POST")
                or (path == "/webhooks/lavatop" and request.method == "POST")
                or (path == "/api/public/promo-chat" and request.method == "POST")
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
        billing_lavatop,
        billing_nowpayments,
        blog,
        contacted,
        crm_funnel,
        dashboard,
        desktop_license_public,
        desktop_public,
        discovery,
        donors,
        release_download,
        faq,
        fb_accounts,
        import_base,
        license_internal,
        message_templates,
        messenger,
        messenger2,
        outreach,
        parser,
        people,
        placeholders,
        platform_admin,
        promo,
        promo_chat,
        scenarios,
        seo,
        system,
        warmup,
        web_cabinet_gate,
        workspaces,
    )

    app.include_router(promo.router)
    app.include_router(promo_chat.router)
    app.include_router(web_cabinet_gate.router)
    app.include_router(auth.router)
    app.include_router(release_download.router)
    app.include_router(billing_nowpayments.router)
    app.include_router(billing_nowpayments.webhook_router)
    app.include_router(billing_lavatop.router)
    app.include_router(billing_lavatop.webhook_router)
    app.include_router(blog.router)
    app.include_router(seo.router)
    app.include_router(dashboard.router)
    app.include_router(desktop_public.router)
    app.include_router(desktop_license_public.router)
    app.include_router(license_internal.router)
    app.include_router(faq.router)
    app.include_router(donors.router)
    app.include_router(discovery.router)
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
    app.include_router(workspaces.router)

    # ── Multi-workspace: регистрация модулей в реестре ──
    # Регистрация выполняется всегда, даже при флаге OFF — сами роуты
    # /workspaces* при флаге OFF возвращают 404, но реестр нужен для будущей
    # композиции и тестов. Seal() после регистрации блокирует поздние add'ы.
    try:
        from backend.core.feature_flags import (
            instagram_module_enabled,
            linkedin_module_enabled,
            reddit_module_enabled,
            twitter_module_enabled,
        )
        from backend.core.modules import module_registry
        from backend.modules.facebook import facebook_module

        if "facebook" not in module_registry:
            module_registry.register(facebook_module)

        if reddit_module_enabled():
            # M4+: полный RedditModule с роутерами.
            from backend.modules.reddit.module import reddit_module
            from backend.modules.reddit.router import router as reddit_placeholder_router
            from backend.modules.reddit.routers.accounts import router as reddit_accounts_router
            from backend.modules.reddit.routers.activity import router as reddit_activity_router
            from backend.modules.reddit.routers.analytics import router as reddit_analytics_router
            from backend.modules.reddit.routers.audience import router as reddit_audience_router
            from backend.modules.reddit.routers.campaigns import router as reddit_campaigns_router
            from backend.modules.reddit.routers.compliance import router as reddit_compliance_router
            from backend.modules.reddit.routers.comments import router as reddit_comments_router
            from backend.modules.reddit.routers.conversations import (
                api_router as reddit_conversations_api_router,
                router as reddit_conversations_router,
            )
            from backend.modules.reddit.routers.leads import router as reddit_leads_router
            from backend.modules.reddit.routers.oauth_callback import router as reddit_oauth_router
            from backend.modules.reddit.routers.sequences import router as reddit_sequences_router
            from backend.modules.reddit.routers.settings import router as reddit_settings_router
            from backend.modules.reddit.routers.subreddits import router as reddit_subreddits_router
            from backend.modules.reddit.routers.templates import router as reddit_templates_router

            if "reddit" not in module_registry:
                module_registry.register(reddit_module)
            # Порядок важен: специализированные роутеры подключаются ДО placeholder,
            # чтобы реальные /reddit/accounts и /reddit/settings перекрывали заглушки.
            app.include_router(reddit_accounts_router)
            app.include_router(reddit_settings_router)
            app.include_router(reddit_oauth_router)
            app.include_router(reddit_subreddits_router)
            app.include_router(reddit_audience_router)
            app.include_router(reddit_activity_router)
            app.include_router(reddit_leads_router)
            app.include_router(reddit_templates_router)
            app.include_router(reddit_campaigns_router)
            app.include_router(reddit_conversations_api_router)
            app.include_router(reddit_conversations_router)
            app.include_router(reddit_comments_router)
            app.include_router(reddit_analytics_router)
            app.include_router(reddit_compliance_router)
            app.include_router(reddit_sequences_router)
            app.include_router(reddit_placeholder_router)
        else:
            # При выключенном модуле — только stub-карточка в launcher.
            from backend.modules.reddit import reddit_coming_soon_module
            if "reddit" not in module_registry:
                module_registry.register(reddit_coming_soon_module)

        # ── LinkedIn Master (Beta) ──
        if linkedin_module_enabled():
            from backend.modules.linkedin.module import linkedin_module
            from backend.modules.linkedin.router import router as linkedin_placeholder_router
            from backend.modules.linkedin.routers.accounts import (
                api_router as linkedin_accounts_api_router,
                router as linkedin_accounts_router,
            )
            from backend.modules.linkedin.routers.compliance import (
                router as linkedin_compliance_router,
            )
            from backend.modules.linkedin.routers.conversations import (
                router as linkedin_conversations_router,
            )
            from backend.modules.linkedin.routers.leads import router as linkedin_leads_router
            from backend.modules.linkedin.routers.outreach import (
                router as linkedin_outreach_router,
            )
            from backend.modules.linkedin.routers.sequences import (
                router as linkedin_sequences_router,
            )
            from backend.modules.linkedin.routers.templates import (
                router as linkedin_templates_router,
            )

            if "linkedin" not in module_registry:
                module_registry.register(linkedin_module)
            # Порядок важен: специализированные роутеры — ДО placeholder,
            # чтобы реальные /linkedin/accounts и т.д. перекрывали placeholder.
            app.include_router(linkedin_accounts_router)
            app.include_router(linkedin_accounts_api_router)
            app.include_router(linkedin_leads_router)
            app.include_router(linkedin_templates_router)
            app.include_router(linkedin_outreach_router)
            app.include_router(linkedin_sequences_router)
            app.include_router(linkedin_conversations_router)
            app.include_router(linkedin_compliance_router)
            app.include_router(linkedin_placeholder_router)
        else:
            from backend.modules.linkedin import linkedin_coming_soon_module

            if "linkedin" not in module_registry:
                module_registry.register(linkedin_coming_soon_module)

        # ── Twitter / X Master (Beta) ──
        if twitter_module_enabled():
            from backend.modules.twitter.module import twitter_module
            from backend.modules.twitter.router import router as twitter_placeholder_router
            from backend.modules.twitter.routers.accounts import (
                api_router as twitter_accounts_api_router,
                router as twitter_accounts_router,
            )
            from backend.modules.twitter.routers.compliance import (
                router as twitter_compliance_router,
            )
            from backend.modules.twitter.routers.conversations import (
                router as twitter_conversations_router,
            )
            from backend.modules.twitter.routers.leads import router as twitter_leads_router
            from backend.modules.twitter.routers.outreach import (
                router as twitter_outreach_router,
            )
            from backend.modules.twitter.routers.templates import (
                router as twitter_templates_router,
            )

            if "twitter" not in module_registry:
                module_registry.register(twitter_module)
            app.include_router(twitter_accounts_router)
            app.include_router(twitter_accounts_api_router)
            app.include_router(twitter_leads_router)
            app.include_router(twitter_templates_router)
            app.include_router(twitter_outreach_router)
            app.include_router(twitter_conversations_router)
            app.include_router(twitter_compliance_router)
            # AI-autoresponder router (Stage M3.2 — добавлен в M3).
            try:
                from backend.modules.twitter.routers.autoresponder import (
                    router as twitter_autoresponder_router,
                )
                app.include_router(twitter_autoresponder_router)
            except Exception:  # noqa: BLE001
                logger.exception("twitter autoresponder router import failed")
            app.include_router(twitter_placeholder_router)
        else:
            from backend.modules.twitter import twitter_coming_soon_module

            if "twitter" not in module_registry:
                module_registry.register(twitter_coming_soon_module)

        # ── Instagram Master (Beta) ──
        if instagram_module_enabled():
            from backend.modules.instagram.module import instagram_module
            from backend.modules.instagram.router import router as instagram_placeholder_router
            from backend.modules.instagram.routers.accounts import (
                api_router as instagram_accounts_api_router,
                router as instagram_accounts_router,
            )
            from backend.modules.instagram.routers.compliance import (
                router as instagram_compliance_router,
            )
            from backend.modules.instagram.routers.competitor_audience import (
                router as instagram_competitor_router,
            )
            from backend.modules.instagram.routers.conversations import (
                router as instagram_conversations_router,
            )
            from backend.modules.instagram.routers.engagement import (
                router as instagram_engagement_router,
            )
            from backend.modules.instagram.routers.leads import (
                audience_router as instagram_audience_router,
                router as instagram_leads_router,
            )
            from backend.modules.instagram.routers.outreach import (
                router as instagram_outreach_router,
            )
            from backend.modules.instagram.routers.templates import (
                router as instagram_templates_router,
            )

            if "instagram" not in module_registry:
                module_registry.register(instagram_module)
            app.include_router(instagram_accounts_router)
            app.include_router(instagram_accounts_api_router)
            app.include_router(instagram_leads_router)
            app.include_router(instagram_audience_router)
            app.include_router(instagram_competitor_router)
            app.include_router(instagram_templates_router)
            app.include_router(instagram_outreach_router)
            app.include_router(instagram_conversations_router)
            app.include_router(instagram_engagement_router)
            app.include_router(instagram_compliance_router)
            try:
                from backend.modules.instagram.routers.autoresponder import (
                    router as instagram_autoresponder_router,
                )
                app.include_router(instagram_autoresponder_router)
            except Exception:  # noqa: BLE001
                logger.exception("instagram autoresponder router import failed")
            app.include_router(instagram_placeholder_router)
        else:
            from backend.modules.instagram import instagram_coming_soon_module

            if "instagram" not in module_registry:
                module_registry.register(instagram_coming_soon_module)

        module_registry.seal()
    except Exception:
        logger.exception("module_registry bootstrap failed")

    return app
