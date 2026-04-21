"""Twitter / X Master — основной FastAPI router (dashboard + placeholders)."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/twitter")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("")
async def twitter_index(request: Request):
    return _tpl(request).TemplateResponse(
        "twitter/dashboard.html",
        {"request": request, "user": request.session.get("user"), "page_id": "twitter_dashboard"},
    )


@router.get("/")
async def twitter_root(request: Request):
    return await twitter_index(request)


@router.get("/audience")
async def twitter_audience(request: Request):
    from backend.modules.twitter.routers.leads import render_leads_page

    return render_leads_page(
        request,
        page_id="twitter_audience",
        page_title="Поиск аудитории",
        page_subtitle="Browser-поиск через X Search с сохранением найденных профилей в leads.",
    )


_PLACEHOLDERS = {
    "readiness": {"page_id": "twitter_readiness", "title": "Готовность аккаунта",
                  "subtitle": "Чеклист безопасности перед outreach.", "icon": "checks"},
    "analytics": {"page_id": "twitter_analytics", "title": "Аналитика",
                  "subtitle": "Метрики DM, replies, follows и AI-сессий.", "icon": "chart-line"},
    "settings": {"page_id": "twitter_settings", "title": "Настройки",
                 "subtitle": "Cookies, прокси, persona, defaults.", "icon": "settings"},
}


def _render_placeholder(request: Request, key: str):
    sec = _PLACEHOLDERS[key]
    return _tpl(request).TemplateResponse(
        "twitter/_placeholder.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": sec["page_id"],
            "page_title": sec["title"],
            "page_subtitle": sec["subtitle"],
            "page_icon": sec["icon"],
        },
    )


@router.get("/readiness")
async def twitter_readiness(request: Request):
    return _render_placeholder(request, "readiness")


@router.get("/analytics")
async def twitter_analytics(request: Request):
    return _render_placeholder(request, "analytics")


@router.get("/settings")
async def twitter_settings(request: Request):
    return _render_placeholder(request, "settings")


__all__ = ["router"]
