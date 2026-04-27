"""Настройка скорости парсера друзей/аудитории (Setting + env fallback).

Клиент может выбрать, как быстро парсер скроллит список друзей донора:
- ``turbo``  — как в первых версиях парсера: ≈0.2–0.5 с/раунд, без рандом-пауз.
                Самый быстрый режим, но выше риск капчи. Если FB начал её
                показывать — переключитесь на «Быстрый» / «Обычный».
- ``fast``   — минимальные паузы (~1.0–2.0 с/раунд), без доп-рандом-пауз.
                Подходит, когда прокси стабильный и аккаунт разогрет.
                Риск: FB может показать капчу / временную throttle.
- ``normal`` — текущий дефолт: 2.2–5.2 с/раунд, редкие длинные паузы
                до 11 с — имитирует живой скролл.
- ``gentle`` — x1.3 от normal, больше «дыхания» — для аккаунтов
                на грани restricted / при слабом прокси.

Переключается через Setting ``parser_scroll_speed`` (UI на /parser) или
через переменную окружения ``FB_MASTER_PARSER_SPEED`` (override для
клиентов, у которых нет доступа к настройкам кабинета).
"""

from __future__ import annotations

import os
from typing import Literal

from sqlalchemy.orm import Session

from backend.models import Setting

SpeedMode = Literal["turbo", "fast", "normal", "gentle"]

PARSER_SCROLL_SPEED_KEY = "parser_scroll_speed"
DEFAULT_SPEED: SpeedMode = "normal"

_VALID_MODES: tuple[SpeedMode, ...] = ("turbo", "fast", "normal", "gentle")

# Множители для задержек: 1.0 = как в коде сейчас (normal).
# Turbo подняли с 0.1 → 0.3: на 0.1 (≈220-500ms) Facebook не успевал
# подгружать виртуальный список группы (65k участников) — парсер крутил
# впустую и казался зависшим. На 0.3 (~660-1500ms) FB рендерит свежие
# DOM-узлы между скроллами и парсер реально продвигается.
_MULTIPLIERS: dict[SpeedMode, float] = {
    "turbo": 0.3,
    "fast": 0.35,
    "normal": 1.0,
    "gentle": 1.3,
}

# Вероятность длинных случайных пауз (в scroll_friends_page была 16–22% + 12%).
_RANDOM_PAUSE_PROB: dict[SpeedMode, float] = {
    "turbo": 0.0,
    "fast": 0.0,
    "normal": 1.0,
    "gentle": 1.15,
}


def _normalize(raw: object) -> SpeedMode:
    s = str(raw or "").strip().lower()
    if s in _VALID_MODES:
        return s  # type: ignore[return-value]
    return DEFAULT_SPEED


def get_parser_scroll_speed(db: Session | None = None) -> SpeedMode:
    """Возвращает текущий режим. env-override имеет приоритет.

    Если db=None (безопасный fallback, модуль не хочет тянуть сессию),
    читаем только env. Иначе — сначала Setting из БД, затем env.
    """
    env = os.environ.get("FB_MASTER_PARSER_SPEED", "").strip().lower()
    if env in _VALID_MODES:
        return env  # type: ignore[return-value]
    if db is not None:
        try:
            row = db.get(Setting, PARSER_SCROLL_SPEED_KEY)
            if row and row.value is not None:
                return _normalize(row.value)
        except Exception:
            return DEFAULT_SPEED
    return DEFAULT_SPEED


def set_parser_scroll_speed(db: Session, mode: SpeedMode) -> SpeedMode:
    norm = _normalize(mode)
    row = db.get(Setting, PARSER_SCROLL_SPEED_KEY)
    if row:
        row.value = norm
    else:
        db.add(Setting(key=PARSER_SCROLL_SPEED_KEY, value=norm))
    db.commit()
    return norm


def scroll_wait_multiplier(mode: SpeedMode | None = None) -> float:
    """Коэффициент для paging-пауз в scroll_friends_page."""
    return _MULTIPLIERS.get(mode or DEFAULT_SPEED, 1.0)


def scroll_random_pause_prob(mode: SpeedMode | None = None) -> float:
    """Коэффициент для вероятности случайных «дышащих» пауз."""
    return _RANDOM_PAUSE_PROB.get(mode or DEFAULT_SPEED, 1.0)
