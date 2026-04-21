"""Один «текущий» Facebook-аккаунт на сессию: Мессенджер и Playwright без отдельного переключателя на странице."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from backend.models import FBAccount

SESSION_KEY_ACTIVE_FB = "active_fb_account_id"


def _account_in_org(db: Session, account_id: int, org_id: int) -> bool:
    acc = db.get(FBAccount, account_id)
    return bool(acc and acc.organization_id == org_id)


def get_active_fb_account_id(request: Request, db: Session, org_id: int) -> int | None:
    raw = request.session.get(SESSION_KEY_ACTIVE_FB)
    if raw is None:
        return None
    try:
        aid = int(raw)
    except (TypeError, ValueError):
        request.session.pop(SESSION_KEY_ACTIVE_FB, None)
        return None
    if _account_in_org(db, aid, org_id):
        return aid
    request.session.pop(SESSION_KEY_ACTIVE_FB, None)
    return None


def set_active_fb_account_id(request: Request, account_id: int) -> None:
    request.session[SESSION_KEY_ACTIVE_FB] = int(account_id)


def clear_active_fb_account_id(request: Request) -> None:
    request.session.pop(SESSION_KEY_ACTIVE_FB, None)


def resolve_active_fb_account_id(request: Request, db: Session, org_id: int, accounts: list[FBAccount]) -> int | None:
    """
    Текущий аккаунт для Мессенджера: только из аккаунтов со слотом 1–3 (в работе).
    Если список уже отфильтрован — используется как есть.
    """
    usable = [a for a in accounts if a.active_slot is not None and 1 <= int(a.active_slot) <= 3]
    if not usable:
        clear_active_fb_account_id(request)
        return None
    cur = get_active_fb_account_id(request, db, org_id)
    if cur is not None and any(a.id == cur for a in usable):
        return cur
    if cur is not None:
        clear_active_fb_account_id(request)
    ordered = sorted(usable, key=lambda a: (int(a.active_slot or 99), (a.label or "").lower()))
    fid = ordered[0].id
    set_active_fb_account_id(request, fid)
    return fid
