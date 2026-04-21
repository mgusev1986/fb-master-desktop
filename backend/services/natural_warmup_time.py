"""Локальное время прогрева: дневное окно и календарный день (тот же пояс, что у сценариев по дням)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.services.sequence_timezone import get_sequence_zoneinfo

# Активные часы прогрева в локальном поясе (как в настройках sequence_timezone / SEQUENCE_TIMEZONE)
_LOCAL_START_H = 8
_LOCAL_END_H = 22  # полуинтервал [8, 22): с 08:00 до 21:59:59


def natural_warmup_local_now(db: Session, *, now_utc: datetime | None = None) -> datetime:
    """Текущий момент в часовом поясе сценариев (для отображения логики «днём / ночью»)."""
    base = now_utc or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(get_sequence_zoneinfo(db))


def natural_warmup_local_date_iso(db: Session, *, now_utc: datetime | None = None) -> str:
    """Дата YYYY-MM-DD в локальном поясе — для лимитов «за сутки»."""
    return natural_warmup_local_now(db, now_utc=now_utc).date().isoformat()


def is_natural_warmup_active_local_hours(db: Session, *, now_utc: datetime | None = None) -> bool:
    """True, если сейчас дневное окно прогрева (не ночь)."""
    loc = natural_warmup_local_now(db, now_utc=now_utc)
    return _LOCAL_START_H <= loc.hour < _LOCAL_END_H
