"""Вход в FB Master: ключ доступа; опционально Google/Supabase/Developer, если не отключены в .env."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.config import (
    FB_MASTER_LICENSE_API_BASE,
    dev_login_allowed,
    fb_master_access_key_required,
    fb_master_no_auth,
)
from backend.database import get_db
from backend.models import AdminUser
from backend.services.desktop_client import user_agent_is_fb_master_desktop
from backend.services.desktop_device import effective_device_fingerprint
from backend.services.i18n import get_lang, select_template
from backend.services.fbm_client_version import (
    append_fbm_app_to_url,
    cabinet_web_auth_redirect_path,
    fbm_app_value_raw,
)
from backend.services.web_cabinet_gate import WEB_CABINET_GATE_SESSION_KEY
from backend.services.cabinet_settings import (
    effective_allowed_google_emails,
    effective_google_client_id,
    effective_google_client_secret,
    effective_supabase_anon_key,
    effective_supabase_url,
    oauth_callback_url_for_request,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

oauth = OAuth()


def _unlock_show_key_form(request: Request) -> bool:
    """
    Форма ключа на /auth/unlock: при FB_MASTER_ACCESS_KEY_REQUIRED или при запросе из Electron
    (User-Agent … FBMasterDesktop… / query fbm_app). Иначе на публичном VPS показывается
    лендинг «скачайте приложение» — без этого клиент с удалённым URL не видит поле ключа.
    """
    if fb_master_access_key_required():
        return True
    ua = (request.headers.get("user-agent") or "")
    if user_agent_is_fb_master_desktop(ua):
        return True
    return bool((fbm_app_value_raw(request) or "").strip())


def _unlock_from_fb_master_desktop(request: Request) -> bool:
    """True — окно открыто внутри Electron (не браузер клиента на сайте): только ключ, без NOWPayments."""
    ua = request.headers.get("user-agent") or ""
    if user_agent_is_fb_master_desktop(ua):
        return True
    return bool((fbm_app_value_raw(request) or "").strip())


def _ensure_google_oauth_client(db: Session) -> bool:
    """Регистрация клиента Google OAuth с учётом override из кабинета."""
    cid = effective_google_client_id(db)
    sec = effective_google_client_secret(db)
    if not cid.strip():
        return False
    try:
        oauth._clients.pop("google", None)  # type: ignore[attr-defined]
    except Exception:
        pass
    oauth.register(
        name="google",
        client_id=cid,
        client_secret=sec or "",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return bool(sec.strip())


async def _establish_cabinet_session_from_supabase_token(
    request: Request, db: Session, access_token: str
) -> str | None:
    """
    Проверяет access_token через Supabase /user и создаёт сессию кабинета.
    Возвращает None при успехе, иначе код ошибки для ?error=.
    """
    from backend.services.supabase_auth import fetch_supabase_user

    su = await fetch_supabase_user(db, access_token)
    if not su:
        return "supabase_token_invalid"

    email = (su.get("email") or "").strip().lower()
    if not email:
        return "supabase_no_email"

    allowed = effective_allowed_google_emails(db)
    if allowed and email not in allowed:
        logger.warning("Supabase access denied for %s", email)
        return "access_denied"

    meta = su.get("user_metadata") if isinstance(su.get("user_metadata"), dict) else {}
    name = str(meta.get("full_name") or meta.get("name") or "").strip() or ""
    picture = str(meta.get("avatar_url") or meta.get("picture") or "").strip() or ""

    admin = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not admin:
        admin = AdminUser(email=email, name=name or None, picture_url=picture or None)
        db.add(admin)
    else:
        if name:
            admin.name = name
        if picture:
            admin.picture_url = picture
    admin.last_login_at = datetime.now(timezone.utc)
    db.commit()

    request.session["user"] = {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name,
        "picture": admin.picture_url,
    }
    from backend.services.tenancy import effective_organization_id

    effective_organization_id(request, db, admin.id)
    logger.info("Supabase login: %s", email)
    return None


@router.get("/unlock")
async def access_key_unlock_page(request: Request, db: Session = Depends(get_db)):
    templates = request.app.state.templates
    err = (request.query_params.get("error") or "").strip()
    if err == "web_login_disabled":
        err = "Вход через Google и email отключён. Введите оплаченный ключ доступа ниже."
    reason = (request.query_params.get("reason") or "").strip()
    ak_req = fb_master_access_key_required()
    if ak_req:
        from backend.services.access_keys import session_access_valid
        from backend.services.local_operator_session import ensure_no_auth_session_user

        # 3.0+: если license_watcher активен и сказал invalid/blocked,
        # не редиректим на /home — показываем форму ввода нового ключа.
        # Иначе redirect-loop: handler редирект на /home, middleware
        # редирект назад на /auth/unlock.
        _watcher_blocks = False
        try:
            from backend.services.license_watcher import (
                is_active_on_this_machine as _lic_active,
                is_valid as _lic_valid,
            )
            if _lic_active() and not _lic_valid():
                _watcher_blocks = True
        except Exception:
            pass

        if not _watcher_blocks and session_access_valid(db, request):
            if not request.session.get("user"):
                ensure_no_auth_session_user(request, db)
            if request.session.get("user"):
                return RedirectResponse(append_fbm_app_to_url("/home", request), status_code=303)
    show_key = _unlock_show_key_form(request)
    # После успешного ключа редирект на /home (кабинет); корень / — публичный лендинг.
    return templates.TemplateResponse(
        select_template(request, "auth/unlock.html"),
        {
            "request": request,
            "lang": get_lang(request),
            "access_key_required": ak_req,
            "show_unlock_key_form": show_key,
            "buy_url": app_config.marketing_buy_page_url(),
            "unlock_app_download_href": app_config.unlock_page_desktop_download_href(),
            "unlock_app_download_hrefs": app_config.unlock_page_desktop_download_hrefs(),
            "unlock_buy_fallback": app_config.marketing_buy_page_url(),
            "purchase_url": app_config.marketing_purchase_page_url(),
            "unlock_error": err,
            "unlock_reason": reason,
            "unlock_desktop_app": _unlock_from_fb_master_desktop(request),
            "nowpayments_enabled": (
                app_config.nowpayments_enabled()
                if show_key and not _unlock_from_fb_master_desktop(request)
                else False
            ),
            "np_price_30": app_config.NOWPAYMENTS_PRICE_USD_30,
            "np_price_90": app_config.NOWPAYMENTS_PRICE_USD_90,
            "np_price_180": app_config.NOWPAYMENTS_PRICE_USD_180,
            "np_price_365": app_config.NOWPAYMENTS_PRICE_USD_365,
            "np_price_test": app_config.NOWPAYMENTS_PRICE_USD_TEST,
            "np_test_option_label": app_config.nowpayments_test_tariff_option_label(),
            "np_test_enabled": app_config.nowpayments_test_tariff_enabled(),
        },
    )


@router.post("/access-key/activate")
async def access_key_activate(
    request: Request,
    db: Session = Depends(get_db),
    key: str = Form(""),
    device_id: str = Form(""),
):
    if not _unlock_show_key_form(request):
        return RedirectResponse("/auth/unlock", status_code=303)
    from urllib.parse import quote

    from backend.services.access_keys import mirror_access_key_locally, try_activate

    fp = effective_device_fingerprint(request, device_id)
    remote = (FB_MASTER_LICENSE_API_BASE or "").strip().rstrip("/")
    if remote:
        import httpx
        from datetime import datetime

        try:
            with httpx.Client(timeout=45.0) as client:
                r = client.post(
                    f"{remote}/api/public/desktop-license/activate",
                    json={"key": key.strip(), "device_fingerprint": fp},
                )
        except httpx.RequestError:
            return RedirectResponse(
                "/auth/unlock?error=" + quote("Нет связи с сервером лицензий. Проверьте интернет."),
                status_code=303,
            )
        if r.status_code != 200:
            try:
                body = r.json()
                err_remote = (body.get("error") or body.get("detail") or "Ошибка активации") if isinstance(body, dict) else r.text
            except Exception:
                err_remote = r.text or "Ошибка активации"
            return RedirectResponse("/auth/unlock?error=" + quote(str(err_remote)[:300]), status_code=303)
        try:
            payload = r.json()
        except Exception:
            return RedirectResponse("/auth/unlock?error=" + quote("Некорректный ответ сервера лицензий."), status_code=303)
        exp = None
        exp_raw = payload.get("expires_at") if isinstance(payload, dict) else None
        if exp_raw:
            try:
                exp = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    from datetime import timezone as _tz

                    exp = exp.replace(tzinfo=_tz.utc)
            except Exception:
                exp = None
        mirror_access_key_locally(
            db, plaintext=key, device_fingerprint=fp, expires_at=exp
        )
    ok, err_msg = try_activate(db, request, plaintext=key, device_fingerprint=fp)
    if not ok:
        return RedirectResponse("/auth/unlock?error=" + quote(err_msg[:300]), status_code=303)
    # 3.0+: синхронно дёргаем license_watcher, чтобы он обновил state на
    # 'valid' ПЕРЕД редиректом на /home. Иначе middleware с state=invalid
    # моментально завернёт обратно на /auth/unlock.
    try:
        from backend.services.license_watcher import force_check_now, is_active_on_this_machine
        if is_active_on_this_machine():
            await force_check_now()
    except Exception:
        logger.exception("license_watcher.force_check_now after activate (non-fatal)")
    return RedirectResponse("/home", status_code=303)


@router.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(append_fbm_app_to_url("/auth/unlock", request), status_code=303)
    if fb_master_no_auth() and not fb_master_access_key_required():
        return RedirectResponse("/home", status_code=303)
    user = request.session.get("user")
    if user:
        return RedirectResponse("/home", status_code=303)

    from backend.services.access_keys import session_access_valid
    from backend.services.local_operator_session import ensure_no_auth_session_user

    if session_access_valid(db, request):
        ensure_no_auth_session_user(request, db)
        return RedirectResponse("/home", status_code=303)

    templates = request.app.state.templates
    oauth_configured = bool(effective_google_client_id(db).strip())
    callback_url = oauth_callback_url_for_request(request, db)
    from backend.services.supabase_auth import supabase_auth_configured

    supabase_ok = supabase_auth_configured(db)
    base = str(request.base_url).rstrip("/")
    supabase_callback_full_url = f"{base}/auth/supabase/callback"
    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "oauth_configured": oauth_configured,
            "oauth_callback_url": callback_url,
            "supabase_auth_configured": supabase_ok,
            "supabase_callback_full_url": supabase_callback_full_url,
            "dev_login_allowed": dev_login_allowed(),
            "fbm_client_version": fbm_app_value_raw(request),
        },
    )


@router.get("/google")
async def google_login(request: Request, db: Session = Depends(get_db)):
    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="web_login_disabled"), status_code=303)
    if not effective_google_client_id(db).strip():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="oauth_not_configured"), status_code=303)
    if not _ensure_google_oauth_client(db):
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="oauth_not_configured"), status_code=303)
    redirect_uri = oauth_callback_url_for_request(request, db)
    logger.debug("OAuth redirect_uri=%s", redirect_uri)
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="web_login_disabled"), status_code=303)
    if not effective_google_client_id(db).strip():
        return RedirectResponse(cabinet_web_auth_redirect_path(request), status_code=303)
    if not _ensure_google_oauth_client(db):
        return RedirectResponse(cabinet_web_auth_redirect_path(request), status_code=303)

    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        logger.exception("OAuth callback error")
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="oauth_failed"), status_code=303)

    userinfo = token.get("userinfo", {})
    email = (userinfo.get("email") or "").strip().lower()
    name = userinfo.get("name", "")
    picture = userinfo.get("picture", "")

    if not email:
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="no_email"), status_code=303)

    allowed = effective_allowed_google_emails(db)
    if allowed and email not in allowed:
        logger.warning("Access denied for %s", email)
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="access_denied"), status_code=303)

    admin = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not admin:
        admin = AdminUser(email=email, name=name, picture_url=picture)
        db.add(admin)
    else:
        admin.name = name or admin.name
        admin.picture_url = picture or admin.picture_url
    admin.last_login_at = datetime.now(timezone.utc)
    db.commit()

    request.session["user"] = {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name,
        "picture": admin.picture_url,
    }
    from backend.services.tenancy import effective_organization_id

    effective_organization_id(request, db, admin.id)
    logger.info("Login: %s", email)
    return RedirectResponse("/home", status_code=303)


@router.post("/supabase/password")
async def supabase_password_login(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(""),
    password: str = Form(""),
):
    from backend.services.supabase_auth import sign_in_with_password, supabase_auth_configured

    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="web_login_disabled"), status_code=303)
    if not supabase_auth_configured(db):
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="supabase_not_configured"), status_code=303)

    token, err = await sign_in_with_password(db, email=email, password=password)
    if err or not token:
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="supabase_password_failed"), status_code=303)

    code = await _establish_cabinet_session_from_supabase_token(request, db, token)
    if code:
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error=code), status_code=303)
    return RedirectResponse("/home", status_code=303)


@router.post("/supabase/session")
async def supabase_session_from_token(request: Request, db: Session = Depends(get_db)):
    """После OAuth PKCE на клиенте: обмен кода → access_token → сессия кабинета."""
    from backend.services.supabase_auth import supabase_auth_configured

    if app_config.fb_master_web_cabinet_email_login_disabled():
        return JSONResponse({"ok": False, "error": "web_login_disabled"}, status_code=403)
    if not supabase_auth_configured(db):
        return JSONResponse({"ok": False, "error": "supabase_not_configured"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "supabase_bad_payload"}, status_code=400)

    token = (body.get("access_token") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "supabase_no_token"}, status_code=400)

    code = await _establish_cabinet_session_from_supabase_token(request, db, token)
    if code:
        return JSONResponse({"ok": False, "error": code}, status_code=401)
    return JSONResponse({"ok": True})


@router.get("/supabase/callback")
async def supabase_oauth_callback_page(request: Request, db: Session = Depends(get_db)):
    """HTML: exchangeCodeForSession + POST токена на сервер."""
    from backend.services.supabase_auth import supabase_auth_configured

    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="web_login_disabled"), status_code=303)
    if not supabase_auth_configured(db):
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="supabase_not_configured"), status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "auth/supabase_callback.html",
        {
            "request": request,
            "supabase_url": effective_supabase_url(db),
            "supabase_anon_key": effective_supabase_anon_key(db),
        },
    )


@router.get("/supabase/oauth-page/{provider}")
async def supabase_oauth_start_page(request: Request, provider: str, db: Session = Depends(get_db)):
    from backend.services.supabase_auth import supabase_auth_configured, supabase_oauth_redirect_url

    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="web_login_disabled"), status_code=303)
    if not supabase_auth_configured(db):
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="supabase_not_configured"), status_code=303)

    p = (provider or "google").strip().lower()
    if p not in ("google", "github", "azure", "facebook", "apple", "discord", "slack"):
        p = "google"

    redirect_to = supabase_oauth_redirect_url(str(request.base_url), db)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "auth/supabase_oauth.html",
        {
            "request": request,
            "supabase_url": effective_supabase_url(db),
            "supabase_anon_key": effective_supabase_anon_key(db),
            "oauth_provider": p,
            "redirect_to": redirect_to,
        },
    )


@router.get("/dev-login")
async def dev_login(request: Request, db: Session = Depends(get_db)):
    """Быстрый вход в режиме разработки (без Google OAuth)."""
    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="web_login_disabled"), status_code=303)
    if not dev_login_allowed():
        return RedirectResponse(cabinet_web_auth_redirect_path(request, error="dev_login_disabled"), status_code=303)

    email = "dev@localhost"
    admin = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not admin:
        admin = AdminUser(email=email, name="Developer")
        db.add(admin)
        db.commit()
        db.refresh(admin)
    admin.last_login_at = datetime.now(timezone.utc)
    db.commit()

    request.session["user"] = {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name,
        "picture": "",
    }
    from backend.services.tenancy import effective_organization_id

    effective_organization_id(request, db, admin.id)
    return RedirectResponse("/home", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    from backend.services.access_keys import SESSION_KEY_FP, SESSION_KEY_ID

    request.session.pop(WEB_CABINET_GATE_SESSION_KEY, None)
    if app_config.fb_master_operator_after_access_key():
        request.session.clear()
    else:
        for k in list(request.session.keys()):
            if k not in (SESSION_KEY_ID, SESSION_KEY_FP):
                del request.session[k]
    if fb_master_access_key_required():
        return RedirectResponse(append_fbm_app_to_url("/auth/unlock", request), status_code=303)
    return RedirectResponse(cabinet_web_auth_redirect_path(request), status_code=303)
