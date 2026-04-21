"""Публичное API активации ключа для десктопа с локальным бэкендом (проверка на VPS)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import AccessKey, ClientMachinePresence
from backend.services.access_keys import (
    bind_access_key_to_device,
    expires_at_is_past,
    hmac_compare_fp,
)

router = APIRouter(prefix="/api/public", tags=["public"])
logger = logging.getLogger(__name__)


class DesktopLicenseActivateIn(BaseModel):
    key: str = Field(min_length=1)
    device_fingerprint: str = Field(min_length=8)


@router.post("/desktop-license/activate")
def desktop_license_activate(payload: DesktopLicenseActivateIn, db: Session = Depends(get_db)):
    ok, err, row = bind_access_key_to_device(
        db,
        plaintext=payload.key.strip(),
        device_fingerprint=payload.device_fingerprint.strip(),
    )
    if not ok or row is None:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    exp = row.expires_at
    return {
        "ok": True,
        "expires_at": exp.isoformat() if exp else None,
        "label": row.label,
    }


class ClientPresenceReportIn(BaseModel):
    """Пульс с десктопа: привязка к активированному ключу по device_fingerprint (как при активации)."""

    installation_id: str = Field(min_length=32, max_length=40)
    device_fingerprint: str = Field(min_length=8, max_length=128)
    hostname: str | None = Field(None, max_length=255)
    desktop_app_version: str | None = Field(None, max_length=64)


@router.post("/client-presence")
def client_presence_report(payload: ClientPresenceReportIn, db: Session = Depends(get_db)):
    """
    Локальный SQLite на ПК клиента не виден админке на VPS — десктоп шлёт сюда heartbeat.
    Доступ только если тот же device_fingerprint привязан к неотозванному ключу.
    """
    fp = payload.device_fingerprint.strip()
    low = fp.lower()
    row = (
        db.query(AccessKey)
        .filter(AccessKey.revoked_at.is_(None))
        .filter(AccessKey.device_fingerprint.isnot(None))
        .filter(func.lower(AccessKey.device_fingerprint) == low)
        .order_by(AccessKey.id.desc())
        .first()
    )
    if not row or expires_at_is_past(row.expires_at):
        return JSONResponse({"ok": False, "error": "invalid_device"}, status_code=401)
    if not hmac_compare_fp(row.device_fingerprint or "", fp):
        return JSONResponse({"ok": False, "error": "invalid_device"}, status_code=401)

    iid = payload.installation_id.strip()[:40]
    now = datetime.now(timezone.utc)
    host = (payload.hostname or "").strip()[:255] or None
    ver = (payload.desktop_app_version or "").strip()[:64] or None

    pr = db.get(ClientMachinePresence, iid)
    try:
        if pr:
            pr.access_key_id = row.id
            pr.admin_user_id = None
            pr.organization_id = None
            pr.hostname = host
            pr.desktop_app_version = ver
            pr.last_seen_at = now
        else:
            db.add(
                ClientMachinePresence(
                    installation_id=iid,
                    access_key_id=row.id,
                    admin_user_id=None,
                    organization_id=None,
                    hostname=host,
                    desktop_app_version=ver,
                    last_seen_at=now,
                    created_at=now,
                )
            )
        db.commit()
    except Exception:
        logger.exception("client_presence_report")
        db.rollback()
        return JSONResponse({"ok": False, "error": "server"}, status_code=500)
    return {"ok": True}
