"""Настройки мессенджера: единый флаг автосинхронизации и интервалы."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.models import Setting

MESSENGER_POLL_INTERVAL_KEY = "messenger_poll_interval_sec"
MESSENGER_AUTO_PULL_ENABLED_KEY = "messenger_auto_pull_enabled"
MESSENGER_AUTO_INBOX_SYNC_ENABLED_KEY = "messenger_auto_inbox_sync_enabled"
MESSENGER_AUTO_INBOX_SYNC_INTERVAL_KEY = "messenger_auto_inbox_sync_interval_sec"
MESSENGER_FOCUS_PINGS_KEY = "messenger_focus_pings_iso"
# Нет свежего heartbeat со страницы мессенджера дольше — считаем, что пользователь ушёл с раздела.
MESSENGER_FOCUS_STALE_SEC = 90
# Пока пользователь не на странице мессенджера — не чаще этого интервала (сек).
MESSENGER_AWAY_INTERVAL_FLOOR_SEC = 600
DEFAULT_POLL_INTERVAL_SEC = 30
MIN_POLL_INTERVAL_SEC = 15
MAX_POLL_INTERVAL_SEC = 300
DEFAULT_INBOX_AUTOSYNC_INTERVAL_SEC = 300
MIN_INBOX_AUTOSYNC_INTERVAL_SEC = 300
MAX_INBOX_AUTOSYNC_INTERVAL_SEC = 7200


def get_messenger_poll_interval_sec(db: Session) -> int:
    row = db.get(Setting, MESSENGER_POLL_INTERVAL_KEY)
    raw = row.value if row and row.value is not None else DEFAULT_POLL_INTERVAL_SEC
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_POLL_INTERVAL_SEC
    return max(MIN_POLL_INTERVAL_SEC, min(MAX_POLL_INTERVAL_SEC, n))


def set_messenger_poll_interval_sec(db: Session, seconds: int) -> int:
    n = int(seconds)
    n = max(MIN_POLL_INTERVAL_SEC, min(MAX_POLL_INTERVAL_SEC, n))
    row = db.get(Setting, MESSENGER_POLL_INTERVAL_KEY)
    if row:
        row.value = n
    else:
        db.add(Setting(key=MESSENGER_POLL_INTERVAL_KEY, value=n))
    db.commit()
    return n


def get_messenger_auto_pull_enabled(db: Session) -> bool:
    """Периодически подтягивать открытый чат с Facebook (Playwright). По умолчанию выключено (меньше запусков браузера)."""
    row = db.get(Setting, MESSENGER_AUTO_PULL_ENABLED_KEY)
    if not row or row.value is None:
        return False
    v = row.value
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def set_messenger_auto_pull_enabled(db: Session, enabled: bool) -> bool:
    row = db.get(Setting, MESSENGER_AUTO_PULL_ENABLED_KEY)
    if row:
        row.value = bool(enabled)
    else:
        db.add(Setting(key=MESSENGER_AUTO_PULL_ENABLED_KEY, value=bool(enabled)))
    db.commit()
    return bool(enabled)


def get_messenger_auto_inbox_sync_enabled(db: Session) -> bool:
    """Фоновая синхронизация списка чатов; без записи в БД — выкл (как у авто-pull), единая логика с мессенджером."""
    row = db.get(Setting, MESSENGER_AUTO_INBOX_SYNC_ENABLED_KEY)
    if not row or row.value is None:
        return False
    v = row.value
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def set_messenger_auto_inbox_sync_enabled(db: Session, enabled: bool) -> bool:
    row = db.get(Setting, MESSENGER_AUTO_INBOX_SYNC_ENABLED_KEY)
    if row:
        row.value = bool(enabled)
    else:
        db.add(Setting(key=MESSENGER_AUTO_INBOX_SYNC_ENABLED_KEY, value=bool(enabled)))
    db.commit()
    return bool(enabled)


def set_messenger_auto_sync_enabled(db: Session, enabled: bool) -> bool:
    """Единый режим: оба флага (открытый чат + фоновый список) всегда одинаковые, один commit."""
    en = bool(enabled)
    row_p = db.get(Setting, MESSENGER_AUTO_PULL_ENABLED_KEY)
    if row_p:
        row_p.value = en
    else:
        db.add(Setting(key=MESSENGER_AUTO_PULL_ENABLED_KEY, value=en))
    row_i = db.get(Setting, MESSENGER_AUTO_INBOX_SYNC_ENABLED_KEY)
    if row_i:
        row_i.value = en
    else:
        db.add(Setting(key=MESSENGER_AUTO_INBOX_SYNC_ENABLED_KEY, value=en))
    db.commit()
    return en


def _load_messenger_focus_pings(db: Session) -> dict[str, str]:
    row = db.get(Setting, MESSENGER_FOCUS_PINGS_KEY)
    raw = row.value if row else None
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    return {}


def touch_messenger_focus_ping(db: Session, organization_id: int) -> None:
    """Фиксирует, что пользователь сейчас на странице мессенджера (вкладка видима)."""
    oid = str(int(organization_id))
    m = dict(_load_messenger_focus_pings(db))
    m[oid] = datetime.now(timezone.utc).isoformat()
    row = db.get(Setting, MESSENGER_FOCUS_PINGS_KEY)
    if row:
        row.value = m
    else:
        db.add(Setting(key=MESSENGER_FOCUS_PINGS_KEY, value=m))
    db.commit()


def is_messenger_page_focus_recent(db: Session, organization_id: int) -> bool:
    """
    True, если недавно был heartbeat с /messenger для этой организации.
    Иначе (другая вкладка, другой раздел, закрыта страница) — фон можно реже и легче.
    """
    raw = _load_messenger_focus_pings(db).get(str(int(organization_id)))
    if not raw:
        return False
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age <= float(MESSENGER_FOCUS_STALE_SEC)


def get_messenger_effective_autosync_interval_sec(db: Session, organization_id: int) -> int:
    """
    Интервал между фоновыми запусками для организации: на странице мессенджера — как в настройках,
    иначе не чаще MESSENGER_AWAY_INTERVAL_FLOOR_SEC.
    """
    base = get_messenger_auto_inbox_sync_interval_sec(db)
    if is_messenger_page_focus_recent(db, organization_id):
        return base
    return max(base, int(MESSENGER_AWAY_INTERVAL_FLOOR_SEC))


def normalize_messenger_auto_sync_flags(db: Session) -> bool:
    """
    После старых версий флаги могли разойтись — выровнять одним значением.
    Правило: если хотя бы один включён → оба вкл; иначе оба выкл.
    Возвращает True, если выполнялась запись в БД.
    """
    pull = get_messenger_auto_pull_enabled(db)
    inbox = get_messenger_auto_inbox_sync_enabled(db)
    if pull == inbox:
        return False
    set_messenger_auto_sync_enabled(db, pull or inbox)
    return True


def get_messenger_auto_inbox_sync_interval_sec(db: Session) -> int:
    row = db.get(Setting, MESSENGER_AUTO_INBOX_SYNC_INTERVAL_KEY)
    raw = row.value if row and row.value is not None else DEFAULT_INBOX_AUTOSYNC_INTERVAL_SEC
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_INBOX_AUTOSYNC_INTERVAL_SEC
    return max(MIN_INBOX_AUTOSYNC_INTERVAL_SEC, min(MAX_INBOX_AUTOSYNC_INTERVAL_SEC, n))


def set_messenger_auto_inbox_sync_interval_sec(db: Session, seconds: int) -> int:
    n = int(seconds)
    n = max(MIN_INBOX_AUTOSYNC_INTERVAL_SEC, min(MAX_INBOX_AUTOSYNC_INTERVAL_SEC, n))
    row = db.get(Setting, MESSENGER_AUTO_INBOX_SYNC_INTERVAL_KEY)
    if row:
        row.value = n
    else:
        db.add(Setting(key=MESSENGER_AUTO_INBOX_SYNC_INTERVAL_KEY, value=n))
    db.commit()
    return n
