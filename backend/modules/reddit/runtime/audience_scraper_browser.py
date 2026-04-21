"""Reddit audience scraper через browser-mode (Playwright).

Два режима:
  1) `scan_subreddit_authors(account_id, subreddit, limit, sort)` — открывает
     /r/<subreddit>/<sort>/, парсит первые N постов и собирает их authors
     как leads (source_type="subreddit_scan", source_ref="r/<subreddit>").
  2) `search_users(account_id, keywords, limit)` — открывает
     /search/?q=<kw>&type=user, собирает первые N результатов как leads
     (source_type="user_mentions", source_ref=keywords).

Дедупликация по `(organization_id, username)` — повторный username не создаётся.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.reddit.models import RedditAccount, RedditLead
from backend.modules.reddit.runtime.dm_sender import _LOGIN_HINTS, _SUSPEND_HINTS
from backend.modules.reddit.runtime.playwright_session import RedditBrowser

logger = logging.getLogger(__name__)


_SUBREDDIT_RE = re.compile(r"^[a-zA-Z0-9_]{2,21}$")


# Селекторы для постов — несколько fallback'ов под старый и новый редизайн.
_POST_AUTHOR_SELECTORS = (
    "shreddit-post a[href*='/user/']",  # new shreddit
    "article a[href*='/user/']",
    "div.Post a[href^='/user/']",
    "div.thing a.author",  # legacy
    "[data-testid='post-author-link']",
)

# Поисковая выдача users.
_USER_SEARCH_RESULT_LINKS = (
    "shreddit-search-result a[href^='/user/']",
    "div.search-result a[href^='/user/']",
    "a.search-result-header-meta-link[href^='/user/']",
    "main a[href*='/user/']",
)


def _extract_username(href: str) -> str | None:
    if not href:
        return None
    s = href.lower()
    for prefix in ("/user/", "/u/"):
        idx = s.find(prefix)
        if idx >= 0:
            tail = href[idx + len(prefix):]
            tail = tail.split("/")[0].split("?")[0].strip()
            if tail and tail != "[deleted]":
                return tail
    return None


def _save_lead_if_new(
    db: Session,
    organization_id: int | None,
    username: str,
    source_type: str,
    source_ref: str,
    owner_account_id: int | None = None,
) -> bool:
    if not username:
        return False
    existing = (
        db.query(RedditLead)
        .filter(
            RedditLead.organization_id == organization_id,
            RedditLead.username == username,
        )
        .first()
    )
    if existing is not None:
        return False
    lead = RedditLead(
        organization_id=organization_id,
        username=username,
        source_type=source_type,
        source_ref=source_ref[:400],
        activity_timestamp=datetime.now(timezone.utc),
        status="new",
        owner_account_id=owner_account_id,
    )
    db.add(lead)
    return True


def _common_browser_setup(page) -> None:
    try:
        from backend.modules.reddit.runtime.human_behavior import (
            antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
        )

        if antidetect_enabled():
            gentle_scroll(page, total_px=600)
            mouse_jitter(page, steps=4)
            random_idle(0.7, 1.5)
    except Exception:  # noqa: BLE001
        pass


def _extract_unique_usernames_from_locator(page, selectors: tuple[str, ...], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sel in selectors:
        if len(out) >= limit:
            break
        try:
            loc = page.locator(sel)
            count = loc.count()
        except Exception:  # noqa: BLE001
            count = 0
        for i in range(min(count, limit * 4)):  # x4 запас, потому что часть отфильтруется
            if len(out) >= limit:
                break
            try:
                href = loc.nth(i).get_attribute("href", timeout=400) or ""
            except Exception:  # noqa: BLE001
                href = ""
            uname = _extract_username(href)
            if not uname:
                continue
            if uname.lower() in seen:
                continue
            seen.add(uname.lower())
            out.append(uname)
    return out


def scan_subreddit_authors(
    db: Session,
    account_id: int,
    subreddit: str,
    *,
    headless: bool = True,
    limit: int = 25,
    sort: str = "new",  # new | hot | top
) -> dict[str, Any]:
    name = (subreddit or "").strip().lstrip("r/").lstrip("/r/").lstrip("/")
    if not name or not _SUBREDDIT_RE.match(name):
        return {"ok": False, "reason_code": "invalid_subreddit"}
    sort = (sort or "new").lower().strip()
    if sort not in ("new", "hot", "top", "rising"):
        sort = "new"

    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if acc.auth_mode != "browser_profile":
        return {"ok": False, "reason_code": "not_browser_mode"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    url = f"https://www.reddit.com/r/{name}/{sort}/"
    found = 0
    saved = 0
    skipped = 0
    try:
        with RedditBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:2000]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in cur for h in _LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if any(h in body_excerpt for h in _SUSPEND_HINTS):
                return {"ok": False, "reason_code": "account_suspended"}
            if "this community is private" in body_excerpt or "subreddit, banned" in body_excerpt:
                return {"ok": False, "reason_code": "subreddit_unavailable"}

            _common_browser_setup(page)

            usernames = _extract_unique_usernames_from_locator(page, _POST_AUTHOR_SELECTORS, limit=limit)
            found = len(usernames)
            for u in usernames:
                if _save_lead_if_new(db, org_id, u, "subreddit_scan", f"r/{name}"):
                    saved += 1
                else:
                    skipped += 1
            db.commit()
            return {"ok": True, "found": found, "saved": saved, "skipped": skipped, "url": url}
    except Exception as e:  # noqa: BLE001
        logger.exception("reddit subreddit scan failed for account_id=%s", account_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def search_users(
    db: Session,
    account_id: int,
    keywords: str,
    *,
    headless: bool = True,
    limit: int = 25,
) -> dict[str, Any]:
    keywords = (keywords or "").strip()
    if not keywords:
        return {"ok": False, "reason_code": "empty_keywords"}

    acc = db.get(RedditAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if acc.auth_mode != "browser_profile":
        return {"ok": False, "reason_code": "not_browser_mode"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    url = "https://www.reddit.com/search/?" + urllib.parse.urlencode({"q": keywords, "type": "user"})
    found = 0
    saved = 0
    skipped = 0
    try:
        with RedditBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2000) or "").lower()[:1500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if any(h in cur for h in _LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if any(h in body_excerpt for h in _SUSPEND_HINTS):
                return {"ok": False, "reason_code": "account_suspended"}

            _common_browser_setup(page)

            usernames = _extract_unique_usernames_from_locator(page, _USER_SEARCH_RESULT_LINKS, limit=limit)
            found = len(usernames)
            for u in usernames:
                if _save_lead_if_new(db, org_id, u, "user_mentions", keywords):
                    saved += 1
                else:
                    skipped += 1
            db.commit()
            return {"ok": True, "found": found, "saved": saved, "skipped": skipped, "url": url}
    except Exception as e:  # noqa: BLE001
        logger.exception("reddit search_users failed for account_id=%s", account_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["scan_subreddit_authors", "search_users"]
