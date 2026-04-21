"""LinkedIn anti-detect helpers — human-like behaviors для Playwright.

Не претендуем на полное обхождение fingerprint-детекта; задача — снизить
очевидные бот-паттерны (мгновенные клики, идеально равномерные timings,
полное отсутствие mouse movement / scroll).

Главные хелперы:
  * random_idle(min_s, max_s)            — рандомная пауза.
  * micro_idle()                          — короткая «человеческая» микро-пауза 80–250ms.
  * mouse_jitter(page, steps=3..6)        — лёгкое движение мыши по случайной траектории.
  * mouse_to_locator_humanly(page, loc)   — навестись на элемент через 3-5 промежуточных точек.
  * gentle_scroll(page, total_px=300..900)— проскроллить страницу несколькими шажками.
  * randomize_viewport_size_in_session()  — лёгкая рандомизация viewport (±5-30 px).
  * randomized_session_seconds()          — длительность сессии (используется calling-кодом для self-stop).

Все функции no-op safe: если page сломан / страница ещё не загрузилась — молча
проглатывают исключения и возвращаются. Рассчитаны на sync Playwright.
"""

from __future__ import annotations

import logging
import math
import os
import random
import time
from typing import Any

logger = logging.getLogger(__name__)


# ── timings ────────────────────────────────────────────────


def random_idle(min_s: float = 0.6, max_s: float = 2.2) -> None:
    """Мягкая «человеческая» пауза. Используется между крупными действиями."""
    time.sleep(random.uniform(max(0.05, min_s), max(min_s, max_s)))


def micro_idle() -> None:
    """Короткая микро-пауза 80–250 мс (между микро-шагами в одном скрине)."""
    time.sleep(random.uniform(0.08, 0.25))


def randomized_session_seconds(min_s: int = 90, max_s: int = 420) -> int:
    """Случайная «жизнь» сессии в секундах, чтобы worker не держал браузер ровно по таймеру."""
    return random.randint(max(30, min_s), max(min_s + 1, max_s))


# ── mouse / scroll ─────────────────────────────────────────


def _safe_call(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        # Не логируем — это нормально, anti-detect best-effort.
        pass


def mouse_jitter(page, steps: int | None = None) -> None:
    """Произвольно подвигать мышью внутри viewport.

    Не требует знания конкретных координат элемента — просто рандомные точки.
    """
    n = steps if steps is not None else random.randint(3, 6)
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        w = max(200, int(vp["width"]))
        h = max(200, int(vp["height"]))
    except Exception:  # noqa: BLE001
        w, h = 1280, 800
    for _ in range(n):
        x = random.randint(60, w - 60)
        y = random.randint(60, h - 60)
        _safe_call(page.mouse.move, x, y, steps=random.randint(8, 20))
        time.sleep(random.uniform(0.05, 0.18))


def mouse_to_locator_humanly(page, locator) -> bool:
    """Навестись на элемент через несколько промежуточных точек.

    Возвращает True, если получилось (bbox получен). Иначе False — caller
    может просто кликнуть как обычно.
    """
    try:
        box = locator.bounding_box(timeout=2000)
    except Exception:  # noqa: BLE001
        box = None
    if not box:
        return False
    try:
        # Стартовая позиция — текущая или случайная.
        sx = random.randint(40, 240)
        sy = random.randint(40, 240)
        target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        steps = random.randint(3, 5)
        for i in range(1, steps + 1):
            t = i / steps
            # Лёгкий синусоидальный шум вокруг прямой.
            jitter = math.sin(t * math.pi) * random.uniform(-12, 12)
            mx = sx + (target_x - sx) * t + jitter
            my = sy + (target_y - sy) * t + jitter * 0.5
            _safe_call(page.mouse.move, mx, my, steps=random.randint(6, 14))
            time.sleep(random.uniform(0.04, 0.12))
        _safe_call(page.mouse.move, target_x, target_y, steps=random.randint(4, 10))
    except Exception:  # noqa: BLE001
        return False
    return True


def gentle_scroll(page, *, total_px: int | None = None) -> None:
    """Проскроллить страницу вниз серией маленьких шажков с микро-паузами."""
    target = total_px if total_px is not None else random.randint(300, 900)
    remaining = target
    while remaining > 0:
        step = min(remaining, random.randint(80, 220))
        _safe_call(page.mouse.wheel, 0, step)
        time.sleep(random.uniform(0.18, 0.45))
        remaining -= step
    # Иногда — лёгкий скролл назад.
    if random.random() < 0.35:
        _safe_call(page.mouse.wheel, 0, -random.randint(80, 200))
        time.sleep(random.uniform(0.15, 0.35))


# ── viewport ───────────────────────────────────────────────


def jittered_viewport(base_w: int, base_h: int) -> tuple[int, int]:
    """Лёгкая рандомизация viewport ±5..30 px от базы."""
    return (
        max(800, base_w + random.randint(-30, 30)),
        max(600, base_h + random.randint(-20, 20)),
    )


# ── env-управление anti-detect ──────────────────────────────


def antidetect_enabled() -> bool:
    """По умолчанию ВКЛЮЧЕНО. Можно выключить через FB_MASTER_LINKEDIN_ANTIDETECT_DISABLED=1."""
    return (os.getenv("FB_MASTER_LINKEDIN_ANTIDETECT_DISABLED", "") or "").strip().lower() not in (
        "1", "true", "yes",
    )


__all__ = [
    "antidetect_enabled",
    "gentle_scroll",
    "jittered_viewport",
    "micro_idle",
    "mouse_jitter",
    "mouse_to_locator_humanly",
    "random_idle",
    "randomized_session_seconds",
]
