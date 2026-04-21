"""Мультитенантность: организация в сессии, членство, фильтрация запросов."""

from __future__ import annotations

import re
import secrets

from fastapi import HTTPException, Request
from sqlalchemy.orm import Query, Session

from backend.models import (
    AdminUser,
    Donor,
    FBAccount,
    ImportBatch,
    Job,
    Organization,
    OrganizationMember,
    OutreachCampaign,
    Person,
    SequenceCampaign,
    Template,
    WarmupCampaign,
)


def _slugify_base(email: str) -> str:
    local = (email or "org").split("@")[0].lower()
    local = re.sub(r"[^a-z0-9]+", "-", local).strip("-")[:40] or "org"
    return local


def ensure_user_organization(db: Session, user: AdminUser) -> Organization:
    """
    У пользователя должен быть хотя бы один кабинет (организация).
    При первом входе создаём личную организацию.
    """
    row = (
        db.query(OrganizationMember)
        .filter(OrganizationMember.user_id == user.id)
        .order_by(OrganizationMember.id.asc())
        .first()
    )
    if row:
        org = db.get(Organization, row.organization_id)
        if org and org.is_active:
            return org

    base = _slugify_base(user.email)
    for _ in range(20):
        slug = f"{base}-{secrets.token_hex(3)}"
        exists = db.query(Organization.id).filter(Organization.slug == slug).first()
        if not exists:
            org = Organization(
                name=f"Кабинет {user.email}",
                slug=slug,
                is_active=True,
            )
            db.add(org)
            db.flush()
            db.add(
                OrganizationMember(
                    organization_id=org.id,
                    user_id=user.id,
                    role="owner",
                )
            )
            db.commit()
            db.refresh(org)
            return org

    raise RuntimeError("Could not allocate unique organization slug")


def organizations_for_user(db: Session, user_id: int) -> list[Organization]:
    q = (
        db.query(Organization)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .filter(OrganizationMember.user_id == user_id, Organization.is_active.is_(True))
        .order_by(Organization.id.asc())
    )
    return list(q)


def set_session_organization(request: Request, db: Session, user_id: int, organization_id: int) -> None:
    ok = (
        db.query(OrganizationMember.id)
        .filter(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == organization_id,
        )
        .first()
    )
    if not ok:
        raise HTTPException(status_code=403, detail="Нет доступа к этой организации")
    request.session["organization_id"] = organization_id


def require_org_id(request: Request, db: Session) -> int:
    """Текущая организация для авторизованного пользователя (вызывать из роутеров с get_db)."""
    user = request.session.get("user") or {}
    uid = int(user["id"])
    return effective_organization_id(request, db, uid)


def default_organization_id_for_system_jobs(db: Session) -> int:
    """
    Первая активная организация — для фоновых задач без HTTP-сессии (потоки, планировщики).
    Если активных нет — любая по id; иначе 1 (FK не падает на пустой БД).
    """
    row = (
        db.query(Organization.id)
        .filter(Organization.is_active.is_(True))
        .order_by(Organization.id.asc())
        .first()
    )
    if row:
        return int(row[0])
    row2 = db.query(Organization.id).order_by(Organization.id.asc()).first()
    return int(row2[0]) if row2 else 1


def effective_organization_id(request: Request, db: Session, user_id: int) -> int:
    """ID текущей организации в сессии; при отсутствии — первая организация пользователя."""
    raw = request.session.get("organization_id")
    if raw is not None:
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            oid = 0
        if oid > 0:
            ok = (
                db.query(OrganizationMember.id)
                .filter(
                    OrganizationMember.user_id == user_id,
                    OrganizationMember.organization_id == oid,
                )
                .first()
            )
            if ok:
                return oid

    user = db.get(AdminUser, user_id)
    if not user:
        return 1
    org = ensure_user_organization(db, user)
    request.session["organization_id"] = org.id
    return org.id


# --- Фильтры по organization_id (единый стиль для роутеров) ---


def donors_in_org(q: Query, org_id: int) -> Query:
    return q.filter(Donor.organization_id == org_id)


def people_in_org(q: Query, org_id: int) -> Query:
    return q.filter(Person.organization_id == org_id)


def fb_accounts_in_org(q: Query, org_id: int) -> Query:
    return q.filter(FBAccount.organization_id == org_id)


def templates_in_org(q: Query, org_id: int) -> Query:
    return q.filter(Template.organization_id == org_id)


def jobs_in_org(q: Query, org_id: int) -> Query:
    return q.filter(Job.organization_id == org_id)


def import_batches_in_org(q: Query, org_id: int) -> Query:
    return q.filter(ImportBatch.organization_id == org_id)


def outreach_campaigns_in_org(q: Query, org_id: int) -> Query:
    return q.filter(OutreachCampaign.organization_id == org_id)


def warmup_campaigns_in_org(q: Query, org_id: int) -> Query:
    return q.filter(WarmupCampaign.organization_id == org_id)


def sequence_campaigns_in_org(q: Query, org_id: int) -> Query:
    return q.filter(SequenceCampaign.organization_id == org_id)
