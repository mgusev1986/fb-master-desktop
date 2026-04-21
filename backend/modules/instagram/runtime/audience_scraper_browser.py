"""Instagram audience scraper через browser-mode.

Поддерживает 4 режима:
  * search_users(keywords) — поиск через explore/search.
  * scrape_hashtag(tag) — авторы постов с hashtag.
  * scrape_post_likers(post_url) — лайкавшие пост.
  * scrape_post_commenters(post_url) — комментировавшие пост.
  * scrape_followers(handle) — подписчики (для competitor_followers).

run_competitor_audience_task(task_id) — диспатчер для UI.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.instagram.models import (
    InstagramAccount, InstagramCompetitorAudienceTask, InstagramLead,
)
from backend.modules.instagram.runtime.dom_resilience import (
    COMMENT_AUTHOR_LINK_SELECTORS, HASHTAG_POST_LINK_SELECTORS,
    LIKES_MODAL_USER_LINK_SELECTORS, LOCALIZED_LOGIN_HINTS,
    LOCALIZED_RESTRICTED_HINTS, SEARCH_USER_LINK_SELECTORS, page_has_any_hint,
)
from backend.modules.instagram.runtime.playwright_session import InstagramBrowser
from backend.modules.instagram.services import accounts as accounts_svc

logger = logging.getLogger(__name__)


_RESERVED = {
    "explore", "reels", "stories", "direct", "p", "reel",
    "accounts", "challenge", "developer", "about", "legal", "press",
    "api", "logout", "your_activity",
}


def _extract_handle(href: str) -> str | None:
    if not href:
        return None
    s = href.strip()
    if "://" in s:
        try:
            s = urllib.parse.urlparse(s).path
        except Exception:  # noqa: BLE001
            pass
    s = s.lstrip("/")
    parts = s.split("/")
    if not parts or not parts[0]:
        return None
    cand = parts[0].split("?")[0].lower()
    if cand in _RESERVED:
        return None
    if not re.match(r"^[a-z0-9._]{1,30}$", cand):
        return None
    return cand


def _save_lead_if_new(db: Session, organization_id: int | None, handle: str, source_kind: str, source_ref: str) -> bool:
    if not handle:
        return False
    existing = (
        db.query(InstagramLead)
        .filter(InstagramLead.organization_id == organization_id, InstagramLead.handle == handle)
        .first()
    )
    if existing is not None:
        return False
    lead = InstagramLead(
        organization_id=organization_id, handle=handle,
        profile_url=f"https://www.instagram.com/{handle}/",
        source_kind=source_kind, source_ref=source_ref[:512],
    )
    db.add(lead)
    return True


def _common_setup(page) -> None:
    try:
        from backend.modules.instagram.runtime.human_behavior import (
            antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
        )

        if antidetect_enabled():
            gentle_scroll(page, total_px=600)
            mouse_jitter(page, steps=4)
            random_idle(0.7, 1.5)
    except Exception:  # noqa: BLE001
        pass


def _extract_unique_handles(page, selectors: tuple[str, ...], limit: int) -> list[str]:
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
        for i in range(min(count, limit * 4)):
            if len(out) >= limit:
                break
            try:
                href = loc.nth(i).get_attribute("href", timeout=400) or ""
            except Exception:  # noqa: BLE001
                href = ""
            h = _extract_handle(href)
            if h and h not in seen:
                seen.add(h)
                out.append(h)
    return out


# ── Public scrapers ───────────────────────────────────────


def search_users(
    db: Session, account_id: int, keywords: str,
    *, headless: bool = True, max_results: int = 20, segment_id: int | None = None,
) -> dict[str, Any]:
    keywords = (keywords or "").strip()
    if not keywords:
        return {"ok": False, "reason_code": "empty_keywords"}
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    url = "https://www.instagram.com/explore/search/keyword/?q=" + urllib.parse.quote(keywords)
    return _scrape_with_url(db, acc, url, max_results, source_kind="search_users", source_ref=keywords, segment_id=segment_id, selectors=SEARCH_USER_LINK_SELECTORS)


def scrape_hashtag(
    db: Session, account_id: int, hashtag: str,
    *, headless: bool = True, max_results: int = 25, segment_id: int | None = None,
) -> dict[str, Any]:
    tag = (hashtag or "").strip().lstrip("#").lower()
    if not tag or not re.match(r"^[a-z0-9_]{1,80}$", tag):
        return {"ok": False, "reason_code": "invalid_hashtag"}
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    found = 0
    saved = 0
    skipped = 0
    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}

            _common_setup(page)

            # Собираем ссылки на посты (article a[href^='/p/']), затем для top-K берём authors.
            try:
                post_links = page.locator(", ".join(HASHTAG_POST_LINK_SELECTORS))
                count = post_links.count()
            except Exception:  # noqa: BLE001
                count = 0
            posts_to_visit = []
            seen_posts: set[str] = set()
            for i in range(min(count, max_results * 2)):
                try:
                    href = post_links.nth(i).get_attribute("href", timeout=300) or ""
                except Exception:  # noqa: BLE001
                    href = ""
                if href and href not in seen_posts and "/p/" in href:
                    seen_posts.add(href)
                    posts_to_visit.append(href if href.startswith("http") else f"https://www.instagram.com{href}")
                if len(posts_to_visit) >= max_results:
                    break

            seen_handles: set[str] = set()
            for post_url in posts_to_visit[:max_results]:
                try:
                    page.goto(post_url, wait_until="domcontentloaded", timeout=15000)
                    import time as _time
                    _time.sleep(1.0)
                    # author link обычно первый в article header.
                    try:
                        href = page.locator("article header a[href^='/']").first.get_attribute("href", timeout=400) or ""
                    except Exception:  # noqa: BLE001
                        href = ""
                    h = _extract_handle(href)
                    if h and h not in seen_handles:
                        seen_handles.add(h)
                        found += 1
                        if _save_lead_if_new(db, org_id, h, "hashtag_scan", f"#{tag}"):
                            saved += 1
                        else:
                            skipped += 1
                except Exception:  # noqa: BLE001
                    skipped += 1
                    continue

            db.commit()
            accounts_svc.log_event(db, org_id, acc.id, "hashtag_scrape", "info",
                                   {"tag": tag, "found": found, "saved": saved, "skipped": skipped})
            db.commit()

            if segment_id:
                from backend.modules.instagram.services import audience_segments as seg_svc
                try:
                    seg_svc.record_run(db, segment_id, saved)
                except Exception:  # noqa: BLE001
                    logger.exception("instagram hashtag: segment record_run failed")

            return {"ok": True, "found": found, "saved": saved, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram hashtag scraper failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def _scrape_with_url(
    db: Session, acc: InstagramAccount, url: str, max_results: int,
    *, source_kind: str, source_ref: str, segment_id: int | None,
    selectors: tuple[str, ...], headless: bool = True,
) -> dict[str, Any]:
    """Generic scraper: открыть URL и собрать handles по selectors."""
    org_id = acc.organization_id
    found = 0
    saved = 0
    skipped = 0
    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}

            _common_setup(page)

            handles = _extract_unique_handles(page, selectors, limit=max_results)
            found = len(handles)
            for h in handles:
                if _save_lead_if_new(db, org_id, h, source_kind, source_ref):
                    saved += 1
                else:
                    skipped += 1
            db.commit()

            accounts_svc.log_event(db, org_id, acc.id, "audience_scrape", "info",
                                   {"source_kind": source_kind, "ref": source_ref[:80],
                                    "found": found, "saved": saved, "skipped": skipped})
            db.commit()

            if segment_id:
                from backend.modules.instagram.services import audience_segments as seg_svc
                try:
                    seg_svc.record_run(db, segment_id, saved)
                except Exception:  # noqa: BLE001
                    logger.exception("instagram scraper: segment record_run failed")

            return {"ok": True, "found": found, "saved": saved, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram scraper failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def scrape_post_likers(
    db: Session, account_id: int, post_url: str,
    *, headless: bool = True, max_results: int = 50,
) -> dict[str, Any]:
    """Открыть пост → клик на «X likes» → парсинг modal."""
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not post_url or "instagram.com/p/" not in post_url.lower():
        return {"ok": False, "reason_code": "invalid_post_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    found = 0
    saved = 0
    skipped = 0
    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}

            _common_setup(page)

            # Кликаем на "likes count" (обычно ссылка под постом).
            try:
                # Находим ссылку с числом + словом "like".
                like_count_link = page.locator(
                    "a:has-text('likes'), a:has-text('лайк'), a[href$='/liked_by/']"
                ).first
                like_count_link.click(timeout=4000)
                import time as _time
                _time.sleep(2.0)
            except Exception:  # noqa: BLE001
                return {"ok": False, "reason_code": "likes_link_not_found"}

            handles = _extract_unique_handles(page, LIKES_MODAL_USER_LINK_SELECTORS, limit=max_results)
            found = len(handles)
            for h in handles:
                if _save_lead_if_new(db, org_id, h, "competitor_likers", post_url):
                    saved += 1
                else:
                    skipped += 1
            db.commit()
            return {"ok": True, "found": found, "saved": saved, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram scrape_post_likers failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def scrape_post_commenters(
    db: Session, account_id: int, post_url: str,
    *, headless: bool = True, max_results: int = 50,
) -> dict[str, Any]:
    """Открыть пост → парсить authors из секции комментариев."""
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not post_url or "instagram.com/p/" not in post_url.lower():
        return {"ok": False, "reason_code": "invalid_post_url"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    found = 0
    saved = 0
    skipped = 0
    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}

            _common_setup(page)
            # Скроллим в комментариях для подгрузки.
            for _ in range(3):
                try:
                    page.mouse.wheel(0, 600)
                    import time as _time
                    _time.sleep(0.6)
                except Exception:  # noqa: BLE001
                    break

            handles = _extract_unique_handles(page, COMMENT_AUTHOR_LINK_SELECTORS, limit=max_results)
            found = len(handles)
            for h in handles:
                if _save_lead_if_new(db, org_id, h, "competitor_commenters", post_url):
                    saved += 1
                else:
                    skipped += 1
            db.commit()
            return {"ok": True, "found": found, "saved": saved, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram scrape_post_commenters failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def scrape_followers(
    db: Session, account_id: int, competitor_handle: str,
    *, headless: bool = True, max_results: int = 100,
) -> dict[str, Any]:
    """Открыть профиль → followers modal → парсинг."""
    acc = db.get(InstagramAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    competitor_handle = (competitor_handle or "").strip().lstrip("@").lstrip("/")
    if not competitor_handle:
        return {"ok": False, "reason_code": "empty_competitor"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    url = f"https://www.instagram.com/{competitor_handle}/followers/"
    found = 0
    saved = 0
    skipped = 0
    try:
        with InstagramBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2500]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}
            if "user not found" in body_excerpt or "не найден" in body_excerpt:
                return {"ok": False, "reason_code": "competitor_not_found"}

            _common_setup(page)
            # Modal followers — нужно скроллить.
            for _ in range(5):
                try:
                    page.mouse.wheel(0, 800)
                    import time as _time
                    _time.sleep(0.8)
                except Exception:  # noqa: BLE001
                    break

            handles = _extract_unique_handles(page, LIKES_MODAL_USER_LINK_SELECTORS, limit=max_results)
            # Уберём competitor handle из результатов.
            handles = [h for h in handles if h.lower() != competitor_handle.lower()]
            found = len(handles)
            for h in handles:
                if _save_lead_if_new(db, org_id, h, "competitor_followers", competitor_handle):
                    saved += 1
                else:
                    skipped += 1
            db.commit()
            return {"ok": True, "found": found, "saved": saved, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        logger.exception("instagram scrape_followers failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


def run_competitor_audience_task(
    db: Session, task_id: int, *, headless: bool = True,
) -> dict[str, Any]:
    """UI-диспатчер для CompetitorAudienceTask. Обновляет task.found/saved/status."""
    task = db.get(InstagramCompetitorAudienceTask, int(task_id))
    if task is None:
        return {"ok": False, "reason_code": "task_not_found"}
    if not task.account_id:
        task.status = "failed"
        task.last_error = "no_account_assigned"
        db.commit()
        return {"ok": False, "reason_code": "no_account_assigned"}

    task.status = "running"
    db.commit()
    max_results = task.max_results or 50

    if task.kind == "competitor_likers":
        if not task.target_post_url:
            task.status = "failed"; task.last_error = "no_post_url"; db.commit()
            return {"ok": False, "reason_code": "no_post_url"}
        res = scrape_post_likers(db, task.account_id, task.target_post_url, headless=headless, max_results=max_results)
    elif task.kind == "competitor_commenters":
        if not task.target_post_url:
            task.status = "failed"; task.last_error = "no_post_url"; db.commit()
            return {"ok": False, "reason_code": "no_post_url"}
        res = scrape_post_commenters(db, task.account_id, task.target_post_url, headless=headless, max_results=max_results)
    elif task.kind == "competitor_followers":
        if not task.competitor_handle:
            task.status = "failed"; task.last_error = "no_competitor_handle"; db.commit()
            return {"ok": False, "reason_code": "no_competitor_handle"}
        res = scrape_followers(db, task.account_id, task.competitor_handle, headless=headless, max_results=max_results)
    else:
        task.status = "failed"; task.last_error = f"unknown_kind:{task.kind}"; db.commit()
        return {"ok": False, "reason_code": "unknown_kind"}

    if res.get("ok"):
        task.found = int(res.get("found", 0))
        task.saved = int(res.get("saved", 0))
        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc)
        task.last_error = None
    else:
        task.status = "failed"
        task.last_error = (res.get("reason_code") or "unknown")[:512]
    db.commit()
    return res


__all__ = [
    "run_competitor_audience_task",
    "scrape_followers", "scrape_hashtag", "scrape_post_commenters",
    "scrape_post_likers", "search_users",
]
