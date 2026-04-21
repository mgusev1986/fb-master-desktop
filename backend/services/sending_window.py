"""Окно часов отправки (рассылка, режим агента): локальный пояс как у sequence_timezone."""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.services.sequence_timezone import get_sequence_zoneinfo

# Если в конфиге кампании нет настроек — только дневное окно (как «натуральный» прогрев).
_DEFAULT_START_H = 8
_DEFAULT_END_H = 22  # полуинтервал [start, end) по локальному часу


def _cfg(c: Any) -> dict[str, Any]:
    return c if isinstance(c, dict) else {}


def send_window_24h(cfg: dict[str, Any] | None) -> bool:
    return bool(_cfg(cfg).get("send_window_24h"))


def send_window_bounds(cfg: dict[str, Any] | None) -> tuple[int, int]:
    c = _cfg(cfg)
    try:
        start = int(c.get("send_window_start_hour", _DEFAULT_START_H))
    except (TypeError, ValueError):
        start = _DEFAULT_START_H
    try:
        end = int(c.get("send_window_end_hour", _DEFAULT_END_H))
    except (TypeError, ValueError):
        end = _DEFAULT_END_H
    start = max(0, min(23, start))
    end = max(0, min(23, end))
    return start, end


def _local_now(db: Session) -> datetime:
    base = datetime.now(timezone.utc)
    return base.astimezone(get_sequence_zoneinfo(db))


def local_hour_in_window(hour: int, start: int, end: int) -> bool:
    """Активен ли целый локальный час `hour` (0..23) в окне [start, end)."""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def is_now_within_send_window(db: Session, cfg: dict[str, Any] | None) -> bool:
    if send_window_24h(cfg):
        return True
    start, end = send_window_bounds(cfg)
    h = _local_now(db).hour
    return local_hour_in_window(h, start, end)


def seconds_until_send_window_opens(db: Session, cfg: dict[str, Any] | None) -> int:
    """
    Секунды до ближайшего начала окна (минимум 1, максимум 3600 — периодически перепроверяем).
    Если окно 24ч или уже внутри — 0.
    """
    if send_window_24h(cfg) or is_now_within_send_window(db, cfg):
        return 0
    now_l = _local_now(db)
    start_h, end_h = send_window_bounds(cfg)
    zi = now_l.tzinfo
    d = now_l.date()

    def combine(dd, hh: int) -> datetime:
        return datetime.combine(dd, dt_time(hh, 0, 0), tzinfo=zi)

    if start_h < end_h:
        open_today = combine(d, start_h)
        if now_l < open_today:
            sec = (open_today - now_l).total_seconds()
        else:
            sec = (combine(d + timedelta(days=1), start_h) - now_l).total_seconds()
    else:
        open_today = combine(d, start_h)
        if end_h <= now_l.hour < start_h:
            if now_l < open_today:
                sec = (open_today - now_l).total_seconds()
            else:
                sec = (combine(d + timedelta(days=1), start_h) - now_l).total_seconds()
        else:
            sec = (combine(d + timedelta(days=1), start_h) - now_l).total_seconds()

    return max(1, min(int(sec), 3600))


def parse_send_window_form(form: Any) -> tuple[bool, int, int]:
    """Из формы кампании: круглосуточно и границы окна (часы 0–23)."""
    sw24 = form.get("send_window_24h") == "1"
    try:
        sh = int(str(form.get("send_window_start_hour") or "8").strip())
    except (TypeError, ValueError):
        sh = _DEFAULT_START_H
    try:
        eh = int(str(form.get("send_window_end_hour") or "22").strip())
    except (TypeError, ValueError):
        eh = _DEFAULT_END_H
    return sw24, sh, eh


def merge_send_window_into_config(
    base: dict[str, Any] | None,
    *,
    send_window_24h: bool,
    send_window_start_hour: int,
    send_window_end_hour: int,
) -> dict[str, Any]:
    out = dict(base) if isinstance(base, dict) else {}
    if send_window_24h:
        out["send_window_24h"] = True
        out.pop("send_window_start_hour", None)
        out.pop("send_window_end_hour", None)
    else:
        out.pop("send_window_24h", None)
        out["send_window_start_hour"] = max(0, min(23, int(send_window_start_hour)))
        out["send_window_end_hour"] = max(0, min(23, int(send_window_end_hour)))
    return out
