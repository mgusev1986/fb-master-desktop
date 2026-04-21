"""Микроповедение «как человек» в Playwright: прокрутка ленты, короткие паузы «чтения».

Паузы берутся из тех же пресетов темпа (resolve_delay), что и глобальные настройки
«Улитка / Медленно / Средне / Быстро / Очень быстро», но долями от диапазона scroll_page / navigate_profile,
чтобы не дублировать полный вес каждого типа на каждый жест — итоговый ритм остаётся
в том же масштабе, что задан в /system/speed.
"""

from __future__ import annotations

import logging
import random
import time
from playwright.sync_api import Page

from backend.services.throttle import is_valid_throttle_preset, resolve_delay

logger = logging.getLogger(__name__)


def _preset(preset: str) -> str:
    return preset if is_valid_throttle_preset(preset) else "medium"


def _sleep_action_fraction(action: str, preset: str, lo_f: float, hi_f: float) -> None:
    lo, hi = resolve_delay(action, preset=_preset(preset))
    time.sleep(random.uniform(lo * lo_f, hi * hi_f))


def _viewport_point(page: Page) -> tuple[int, int]:
    try:
        vp = page.viewport_size
        if vp and vp.get("width") and vp.get("height"):
            w, h = int(vp["width"]), int(vp["height"])
            return (
                max(40, int(w * random.uniform(0.32, 0.68))),
                max(40, int(h * random.uniform(0.26, 0.62))),
            )
    except Exception:
        pass
    return (640, 420)


def soft_scroll_feed(page: Page, preset: str, *, strength: float = 1.0) -> None:
    """Лёгкая прокрутка основной колонки / окна — как просмотр ленты."""
    pr = _preset(preset)
    _sleep_action_fraction("scroll_page", pr, 0.28 * strength, 0.52 * strength)
    try:
        x, y = _viewport_point(page)
        page.mouse.move(x, y, steps=random.randint(5, 16))
        base = random.randint(220, 780)
        direction = random.choice([1, 1, 1, -1])
        dy = int(base * strength * direction)
        page.mouse.wheel(0, dy)
    except Exception:
        try:
            n = int(180 * strength * random.choice([1, 1, -1]))
            page.evaluate("(y) => window.scrollBy(0, y)", n)
        except Exception as ex:
            logger.debug("soft_scroll_feed fallback: %s", ex)
    _sleep_action_fraction("scroll_page", pr, 0.22 * strength, 0.45 * strength)


def micro_reading_pause(page: Page, preset: str) -> None:
    """Короткая пауза «смотрит в экран» без скролла."""
    pr = _preset(preset)
    _sleep_action_fraction("navigate_profile", pr, 0.14, 0.38)
    try:
        x, y = _viewport_point(page)
        page.mouse.move(x + random.randint(-40, 40), y + random.randint(-25, 25), steps=random.randint(3, 10))
    except Exception:
        pass


def humanize_after_profile_open(page: Page, preset: str) -> None:
    """Сразу после перехода на профиль: чаще всего скролл + иногда вторая прокрутка или пауза."""
    pr = _preset(preset)
    if random.random() < 0.9:
        soft_scroll_feed(page, pr, strength=random.uniform(0.75, 1.1))
    if random.random() < 0.48:
        micro_reading_pause(page, pr)
    if random.random() < 0.38:
        soft_scroll_feed(page, pr, strength=random.uniform(0.45, 0.85))


def humanize_before_like(page: Page, preset: str) -> None:
    pr = _preset(preset)
    if random.random() < 0.82:
        soft_scroll_feed(page, pr, strength=random.uniform(0.55, 1.0))
    if random.random() < 0.55:
        micro_reading_pause(page, pr)
    if random.random() < 0.35:
        soft_scroll_feed(page, pr, strength=random.uniform(0.4, 0.75))


def humanize_before_comment(page: Page, preset: str) -> None:
    pr = _preset(preset)
    if random.random() < 0.88:
        soft_scroll_feed(page, pr, strength=random.uniform(0.65, 1.15))
    if random.random() < 0.62:
        micro_reading_pause(page, pr)
    if random.random() < 0.5:
        soft_scroll_feed(page, pr, strength=random.uniform(0.5, 0.9))
    if random.random() < 0.28:
        _sleep_action_fraction("comment_post", pr, 0.08, 0.18)


def humanize_before_friend_request(page: Page, preset: str) -> None:
    pr = _preset(preset)
    if random.random() < 0.75:
        soft_scroll_feed(page, pr, strength=random.uniform(0.5, 0.95))
    if random.random() < 0.58:
        micro_reading_pause(page, pr)
    if random.random() < 0.4:
        _sleep_action_fraction("friend_request", pr, 0.1, 0.22)


def humanize_before_dm(page: Page, preset: str) -> None:
    pr = _preset(preset)
    if random.random() < 0.7:
        soft_scroll_feed(page, pr, strength=random.uniform(0.45, 0.85))
    if random.random() < 0.52:
        micro_reading_pause(page, pr)
    if random.random() < 0.35:
        _sleep_action_fraction("open_dm", pr, 0.12, 0.28)


def humanize_before_combo_like_comment(page: Page, preset: str) -> None:
    humanize_before_like(page, preset)
    if random.random() < 0.45:
        micro_reading_pause(page, preset)


def humanize_between_like_and_comment(page: Page, preset: str) -> None:
    pr = _preset(preset)
    if random.random() < 0.85:
        soft_scroll_feed(page, pr, strength=random.uniform(0.55, 1.0))
    if random.random() < 0.5:
        micro_reading_pause(page, pr)


def humanize_between_comment_and_friend(page: Page, preset: str) -> None:
    humanize_before_friend_request(page, preset)


def humanize_between_friend_and_dm(page: Page, preset: str) -> None:
    humanize_before_dm(page, preset)


def humanize_for_sequence_action(page: Page, preset: str, action_type: str) -> None:
    """После открытия профиля в шаге сценария — в зависимости от типа шага."""
    at = (action_type or "").strip().lower().replace("-", "_")
    if at == "like_post":
        humanize_after_profile_open(page, preset)
        humanize_before_like(page, preset)
        return
    if at == "comment_post":
        humanize_after_profile_open(page, preset)
        humanize_before_comment(page, preset)
        return
    if at == "friend_request":
        humanize_after_profile_open(page, preset)
        humanize_before_friend_request(page, preset)
        return
    if at in ("send_dm", "open_dm"):
        humanize_after_profile_open(page, preset)
        humanize_before_dm(page, preset)
        return
    if at in ("like_and_comment", "like_comment"):
        humanize_after_profile_open(page, preset)
        humanize_before_combo_like_comment(page, preset)
        return
    if at in ("like_comment_friend_request", "like_comment_friend_request_send_dm"):
        humanize_after_profile_open(page, preset)
        humanize_before_combo_like_comment(page, preset)
        return
    humanize_after_profile_open(page, preset)
