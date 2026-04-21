"""Compliance Center + Readiness roster for Reddit."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit import config as reddit_config
from backend.modules.reddit.manifest import CAPABILITIES
from backend.modules.reddit.models import (
    RedditAccount,
    RedditComplianceEvent,
    RedditRateLimitState,
)
from backend.modules.reddit.services import accounts as accounts_svc

router = APIRouter(prefix="/reddit")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


def _grouped_capabilities():
    buckets = {"supported": [], "partial": [], "restricted": [], "unsupported": []}
    for cap in CAPABILITIES.capabilities:
        buckets[cap.level.value].append(cap)
    return buckets


@router.get("/compliance", response_class=HTMLResponse)
async def compliance_page(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        accounts = accounts_svc.list_accounts(db, org_id)
        rl_rows = (
            db.query(RedditRateLimitState)
            .filter(RedditRateLimitState.account_id.in_([a.id for a in accounts] or [0]))
            .order_by(RedditRateLimitState.observed_at.desc())
            .all()
        )
        events = (
            db.query(RedditComplianceEvent)
            .filter(RedditComplianceEvent.organization_id == org_id)
            .order_by(RedditComplianceEvent.at.desc())
            .limit(50)
            .all()
        )
        approval_mode = reddit_config.get_approval_mode(db)
        per_day_cap = reddit_config.get_per_day_cap(db)
        per_hour_cap = reddit_config.get_per_hour_cap(db)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/compliance/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_compliance",
            "capabilities": _grouped_capabilities(),
            "accounts": accounts,
            "rate_limits": rl_rows,
            "events": events,
            "approval_mode": approval_mode,
            "per_day_cap": per_day_cap,
            "per_hour_cap": per_hour_cap,
        },
    )


@router.get("/readiness", response_class=HTMLResponse)
async def readiness_page(request: Request):
    db = SessionLocal()
    try:
        org_id = _org_id(request)
        accounts = accounts_svc.list_accounts(db, org_id)
        reports = []
        for acc in accounts:
            r = accounts_svc.readiness_report_for(db, acc.id)
            if r:
                reports.append(r)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/readiness/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_readiness",
            "reports": reports,
        },
    )


@router.post("/readiness/{account_id}/probe")
async def readiness_probe(account_id: int, request: Request):
    db = SessionLocal()
    try:
        accounts_svc.refresh_probe(db, account_id)
    finally:
        db.close()
    return RedirectResponse("/reddit/readiness", status_code=303)


__all__ = ["router"]
