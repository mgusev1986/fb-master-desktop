"""Playwright: кампании ЛС и комментинга на профиле Facebook."""

from __future__ import annotations

import logging
import random

from playwright.sync_api import Page

from backend.services.playwright_humanize import (
    humanize_after_profile_open,
    humanize_before_comment,
    humanize_before_dm,
    humanize_before_friend_request,
    humanize_before_like,
    humanize_between_like_and_comment,
)
from backend.services.sequence_actions import (
    profile_page_has_dm_entry_visible,
    send_dm_via_profile_popup_chat,
    try_friend_request_on_profile,
    try_send_dm_from_profile,
)
from backend.services.warmup_actions import (
    profile_url_from_person,
    try_comment_on_timeline,
    try_like_posts_on_profile,
)

logger = logging.getLogger(__name__)


def run_outreach_on_profile(
    page: Page,
    canonical_url: str,
    *,
    message_text: str,
    like_first: bool,
    add_friend_first: bool,
    throttle_preset: str = "medium",
    like_mode: str = "first",
    like_pool_size: int = 5,
    like_count: int = 1,
    navigate_timeout_ms: int = 90_000,
    person_raw_meta: dict[str, object] | None = None,
) -> tuple[bool, str]:
    """
    Успех = отправлено ЛС. Лайк/друзья — опциональные шаги перед этим (через страницу профиля).
    """
    url = profile_url_from_person(canonical_url)
    if not url:
        return False, "empty_url"
    text = (message_text or "").strip()
    if not text:
        return False, "empty_message"

    parts: list[str] = []

    def _goto_profile() -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=navigate_timeout_ms)
        page.wait_for_timeout(1200)

    tp = (throttle_preset or "medium").strip().lower()

    try:
        _goto_profile()
        humanize_after_profile_open(page, tp)
        if not profile_page_has_dm_entry_visible(page):
            return False, "message_button_not_found"
        if like_first:
            humanize_before_like(page, tp)
            lm = (like_mode or "first").strip().lower()
            if lm not in ("first", "random"):
                lm = "first"
            lp = max(2, min(int(like_pool_size or 5), 10))
            lc = max(1, min(int(like_count or 1), 10))
            lk, msg = try_like_posts_on_profile(
                page, mode=lm, pool_size=lp, count=lc
            )
            parts.append(f"like={'ok' if lk else 'fail'}:{msg}")
            page.wait_for_timeout(800)

        if add_friend_first:
            _goto_profile()
            humanize_after_profile_open(page, tp)
            humanize_before_friend_request(page, tp)
            fk, msg = try_friend_request_on_profile(page)
            parts.append(f"friend={'ok' if fk else 'fail'}:{msg}")
            page.wait_for_timeout(800)

        if like_first or add_friend_first:
            _goto_profile()
            humanize_after_profile_open(page, tp)

        dk, msg = send_dm_via_profile_popup_chat(
            page,
            text,
            canonical_url=canonical_url,
            throttle_preset=tp,
        )
        parts.append(f"dm={'ok' if dk else 'fail'}:{msg}")
        if dk:
            return True, "; ".join(parts)
        return False, "; ".join(parts)
    except Exception as e:
        logger.exception("run_outreach_on_profile")
        return False, f"err:{e!s}"[:200]


def run_commenting_on_profile(
    page: Page,
    canonical_url: str,
    *,
    comment_text: str,
    like_first: bool,
    throttle_preset: str = "medium",
    like_mode: str = "first",
    like_pool_size: int = 5,
    like_count: int = 1,
    comment_mode: str = "first",
    comment_pool_size: int = 5,
    comment_pick_index: int = 0,
    navigate_timeout_ms: int = 90_000,
) -> tuple[bool, str]:
    """
    Успех = комментарий оставлен. Лайк — опциональный шаг перед комментарием.
    """
    url = profile_url_from_person(canonical_url)
    if not url:
        return False, "empty_url"
    text = (comment_text or "").strip()
    if not text:
        return False, "empty_comment"

    parts: list[str] = []

    def _goto_profile() -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=navigate_timeout_ms)
        page.wait_for_timeout(1200)

    tp = (throttle_preset or "medium").strip().lower()

    try:
        _goto_profile()
        humanize_after_profile_open(page, tp)
        if like_first:
            humanize_before_like(page, tp)
            lm = (like_mode or "first").strip().lower()
            if lm not in ("first", "random"):
                lm = "first"
            lp = max(2, min(int(like_pool_size or 5), 10))
            lc = max(1, min(int(like_count or 1), 10))
            lk, msg = try_like_posts_on_profile(
                page, mode=lm, pool_size=lp, count=lc
            )
            parts.append(f"like={'ok' if lk else 'fail'}:{msg}")
            page.wait_for_timeout(800)

        _goto_profile()
        humanize_after_profile_open(page, tp)
        if like_first:
            humanize_between_like_and_comment(page, tp)
        else:
            humanize_before_comment(page, tp)
        cm = (comment_mode or "first").strip().lower()
        if cm not in ("first", "random"):
            cm = "first"
        cp = max(1, min(int(comment_pool_size or 5), 10))
        pick = max(0, int(comment_pick_index or 0))
        ck, msg = try_comment_on_timeline(
            page,
            text,
            mode=cm,
            pool_size=cp,
            pick_index=pick,
        )
        parts.append(f"comment={'ok' if ck else 'fail'}:{msg}")
        if ck:
            return True, "; ".join(parts)
        return False, "; ".join(parts)
    except Exception as e:
        logger.exception("run_commenting_on_profile")
        return False, f"err:{e!s}"[:200]
