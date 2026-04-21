"""Секретный URL /gate/<slug>: пароль → сессия → редирект на /auth/login или /auth/unlock (если отключён email-вход)."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from backend import config as app_config
from backend.services.fbm_client_version import append_fbm_app_to_url
from backend.services.web_cabinet_gate import (
    WEB_CABINET_GATE_SESSION_KEY,
    verify_cabinet_gate_password,
)

router = APIRouter(tags=["web_cabinet_gate"])


def _expect_slug() -> str:
    return app_config.web_cabinet_gate_slug()


@router.get("/gate/{slug}")
async def cabinet_gate_form(request: Request, slug: str):
    if not app_config.web_cabinet_gate_enabled() or slug != _expect_slug():
        raise HTTPException(status_code=404, detail="Not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "auth/cabinet_gate.html",
        {
            "request": request,
            "gate_error": None,
        },
    )


@router.post("/gate/{slug}")
async def cabinet_gate_submit(
    request: Request,
    slug: str,
    password: str = Form(""),
):
    if not app_config.web_cabinet_gate_enabled() or slug != _expect_slug():
        raise HTTPException(status_code=404, detail="Not found")
    templates = request.app.state.templates
    expected = app_config.web_cabinet_gate_password()
    if not verify_cabinet_gate_password(password, expected):
        return templates.TemplateResponse(
            "auth/cabinet_gate.html",
            {
                "request": request,
                "gate_error": "Неверный пароль.",
            },
            status_code=401,
        )
    request.session[WEB_CABINET_GATE_SESSION_KEY] = True
    if app_config.fb_master_web_cabinet_email_login_disabled():
        return RedirectResponse(append_fbm_app_to_url("/auth/unlock", request), status_code=303)
    return RedirectResponse("/auth/login", status_code=303)
