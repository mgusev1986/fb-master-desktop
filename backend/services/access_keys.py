"""Логика активации ключа и проверки сессии."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models import AccessKey
from backend.services.access_key_crypto import hash_access_key

logger = logging.getLogger(__name__)

_MADRID_TZ = ZoneInfo("Europe/Madrid")

# SHA256 hex от Electron (main): X-FB-Master-Device-Id
_DESKTOP_HW_FP = re.compile(r"^[a-f0-9]{64}$")

SESSION_KEY_ID = "ak_id"
SESSION_KEY_FP = "ak_fp"


def expires_at_is_past(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    """True, если задан срок и текущее время позже него (сравнение в UTC)."""
    if expires_at is None:
        return False
    n = now or datetime.now(timezone.utc)
    exp = expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return n > exp


def find_key_by_plaintext(db: Session, plaintext: str) -> AccessKey | None:
    """Подбор по хешу (один запрос)."""
    h = hash_access_key(plaintext)
    if not h:
        return None
    return db.query(AccessKey).filter(AccessKey.key_hash == h).first()


def bind_access_key_to_device(
    db: Session, *, plaintext: str, device_fingerprint: str
) -> tuple[bool, str, AccessKey | None]:
    """
    Привязка ключа к устройству в БД (без сессии). Для публичного API десктопа на VPS.
    """
    fp = (device_fingerprint or "").strip()
    if len(fp) < 8:
        return False, "Некорректный идентификатор устройства.", None

    row = find_key_by_plaintext(db, plaintext)
    if not row:
        return False, "Ключ не найден или неверен.", None
    if row.revoked_at is not None:
        return False, "Ключ отозван.", None
    if expires_at_is_past(row.expires_at):
        return False, "Срок действия ключа истёк.", None

    if row.device_fingerprint:
        if not hmac_compare_fp(fp, row.device_fingerprint):
            return False, "Ключ уже привязан к другому устройству.", None
        if row.activated_at is None:
            row.activated_at = datetime.now(timezone.utc)
            db.add(row)
            db.commit()
    else:
        row.device_fingerprint = fp
        row.activated_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()

    logger.info("Access key %s bound to device prefix %s…", row.id, fp[:8])
    return True, "", row


def mirror_access_key_locally(
    db: Session,
    *,
    plaintext: str,
    device_fingerprint: str,
    expires_at: datetime | None,
) -> AccessKey | None:
    """
    После активации на VPS — создать/обновить запись access_keys в локальной SQLite десктопа.
    """
    h = hash_access_key(plaintext)
    if not h:
        return None
    fp = (device_fingerprint or "").strip()
    now = datetime.now(timezone.utc)
    row = db.query(AccessKey).filter(AccessKey.key_hash == h).first()
    if not row:
        row = AccessKey(
            key_hash=h,
            label=None,
            device_fingerprint=fp,
            activated_at=now,
            revoked_at=None,
            expires_at=expires_at,
        )
        db.add(row)
    else:
        row.device_fingerprint = fp
        row.activated_at = row.activated_at or now
        row.revoked_at = None
        if expires_at is not None:
            row.expires_at = expires_at
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def try_activate(
    db: Session, request: Request, *, plaintext: str, device_fingerprint: str
) -> tuple[bool, str]:
    """
    Активирует ключ на устройстве. Один ключ после привязки работает только с тем же device_fingerprint.
    Возвращает (ok, сообщение_об_ошибке).
    """
    ok, err, row = bind_access_key_to_device(
        db, plaintext=plaintext, device_fingerprint=device_fingerprint
    )
    if not ok or row is None:
        return False, err

    fp = (device_fingerprint or "").strip()
    request.session[SESSION_KEY_ID] = row.id
    request.session[SESSION_KEY_FP] = fp
    logger.info("Access key %s activated for device prefix %s…", row.id, fp[:8])
    return True, ""


def hmac_compare_fp(a: str, b: str) -> bool:
    import hmac as _h

    return _h.compare_digest((a or "").strip(), (b or "").strip())


def session_access_valid(db: Session, request: Request) -> bool:
    raw_id = request.session.get(SESSION_KEY_ID)
    fp = request.session.get(SESSION_KEY_FP)
    if raw_id is None or not fp:
        return False
    try:
        kid = int(raw_id)
    except (TypeError, ValueError):
        return False
    row = db.get(AccessKey, kid)
    if not row or row.revoked_at is not None:
        return False
    if expires_at_is_past(row.expires_at):
        return False
    if not row.device_fingerprint:
        return False
    return hmac_compare_fp(fp, row.device_fingerprint)


def clear_access_session(request: Request) -> None:
    request.session.pop(SESSION_KEY_ID, None)
    request.session.pop(SESSION_KEY_FP, None)


def _dt_as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _madrid_calendar_date(dt: datetime) -> date:
    return _dt_as_utc(dt).astimezone(_MADRID_TZ).date()


def format_access_key_paid_period_ru(row: AccessKey) -> str:
    """
    Человекочитаемый оплаченный период: от выдачи ключа (created_at) до expires_at.
    Совпадает с логикой выдачи на VPS (срок от момента создания записи).
    """
    exp = _dt_as_utc(row.expires_at)
    if exp is None:
        return "бессрочный доступ"
    start_src = row.created_at or row.activated_at
    if start_src is None:
        return "ограниченный по дате доступ"
    start = _dt_as_utc(start_src)
    if start is None:
        return "ограниченный по дате доступ"
    span = exp - start
    if span.total_seconds() <= 0:
        return "ограниченный по дате доступ"
    if span < timedelta(days=1):
        total_minutes = max(1, int(span.total_seconds() // 60))
        if total_minutes < 120:
            return f"краткий доступ ({total_minutes} мин.)"
        return f"краткий доступ ({total_minutes // 60} ч)"
    d0 = _madrid_calendar_date(start)
    d1 = _madrid_calendar_date(exp)
    paid_days = max(1, (d1 - d0).days)
    return _ru_days_label(paid_days)


def _ru_days_word_only(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= (n % 10) <= 4 and (n % 100 < 10 or n % 100 > 20):
        return "дня"
    return "дней"


def _ru_days_label(n: int) -> str:
    n = abs(int(n))
    return f"{n} {_ru_days_word_only(n)}"


def access_key_calendar_days_remaining_madrid(expires_at: datetime, *, now: datetime | None = None) -> int:
    """Полных календарных дней по Europe/Madrid от «сегодня» до даты окончания (включительно как дата)."""
    n = now or datetime.now(timezone.utc)
    exp = _dt_as_utc(expires_at)
    if exp is None:
        return 0
    if n >= exp:
        return 0
    d0 = _madrid_calendar_date(n)
    d1 = _madrid_calendar_date(exp)
    return max(0, (d1 - d0).days)


def sidebar_access_key_license_info(db: Session, request: Request) -> dict[str, Any] | None:
    """
    Данные для сайдбара: оплаченный период и остаток календарных дней по ключу в сессии.
    None — если вход не по ключу или сессия недействительна.
    """
    if not session_access_valid(db, request):
        return None
    raw_id = request.session.get(SESSION_KEY_ID)
    try:
        kid = int(raw_id)
    except (TypeError, ValueError):
        return None
    row = db.get(AccessKey, kid)
    if not row or row.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    if expires_at_is_past(row.expires_at, now=now):
        return None

    if row.expires_at is None:
        return {
            "forever": True,
            "paid_period_label": format_access_key_paid_period_ru(row),
            "days_remaining": None,
            "expires_at": None,
            "expires_at_iso": None,
            "last_calendar_day": False,
        }

    exp = row.expires_at
    days_left = access_key_calendar_days_remaining_madrid(exp, now=now)
    last_cal = days_left == 0 and not expires_at_is_past(exp, now=now)
    return {
        "forever": False,
        "paid_period_label": format_access_key_paid_period_ru(row),
        "days_remaining": days_left,
        "days_word": _ru_days_word_only(days_left) if days_left > 0 else "дней",
        "expires_at": exp,
        "expires_at_iso": _dt_as_utc(exp).isoformat(),
        "last_calendar_day": last_cal,
    }


def try_rehydrate_desktop_access_session(db: Session, request: Request) -> bool:
    """
    После обновления приложения или смены SECRET_KEY cookie-сессия с ключом пропадает,
    а запись в локальной SQLite остаётся. Восстанавливаем session[ak_id]/[ak_fp] по тому же
    железному отпечатку, что шлёт Electron (User-Agent … FBMasterDesktop + X-FB-Master-Device-Id).
    """
    ua = (request.headers.get("user-agent") or "").lower().replace(" ", "")
    if "fbmasterdesktop" not in ua:
        return False
    if session_access_valid(db, request):
        return True
    fp = (request.headers.get("x-fb-master-device-id") or "").strip().lower()
    if not _DESKTOP_HW_FP.match(fp):
        return False
    row = (
        db.query(AccessKey)
        .filter(AccessKey.revoked_at.is_(None))
        .filter(AccessKey.device_fingerprint.isnot(None))
        .filter(func.lower(AccessKey.device_fingerprint) == fp)
        .order_by(AccessKey.id.desc())
        .first()
    )
    if not row or expires_at_is_past(row.expires_at):
        return False
    request.session[SESSION_KEY_ID] = row.id
    request.session[SESSION_KEY_FP] = fp
    logger.info("Восстановлена сессия ключа id=%s по отпечатку устройства (десктоп)", row.id)
    return True
