"""LinkedIn-аккаунты: HTML список + JSON-API + импорт купленных."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.linkedin.services import accounts as accounts_svc

router = APIRouter(prefix="/linkedin/accounts")
api_router = APIRouter(prefix="/linkedin/api/accounts")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int | None:
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("", response_class=HTMLResponse)
async def accounts_index(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request) or 1
        accounts = accounts_svc.list_accounts(db, org_id)
        summary = accounts_svc.status_summary(db, org_id)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "linkedin/accounts/list.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_accounts_list",
            "accounts": accounts,
            "summary": summary,
            "notice": request.query_params.get("notice"),
            "imported": request.query_params.get("imported"),
            "errors_count": request.query_params.get("errors"),
        },
    )


@router.post("/import")
async def accounts_import(request: Request, blob: str = Form(default="")):
    """Импорт купленных аккаунтов одним textarea (JSON / TSV / cookies-array)."""
    db = SessionLocal()
    try:
        org_id = _org_id(request) or 1
        result = accounts_svc.import_from_text(db, org_id, blob or "")
    finally:
        db.close()
    qs = f"?notice=imported&imported={result['imported']}&errors={len(result['errors'])}"
    return RedirectResponse(f"/linkedin/accounts{qs}", status_code=303)


@router.post("/{account_id}/proxy")
async def accounts_set_proxy(
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
            db,
            account_id,
            proxy_url=proxy_url,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
            proxy_enabled=(proxy_enabled.lower() in ("on", "1", "true", "yes")),
        )
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/accounts?notice=proxy_saved", status_code=303)


@router.post("/{account_id}/delete")
async def accounts_delete(account_id: int, request: Request):
    db = SessionLocal()
    try:
        accounts_svc.delete_account(db, account_id)
    finally:
        db.close()
    return RedirectResponse(f"/linkedin/accounts?notice=deleted", status_code=303)


@router.post("/{account_id}/inbox-sync")
async def accounts_inbox_sync(account_id: int, request: Request):
    from backend.modules.linkedin.runtime.inbox_sync import sync_inbox

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
    return RedirectResponse(f"/linkedin/accounts?notice={qs}", status_code=303)


@router.post("/{account_id}/probe")
async def accounts_probe(account_id: int, request: Request):
    """Запустить readiness-probe (Playwright) — обновит status/session_ok.

    Запускается синхронно — UI редиректится после завершения. Для аккаунта
    без cookies probe мгновенный; с cookies — занимает 5–15 сек.
    """
    from backend.modules.linkedin.runtime.readiness_probe import probe_account

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
    return RedirectResponse(f"/linkedin/accounts?notice=probe_{code}", status_code=303)


# ── JSON API ──


@api_router.get("")
async def api_list(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request) or 1
        items = [
            {
                "id": a.id,
                "label": a.label,
                "public_identifier": a.public_identifier,
                "full_name": a.full_name,
                "status": a.status,
                "session_ok": a.session_ok,
                "proxy_enabled": a.proxy_enabled,
                "proxy_url": a.proxy_url,
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
