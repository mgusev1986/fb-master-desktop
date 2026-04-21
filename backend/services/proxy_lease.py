"""Срок аренды прокси: дата/время окончания у провайдера; запас 1 ч для остановки автоматизации; UI-уровни."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

# Ввод в форме трактуется как локальное время Europe/Madrid (как в format_dt_madrid).
LEASE_INPUT_TZ = ZoneInfo("Europe/Madrid")
PROXY_LEASE_SAFETY_MARGIN = timedelta(hours=1)

LeaseTier = Literal["none", "ok_green", "warn_orange", "alert_red", "critical_blink", "expired"]


def _legacy_proxy_lease_configured(acc: Any) -> bool:
    d = getattr(acc, "proxy_lease_days", None)
    p = getattr(acc, "proxy_lease_purchased_at", None)
    try:
        nd = int(d) if d is not None else 0
    except (TypeError, ValueError):
        return False
    return nd > 0 and p is not None


def proxy_lease_configured(acc: Any) -> bool:
    if getattr(acc, "proxy_lease_ends_at", None) is not None:
        return True
    return _legacy_proxy_lease_configured(acc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Как в кабинетах (proxy6 и др.): «18.04.26, 16:12» или «18.04.2026, 16:12», также без запятой.
_PROXY_CABINET_END_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\s*,\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$"
)
_PROXY_CABINET_END_SPACE_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$"
)


def _parse_cabinet_style_datetime_madrid(s: str) -> datetime | None:
    """DD.MM.YY[,] HH:MM в локальном Europe/Madrid → aware UTC."""
    m = _PROXY_CABINET_END_RE.match(s) or _PROXY_CABINET_END_SPACE_RE.match(s)
    if not m:
        return None
    d_s, mo_s, y_tok, h_s, mi_s, sec_g = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
    try:
        d_i, mo_i, h_i, mi_i = int(d_s), int(mo_s), int(h_s), int(mi_s)
        y_tok = y_tok.strip()
        if len(y_tok) == 2:
            y_i = 2000 + int(y_tok)
        else:
            y_i = int(y_tok)
        sec_i = int(sec_g) if sec_g else 0
        naive = datetime(y_i, mo_i, d_i, h_i, mi_i, sec_i)
    except (ValueError, TypeError, OverflowError):
        return None
    local = naive.replace(tzinfo=LEASE_INPUT_TZ)
    return local.astimezone(timezone.utc)


def parse_proxy_lease_ends_at_from_form(raw: str) -> datetime | None:
    """Дата окончания прокси: Europe/Madrid для «наивного» ввода.

    Поддерживается:
    - формат кабинета: ``18.04.26, 16:12`` / ``18.04.2026, 16:12`` (запятую можно заменить пробелом);
    - ISO и вид ``datetime-local``: ``2026-04-18T16:12`` (и с пробелом вместо ``T``).
    """
    s = (raw or "").strip()
    if not s:
        return None
    cab = _parse_cabinet_style_datetime_madrid(s)
    if cab is not None:
        return cab
    s_iso = s.replace(" ", "T", 1) if " " in s and "T" not in s else s
    try:
        naive = datetime.fromisoformat(s_iso)
    except ValueError:
        return None
    if naive.tzinfo is not None:
        return _as_utc(naive)
    local = naive.replace(tzinfo=LEASE_INPUT_TZ)
    return local.astimezone(timezone.utc)


def parse_proxy_lease_purchased_at_from_form(raw: str) -> datetime | None:
    """Алиас для совместимости со старым именем поля (тот же парсинг, что у даты окончания)."""
    return parse_proxy_lease_ends_at_from_form(raw)


def parse_proxy_lease_days_from_form(raw: str) -> int | None:
    """Оставлено для совместимости; текущие формы поле «дни» не используют."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    if n <= 0 or n > 3660:
        return None
    return n


def proxy_lease_provider_deadline_utc(acc: Any) -> datetime | None:
    """Момент окончания у провайдера (без вычета 1 ч запаса FB Master)."""
    ends = getattr(acc, "proxy_lease_ends_at", None)
    if ends is not None:
        return _as_utc(ends)
    if not _legacy_proxy_lease_configured(acc):
        return None
    p = _as_utc(acc.proxy_lease_purchased_at)
    return p + timedelta(days=int(acc.proxy_lease_days))


def proxy_lease_effective_deadline_utc(acc: Any) -> datetime | None:
    """Момент остановки автоматизации: конец у провайдера − 1 ч."""
    prov = proxy_lease_provider_deadline_utc(acc)
    if not prov:
        return None
    return prov - PROXY_LEASE_SAFETY_MARGIN


def proxy_lease_expired_for_automation(acc: Any) -> bool:
    cutoff = proxy_lease_effective_deadline_utc(acc)
    if not cutoff:
        return False
    return datetime.now(timezone.utc) >= cutoff


def proxy_lease_remaining_td(acc: Any) -> timedelta | None:
    cutoff = proxy_lease_effective_deadline_utc(acc)
    if not cutoff:
        return None
    return cutoff - datetime.now(timezone.utc)


def format_proxy_lease_deadline_madrid(acc: Any) -> str:
    end = proxy_lease_provider_deadline_utc(acc)
    if not end:
        return ""
    return end.astimezone(LEASE_INPUT_TZ).strftime("%d.%m.%Y %H:%M")


def proxy_lease_ui_tier(acc: Any) -> LeaseTier:
    if not proxy_lease_configured(acc):
        return "none"
    if proxy_lease_expired_for_automation(acc):
        return "expired"
    rem = proxy_lease_remaining_td(acc)
    if rem is None:
        return "none"
    if rem.total_seconds() <= 0:
        return "expired"
    days = rem.total_seconds() / 86400.0
    if days > 7:
        return "ok_green"
    if days > 3:
        return "warn_orange"
    if days > 1:
        return "alert_red"
    return "critical_blink"


def format_proxy_lease_remaining_ru(rem: timedelta | None, *, expired: bool) -> str:
    if expired or rem is None or rem.total_seconds() <= 0:
        return "срок истёк (остановка с запасом 1 ч)"
    sec = int(rem.total_seconds())
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts: list[str] = []
    if d:
        parts.append(f"{d}д")
    if h:
        parts.append(f"{h}ч")
    if m:
        if d == 0:
            parts.append(f"{m}м")
        elif h == 0:
            parts.append(f"{m}м")
    return " ".join(parts) if parts else "<1м"


def proxy_lease_datetime_local_value(dt: datetime | None) -> str:
    """Значение для input datetime-local (Europe/Madrid), пустая строка если нет."""
    if not dt:
        return ""
    dtu = _as_utc(dt)
    local = dtu.astimezone(LEASE_INPUT_TZ)
    return local.strftime("%Y-%m-%dT%H:%M")


def proxy_lease_ends_at_form_value(acc: Any) -> str:
    """Текст поля «окончание»: как в кабинете ``DD.MM.YY, HH:MM`` (Europe/Madrid), или пусто."""
    ends = getattr(acc, "proxy_lease_ends_at", None)
    if ends is not None:
        dtu = _as_utc(ends)
        return dtu.astimezone(LEASE_INPUT_TZ).strftime("%d.%m.%y, %H:%M")
    if _legacy_proxy_lease_configured(acc):
        prov = _as_utc(acc.proxy_lease_purchased_at) + timedelta(days=int(acc.proxy_lease_days))
        return prov.astimezone(LEASE_INPUT_TZ).strftime("%d.%m.%y, %H:%M")
    return ""
