"""Секретный URL /gate/<slug>: пароль → сессия → вход владельца или редирект на /auth/login."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.database import get_db
from backend.services.fbm_client_version import append_fbm_app_to_url
from backend.services.platform_owner import (
    default_platform_owner_email,
    establish_platform_owner_session,
)
from backend.services.web_cabinet_gate import (
    WEB_CABINET_GATE_SESSION_KEY,
    verify_cabinet_gate_password,
)

router = APIRouter(tags=["web_cabinet_gate"])


def _expect_slug() -> str:
    return app_config.web_cabinet_gate_slug()


def _gate_owner_direct_login_enabled() -> bool:
    return app_config.fb_master_web_cabinet_email_login_disabled() and bool(
        default_platform_owner_email()
    )


def _gate_template_context(request: Request, gate_error: str | None) -> dict:
    return {
        "request": request,
        "gate_error": gate_error,
        "gate_owner_direct_login": _gate_owner_direct_login_enabled(),
    }


@router.get("/gate/{slug}")
async def cabinet_gate_form(request: Request, slug: str):
    if not app_config.web_cabinet_gate_enabled() or slug != _expect_slug():
        raise HTTPException(status_code=404, detail="Not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "auth/cabinet_gate.html",
        _gate_template_context(request, gate_error=None),
    )


@router.post("/gate/{slug}")
async def cabinet_gate_submit(
    request: Request,
    slug: str,
    db: Session = Depends(get_db),
    password: str = Form(""),
):
    if not app_config.web_cabinet_gate_enabled() or slug != _expect_slug():
        raise HTTPException(status_code=404, detail="Not found")
    templates = request.app.state.templates
    expected = app_config.web_cabinet_gate_password()
    if not verify_cabinet_gate_password(password, expected):
        return templates.TemplateResponse(
            "auth/cabinet_gate.html",
            _gate_template_context(request, gate_error="Неверный пароль."),
            status_code=401,
        )
    request.session[WEB_CABINET_GATE_SESSION_KEY] = True
    if app_config.fb_master_web_cabinet_email_login_disabled():
        if establish_platform_owner_session(request, db):
            return RedirectResponse("/admin/platform", status_code=303)
        return RedirectResponse(append_fbm_app_to_url("/auth/unlock", request), status_code=303)
    return RedirectResponse("/auth/login", status_code=303)
