"""Reddit analytics router."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import analytics as analytics_svc

router = APIRouter(prefix="/reddit/analytics")


def _tpl(request: Request):
    return request.app.state.templates


def _org_id(request: Request):
    u = request.session.get("user") or {}
    return u.get("organization_id")


@router.get("", response_class=HTMLResponse)
async def analytics_page(request: Request):
    db = SessionLocal()
    try:
        data = analytics_svc.dashboard(db, _org_id(request))
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/analytics/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_analytics",
            "data": data,
        },
    )


__all__ = ["router"]
