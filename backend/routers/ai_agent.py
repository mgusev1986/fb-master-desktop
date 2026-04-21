"""Совместимость: раздел перенесён в «Рассылка». Старые ссылки ведут на /outreach."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/ai-agent", tags=["ai_agent"])

OUTREACH_AGENT = "/outreach?focus=agent"


@router.get("")
async def ai_agent_legacy_redirect():
    return RedirectResponse(OUTREACH_AGENT, status_code=302)
