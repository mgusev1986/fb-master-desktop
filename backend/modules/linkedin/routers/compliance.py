"""LinkedIn Master — Compliance & Limits center.

Единственный полностью реализованный (не placeholder) раздел в Beta:
честно показывает capability matrix из манифеста + safety-лимиты, чтобы
пользователь с первого визита видел, что модуль умеет / частично / не
умеет / запрещено.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend.database import SessionLocal
from backend.modules.linkedin import config as li_config
from backend.modules.linkedin.manifest import CAPABILITIES

router = APIRouter(prefix="/linkedin/compliance")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse)
async def linkedin_compliance_index(request: Request):
    db = SessionLocal()
    try:
        approval_mode = li_config.get_approval_mode(db)
        daily_invites_cap = li_config.get_daily_invites_cap(db)
        weekly_invites_cap = li_config.get_weekly_invites_cap(db)
        daily_messages_cap = li_config.get_daily_messages_cap(db)
    finally:
        db.close()

    grouped = {
        "supported": [c for c in CAPABILITIES.capabilities if c.level.value == "supported"],
        "partial": [c for c in CAPABILITIES.capabilities if c.level.value == "partial"],
        "restricted": [c for c in CAPABILITIES.capabilities if c.level.value == "restricted"],
        "unsupported": [c for c in CAPABILITIES.capabilities if c.level.value == "unsupported"],
    }

    known_limitations = (
        "LinkedIn API не выдаёт бесплатный массовый доступ к invitations / messages — "
        "большая часть outreach-сценариев работает в режиме manual review.",
        "InMail (сообщения непринятым) — платная функция; модуль предупреждает о цене перед каждой отправкой.",
        "Online-статус пользователей не раскрывается — мы используем только recent visible activity.",
        "Browser-automation запрещён LinkedIn ToS и заблокирован флагом FB_MASTER_LINKEDIN_OFFICIAL_API_ONLY=1 по умолчанию.",
        "Inbox-синхронизация без partner-доступа недоступна; модуль работает как conversation workspace c manual-assist.",
    )

    return _tpl(request).TemplateResponse(
        "linkedin/compliance/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_compliance",
            "capabilities": grouped,
            "approval_mode": approval_mode,
            "daily_invites_cap": daily_invites_cap,
            "weekly_invites_cap": weekly_invites_cap,
            "daily_messages_cap": daily_messages_cap,
            "known_limitations": known_limitations,
        },
    )


__all__ = ["router"]
