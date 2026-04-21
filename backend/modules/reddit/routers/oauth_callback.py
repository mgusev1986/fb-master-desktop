"""Reddit OAuth callback — обмен code на tokens и сохранение аккаунта."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.database import SessionLocal
from backend.modules.reddit.services import accounts as accounts_svc
from backend.modules.reddit.services.oauth import RedditOAuthError, exchange_code

router = APIRouter(prefix="/reddit/oauth")

_OAUTH_STATE_KEY = "reddit_oauth_state"
_OAUTH_RETURN_KEY = "reddit_oauth_return_to"


def _tpl(request: Request):
    return request.app.state.templates


def _error_page(request: Request, message: str, code: int = 400) -> HTMLResponse:
    return _tpl(request).TemplateResponse(
        "reddit/oauth/error.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_accounts_list",
            "message": message,
        },
        status_code=code,
    )


@router.get("/callback")
async def reddit_oauth_callback(request: Request):
    params = dict(request.query_params)
    returned_state = params.get("state") or ""
    code = params.get("code")
    error = params.get("error")

    expected_state = request.session.pop(_OAUTH_STATE_KEY, None)
    return_to = request.session.pop(_OAUTH_RETURN_KEY, None) or "/reddit/accounts"

    if error:
        return _error_page(request, f"Reddit вернул ошибку: {error}")
    if not code:
        return _error_page(request, "Нет code в ответе Reddit — попробуйте подключить аккаунт снова.")
    if not expected_state or expected_state != returned_state:
        return _error_page(request, "state не совпадает — OAuth flow отклонён из соображений безопасности.")

    db = SessionLocal()
    try:
        try:
            tokens = exchange_code(db, code)
        except RedditOAuthError as exc:
            return _error_page(request, f"Не удалось обменять code: {exc}")

        u = request.session.get("user") or {}
        org_id = u.get("organization_id")
        accounts_svc.create_account_from_tokens(db, org_id, tokens)
    finally:
        db.close()

    return RedirectResponse(return_to, status_code=303)


__all__ = ["router"]
