"""Регистрация «онлайн»: локальная БД + для десктопа (SQLite) — пульс на VPS по FB_MASTER_LICENSE_API_BASE."""

from __future__ import annotations

import logging
import os
import platform
import threading
import time
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.database import using_postgresql
from backend.models import ClientMachinePresence
from backend.services.client_installation import get_client_installation_id

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LAST_TOUCH_MONO = 0.0


def _presence_interval_sec() -> float:
    raw = (os.getenv("FBM_CLIENT_PRESENCE_INTERVAL_SEC") or "").strip()
    if raw:
        try:
            v = float(raw)
            return max(15.0, min(600.0, v))
        except ValueError:
            pass
    return 45.0


def _device_fingerprint_for_remote(db: Session, request: Request) -> str:
    """Тот же отпечаток, что при активации ключа (сессия или заголовок десктопа)."""
    from backend.services.access_keys import SESSION_KEY_FP, session_access_valid

    if session_access_valid(db, request):
        fp = (request.session.get(SESSION_KEY_FP) or "").strip()
        if fp:
            return fp
    from backend.services.desktop_device import effective_device_fingerprint

    hdr = (request.headers.get("x-fb-master-device-id") or "").strip().lower()
    return (effective_device_fingerprint(request, hdr) or "").strip()[:128]


def _post_remote_presence(url: str, body: dict) -> None:
    try:
        import httpx

        with httpx.Client(timeout=12.0) as client:
            r = client.post(url, json=body)
        if r.status_code >= 400:
            logger.warning(
                "client presence remote HTTP %s: %s", r.status_code, (r.text or "")[:200]
            )
    except Exception as e:
        logger.warning("client presence remote: %s", e)


def maybe_record_client_presence(
    db: Session,
    request: Request,
    *,
    admin_user_id: int,
    organization_id: int,
) -> None:
    """
    Обновляет last_seen для installation_id (с троттлингом).
    Десктоп с SQLite дополнительно шлёт пульс на VPS (таблица в PostgreSQL админки).
    """
    global _LAST_TOUCH_MONO
    interval = _presence_interval_sec()
    now_mono = time.monotonic()
    with _LOCK:
        if now_mono - _LAST_TOUCH_MONO < interval:
            return
        _LAST_TOUCH_MONO = now_mono

    iid = get_client_installation_id()
    now = datetime.now(timezone.utc)
    host = (platform.node() or "").strip()[:255] or None
    ver = (app_config.desktop_app_version_ui() or "").strip()[:64] or None

    try:
        row = db.get(ClientMachinePresence, iid)
        if row:
            row.organization_id = organization_id
            row.admin_user_id = admin_user_id
            row.hostname = host
            row.desktop_app_version = ver
            row.last_seen_at = now
        else:
            db.add(
                ClientMachinePresence(
                    installation_id=iid,
                    organization_id=organization_id,
                    admin_user_id=admin_user_id,
                    hostname=host,
                    desktop_app_version=ver,
                    last_seen_at=now,
                    created_at=now,
                )
            )
        db.commit()
    except Exception:
        logger.exception("client presence heartbeat failed")
        db.rollback()

    remote_url = app_config.client_presence_remote_post_url()
    if remote_url and not using_postgresql():
        fp = _device_fingerprint_for_remote(db, request)
        if len(fp) >= 8:
            body = {
                "installation_id": iid,
                "device_fingerprint": fp,
                "hostname": host,
                "desktop_app_version": ver,
            }
            threading.Thread(
                target=_post_remote_presence,
                args=(remote_url, body),
                daemon=True,
                name="fbm-client-presence",
            ).start()
