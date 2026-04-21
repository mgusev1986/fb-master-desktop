"""Instagram compliance center."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend.modules.instagram.manifest import CAPABILITIES

router = APIRouter(prefix="/instagram/compliance")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse)
async def compliance_index(request: Request):
    grouped = {
        "supported": [c for c in CAPABILITIES.capabilities if c.level.value == "supported"],
        "partial": [c for c in CAPABILITIES.capabilities if c.level.value == "partial"],
        "restricted": [c for c in CAPABILITIES.capabilities if c.level.value == "restricted"],
        "unsupported": [c for c in CAPABILITIES.capabilities if c.level.value == "unsupported"],
    }
    known_limitations = (
        "Instagram чрезвычайно агрессивно ловит bot-паттерны: shadowban за >150 follow/день, >30 DM/день, одинаковые комментарии массово.",
        "Mobile UA рекомендован для веб-сессии (Instagram лучше принимает мобильную версию).",
        "Mobile/residential proxy крайне желателен — datacenter IP моментально получает action-block.",
        "DM непринятым уходят в request-папку, вероятность открытия низкая.",
        "Cookies (sessionid) живут несколько недель, но требуют matching IP/UA.",
        "AI-автоответчик ведёт переписку до достижения цели, оператор контролирует через approval_mode.",
    )
    return _tpl(request).TemplateResponse(
        "instagram/compliance/index.html",
        {
            "request": request, "user": request.session.get("user"),
            "page_id": "ig_compliance",
            "capabilities": grouped, "known_limitations": known_limitations,
        },
    )


__all__ = ["router"]
