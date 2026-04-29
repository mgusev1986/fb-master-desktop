"""Лендинг (промо-страница)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend import config as app_config

router = APIRouter(tags=["promo"])


def _promo_ctx(request: Request) -> dict:
    return {
        "request": request,
        "np_price_365": app_config.NOWPAYMENTS_PRICE_USD_365,
    }


@router.get("/promo", response_class=HTMLResponse)
async def promo_page(request: Request):
    """Отдельная ссылка на продающий лендинг FB Master."""
    templates = request.app.state.templates
    return templates.TemplateResponse("promo.html", _promo_ctx(request))

@router.get("/", response_class=HTMLResponse)
async def root_page(request: Request):
    """Главная: лендинг для гостей; вошедших пользователей — в кабинет (/home)
    или на multi-workspace launcher, если он активен как default entry.

    3.0.6+: владельца платформы (admin / FB_MASTER_OWNER_EMAILS) НЕ редиректим —
    он должен видеть публичный лендинг как любой посетитель сайта. В кабинет
    зайдёт по прямой ссылке /home или через сайдбар на других страницах.
    """
    user = request.session.get("user")
    if user:
        from backend.services.platform_owner import is_platform_owner_user

        if not is_platform_owner_user(user):
            from backend.core.feature_flags import (
                launcher_is_default_entry,
                multi_workspace_enabled,
            )
            from backend.core.workspace.session import get_current_workspace_id

            if (
                multi_workspace_enabled()
                and launcher_is_default_entry()
                and not get_current_workspace_id(request)
            ):
                return RedirectResponse("/workspaces", status_code=303)
            return RedirectResponse("/home", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse("promo2.html", _promo_ctx(request))


@router.get("/landing")
async def landing_redirect():
    """Раньше лендинг был здесь — постоянный редирект на корень."""
    return RedirectResponse("/", status_code=301)


@router.get("/promo2")
async def promo2_redirect():
    """Старый адрес лендинга — постоянный редирект на корень."""
    return RedirectResponse("/", status_code=301)
