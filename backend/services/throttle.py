"""Модуль управления темпом (задержками) для парсинга, прогрева и рассылки.

Пресеты: Улитка / Медленно / Средне / Быстро / Очень быстро — целевые **сообщения в час** (N):
  20 / 30 / 45 / 60 / 100.

Базовая единица времени: T = 3600 / N секунд (средний масштаб «между исходящими
личными сообщениями»). Каждому типу действия задан вес относительно send_dm (1.0):
лайк короче, заявка в друзья и комментарий длиннее — чтобы цепочка выглядела
естественно, с нормальными паузами между ЛС, заявкой, лайком и комментарием.

Режим «Уложиться во время» по-прежнему делит бюджет на число шагов.
Каждая пауза — случайная в диапазоне [min, max] с джиттером ~±22%.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

# Эталонные диапазоны (сек.) — для обратной совместимости UI и оценки веса неизвестных ключей.
REFERENCE_DELAYS: dict[str, tuple[float, float]] = {
    "navigate_profile": (3.0, 7.0),
    "scroll_page": (2.0, 5.0),
    "like_post": (2.0, 5.0),
    "like_video": (2.0, 5.0),
    "comment_post": (4.0, 10.0),
    "comment_video": (4.0, 10.0),
    "friend_request": (3.0, 8.0),
    "open_dm": (3.0, 7.0),
    "send_dm": (4.0, 10.0),
    "next_person": (5.0, 15.0),
    "group_action": (3.0, 8.0),
}

_SEND_DM_REF_MID = (REFERENCE_DELAYS["send_dm"][0] + REFERENCE_DELAYS["send_dm"][1]) / 2.0

# Вес относительно send_dm (1.0): средняя пауза ≈ T * вес.
ACTION_HUMAN_WEIGHT: dict[str, float] = {
    "send_dm": 1.0,
    "comment_post": 0.93,
    "comment_video": 0.93,
    "friend_request": 0.54,
    "open_dm": 0.22,
    "next_person": 0.28,
    "like_post": 0.16,
    "like_video": 0.16,
    "navigate_profile": 0.12,
    "scroll_page": 0.08,
    # Составной шаг сценария (лайк+комментарий+заявка+ЛС и т.п.)
    "group_action": 0.42,
}

# Нижние полы (сек.) — не ниже даже при большом N (быстрый режим).
MIN_FLOORS: dict[str, float] = {
    "navigate_profile": 3.5,
    "scroll_page": 2.5,
    "like_post": 3.0,
    "like_video": 3.0,
    "comment_post": 2.5,
    "comment_video": 2.5,
    "friend_request": 5.0,
    "open_dm": 3.5,
    "send_dm": 2.5,
    "next_person": 4.5,
    "group_action": 4.0,
}

_PRESET_TARGET_MESSAGES_PER_HOUR: dict[str, int] = {
    "ulitka": 20,
    "slow": 30,
    "medium": 45,
    "fast": 60,
    "very_fast": 100,
}

_PRESET_REFERENCE_MSG_PER_HOUR: int = _PRESET_TARGET_MESSAGES_PER_HOUR["medium"]

PRESET_MULTIPLIERS: dict[str, float] = {
    preset: round(
        (3600.0 / float(_PRESET_TARGET_MESSAGES_PER_HOUR[preset])) / _SEND_DM_REF_MID,
        6,
    )
    for preset in _PRESET_TARGET_MESSAGES_PER_HOUR
}

PRESET_LABELS: dict[str, str] = {
    "ulitka": "Улитка (~20 сообщ./час)",
    "slow": "Медленно (~30 сообщ./час)",
    "medium": "Средне (~45 сообщ./час)",
    "fast": "Быстро (~60 сообщ./час, повышенный риск)",
    "very_fast": "Очень быстро (~100 сообщ./час, высокий риск)",
}


def _human_weight_for_action(action_type: str) -> float:
    key = (action_type or "").strip()
    if key in ACTION_HUMAN_WEIGHT:
        return ACTION_HUMAN_WEIGHT[key]
    ref = REFERENCE_DELAYS.get(key, (3.0, 7.0))
    mid = (ref[0] + ref[1]) / 2.0
    return max(0.06, min(1.05, mid / _SEND_DM_REF_MID))


def _preset_base_seconds(preset: str) -> float:
    n = int(_PRESET_TARGET_MESSAGES_PER_HOUR.get(preset, _PRESET_REFERENCE_MSG_PER_HOUR))
    return 3600.0 / max(1, n)


def _delay_range_from_weight(preset: str, weight: float, floor: float) -> tuple[float, float]:
    t = _preset_base_seconds(preset)
    avg = max(0.5, t * weight)
    lo = max(floor, avg * 0.78)
    hi = max(lo + 0.5, avg * 1.22)
    return (round(lo, 1), round(hi, 1))


def is_valid_throttle_preset(preset: str | None) -> bool:
    return isinstance(preset, str) and preset in PRESET_MULTIPLIERS


TIME_BUDGET_PRESETS = [
    ("5m", "5 минут", 300),
    ("30m", "30 минут", 1800),
    ("1h", "1 час", 3600),
    ("3h", "3 часа", 10800),
    ("12h", "12 часов", 43200),
]


def resolve_delay(
    action_type: str,
    *,
    preset: str = "medium",
    time_budget_seconds: float | None = None,
    total_actions: int | None = None,
) -> tuple[float, float]:
    """Вычислить диапазон задержки (min_sec, max_sec) для типа действия.

    При указании time_budget_seconds и total_actions — режим «Уложиться во время»:
    бюджет делится на действия, затем добавляется джиттер ±30%.
    """
    floor = MIN_FLOORS.get(action_type, 3.0)

    if time_budget_seconds and total_actions and total_actions > 0:
        avg = time_budget_seconds / total_actions
        lo = max(floor, avg * 0.7)
        hi = max(lo + 0.5, avg * 1.3)
        return (round(lo, 1), round(hi, 1))

    w = _human_weight_for_action(action_type)
    return _delay_range_from_weight(preset, w, floor)


def estimate_actions_per_hour(
    action_type: str,
    preset: str = "medium",
) -> tuple[int, int]:
    """Оценить количество действий в час (min, max) для UI."""
    lo, hi = resolve_delay(action_type, preset=preset)
    if hi <= 0:
        return (9999, 9999)
    per_hour_max = int(3600 / lo) if lo > 0 else 9999
    per_hour_min = int(3600 / hi) if hi > 0 else 9999
    return (per_hour_min, per_hour_max)


def check_time_budget_feasibility(
    total_actions: int,
    time_budget_seconds: float,
    action_type: str = "next_person",
) -> dict[str, Any]:
    """Проверить достижимость цели по времени.

    Возвращает dict с ключами: feasible, min_required_seconds, warning.
    """
    floor = MIN_FLOORS.get(action_type, 3.0)
    min_total = floor * total_actions
    feasible = time_budget_seconds >= min_total
    warning = ""
    if not feasible:
        safe_mins = math.ceil(min_total / 60)
        warning = (
            f"Невозможно безопасно выполнить {total_actions} действий за "
            f"{int(time_budget_seconds // 60)} мин. "
            f"Минимальное безопасное время: ~{safe_mins} мин."
        )
    return {
        "feasible": feasible,
        "min_required_seconds": round(min_total, 1),
        "warning": warning,
    }


async def sleep_random(action_type: str, **kwargs: Any) -> float:
    """Заснуть на случайное время в вычисленном диапазоне. Вернуть фактическую паузу."""
    lo, hi = resolve_delay(action_type, **kwargs)
    delay = random.uniform(lo, hi)
    await asyncio.sleep(delay)
    return delay


def get_all_delays_for_preset(preset: str) -> dict[str, tuple[float, float]]:
    """Все задержки по типам действий для данного пресета (для UI настроек)."""
    return {
        action: resolve_delay(action, preset=preset)
        for action in REFERENCE_DELAYS
    }


def get_throttle_config_snapshot(
    preset: str = "medium",
    time_budget_seconds: float | None = None,
    total_actions: int | None = None,
) -> dict[str, Any]:
    """Снимок конфига темпа для сохранения в job.config_snapshot."""
    return {
        "preset": preset,
        "time_budget_seconds": time_budget_seconds,
        "total_actions": total_actions,
        "multiplier": PRESET_MULTIPLIERS.get(preset, 1.0),
        "target_messages_per_hour": _PRESET_TARGET_MESSAGES_PER_HOUR.get(preset, 45),
        "human_weights": {k: ACTION_HUMAN_WEIGHT.get(k, _human_weight_for_action(k)) for k in REFERENCE_DELAYS},
        "delays": get_all_delays_for_preset(preset),
    }
