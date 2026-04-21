"""LinkedIn-специфичные env + Setting helpers.

Аналогично Reddit-модулю: OAuth-креды и параметры безопасности хранятся
в таблице `Setting` с префиксом `linkedin.*`. Fallback — env-переменные
`FB_MASTER_LINKEDIN_*`. UI настроек редактирует именно `Setting`, чтобы
клиент мог ввести свои ключи без правки .env.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SETTING_CLIENT_ID = "linkedin.oauth.client_id"
SETTING_CLIENT_SECRET = "linkedin.oauth.client_secret"
SETTING_REDIRECT_URI = "linkedin.oauth.redirect_uri"
SETTING_APPROVAL_MODE = "linkedin.send.approval_mode"
SETTING_DEFAULT_TIMEZONE = "linkedin.schedule.timezone"
SETTING_DAILY_INVITES_CAP = "linkedin.safety.daily_invites_cap"
SETTING_DAILY_MESSAGES_CAP = "linkedin.safety.daily_messages_cap"
SETTING_WEEKLY_INVITES_CAP = "linkedin.safety.weekly_invites_cap"

_ALL_SETTING_KEYS = (
    SETTING_CLIENT_ID,
    SETTING_CLIENT_SECRET,
    SETTING_REDIRECT_URI,
    SETTING_APPROVAL_MODE,
    SETTING_DEFAULT_TIMEZONE,
    SETTING_DAILY_INVITES_CAP,
    SETTING_DAILY_MESSAGES_CAP,
    SETTING_WEEKLY_INVITES_CAP,
)


def _env_str(key: str) -> str:
    return (os.getenv(key) or "").strip()


def _db_setting(db: "Session", key: str) -> Any | None:
    from backend.models import Setting

    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    return row.value if row is not None else None


def _set_db_setting(db: "Session", key: str, value: Any) -> None:
    from backend.models import Setting

    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def get_oauth_client_id(db: "Session") -> str:
    return _db_setting(db, SETTING_CLIENT_ID) or _env_str("FB_MASTER_LINKEDIN_CLIENT_ID")


def get_oauth_client_secret(db: "Session") -> str:
    return _db_setting(db, SETTING_CLIENT_SECRET) or _env_str("FB_MASTER_LINKEDIN_CLIENT_SECRET")


def get_oauth_redirect_uri(db: "Session") -> str:
    val = _db_setting(db, SETTING_REDIRECT_URI) or _env_str("FB_MASTER_LINKEDIN_REDIRECT_URI")
    if val:
        return val
    from backend.config import APP_BASE_URL

    base = (APP_BASE_URL or "http://127.0.0.1:8799").rstrip("/")
    return f"{base}/linkedin/oauth/callback"


def get_approval_mode(db: "Session") -> str:
    val = _db_setting(db, SETTING_APPROVAL_MODE) or _env_str("FB_MASTER_LINKEDIN_SEND_APPROVAL")
    raw = (val or "").strip().lower()
    if raw in ("manual", "semi", "auto"):
        return raw
    return "manual"


def get_default_timezone(db: "Session") -> str:
    val = _db_setting(db, SETTING_DEFAULT_TIMEZONE) or _env_str("FB_MASTER_LINKEDIN_TIMEZONE")
    return val or "Europe/Madrid"


def _safe_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def get_daily_invites_cap(db: "Session") -> int:
    """LinkedIn рекомендует не более ~20 invitations в день для свежего аккаунта.

    Default=15, hard-cap=80 (LinkedIn weekly soft-limit ≈ 100/week).
    """
    return _safe_int(_db_setting(db, SETTING_DAILY_INVITES_CAP), default=15, lo=0, hi=80)


def get_daily_messages_cap(db: "Session") -> int:
    """Default=30 messages/day (только тем, кто в connections)."""
    return _safe_int(_db_setting(db, SETTING_DAILY_MESSAGES_CAP), default=30, lo=0, hi=200)


def get_weekly_invites_cap(db: "Session") -> int:
    """LinkedIn weekly invite cap (~100/week soft-limit на 2024+). Default=80."""
    return _safe_int(_db_setting(db, SETTING_WEEKLY_INVITES_CAP), default=80, lo=0, hi=200)


def is_oauth_configured(db: "Session") -> bool:
    return bool(get_oauth_client_id(db) and get_oauth_client_secret(db))


def view_settings(db: "Session") -> dict[str, Any]:
    """Снимок настроек для формы — секрет маскируется."""
    secret = get_oauth_client_secret(db)
    return {
        "client_id": get_oauth_client_id(db),
        "client_secret_set": bool(secret),
        "client_secret_masked": ("•" * 8) if secret else "",
        "redirect_uri": get_oauth_redirect_uri(db),
        "approval_mode": get_approval_mode(db),
        "timezone": get_default_timezone(db),
        "daily_invites_cap": get_daily_invites_cap(db),
        "daily_messages_cap": get_daily_messages_cap(db),
        "weekly_invites_cap": get_weekly_invites_cap(db),
    }


def save_settings(db: "Session", payload: dict[str, Any]) -> None:
    allowed = dict(payload or {})
    _set_db_setting(db, SETTING_CLIENT_ID, str(allowed.get(SETTING_CLIENT_ID) or "").strip())
    _set_db_setting(db, SETTING_CLIENT_SECRET, str(allowed.get(SETTING_CLIENT_SECRET) or "").strip())
    _set_db_setting(db, SETTING_REDIRECT_URI, str(allowed.get(SETTING_REDIRECT_URI) or "").strip())
    am = str(allowed.get(SETTING_APPROVAL_MODE) or "").strip().lower()
    if am in ("manual", "semi", "auto"):
        _set_db_setting(db, SETTING_APPROVAL_MODE, am)
    tz = str(allowed.get(SETTING_DEFAULT_TIMEZONE) or "").strip()
    if tz:
        _set_db_setting(db, SETTING_DEFAULT_TIMEZONE, tz)
    for key, lo, hi in (
        (SETTING_DAILY_INVITES_CAP, 0, 80),
        (SETTING_DAILY_MESSAGES_CAP, 0, 200),
        (SETTING_WEEKLY_INVITES_CAP, 0, 200),
    ):
        try:
            n = int(allowed.get(key))
            _set_db_setting(db, key, max(lo, min(hi, n)))
        except (TypeError, ValueError):
            pass


__all__ = [
    "SETTING_APPROVAL_MODE",
    "SETTING_CLIENT_ID",
    "SETTING_CLIENT_SECRET",
    "SETTING_DAILY_INVITES_CAP",
    "SETTING_DAILY_MESSAGES_CAP",
    "SETTING_DEFAULT_TIMEZONE",
    "SETTING_REDIRECT_URI",
    "SETTING_WEEKLY_INVITES_CAP",
    "get_approval_mode",
    "get_daily_invites_cap",
    "get_daily_messages_cap",
    "get_default_timezone",
    "get_oauth_client_id",
    "get_oauth_client_secret",
    "get_oauth_redirect_uri",
    "get_weekly_invites_cap",
    "is_oauth_configured",
    "save_settings",
    "view_settings",
]
