"""Подсказка версии десктопа: query/cookie fbm_app (Electron добавляет в URL)."""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from starlette.requests import Request

FBM_APP_PARAM = "fbm_app"
_FBM_APP_RE = re.compile(r"^[\w.+\-]{1,32}$")


def fbm_app_value_raw(request: Request) -> str:
    q = (request.query_params.get(FBM_APP_PARAM) or "").strip()
    if q and _FBM_APP_RE.match(q):
        return q
    c = (request.cookies.get(FBM_APP_PARAM) or "").strip()
    if c and _FBM_APP_RE.match(c):
        return c
    return ""


def append_fbm_app_to_url(path_with_optional_query: str, request: Request) -> str:
    frag = fbm_app_value_raw(request)
    if not frag:
        return path_with_optional_query
    sep = "&" if "?" in path_with_optional_query else "?"
    return f"{path_with_optional_query}{sep}{FBM_APP_PARAM}={quote(frag, safe='')}"


def set_fbm_app_cookie_on_response(request: Request, response) -> None:
    """Если в запросе есть fbm_app в query — продублировать в cookie (для следующих редиректов)."""
    from backend.config import SESSION_COOKIE_SECURE

    v = (request.query_params.get(FBM_APP_PARAM) or "").strip()
    if v and _FBM_APP_RE.match(v):
        response.set_cookie(
            FBM_APP_PARAM,
            v,
            max_age=86400 * 90,
            path="/",
            httponly=False,
            samesite="lax",
            secure=SESSION_COOKIE_SECURE,
        )


async def persist_fbm_app_cookie_middleware(request: Request, call_next):
    """Сохраняет fbm_app из query в cookie, чтобы версия десктопа не терялась после редиректов OAuth."""
    response = await call_next(request)
    set_fbm_app_cookie_on_response(request, response)
    return response


def login_redirect_path(request: Request, **extra: str) -> str:
    params = {k: str(v) for k, v in extra.items() if v is not None and str(v) != ""}
    fa = fbm_app_value_raw(request)
    if fa:
        params[FBM_APP_PARAM] = fa
    if not params:
        return "/auth/login"
    return "/auth/login?" + urlencode(params)


def cabinet_web_auth_redirect_path(request: Request, **extra: str) -> str:
    """Редирект после ошибок OAuth: на /auth/login или на /auth/unlock, если email-вход отключён."""
    from backend import config as app_config

    if app_config.fb_master_web_cabinet_email_login_disabled():
        params = {k: str(v) for k, v in extra.items() if v is not None and str(v) != ""}
        fa = fbm_app_value_raw(request)
        if fa:
            params[FBM_APP_PARAM] = fa
        if not params:
            return "/auth/unlock"
        return "/auth/unlock?" + urlencode(params)
    return login_redirect_path(request, **extra)
