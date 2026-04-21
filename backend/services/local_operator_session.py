"""Автовход без OAuth: один локальный оператор в БД и в сессии."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import AdminUser
from backend.services.tenancy import ensure_user_organization

LOCAL_OPERATOR_EMAIL = "operator@fb-master.local"


def ensure_no_auth_session_user(request: Request, db: Session) -> None:
    """Создаёт при необходимости пользователя и organization, заполняет request.session['user']."""
    if request.session.get("user"):
        return

    admin = db.query(AdminUser).filter(AdminUser.email == LOCAL_OPERATOR_EMAIL).first()
    if not admin:
        admin = AdminUser(email=LOCAL_OPERATOR_EMAIL, name="Локальный оператор")
        db.add(admin)
        try:
            db.commit()
            db.refresh(admin)
        except IntegrityError:
            db.rollback()
            admin = db.query(AdminUser).filter(AdminUser.email == LOCAL_OPERATOR_EMAIL).first()
            if not admin:
                raise

    ensure_user_organization(db, admin)

    request.session["user"] = {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name or "Локальный оператор",
        "picture": admin.picture_url,
    }
