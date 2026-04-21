"""Instagram Master — main router (dashboard + placeholders)."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/instagram")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("")
async def instagram_index(request: Request):
    return _tpl(request).TemplateResponse(
        "instagram/dashboard.html",
        {"request": request, "user": request.session.get("user"), "page_id": "ig_dashboard"},
    )


@router.get("/")
async def instagram_root(request: Request):
    return await instagram_index(request)


_PLACEHOLDERS = {
    "readiness": {"page_id": "ig_readiness", "title": "Готовность аккаунта",
                  "subtitle": "Чеклист безопасности перед outreach.", "icon": "checks"},
    "story-likes": {"page_id": "ig_story_likes", "title": "Лайки сторисов",
                    "subtitle": "Прогрев аудитории через лайки сторисов целевых пользователей.", "icon": "circle-dot"},
    "analytics": {"page_id": "ig_analytics", "title": "Аналитика",
                  "subtitle": "Метрики DM, engagement, AI-сессий.", "icon": "chart-line"},
    "settings": {"page_id": "ig_settings", "title": "Настройки",
                 "subtitle": "Cookies, прокси, persona, defaults.", "icon": "settings"},
}


def _render_placeholder(request: Request, key: str):
    sec = _PLACEHOLDERS[key]
    return _tpl(request).TemplateResponse(
        "instagram/_placeholder.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": sec["page_id"], "page_title": sec["title"],
            "page_subtitle": sec["subtitle"], "page_icon": sec["icon"],
        },
    )


@router.get("/readiness")
async def instagram_readiness(request: Request):
    return _render_placeholder(request, "readiness")


@router.get("/story-likes")
async def instagram_story_likes(request: Request):
    return _render_placeholder(request, "story-likes")


@router.get("/analytics")
async def instagram_analytics(request: Request):
    return _render_placeholder(request, "analytics")


@router.get("/settings")
async def instagram_settings(request: Request):
    return _render_placeholder(request, "settings")


__all__ = ["router"]
