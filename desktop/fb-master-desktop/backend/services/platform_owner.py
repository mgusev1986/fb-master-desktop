"""Доступ владельца платформы (глобальная админка, не путать с ролью в организации)."""

from __future__ import annotations

from backend.config import PLATFORM_OWNER_EMAILS_SET, dev_login_allowed

# Вход «Developer» из /auth/dev-login — тот же доступ к «Платформа», что у FB_MASTER_OWNER_EMAILS.
_DEV_LOGIN_OWNER_EMAIL = "dev@localhost"


def is_platform_owner_email(email: str | None) -> bool:
    e = (email or "").strip().lower()
    if not e:
        return False
    if e == _DEV_LOGIN_OWNER_EMAIL and dev_login_allowed():
        return True
    if not PLATFORM_OWNER_EMAILS_SET:
        return False
    return e in PLATFORM_OWNER_EMAILS_SET


def is_platform_owner_user(user: dict | None) -> bool:
    if not user:
        return False
    return is_platform_owner_email(user.get("email"))
