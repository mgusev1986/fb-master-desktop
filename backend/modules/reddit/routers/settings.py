"""Reddit-specific settings: OAuth creds, user-agent, caps, timezone."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit import config as reddit_config

router = APIRouter(prefix="/reddit")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("/settings", response_class=HTMLResponse)
async def reddit_settings_page(request: Request):
    db = SessionLocal()
    try:
        view = reddit_config.view_settings(db)
    finally:
        db.close()
    return _tpl(request).TemplateResponse(
        "reddit/settings/index.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_settings",
            "settings_view": view,
            "notice": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/settings")
async def reddit_settings_save(
    request: Request,
    client_id: str = Form(default=""),
    client_secret: str = Form(default=""),
    redirect_uri: str = Form(default=""),
    user_agent: str = Form(default=""),
    approval_mode: str = Form(default="manual"),
    timezone: str = Form(default="Europe/Madrid"),
    per_day_cap: str = Form(default="10"),
    per_hour_cap: str = Form(default="3"),
):
    db = SessionLocal()
    try:
        # При пустом значении client_secret — не затираем старое (маскировка).
        existing_secret = reddit_config.get_oauth_client_secret(db)
        secret_to_save = client_secret.strip() or existing_secret

        payload = {
            reddit_config.SETTING_CLIENT_ID: (client_id or "").strip(),
            reddit_config.SETTING_CLIENT_SECRET: secret_to_save,
            reddit_config.SETTING_REDIRECT_URI: (redirect_uri or "").strip(),
            reddit_config.SETTING_USER_AGENT: (user_agent or "").strip(),
            reddit_config.SETTING_APPROVAL_MODE: approval_mode,
            reddit_config.SETTING_DEFAULT_TIMEZONE: (timezone or "").strip() or "Europe/Madrid",
            reddit_config.SETTING_PER_DAY_CAP: per_day_cap,
            reddit_config.SETTING_PER_HOUR_CAP: per_hour_cap,
        }
        reddit_config.save_settings(db, payload)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/reddit/settings?saved=1", status_code=303)


__all__ = ["router"]
