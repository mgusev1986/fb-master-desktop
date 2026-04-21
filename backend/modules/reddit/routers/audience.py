"""Audience search router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import accounts as accounts_svc
from backend.modules.reddit.services import audience as audience_svc
from backend.modules.reddit.services.api_client import RedditApiError

router = APIRouter(prefix="/reddit/audience")


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
async def audience_page(request: Request):
    source = request.query_params.get("source", "by_subreddit")
    subreddit = request.query_params.get("subreddit", "").strip()
    permalink = request.query_params.get("permalink", "").strip()
    limit_raw = request.query_params.get("limit", "25").strip()
    try:
        limit = max(5, min(100, int(limit_raw)))
    except ValueError:
        limit = 25

    db = SessionLocal()
    error: str | None = None
    cards: list[audience_svc.ProspectCard] = []
    account = None
    browser_accounts: list = []
    try:
        org_id = _org_id(request)
        account = _pick_account(db, org_id)
        if account is not None:
            try:
                if source == "by_subreddit" and subreddit:
                    cards = audience_svc.search_by_subreddit(db, account, org_id, subreddit, limit=limit)
                elif source == "by_subreddit_comments" and subreddit:
                    cards = audience_svc.search_by_subreddit(db, account, org_id, subreddit, limit=limit, from_comments=True)
                elif source == "by_post_permalink" and permalink:
                    cards = audience_svc.search_by_post_permalink(db, account, org_id, permalink, limit=limit)
            except RedditApiError as exc:
                error = f"Ошибка Reddit API: {exc}"
        # browser-mode аккаунты с cookies — для browser-scraper форм.
        browser_accounts = [
            a for a in accounts_svc.list_accounts(db, org_id)
            if (a.auth_mode or "") == "browser_profile" and (a.cookies_json or [])
        ]
    finally:
        db.close()

    return _tpl(request).TemplateResponse(
        "reddit/audience/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_audience",
            "source": source,
            "subreddit": subreddit,
            "permalink": permalink,
            "limit": limit,
            "cards": cards,
            "has_account": account is not None,
            "account_id": account.id if account else None,
            "error": error,
            "browser_accounts": browser_accounts,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/scan-subreddit-browser")
async def audience_scan_subreddit_browser(
    request: Request,
    account_id: int = Form(...),
    subreddit: str = Form(...),
    sort: str = Form(default="new"),
    limit: int = Form(default=25),
):
    """Browser-mode сканирование сабреддита: парсит authors постов как leads."""
    from backend.modules.reddit.runtime.audience_scraper_browser import scan_subreddit_authors

    db = SessionLocal()
    try:
        try:
            res = scan_subreddit_authors(
                db, account_id, subreddit,
                headless=True, limit=max(5, min(100, int(limit or 25))), sort=sort,
            )
            if res.get("ok"):
                qs = f"scan_ok&found={res.get('found',0)}&saved={res.get('saved',0)}&skipped={res.get('skipped',0)}"
            else:
                qs = f"scan_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"scan_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/reddit/audience?notice={qs}", status_code=303)


@router.post("/search-users-browser")
async def audience_search_users_browser(
    request: Request,
    account_id: int = Form(...),
    keywords: str = Form(...),
    limit: int = Form(default=25),
):
    """Browser-mode поиск пользователей по keyword."""
    from backend.modules.reddit.runtime.audience_scraper_browser import search_users

    db = SessionLocal()
    try:
        try:
            res = search_users(
                db, account_id, keywords,
                headless=True, limit=max(5, min(100, int(limit or 25))),
            )
            if res.get("ok"):
                qs = f"search_ok&found={res.get('found',0)}&saved={res.get('saved',0)}&skipped={res.get('skipped',0)}"
            else:
                qs = f"search_failed_{res.get('reason_code') or 'unknown'}"
        except Exception as e:  # noqa: BLE001
            qs = f"search_exception&err={str(e)[:120]}"
    finally:
        db.close()
    return RedirectResponse(f"/reddit/audience?notice={qs}", status_code=303)


@router.post("/ingest")
async def audience_ingest_selected(
    request: Request,
    source: str = Form(default=""),
    source_ref: str = Form(default=""),
    usernames: str = Form(default=""),
    owner_account_id: str = Form(default=""),
):
    """Добавить выбранных пользователей в Leads."""
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        try:
            owner_id = int(owner_account_id) if owner_account_id.strip() else None
        except ValueError:
            owner_id = None

        names = [u.strip().lstrip("u/") for u in usernames.splitlines() if u.strip()]
        cards = [
            audience_svc.ProspectCard(
                username=n,
                source_kind=source or "manual",
                source_ref=source_ref or None,
                activity_at=None,
                title_excerpt=None,
                subreddit=None,
                karma_total=None,
                account_age_days=None,
                already_lead=False,
            )
            for n in names
        ]
        audience_svc.ingest_as_leads(db, org_id, owner_id, cards)
    finally:
        db.close()
    return RedirectResponse("/reddit/leads", status_code=303)


__all__ = ["router"]
