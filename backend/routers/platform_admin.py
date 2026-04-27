"""Панель владельца платформы: агрегированная статистика по всем организациям."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.database import get_db, using_postgresql
from backend.models import (
    AccessKey,
    AdminUser,
    BillingRenewalOrder,
    ClientMachinePresence,
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
from backend.services.platform_owner import is_platform_owner_user
from backend.services.preview_as_user import set_preview_as_user

router = APIRouter(tags=["platform_admin"])

_ACCESS_KEY_DURATION_VALUES = frozenset(("forever", "m3", "30", "90", "180", "365", "custom"))


def _access_key_expires_at_for_duration(duration: str) -> datetime | None:
    """Срок с момента выдачи; None — бессрочно.

    Поддерживает кастомный формат `custom:<value>:<unit>`, где unit ∈ {m,h,d}
    (минуты/часы/дни). Например: `custom:40:d` = 40 дней, `custom:90:m` = 90 минут,
    `custom:12:h` = 12 часов. При невалидном вводе fallback → None (бессрочно).
    """
    d = (duration or "forever").strip().lower()
    now = datetime.now(timezone.utc)
    if d.startswith("custom:"):
        parts = d.split(":", 2)
        if len(parts) == 3:
            try:
                val = int(parts[1])
            except (TypeError, ValueError):
                val = 0
            unit = (parts[2] or "d").strip().lower()
            if val > 0 and val <= 1_000_000 and unit in ("m", "h", "d"):
                if unit == "m":
                    return now + timedelta(minutes=val)
                if unit == "h":
                    return now + timedelta(hours=val)
                return now + timedelta(days=val)
        return None
    if d not in _ACCESS_KEY_DURATION_VALUES:
        d = "forever"
    if d == "m3":
        return now + timedelta(minutes=3)
    if d == "30":
        return now + timedelta(days=30)
    if d == "90":
        return now + timedelta(days=90)
    if d == "180":
        return now + timedelta(days=180)
    if d == "365":
        return now + timedelta(days=365)
    return None


def _require_platform_owner(request: Request) -> dict:
    user = request.session.get("user") or {}
    if not is_platform_owner_user(user):
        raise HTTPException(status_code=404, detail="Not found")
    return user


def _tech_snapshot() -> dict:
    """Без секретов: только флаги и пути для диагностики."""
    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "database_kind": "PostgreSQL" if using_postgresql() else "SQLite",
        "data_dir": str(app_config.DATA_DIR),
        "log_dir": str(app_config.LOG_DIR),
        "browser_profiles_dir": str(app_config.BROWSER_PROFILES_DIR),
        "base_fb_dir": str(app_config.BASE_FB_DIR),
        "app_base_url": app_config.APP_BASE_URL,
        "listen": f"{app_config.BO_HOST}:{app_config.BO_PORT}",
        "supabase_auth_configured": bool(app_config.SUPABASE_URL and app_config.SUPABASE_ANON_KEY),
        "google_oauth_configured": bool(app_config.GOOGLE_CLIENT_ID),
        "dev_login_allowed": app_config.dev_login_allowed(),
        "session_cookie_secure": app_config.SESSION_COOKIE_SECURE,
        "local_simulate_production": app_config.LOCAL_SIMULATE_PRODUCTION,
        "access_key_required": app_config.fb_master_access_key_required(),
        "playwright_max_concurrent": app_config.PLAYWRIGHT_MAX_CONCURRENT,
        "warmup_headless": app_config.effective_warmup_headless(),
        "log_level": app_config.LOG_LEVEL,
        "fb_local_chrome_cookies": app_config.effective_fb_local_chrome_cookies(),
        "sequence_timezone": app_config.SEQUENCE_TIMEZONE,
        "messenger_e2ee_wait_seconds": app_config.MESSENGER_E2EE_WAIT_SECONDS,
        "openai_configured": bool(app_config.OPENAI_API_KEY),
        "google_api_key_configured": bool(app_config.GOOGLE_API_KEY),
        "desktop_update_configured": bool(
            app_config.desktop_update_latest_version() and app_config.desktop_update_download_urls()
        ),
        "desktop_dev_update_configured": bool(
            app_config.desktop_dev_update_latest_version() and app_config.desktop_dev_update_download_urls()
        ),
        "nowpayments_billing": app_config.nowpayments_enabled(),
    }


@router.post("/admin/platform/preview-as-user")
async def platform_preview_as_user_post(
    request: Request,
    enable: str = Form("0"),
    next_path: str = Form("/admin/platform", alias="next"),
):
    _require_platform_owner(request)
    nxt = (next_path or "/admin/platform").strip()
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/admin/platform"
    on = str(enable).strip().lower() in ("1", "true", "yes", "on")
    set_preview_as_user(request, on)
    return RedirectResponse(nxt, status_code=303)


@router.get("/admin/platform")
async def platform_admin_page(request: Request, db: Session = Depends(get_db)):
    _require_platform_owner(request)

    n_users = db.query(func.count(AdminUser.id)).scalar() or 0
    n_orgs = db.query(func.count(Organization.id)).scalar() or 0
    n_members = db.query(func.count(OrganizationMember.id)).scalar() or 0
    n_people = db.query(func.count(Person.id)).scalar() or 0
    n_donors = db.query(func.count(Donor.id)).scalar() or 0
    n_fb = db.query(func.count(FBAccount.id)).scalar() or 0
    n_campaigns = db.query(func.count(OutreachCampaign.id)).scalar() or 0
    n_jobs_active = (
        db.query(func.count(Job.id))
        .filter(Job.status.in_(("queued", "running")))
        .scalar()
        or 0
    )
    n_jobs_total = db.query(func.count(Job.id)).scalar() or 0
    n_templates = db.query(func.count(Template.id)).scalar() or 0
    n_import_batches = db.query(func.count(ImportBatch.id)).scalar() or 0
    n_warmup = db.query(func.count(WarmupCampaign.id)).scalar() or 0
    n_sequences = db.query(func.count(SequenceCampaign.id)).scalar() or 0

    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    n_users_active_7d = (
        db.query(func.count(AdminUser.id))
        .filter(AdminUser.last_login_at.isnot(None), AdminUser.last_login_at >= cutoff_7d)
        .scalar()
        or 0
    )

    job_status_rows = db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    job_by_status = sorted(
        [{"status": row[0] or "—", "count": int(row[1])} for row in job_status_rows],
        key=lambda x: (-x["count"], x["status"]),
    )

    org_rows = db.query(Organization).order_by(Organization.id.asc()).all()
    org_stats = []
    for org in org_rows:
        org_stats.append(
            {
                "org": org,
                "members": db.query(OrganizationMember)
                .filter(OrganizationMember.organization_id == org.id)
                .count(),
                "people": db.query(Person).filter(Person.organization_id == org.id).count(),
                "donors": db.query(Donor).filter(Donor.organization_id == org.id).count(),
                "fb_accounts": db.query(FBAccount).filter(FBAccount.organization_id == org.id).count(),
            }
        )

    recent_users = (
        db.query(AdminUser).order_by(AdminUser.created_at.desc().nullslast()).limit(30).all()
    )

    recent_jobs = db.query(Job).order_by(Job.created_at.desc()).limit(15).all()

    now = datetime.now(timezone.utc)
    try:
        online_sec = int((os.getenv("FBM_CLIENT_ONLINE_THRESHOLD_SEC") or "120").strip())
    except ValueError:
        online_sec = 120
    online_sec = max(30, min(3600, online_sec))
    online_delta = timedelta(seconds=online_sec)
    try:
        interval_sec = float((os.getenv("FBM_CLIENT_PRESENCE_INTERVAL_SEC") or "45").strip())
    except ValueError:
        interval_sec = 45.0
    interval_sec = max(15.0, min(600.0, interval_sec))

    presence_rows = (
        db.query(ClientMachinePresence)
        .order_by(ClientMachinePresence.last_seen_at.desc().nullslast())
        .limit(300)
        .all()
    )
    machine_presences = []
    for pr in presence_rows:
        admin = db.get(AdminUser, pr.admin_user_id) if pr.admin_user_id else None
        org = db.get(Organization, pr.organization_id) if pr.organization_id else None
        ak = db.get(AccessKey, pr.access_key_id) if getattr(pr, "access_key_id", None) else None
        ls = pr.last_seen_at
        online = False
        if ls:
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=timezone.utc)
            online = (now - ls) <= online_delta
        machine_presences.append(
            {
                "installation_id": pr.installation_id,
                "admin_email": admin.email if admin else None,
                "admin_name": admin.name if admin else None,
                "org_id": pr.organization_id,
                "org_name": org.name if org else None,
                "access_key_id": getattr(pr, "access_key_id", None),
                "access_key_label": (ak.label or "").strip() if ak else None,
                "hostname": (pr.hostname or "").strip(),
                "desktop_app_version": (pr.desktop_app_version or "").strip(),
                "last_seen_at": pr.last_seen_at,
                "online": online,
            }
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "admin/platform.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "platform_admin",
            "machine_presences": machine_presences,
            "online_threshold_sec": online_sec,
            "presence_interval_sec": interval_sec,
            "presence_db_is_postgresql": using_postgresql(),
            "presence_endpoint_hint": app_config.client_presence_remote_post_url(),
            "totals": {
                "users": int(n_users),
                "users_active_7d": int(n_users_active_7d),
                "organizations": int(n_orgs),
                "memberships": int(n_members),
                "people": int(n_people),
                "donors": int(n_donors),
                "fb_accounts": int(n_fb),
                "campaigns": int(n_campaigns),
                "jobs_active": int(n_jobs_active),
                "jobs_total": int(n_jobs_total),
                "templates": int(n_templates),
                "import_batches": int(n_import_batches),
                "warmup_campaigns": int(n_warmup),
                "sequence_campaigns": int(n_sequences),
            },
            "job_by_status": job_by_status,
            "tech": _tech_snapshot(),
            "org_stats": org_stats,
            "recent_users": recent_users,
            "recent_jobs": recent_jobs,
        },
    )


_SESSION_NEW_ACCESS_KEY = "access_key_plaintext_once"

_ACCESS_KEY_FILTER_VALUES = frozenset(("all", "active", "expired", "pending", "revoked"))


def _normalized_access_key_filter(raw: str | None) -> str:
    s = (raw or "all").strip().lower()
    return s if s in _ACCESS_KEY_FILTER_VALUES else "all"


def _access_key_status_sql_exprs(now: datetime):
    """Сегментация как в шаблоне: отозван → истёк → активен (есть устройство) → ожидает."""
    not_revoked = AccessKey.revoked_at.is_(None)
    not_expired = or_(AccessKey.expires_at.is_(None), AccessKey.expires_at >= now)
    expired_row = and_(not_revoked, AccessKey.expires_at.isnot(None), AccessKey.expires_at < now)
    return not_revoked, not_expired, expired_row


def _access_key_counts(db: Session, now: datetime) -> dict[str, int]:
    not_revoked, not_expired, expired_row = _access_key_status_sql_exprs(now)
    total = int(db.query(func.count(AccessKey.id)).scalar() or 0)
    n_revoked = int(db.query(func.count(AccessKey.id)).filter(AccessKey.revoked_at.isnot(None)).scalar() or 0)
    n_expired = int(db.query(func.count(AccessKey.id)).filter(expired_row).scalar() or 0)
    n_active = int(
        db.query(func.count(AccessKey.id))
        .filter(not_revoked, not_expired, AccessKey.device_fingerprint.isnot(None))
        .scalar()
        or 0
    )
    n_pending = int(
        db.query(func.count(AccessKey.id))
        .filter(not_revoked, not_expired, AccessKey.device_fingerprint.is_(None))
        .scalar()
        or 0
    )
    return {
        "all": total,
        "active": n_active,
        "expired": n_expired,
        "pending": n_pending,
        "revoked": n_revoked,
    }


@router.get("/admin/platform/access-keys")
async def platform_access_keys_page(request: Request, db: Session = Depends(get_db), status: str = "all"):
    _require_platform_owner(request)
    filt = _normalized_access_key_filter(status)
    now = datetime.now(timezone.utc)
    not_revoked, not_expired, expired_row = _access_key_status_sql_exprs(now)

    q = db.query(AccessKey).order_by(AccessKey.id.desc())
    if filt == "revoked":
        q = q.filter(AccessKey.revoked_at.isnot(None))
    elif filt == "expired":
        q = q.filter(expired_row)
    elif filt == "active":
        q = q.filter(not_revoked, not_expired, AccessKey.device_fingerprint.isnot(None))
    elif filt == "pending":
        q = q.filter(not_revoked, not_expired, AccessKey.device_fingerprint.is_(None))

    keys = q.limit(200).all()
    flash_plain = (request.session.pop(_SESSION_NEW_ACCESS_KEY, None) or "").strip()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "admin/platform_access_keys.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "platform_access_keys",
            "keys": keys,
            "new_key_plaintext": flash_plain,
            "filter_status": filt,
            "access_key_counts": _access_key_counts(db, now),
        },
    )


@router.post("/admin/platform/access-keys/create")
async def platform_access_keys_create(
    request: Request,
    db: Session = Depends(get_db),
    label: str = Form(""),
    duration: str = Form("forever"),
    custom_value: str = Form(""),
    custom_unit: str = Form("d"),
):
    _require_platform_owner(request)
    from backend.services.access_key_crypto import generate_plaintext_key, hash_access_key

    from backend.services.fb_credentials_crypto import encrypt_secret

    plain = generate_plaintext_key()
    h = hash_access_key(plain)
    if not h:
        raise HTTPException(status_code=500, detail="hash failed")
    enc = encrypt_secret(plain)
    # Кастомный период (например 40 дней / 90 минут / 12 часов).
    duration_value = (duration or "forever").strip().lower()
    if duration_value == "custom":
        cv = (custom_value or "").strip()
        cu = (custom_unit or "d").strip().lower()
        if cu not in ("m", "h", "d"):
            cu = "d"
        duration_value = f"custom:{cv}:{cu}"
    row = AccessKey(
        key_hash=h,
        key_plain_enc=enc,
        label=(label or "").strip() or None,
        expires_at=_access_key_expires_at_for_duration(duration_value),
    )
    db.add(row)
    db.commit()
    request.session[_SESSION_NEW_ACCESS_KEY] = plain
    return RedirectResponse("/admin/platform/access-keys?status=pending", status_code=303)


@router.post("/admin/platform/access-keys/{key_id}/reveal")
async def platform_access_key_reveal(
    request: Request,
    key_id: int,
    db: Session = Depends(get_db),
):
    """Вернуть plaintext ключа только владельцу платформы (хранится в БД в виде Fernet)."""
    _require_platform_owner(request)
    from backend.services.fb_credentials_crypto import decrypt_secret

    row = db.get(AccessKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    blob = (row.key_plain_enc or "").strip()
    if not blob:
        return JSONResponse(
            {
                "ok": False,
                "error": "no_encrypted_copy",
                "message": "Ключ выдан до включения сохранения копии; восстановить фразу из базы нельзя.",
            },
            status_code=200,
        )
    plain = decrypt_secret(blob)
    if not plain:
        return JSONResponse(
            {
                "ok": False,
                "error": "decrypt_failed",
                "message": "Не удалось расшифровать (проверьте SECRET_KEY на сервере).",
            },
            status_code=200,
        )
    return JSONResponse({"ok": True, "key": plain})


@router.post("/admin/platform/access-keys/{key_id}/revoke")
async def platform_access_keys_revoke(
    request: Request,
    key_id: int,
    db: Session = Depends(get_db),
    return_status: str = Form("all"),
):
    _require_platform_owner(request)

    row = db.get(AccessKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    row.revoked_at = datetime.now(timezone.utc)
    db.add(row)
    db.commit()
    st = _normalized_access_key_filter(return_status)
    return RedirectResponse(
        f"/admin/platform/access-keys?status={quote(st)}",
        status_code=303,
    )


@router.post("/admin/platform/access-keys/{key_id}/delete")
async def platform_access_keys_delete(
    request: Request,
    key_id: int,
    db: Session = Depends(get_db),
    confirm_delete: str = Form(""),
    return_status: str = Form("all"),
):
    """Полное удаление записи ключа из БД (после явного согласия в форме)."""
    _require_platform_owner(request)
    st = _normalized_access_key_filter(return_status)
    consent = (confirm_delete or "").strip().lower()
    if consent not in ("yes", "on", "1", "true"):
        return RedirectResponse(
            f"/admin/platform/access-keys?status={quote(st)}&err=delete_no_consent",
            status_code=303,
        )
    row = db.get(AccessKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    db.query(ClientMachinePresence).filter(ClientMachinePresence.access_key_id == key_id).update(
        {ClientMachinePresence.access_key_id: None},
        synchronize_session=False,
    )
    db.query(BillingRenewalOrder).filter(BillingRenewalOrder.access_key_id == key_id).update(
        {BillingRenewalOrder.access_key_id: None},
        synchronize_session=False,
    )
    db.delete(row)
    db.commit()
    return RedirectResponse(
        f"/admin/platform/access-keys?status={quote(st)}&msg={quote('Ключ удалён из базы безвозвратно.')}",
        status_code=303,
    )
