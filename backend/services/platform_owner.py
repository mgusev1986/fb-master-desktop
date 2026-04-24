"""Доступ владельца платформы (глобальная админка, не путать с ролью в организации)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy.orm import Session

from backend.config import PLATFORM_OWNER_EMAILS_SET, dev_login_allowed
from backend.models import AdminUser
from backend.services.tenancy import effective_organization_id

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


def default_platform_owner_email() -> str | None:
    """
    Email владельца для аварийного входа через скрытый gate.
    На проде используем первый email из FB_MASTER_OWNER_EMAILS, локально можно
    откатиться к dev@localhost.
    """
    if PLATFORM_OWNER_EMAILS_SET:
        return sorted(PLATFORM_OWNER_EMAILS_SET)[0]
    if dev_login_allowed():
        return _DEV_LOGIN_OWNER_EMAIL
    return None


def establish_platform_owner_session(
    request: Request,
    db: Session,
    email: str | None = None,
) -> dict | None:
    """
    Создаёт веб-сессию владельца платформы без OAuth.
    Используется как аварийный вход через секретный gate, когда обычный веб-логин отключён.
    """
    target_email = (email or default_platform_owner_email() or "").strip().lower()
    if not target_email or not is_platform_owner_email(target_email):
        return None

    admin = db.query(AdminUser).filter(AdminUser.email == target_email).first()
    now = datetime.now(timezone.utc)
    default_name = "Developer" if target_email == _DEV_LOGIN_OWNER_EMAIL else "Platform Owner"

    if not admin:
        admin = AdminUser(
            email=target_email,
            name=default_name,
            last_login_at=now,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
    else:
        if not (admin.name or "").strip():
            admin.name = default_name
        admin.last_login_at = now
        db.commit()

    request.session["user"] = {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name,
        "picture": admin.picture_url,
    }
    effective_organization_id(request, db, admin.id)
    return request.session["user"]
