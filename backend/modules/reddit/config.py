"""Reddit-специфичные env + Setting helpers.

OAuth creds и user-agent хранятся в `Setting` (с префиксом `reddit.*`),
а не в env — чтобы клиент мог ввести свои ключи через UI. Fallback:
env `FB_MASTER_REDDIT_*` если задано.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SETTING_CLIENT_ID = "reddit.oauth.client_id"
SETTING_CLIENT_SECRET = "reddit.oauth.client_secret"
SETTING_REDIRECT_URI = "reddit.oauth.redirect_uri"
SETTING_USER_AGENT = "reddit.http.user_agent"
SETTING_APPROVAL_MODE = "reddit.send.approval_mode"
SETTING_DEFAULT_TIMEZONE = "reddit.schedule.timezone"
SETTING_PER_DAY_CAP = "reddit.safety.per_day_cap"
SETTING_PER_HOUR_CAP = "reddit.safety.per_hour_cap"

_ALL_SETTING_KEYS = (
    SETTING_CLIENT_ID,
    SETTING_CLIENT_SECRET,
    SETTING_REDIRECT_URI,
    SETTING_USER_AGENT,
    SETTING_APPROVAL_MODE,
    SETTING_DEFAULT_TIMEZONE,
    SETTING_PER_DAY_CAP,
    SETTING_PER_HOUR_CAP,
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
    return _db_setting(db, SETTING_CLIENT_ID) or _env_str("FB_MASTER_REDDIT_CLIENT_ID")


def get_oauth_client_secret(db: "Session") -> str:
    return _db_setting(db, SETTING_CLIENT_SECRET) or _env_str("FB_MASTER_REDDIT_CLIENT_SECRET")


def get_oauth_redirect_uri(db: "Session") -> str:
    val = _db_setting(db, SETTING_REDIRECT_URI) or _env_str("FB_MASTER_REDDIT_REDIRECT_URI")
    if val:
        return val
    # default: локальный backend
    from backend.config import APP_BASE_URL

    base = (APP_BASE_URL or "http://127.0.0.1:8799").rstrip("/")
    return f"{base}/reddit/oauth/callback"


def get_user_agent(db: "Session") -> str:
    val = _db_setting(db, SETTING_USER_AGENT) or _env_str("FB_MASTER_REDDIT_USER_AGENT")
    if val:
        return val
    return "fb-master-desktop/2.19 (by /u/unknown)"


def get_approval_mode(db: "Session") -> str:
    val = _db_setting(db, SETTING_APPROVAL_MODE) or _env_str("FB_MASTER_REDDIT_SEND_APPROVAL")
    raw = (val or "").strip().lower()
    if raw in ("manual", "semi", "auto"):
        return raw
    return "manual"


def get_default_timezone(db: "Session") -> str:
    val = _db_setting(db, SETTING_DEFAULT_TIMEZONE) or _env_str("FB_MASTER_REDDIT_TIMEZONE")
    return val or "Europe/Madrid"


def get_per_day_cap(db: "Session") -> int:
    val = _db_setting(db, SETTING_PER_DAY_CAP)
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = 10
    return max(0, min(500, n))


def get_per_hour_cap(db: "Session") -> int:
    val = _db_setting(db, SETTING_PER_HOUR_CAP)
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = 3
    return max(0, min(100, n))


def save_settings(db: "Session", payload: dict[str, Any]) -> None:
    """Сохранить все reddit.* настройки из dict (только known keys)."""
    allowed = dict(payload or {})
    _set_db_setting(db, SETTING_CLIENT_ID, str(allowed.get(SETTING_CLIENT_ID) or "").strip())
    _set_db_setting(db, SETTING_CLIENT_SECRET, str(allowed.get(SETTING_CLIENT_SECRET) or "").strip())
    _set_db_setting(db, SETTING_REDIRECT_URI, str(allowed.get(SETTING_REDIRECT_URI) or "").strip())
    _set_db_setting(db, SETTING_USER_AGENT, str(allowed.get(SETTING_USER_AGENT) or "").strip())
    am = str(allowed.get(SETTING_APPROVAL_MODE) or "").strip().lower()
    if am in ("manual", "semi", "auto"):
        _set_db_setting(db, SETTING_APPROVAL_MODE, am)
    tz = str(allowed.get(SETTING_DEFAULT_TIMEZONE) or "").strip()
    if tz:
        _set_db_setting(db, SETTING_DEFAULT_TIMEZONE, tz)
    try:
        pd = int(allowed.get(SETTING_PER_DAY_CAP))
        _set_db_setting(db, SETTING_PER_DAY_CAP, max(0, min(500, pd)))
    except (TypeError, ValueError):
        pass
    try:
        ph = int(allowed.get(SETTING_PER_HOUR_CAP))
        _set_db_setting(db, SETTING_PER_HOUR_CAP, max(0, min(100, ph)))
    except (TypeError, ValueError):
        pass


def is_oauth_configured(db: "Session") -> bool:
    return bool(get_oauth_client_id(db) and get_oauth_client_secret(db))


def view_settings(db: "Session") -> dict[str, Any]:
    """Снимок настроек для формы — без секрета (маскируем secret)."""
    secret = get_oauth_client_secret(db)
    return {
        "client_id": get_oauth_client_id(db),
        "client_secret_set": bool(secret),
        "client_secret_masked": ("•" * 8) if secret else "",
        "redirect_uri": get_oauth_redirect_uri(db),
        "user_agent": get_user_agent(db),
        "approval_mode": get_approval_mode(db),
        "timezone": get_default_timezone(db),
        "per_day_cap": get_per_day_cap(db),
        "per_hour_cap": get_per_hour_cap(db),
    }


__all__ = [
    "SETTING_APPROVAL_MODE",
    "SETTING_CLIENT_ID",
    "SETTING_CLIENT_SECRET",
    "SETTING_DEFAULT_TIMEZONE",
    "SETTING_PER_DAY_CAP",
    "SETTING_PER_HOUR_CAP",
    "SETTING_REDIRECT_URI",
    "SETTING_USER_AGENT",
    "get_approval_mode",
    "get_default_timezone",
    "get_oauth_client_id",
    "get_oauth_client_secret",
    "get_oauth_redirect_uri",
    "get_per_day_cap",
    "get_per_hour_cap",
    "get_user_agent",
    "is_oauth_configured",
    "save_settings",
    "view_settings",
]
