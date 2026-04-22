"""Instagram accounts router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.instagram.services import accounts as accounts_svc

router = APIRouter(prefix="/instagram/accounts")
api_router = APIRouter(prefix="/instagram/api/accounts")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int:
    u = request.session.get("user") or {}
    return u.get("organization_id") or 1


@router.get("", response_class=HTMLResponse)
async def accounts_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        accounts = accounts_svc.list_accounts(db, org_id)
        summary = accounts_svc.status_summary(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "instagram/accounts/list.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_accounts_list",
            "accounts": accounts, "summary": summary,
            "notice": request.query_params.get("notice"),
            "imported": request.query_params.get("imported"),
            "errors_count": request.query_params.get("errors"),
        },
    )


@router.post("/import")
async def accounts_import(request: Request, blob: str = Form(default="")):
    db = SessionLocal()
    try:
        result = accounts_svc.import_from_text(db, _org_id(request), blob or "")
    finally:
        db.close()
    qs = f"?notice=imported&imported={result['imported']}&errors={len(result['errors'])}"
    return RedirectResponse(f"/instagram/accounts{qs}", status_code=303)


@router.post("/import-credentials")
async def accounts_import_credentials(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    label: str = Form(default=""),
    totp_secret: str = Form(default=""),
    proxy_url: str = Form(default=""),
    proxy_username: str = Form(default=""),
    proxy_password: str = Form(default=""),
):
    """Свой личный IG-аккаунт через логин + пароль.

    Cookies появятся после первого Playwright-логина (кнопка «Войти» на карточке).
    """
    from urllib.parse import quote

    db = SessionLocal()
    try:
        try:
            acc = accounts_svc.create_from_credentials(
                db,
                _org_id(request),
                username=username,
                password=password,
                label=label or None,
                totp_secret=totp_secret or None,
                proxy_url=proxy_url or None,
                proxy_username=proxy_username or None,
                proxy_password=proxy_password or None,
            )
            qs = f"?notice=credentials_imported&label={quote((acc.label or '')[:80], safe='')}"
        except ValueError as e:
            qs = f"?notice=credentials_error&err={quote(str(e)[:200], safe='')}"
        except Exception as e:  # noqa: BLE001
            db.rollback()
            qs = f"?notice=credentials_error&err={quote(str(e)[:200], safe='')}"
    finally:
        db.close()
    return RedirectResponse(f"/instagram/accounts{qs}", status_code=303)


@router.post("/{account_id}/proxy")
async def accounts_set_proxy(account_id: int, request: Request,
                             proxy_url: str = Form(default=""),
                             proxy_username: str = Form(default=""),
                             proxy_password: str = Form(default=""),
                             proxy_enabled: str = Form(default="on")):
    db = SessionLocal()
    try:
        accounts_svc.update_proxy(
            db, account_id,
            proxy_url=proxy_url, proxy_username=proxy_username,
            proxy_password=proxy_password,
            proxy_enabled=(proxy_enabled.lower() in ("on", "1", "true", "yes")),
        )
    finally:
        db.close()
    return RedirectResponse("/instagram/accounts?notice=proxy_saved", status_code=303)


@router.post("/{account_id}/probe")
async def accounts_probe(account_id: int, request: Request):
    from backend.modules.instagram.runtime.readiness_probe import probe_account

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
    return RedirectResponse(f"/instagram/accounts?notice=probe_{code}", status_code=303)


@router.post("/{account_id}/inbox-sync")
async def accounts_inbox_sync(account_id: int, request: Request):
    from backend.modules.instagram.runtime.inbox_sync_browser import sync_inbox

    db = SessionLocal()
    try:
        try:
            res = sync_inbox(db, account_id, headless=True)
            qs = (f"sync_ok&new={res.get('new_messages',0)}" if res.get("ok") else f"sync_failed_{res.get('reason_code') or 'unknown'}")
        except Exception as e:  # noqa: BLE001
            qs = f"sync_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/instagram/accounts?notice={qs}", status_code=303)


@router.post("/{account_id}/delete")
async def accounts_delete(account_id: int, request: Request):
    db = SessionLocal()
    try:
        accounts_svc.delete_account(db, account_id)
    finally:
        db.close()
    return RedirectResponse("/instagram/accounts?notice=deleted", status_code=303)


@api_router.get("")
async def api_list(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        items = [
            {
                "id": a.id, "label": a.label, "handle": a.handle,
                "status": a.status, "session_ok": a.session_ok,
                "proxy_enabled": a.proxy_enabled,
                "cookies_count": len(a.cookies_json or []),
                "cookies_imported_at": a.cookies_imported_at.isoformat() if a.cookies_imported_at else None,
            }
            for a in accounts_svc.list_accounts(db, org_id)
        ]
        summary = accounts_svc.status_summary(db, org_id)
    finally:
        db.close()
    return JSONResponse({"items": items, "summary": summary})


__all__ = ["api_router", "router"]
