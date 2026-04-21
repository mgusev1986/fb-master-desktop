"""Часовой пояс для расчёта «календарного дня» в сценариях по дням (не UTC по умолчанию в UI)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from backend.config import SEQUENCE_TIMEZONE as CONFIG_SEQUENCE_TIMEZONE
from backend.models import Setting

logger = logging.getLogger(__name__)

# Для выпадающего списка в настройках (IANA + подпись)
COMMON_TIMEZONES: list[tuple[str, str]] = [
    ("UTC", "UTC"),
    ("Europe/Moscow", "Москва, Санкт-Петербург"),
    ("Europe/Kaliningrad", "Калининград"),
    ("Asia/Yekaterinburg", "Екатеринбург"),
    ("Asia/Omsk", "Омск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Novosibirsk", "Новосибирск"),
    ("Asia/Irkutsk", "Иркутск"),
    ("Asia/Yakutsk", "Якутск"),
    ("Asia/Vladivostok", "Владивосток"),
    ("Asia/Magadan", "Магадан"),
    ("Asia/Kamchatka", "Камчатка"),
    ("Europe/Kyiv", "Киев"),
    ("Europe/Berlin", "Берлин"),
    ("Europe/Madrid", "Мадрид"),
    ("Europe/London", "Лондон"),
    ("America/New_York", "Нью-Йорк"),
    ("America/Los_Angeles", "Лос-Анджелес"),
]


def normalize_sequence_tz_name(name: str) -> str:
    """Короткий синоним для РФ: Europe → Europe/Moscow (полное имя IANA)."""
    n = (name or "").strip()
    if n.lower() == "europe":
        return "Europe/Moscow"
    return n


def raw_sequence_timezone_name(db) -> str:
    """Имя IANA: из settings, иначе из .env (SEQUENCE_TIMEZONE)."""
    row = db.get(Setting, "sequence_timezone")
    if row and isinstance(row.value, str) and row.value.strip():
        return row.value.strip()
    base = (CONFIG_SEQUENCE_TIMEZONE or "UTC").strip()
    return base or "UTC"


def get_sequence_zoneinfo(db) -> ZoneInfo:
    name = normalize_sequence_tz_name(raw_sequence_timezone_name(db))
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Неизвестный часовой пояс для сценариев %r — подставляю UTC", name)
        return ZoneInfo("UTC")


def sequence_today_local(db) -> date:
    """Сегодняшняя дата в выбранном поясе."""
    return datetime.now(get_sequence_zoneinfo(db)).date()


def enrollment_anchor_local_date(db, created_at: datetime | None) -> date:
    """
    Дата зачисления в локальном поясе сценариев.
    Naive datetime в БД считаем сохранённым в UTC (как остальной FB Master).
    """
    if created_at is None:
        return sequence_today_local(db)
    c = created_at
    if c.tzinfo is None:
        c = c.replace(tzinfo=timezone.utc)
    return c.astimezone(get_sequence_zoneinfo(db)).date()


def days_since_enrollment_local(db, enroll) -> int:
    """Полных локальных календарных дней с даты зачисления (в поясе сценариев)."""
    today = sequence_today_local(db)
    d0 = enrollment_anchor_local_date(db, enroll.created_at)
    return (today - d0).days
