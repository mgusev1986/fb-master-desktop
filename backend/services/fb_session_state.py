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
    if not isinstance(data, dict):
        return None
    # Ленивая нормализация сломанных domain типа `.www.facebook.com` — это чинит
    # старые записи в БД (до исправления) без необходимости миграции.
    try:
        _normalize_snapshot_cookie_domains(data)
    except Exception:
        pass
    # On-the-fly repair: ранние версии парсера отбрасывали FILETIME expirationDate
    # (> 1e15) → cookies сохранялись без expires → инжектились как session-only →
    # FB показывал «Продолжить»-picker вместо автологина. Для старых записей в БД
    # восстанавливаем разумную дату истечения для ключевых auth-cookie.
    try:
        _backfill_missing_fb_cookie_expiry(data)
    except Exception:
        pass
    return data


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
    # Сравниваем только когда expected_login похож на FB user ID (длинный числовой).
    # Настоящие c_user — это 14-17 значные числа. Телефоны (79516776278) — до 13
    # цифр. Если сравнивать телефон с c_user → всегда не совпадает → ложное
    # «сессия непригодна». Поэтому проверяем только длинные числовые логины.
    if exp and exp.isdigit() and len(exp) >= 14:
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


_FB_CRITICAL_AUTH_COOKIES = {"c_user", "xs", "fr", "datr", "sb", "locale", "wd", "dpr"}


def _backfill_missing_fb_cookie_expiry(snap: dict[str, Any]) -> None:
    """Для cookies в домене FB без `expires`: подставляем дефолт 2 года вперёд.
    Без expires Electron считает их session-only (живут до рестарта webview),
    и FB показывает «Saved login»-picker вместо автологина."""
    if not isinstance(snap, dict):
        return
    cookies = snap.get("cookies")
    if not isinstance(cookies, list):
        return
    import time as _t
    default_expiry = _t.time() + 2 * 365 * 86_400
    for c in cookies:
        if not isinstance(c, dict):
            continue
        dom = str(c.get("domain") or "").lower()
        if "facebook.com" not in dom and "messenger.com" not in dom:
            continue
        name = str(c.get("name") or "")
        if name not in _FB_CRITICAL_AUTH_COOKIES:
            continue
        exp = c.get("expires")
        if exp is None or (isinstance(exp, (int, float)) and exp < _t.time()):
            c["expires"] = default_expiry


def _normalize_snapshot_cookie_domains(snap: dict[str, Any]) -> dict[str, Any]:
    """Chromium storage_state иногда возвращает domain=.www.facebook.com (невалидный
    формат). При повторном инжекте FB отвергает такие cookies → login gate зацикливается.
    Нормализуем: `.www.facebook.com` → `.facebook.com`, `.www.messenger.com` → `.messenger.com`."""
    if not isinstance(snap, dict):
        return snap
    cookies = snap.get("cookies")
    if not isinstance(cookies, list):
        return snap
    for c in cookies:
        if not isinstance(c, dict):
            continue
        dom = str(c.get("domain") or "")
        low = dom.lower()
        if low == ".www.facebook.com":
            c["domain"] = ".facebook.com"
        elif low == ".www.messenger.com":
            c["domain"] = ".messenger.com"
        elif low.startswith(".www.") and "." in low[5:]:
            c["domain"] = "." + dom[5:]
    return snap


def persist_account_session_snapshot(account_id: int, snap: dict[str, Any] | None) -> None:
    if snap is None:
        return
    try:
        snap = _normalize_snapshot_cookie_domains(snap)
    except Exception:
        logger.debug("normalize_snapshot_cookie_domains id=%s", account_id, exc_info=True)
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
