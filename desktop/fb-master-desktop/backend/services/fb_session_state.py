"""Снимок Playwright storage_state в БД (FBAccount): парсинг, сохранение, эвристика «похоже на живую сессию»."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from backend.database import SessionLocal
from backend.models import FBAccount

logger = logging.getLogger(__name__)


def session_state_from_account(acc: FBAccount) -> dict[str, Any] | None:
    raw = acc.session_state_json
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _facebook_cookie_present(cookies: list[Any]) -> bool:
    for c in cookies:
        if not isinstance(c, dict):
            continue
        dom = str(c.get("domain") or "").lower()
        if "facebook.com" in dom or dom.endswith(".fbcdn.net"):
            return True
    return False


def _c_user_from_cookies(cookies: list[Any]) -> str | None:
    for c in cookies:
        if not isinstance(c, dict):
            continue
        if str(c.get("name") or "") == "c_user":
            v = str(c.get("value") or "").strip()
            return v or None
    return None


def session_state_is_usable(
    snap: dict[str, Any] | None,
    *,
    expected_login: str | None = None,
) -> tuple[bool, str]:
    if not snap:
        return False, "нет снимка"
    cookies = snap.get("cookies")
    if not isinstance(cookies, list) or len(cookies) < 1:
        return False, "нет cookies в снимке"
    if not _facebook_cookie_present(cookies):
        return False, "нет cookies для домена Facebook"
    exp = (expected_login or "").strip()
    if exp and exp.isdigit():
        c_user = _c_user_from_cookies(cookies)
        if c_user and c_user != exp:
            return False, "cookie c_user не совпадает с логином аккаунта"
    return True, ""


def usable_session_state_from_account(acc: FBAccount) -> dict[str, Any] | None:
    snap = session_state_from_account(acc)
    ok, _reason = session_state_is_usable(
        snap,
        expected_login=(acc.fb_login_username or "").strip() or None,
    )
    return snap if ok else None


def persist_account_session_snapshot(account_id: int, snap: dict[str, Any] | None) -> None:
    if snap is None:
        return
    db = SessionLocal()
    try:
        acc = db.get(FBAccount, int(account_id))
        if not acc:
            return
        acc.session_state_json = json.dumps(snap, ensure_ascii=False)
        acc.session_saved_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        logger.exception("persist_account_session_snapshot id=%s", account_id)
        db.rollback()
    finally:
        db.close()


def refresh_account_session_flags(account_id: int) -> None:
    db = SessionLocal()
    try:
        acc = db.get(FBAccount, int(account_id))
        if not acc:
            return
        snap = session_state_from_account(acc)
        ok, _ = session_state_is_usable(
            snap,
            expected_login=(acc.fb_login_username or "").strip() or None,
        )
        acc.session_ok = True if ok else None
        db.commit()
    except Exception:
        logger.exception("refresh_account_session_flags id=%s", account_id)
        db.rollback()
    finally:
        db.close()


def persist_account_login_blocked(account_id: int, reason: str) -> None:
    db = SessionLocal()
    try:
        acc = db.get(FBAccount, int(account_id))
        if not acc:
            return
        acc.login_blocked_at = datetime.now(timezone.utc)
        acc.login_blocked_reason = (reason or "").strip()[:512] or None
        db.commit()
    except Exception:
        logger.exception("persist_account_login_blocked id=%s", account_id)
        db.rollback()
    finally:
        db.close()


def clear_account_login_blocked(account_id: int) -> None:
    db = SessionLocal()
    try:
        acc = db.get(FBAccount, int(account_id))
        if not acc:
            return
        acc.login_blocked_at = None
        acc.login_blocked_reason = None
        db.commit()
    except Exception:
        logger.exception("clear_account_login_blocked id=%s", account_id)
        db.rollback()
    finally:
        db.close()
