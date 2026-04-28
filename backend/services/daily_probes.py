"""Daily Rotating Probes (DRP) — генератор ежедневного списка эталонов (v3.0+).

Идея:
- Каждый день клиенту выдаётся набор из ~6 URL (Facebook + 5 случайных
  из пула других соцсетей и Google).
- Список генерируется ДЕТЕРМИНИРОВАННО на VPS из (today_utc + secret).
  Клиент не знает секрет → не может предугадать какие домены будут
  завтра/послезавтра, чтобы заранее подготовить hosts-blacklist.
- Список + срок действия (`valid_until`) подписываются Ed25519, клиент
  проверяет подпись локально и доверяет содержимому.

Алгоритм проверки (см. license_watcher):
- ≥ 67% probes отвечают, а наш VPS не отвечает → клиент намеренно
  заблокировал socmaster.pro в hosts → kick out.
- 0 живых probes → реально offline (поезд) → доверяем last-known-good.
- 1-3 живых из 6 → "ambiguous" (downtime соцсетей) → 90 сек grace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# Anchor — должен присутствовать всегда. Для большинства бизнес-кейсов
# Facebook критичен (парсинг + рассылки), его блокировка = софт бесполезен.
PROBE_ANCHOR = "https://www.facebook.com/"

# Пул дополнительных эталонов. Все — крупные домены с глобальным
# anycast-CDN, redundant infra, аптайм 99.95%+.
PROBE_POOL_OTHERS: tuple[str, ...] = (
    "https://www.instagram.com/",
    "https://www.reddit.com/",
    "https://www.linkedin.com/",
    "https://twitter.com/",
    "https://telegram.org/",
    "https://www.google.com/",
)

# Сколько случайных доменов добавлять к anchor (итого размер списка = 1 + N).
DRP_EXTRA_PROBES = 5  # 1 anchor + 5 случайных = 6

# TTL подписи probes: клиент в самолёте 24+ часов должен иметь актуальный
# список после возвращения. С запасом — 36ч. Если за это время ни разу не
# было успешного VPS check — fallback на hard-coded в Electron.
DRP_VALID_FOR = timedelta(hours=36)


def _rotation_secret() -> str:
    """Секрет ротации probes. На VPS — обязателен. Локально — дефолт.

    Сменить секрет на VPS = старые подписанные списки клиентов
    моментально становятся невалидными → клиенты обязаны сходить за
    свежим списком. Используется для "холодной" ротации в случае
    компрометации.
    """
    return (os.environ.get("FB_MASTER_DRP_ROTATION_SECRET") or "drp-default-secret-change-me").strip()


def daily_probes_for_today(today: date | None = None) -> list[str]:
    """Список из 1 + DRP_EXTRA_PROBES URL для конкретной даты UTC.

    Детерминированно: один и тот же день → один и тот же список. Это
    важно чтобы клиент при разных запросах в течение дня получал тот же
    набор (для replay-защиты).
    """
    today = today or datetime.now(timezone.utc).date()
    seed_input = f"{today.toordinal()}|{_rotation_secret()}"
    seed = int(hashlib.sha256(seed_input.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)

    others = list(PROBE_POOL_OTHERS)
    rng.shuffle(others)
    extras = others[: DRP_EXTRA_PROBES]
    return [PROBE_ANCHOR, *extras]


def probes_valid_until(now: datetime | None = None) -> datetime:
    """Окончание срока действия подписанного списка (UTC)."""
    now = now or datetime.now(timezone.utc)
    return now + DRP_VALID_FOR


def probes_signing_payload(probes: list[str], valid_until: datetime) -> bytes:
    """Каноническое представление для подписи Ed25519.

    Формат: JSON со стабильной сериализацией (sorted keys, no spaces).
    Клиент должен собрать payload точно так же перед verify.
    """
    obj = {
        "probes": list(probes),
        "valid_until": valid_until.astimezone(timezone.utc).isoformat(),
    }
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def license_state_signing_payload(
    *,
    state: str,
    key_hash: str,
    device_fingerprint: str,
    verified_at: datetime,
    expires_at: datetime | None,
) -> bytes:
    """Каноническое представление license_state для подписи.

    Клиент сохраняет эту строку + signature локально и при каждом запросе
    middleware проверяет подпись. Без приватного ключа клиент не может
    подменить state="valid" если VPS вернул state="expired".
    """
    obj = {
        "device_fingerprint": device_fingerprint,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat() if expires_at else None,
        "key_hash": key_hash,
        "state": state,
        "verified_at": verified_at.astimezone(timezone.utc).isoformat(),
    }
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


__all__ = [
    "DRP_EXTRA_PROBES",
    "DRP_VALID_FOR",
    "PROBE_ANCHOR",
    "PROBE_POOL_OTHERS",
    "daily_probes_for_today",
    "license_state_signing_payload",
    "probes_signing_payload",
    "probes_valid_until",
]
