"""Публичное API активации ключа для десктопа с локальным бэкендом (проверка на VPS).

v3.0+: добавлен POST /desktop-license/check — атомарная проверка валидности
ключа клиентом. Отвечает: {ok, expires_at, signature, probes, probes_signature}.
Подписи Ed25519 (см. backend.services.license_signer + daily_probes).
"""

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
from backend.services.daily_probes import (
    daily_probes_for_today,
    license_state_signing_payload,
    probes_signing_payload,
    probes_valid_until,
)
from backend.services.license_signer import sign_b64

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


class DesktopLicenseCheckIn(BaseModel):
    """v3.0+: Атомарная проверка валидности ключа клиентом.

    Клиент шлёт хеш ключа + отпечаток железа. VPS отвечает state + probes,
    оба подписаны Ed25519. Клиент проверяет подписи локально и доверяет
    содержимому без необходимости знать приватный ключ.
    """
    key_hash: str = Field(min_length=16, max_length=128)
    device_fingerprint: str = Field(min_length=8, max_length=128)


def _ru_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _build_probes_block(now: datetime) -> dict:
    """Готовит подписанный блок probes для ответа клиенту."""
    probes = daily_probes_for_today(now.date())
    valid_until = probes_valid_until(now)
    payload = probes_signing_payload(probes, valid_until)
    sig = sign_b64(payload)
    return {
        "probes": probes,
        "probes_valid_until": _ru_iso(valid_until),
        "probes_signature": sig,
    }


def _build_state_block(
    *,
    state: str,
    key_hash: str,
    device_fingerprint: str,
    expires_at: datetime | None,
    now: datetime,
) -> dict:
    """Готовит подписанный license_state."""
    payload = license_state_signing_payload(
        state=state,
        key_hash=key_hash,
        device_fingerprint=device_fingerprint,
        verified_at=now,
        expires_at=expires_at,
    )
    sig = sign_b64(payload)
    return {
        "state": state,
        "verified_at": _ru_iso(now),
        "expires_at": _ru_iso(expires_at),
        "license_signature": sig,
    }


@router.post("/desktop-license/check")
def desktop_license_check(payload: DesktopLicenseCheckIn, db: Session = Depends(get_db)):
    """Проверка валидности ключа + возврат подписанного state + probes.

    Возвращаемые поля (всегда status 200, клиент сам решает что делать):
    - ok: bool
    - state: "valid" | "invalid"
    - reason: "expired"|"revoked"|"wrong_device"|"not_found" (если invalid)
    - verified_at: ISO-UTC, момент проверки
    - expires_at: ISO-UTC | null, срок ключа (только если valid)
    - license_signature: base64 Ed25519 подпись state-блока
    - probes: [URL...], 6 эталонов на сегодня (только если valid)
    - probes_valid_until: ISO-UTC, срок действия probes
    - probes_signature: base64 Ed25519 подпись probes
    """
    now = datetime.now(timezone.utc)
    fp = payload.device_fingerprint.strip()
    kh = payload.key_hash.strip().lower()

    # 3.0.4+: ищем ключ по device_fingerprint (стабильный идентификатор —
    # одинаковый на VPS и локальной Electron БД). key_hash в запросе нужен
    # только для логов / отладки: SECRET_KEY на VPS и клиенте РАЗНЫЕ, поэтому
    # HMAC(plaintext) от локального клиента ≠ HMAC(plaintext) на VPS.
    row = (
        db.query(AccessKey)
        .filter(AccessKey.device_fingerprint.isnot(None))
        .filter(func.lower(AccessKey.device_fingerprint) == fp.lower())
        .order_by(AccessKey.id.desc())
        .first()
    )

    def _invalid(reason: str):
        state_block = _build_state_block(
            state="invalid",
            key_hash=kh,
            device_fingerprint=fp,
            expires_at=None,
            now=now,
        )
        return {
            "ok": False,
            **state_block,
            "reason": reason,
        }

    if row is None:
        return _invalid("not_found")
    if row.revoked_at is not None:
        return _invalid("revoked")
    if expires_at_is_past(row.expires_at, now=now):
        return _invalid("expired")
    if not row.device_fingerprint:
        # Ключ не привязан — клиент должен сначала вызвать /activate
        return _invalid("not_activated")
    if not hmac_compare_fp(fp, row.device_fingerprint):
        return _invalid("wrong_device")

    state_block = _build_state_block(
        state="valid",
        key_hash=kh,
        device_fingerprint=fp,
        expires_at=row.expires_at,
        now=now,
    )
    probes_block = _build_probes_block(now)
    return {
        "ok": True,
        **state_block,
        **probes_block,
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
