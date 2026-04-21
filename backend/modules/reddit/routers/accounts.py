"""Reddit accounts: list, connect, disconnect, refresh-probe, delete."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit import config as reddit_config
from backend.modules.reddit.services import accounts as accounts_svc
from backend.modules.reddit.services.oauth import (
    RedditOAuthError,
    build_authorize_url,
    new_state,
)

router = APIRouter(prefix="/reddit")

_OAUTH_STATE_KEY = "reddit_oauth_state"
_OAUTH_RETURN_KEY = "reddit_oauth_return_to"


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int | None:
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("/accounts", response_class=HTMLResponse)
async def reddit_accounts_list(request: Request):
    from backend.core.feature_flags import (
        reddit_browser_profile_enabled,
        reddit_official_api_only,
    )

    db = SessionLocal()
    try:
        oauth_ok = reddit_config.is_oauth_configured(db)
        accounts = accounts_svc.list_accounts(db, _org_id(request))
        cards = [accounts_svc.serialize_account_card(a) for a in accounts]
        # Прокинем «сырые» аккаунты для browser-mode UI (cookies count, proxy и т.п.).
        accounts_raw = accounts
    finally:
        db.close()

    return _tpl(request).TemplateResponse(
        "reddit/accounts/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_accounts_list",
            "oauth_configured": oauth_ok,
            "accounts": cards,
            "accounts_raw": accounts_raw,
            "browser_mode_enabled": reddit_browser_profile_enabled() and not reddit_official_api_only(),
            "notice": request.query_params.get("notice"),
            "imported": request.query_params.get("imported"),
            "errors_count": request.query_params.get("errors"),
        },
    )


@router.post("/accounts/import-cookies")
async def reddit_accounts_import_cookies(request: Request, blob: str = Form(default="")):
    """Импорт купленных аккаунтов через JSON / cookies-array / TSV (browser-mode)."""
    db = SessionLocal()
    try:
        result = accounts_svc.import_browser_accounts_from_text(db, _org_id(request), blob or "")
    finally:
        db.close()
    qs = f"notice=imported&imported={result['imported']}&errors={len(result['errors'])}"
    return RedirectResponse(f"/reddit/accounts?{qs}", status_code=303)


@router.post("/accounts/{account_id}/proxy")
async def reddit_accounts_set_proxy(
    account_id: int,
    request: Request,
    proxy_url: str = Form(default=""),
    proxy_username: str = Form(default=""),
    proxy_password: str = Form(default=""),
    proxy_enabled: str = Form(default="on"),
):
    db = SessionLocal()
    try:
        accounts_svc.update_proxy(
            db, account_id,
            proxy_url=proxy_url,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
            proxy_enabled=(proxy_enabled.lower() in ("on", "1", "true", "yes")),
        )
    finally:
        db.close()
    return RedirectResponse("/reddit/accounts?notice=proxy_saved", status_code=303)


@router.post("/accounts/{account_id}/probe-browser")
async def reddit_accounts_probe_browser(account_id: int, request: Request):
    """Запустить browser-readiness probe (Playwright) для browser-mode аккаунта."""
    from backend.modules.reddit.runtime.readiness_probe import probe_account

    db = SessionLocal()
    try:
        try:
            res = probe_account(db, account_id, headless=True)
            code = res.get("reason_code") or ("ok" if res.get("ok") else "failed")
        except Exception as e:  # noqa: BLE001
            code = "exception"
            res = {"error": str(e)[:200]}
    finally:
        db.close()
    return RedirectResponse(f"/reddit/accounts?notice=probe_{code}", status_code=303)


@router.post("/accounts/{account_id}/inbox-sync-browser")
async def reddit_accounts_inbox_sync_browser(account_id: int, request: Request):
    """Browser-mode синхронизация inbox через Playwright."""
    from backend.modules.reddit.runtime.inbox_sync_browser import sync_inbox

    db = SessionLocal()
    try:
        try:
            res = sync_inbox(db, account_id, headless=True, max_threads=25)
            if res.get("ok"):
                qs = f"sync_ok&new={res.get('new_messages',0)}&seen={res.get('threads_seen',0)}"
            else:
                qs = f"sync_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"sync_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/reddit/accounts?notice={qs}", status_code=303)


@router.post("/accounts/connect")
async def reddit_accounts_connect(request: Request):
    """Генерирует Reddit OAuth authorize URL и редиректит."""
    db = SessionLocal()
    try:
        state = new_state()
        try:
            url = build_authorize_url(db, state=state)
        except RedditOAuthError as exc:
            return RedirectResponse(
                f"/reddit/settings?error={str(exc).replace(' ', '+')[:200]}",
                status_code=303,
            )
    finally:
        db.close()

    request.session[_OAUTH_STATE_KEY] = state
    request.session[_OAUTH_RETURN_KEY] = "/reddit/accounts"
    return RedirectResponse(url, status_code=303)


@router.post("/accounts/{account_id}/refresh")
async def reddit_accounts_refresh(account_id: int, request: Request):
    db = SessionLocal()
    try:
        accounts_svc.refresh_probe(db, account_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/accounts", status_code=303)


@router.post("/accounts/{account_id}/disconnect")
async def reddit_accounts_disconnect(account_id: int, request: Request):
    db = SessionLocal()
    try:
        accounts_svc.disconnect(db, account_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/accounts", status_code=303)


@router.post("/accounts/{account_id}/delete")
async def reddit_accounts_delete(account_id: int, request: Request):
    db = SessionLocal()
    try:
        accounts_svc.delete_account(db, account_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/accounts", status_code=303)


@router.post("/accounts/{account_id}/label")
async def reddit_accounts_relabel(account_id: int, request: Request, label: str = Form(default="")):
    db = SessionLocal()
    try:
        acc = accounts_svc.get_account(db, account_id)
        if acc is not None:
            acc.label = (label or "").strip()[:160] or acc.label
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/reddit/accounts", status_code=303)


__all__ = ["router"]
