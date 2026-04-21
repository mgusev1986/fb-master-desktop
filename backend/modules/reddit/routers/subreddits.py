"""Reddit subreddit discovery router."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import accounts as accounts_svc
from backend.modules.reddit.services import subreddit_discovery as discovery_svc
from backend.modules.reddit.services.api_client import RedditApiError

router = APIRouter(prefix="/reddit/subreddits")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request) -> int | None:
    u = request.session.get("user") or {}
    return u.get("organization_id")


def _pick_account(db, org_id):
    accounts = [a for a in accounts_svc.list_accounts(db, org_id) if (a.status or "") == "connected"]
    return accounts[0] if accounts else None


@router.get("", response_class=HTMLResponse)
async def subreddit_discovery(request: Request):
    query = request.query_params.get("q", "").strip()
    sort = request.query_params.get("sort", "relevance").strip() or "relevance"
    limit_raw = request.query_params.get("limit", "25").strip()
    try:
        limit = max(5, min(100, int(limit_raw)))
    except ValueError:
        limit = 25

    db = SessionLocal()
    error: str | None = None
    search_cards: list = []
    saved_rows: list = []
    account = None
    try:
        org_id = _org_id(request)
        account = _pick_account(db, org_id)
        if query and account is not None:
            try:
                search_cards = discovery_svc.search_and_persist(
                    db, account, org_id, query, limit=limit, sort=sort
                )
            except RedditApiError as exc:
                error = f"Ошибка Reddit API: {exc}"
        saved_rows = discovery_svc.list_saved(db, org_id)
    finally:
        db.close()

    return _tpl(request).TemplateResponse(
        "reddit/subreddits/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_subreddits",
            "query": query,
            "sort": sort,
            "limit": limit,
            "search_cards": search_cards,
            "saved_rows": [discovery_svc.card_for(r) for r in saved_rows],
            "saved_raw": saved_rows,
            "has_account": account is not None,
            "error": error,
        },
    )


@router.post("/delete/{subreddit_id}")
async def subreddit_delete(subreddit_id: int, request: Request):
    db = SessionLocal()
    try:
        discovery_svc.delete(db, _org_id(request), subreddit_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/subreddits", status_code=303)


__all__ = ["router"]
