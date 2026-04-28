"""Workspace launcher + switcher роуты.

Доступны только при `FB_MASTER_MULTI_WORKSPACE_ENABLED=1`. При флаге=0
роуты возвращают 404, чтобы случайно не открыть launcher на сервере
socmaster.pro до полного релиза.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backend.core.feature_flags import multi_workspace_enabled
from backend.core.modules import module_registry
from backend.core.modules.base import ModuleStatus
from backend.core.workspace.launcher_view import build_launcher_view
from backend.core.workspace.session import set_current_workspace_id

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _not_found() -> PlainTextResponse:
    return PlainTextResponse("Not Found", status_code=404)


@router.get("/workspaces")
async def launcher(request: Request):
    if not multi_workspace_enabled():
        return _not_found()
    from backend.core.workspace.session import (
        clear_current_workspace,
        get_current_workspace_id,
    )

    # reset=1 приходит от Electron при каждом старте приложения —
    # сбрасываем выбранный workspace, чтобы клиент всегда начинал с launcher'а.
    if (request.query_params.get("reset") or "").strip() == "1":
        clear_current_workspace(request)

    # v3.0.5+: для клиентского билда серым становится всё кроме Facebook.
    is_client = _is_cabinet_client_mode(request)
    view = build_launcher_view(
        current_workspace_id=get_current_workspace_id(request),
        is_client_mode=is_client,
    )
    ctx = {
        "request": request,
        "user": request.session.get("user"),
        "page_id": "launcher",
        "launcher_view": view,
    }
    return _templates(request).TemplateResponse("launcher/index.html", ctx)


def _is_cabinet_client_mode(request: Request) -> bool:
    """Прокси к Jinja global cabinet_client_mode, зарегистрированному в app_factory."""
    try:
        fn = request.app.state.templates.env.globals.get("cabinet_client_mode")
        if callable(fn):
            return bool(fn(request))
    except Exception:
        pass
    return False


@router.post("/workspaces/select/{module_id}")
@router.get("/workspaces/select/{module_id}")
async def select_workspace(module_id: str, request: Request):
    """Выбрать workspace — запомнить в сессии и редирект на default_route."""
    if not multi_workspace_enabled():
        return _not_found()

    module = module_registry.get(module_id)
    if module is None:
        return _not_found()

    status = module.status()
    if status not in (ModuleStatus.READY, ModuleStatus.BETA):
        # Не даём "открыть" coming-soon / disabled — вернуть на launcher.
        return RedirectResponse("/workspaces", status_code=303)

    # v3.0.5+: клиентский билд — открываем только whitelist (пока — Facebook).
    # Защита от прямого URL: если клиент попытается /workspaces/select/twitter
    # в адресной строке, всё равно редиректим обратно на launcher.
    from backend.core.workspace.launcher_view import _CLIENT_OPENABLE_MODULE_IDS
    if _is_cabinet_client_mode(request) and module.id not in _CLIENT_OPENABLE_MODULE_IDS:
        return RedirectResponse("/workspaces", status_code=303)

    set_current_workspace_id(request, module.id)
    target = module.navigation().default_route or "/"
    return RedirectResponse(target, status_code=303)


__all__ = ["router"]
