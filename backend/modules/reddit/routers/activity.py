"""Recent activity scanner router."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import accounts as accounts_svc
from backend.modules.reddit.services import activity_scanner as scanner_svc

router = APIRouter(prefix="/reddit/activity")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


def _pick_account(db, org_id):
    for a in accounts_svc.list_accounts(db, org_id):
        if (a.status or "") == "connected":
            return a
    return None


@router.get("", response_class=HTMLResponse)
async def activity_page(request: Request):
    usernames_raw = request.query_params.get("usernames", "").strip()
    window = request.query_params.get("window", "24h").strip() or "24h"
    custom_hours_raw = request.query_params.get("custom_hours", "").strip()
    try:
        custom_hours = int(custom_hours_raw) if custom_hours_raw else None
    except ValueError:
        custom_hours = None
    use_leads = request.query_params.get("use_leads", "") == "1"

    db = SessionLocal()
    reports: list[scanner_svc.UserActivityReport] = []
    has_account = False
    error: str | None = None
    try:
        org_id = _org_id(request)
        account = _pick_account(db, org_id)
        has_account = account is not None
        if has_account:
            names: list[str] = []
            if use_leads:
                from backend.modules.reddit.models import RedditLead

                leads = (
                    db.query(RedditLead)
                    .filter(RedditLead.organization_id == org_id)
                    .order_by(RedditLead.id.desc())
                    .limit(100)
                    .all()
                )
                names = [l.username for l in leads if l.username]
            else:
                names = [
                    n.strip().lstrip("u/")
                    for n in usernames_raw.replace(",", "\n").splitlines()
                    if n.strip()
                ][:50]
            if names:
                reports = scanner_svc.scan_usernames(
                    db, account, org_id, names, window=window, custom_hours=custom_hours
                )
    finally:
        db.close()

    return _tpl(request).TemplateResponse(
        "reddit/activity/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_activity",
            "usernames_raw": usernames_raw,
            "window": window,
            "custom_hours": custom_hours,
            "use_leads": use_leads,
            "reports": reports,
            "has_account": has_account,
            "error": error,
        },
    )


__all__ = ["router"]
