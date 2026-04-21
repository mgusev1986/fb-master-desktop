"""Twitter Compliance Center."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend.database import SessionLocal
from backend.modules.twitter.manifest import CAPABILITIES

router = APIRouter(prefix="/twitter/compliance")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse)
async def twitter_compliance_index(request: Request):
    grouped = {
        "supported": [c for c in CAPABILITIES.capabilities if c.level.value == "supported"],
        "partial": [c for c in CAPABILITIES.capabilities if c.level.value == "partial"],
        "restricted": [c for c in CAPABILITIES.capabilities if c.level.value == "restricted"],
        "unsupported": [c for c in CAPABILITIES.capabilities if c.level.value == "unsupported"],
    }
    known_limitations = (
        "X-API без partner-доступа дорогой ($5K/mo за Pro). Browser-mode основной.",
        "X жёстко ловит anti-automation: shadowban за подозрительные паттерны (>50 follow/день, >30 DM/день для нового аккаунта).",
        "DM непринятым работают только если у получателя НЕ стоит «only from followers».",
        "ct0 cookie живёт ограниченно — нужен matching IP/UA per-аккаунт.",
        "AI-автоответчик ведёт диалог в DM до достижения цели — оператор контролирует через approval_mode.",
    )
    return _tpl(request).TemplateResponse(
        "twitter/compliance/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "twitter_compliance",
            "capabilities": grouped,
            "known_limitations": known_limitations,
        },
    )


__all__ = ["router"]
