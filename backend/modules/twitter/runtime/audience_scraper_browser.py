"""Twitter / X audience scraper через browser-mode.

Открывает /search?q=<keywords>&f=user (или &f=live), парсит карточки UserCell
или ссылки на профили, сохраняет handles как leads с дедупликацией.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.twitter.models import TwitterAccount, TwitterLead
from backend.modules.twitter.runtime.dom_resilience import (
    LOCALIZED_LOGIN_HINTS,
    LOCALIZED_RESTRICTED_HINTS,
    USER_CELL_SELECTORS,
    USER_HANDLE_LINK_SELECTORS,
    page_has_any_hint,
)
from backend.modules.twitter.runtime.playwright_session import TwitterBrowser
from backend.modules.twitter.services import accounts as accounts_svc

logger = logging.getLogger(__name__)


_RESERVED_PATHS = {
    "home", "explore", "notifications", "messages", "i", "compose",
    "search", "settings", "logout", "login", "signup", "tos", "privacy",
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
    candidate = parts[0].split("?")[0].lower()
    if candidate in _RESERVED_PATHS:
        return None
    if not candidate.replace("_", "").isalnum():
        return None
    if len(candidate) < 1 or len(candidate) > 15:
        return None
    return candidate


def _save_lead_if_new(db: Session, organization_id: int | None, handle: str, source_kind: str, source_ref: str) -> bool:
    if not handle:
        return False
    existing = (
        db.query(TwitterLead)
        .filter(TwitterLead.organization_id == organization_id, TwitterLead.handle == handle)
        .first()
    )
    if existing is not None:
        return False
    lead = TwitterLead(
        organization_id=organization_id,
        handle=handle,
        profile_url=f"https://x.com/{handle}",
        source_kind=source_kind,
        source_ref=source_ref[:512],
    )
    db.add(lead)
    return True


def search_users(
    db: Session,
    account_id: int,
    keywords: str,
    *,
    headless: bool = True,
    max_results: int = 15,
    segment_id: int | None = None,
) -> dict[str, Any]:
    keywords = (keywords or "").strip()
    if not keywords:
        return {"ok": False, "reason_code": "empty_keywords"}
    acc = db.get(TwitterAccount, int(account_id))
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    url = "https://x.com/search?" + urllib.parse.urlencode({"q": keywords, "f": "user"})
    found = 0
    saved = 0
    skipped = 0
    try:
        with TwitterBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            try:
                body_excerpt = (page.locator("body").inner_text(timeout=2500) or "").lower()[:2000]
            except Exception:  # noqa: BLE001
                body_excerpt = ""

            if page_has_any_hint(cur, LOCALIZED_LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}
            if page_has_any_hint(body_excerpt, LOCALIZED_RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}

            try:
                from backend.modules.twitter.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=600)
                    mouse_jitter(page, steps=4)
                    random_idle(0.7, 1.5)
            except Exception:  # noqa: BLE001
                pass

            seen: set[str] = set()
            usernames: list[str] = []

            cells_combined = ", ".join(USER_CELL_SELECTORS)
            try:
                cells = page.locator(cells_combined)
                count = cells.count()
            except Exception:  # noqa: BLE001
                count = 0

            limit = max_results
            for i in range(min(count, limit * 4)):
                if len(usernames) >= limit:
                    break
                try:
                    cell = cells.nth(i)
                    links = cell.locator(", ".join(USER_HANDLE_LINK_SELECTORS))
                    lcount = links.count()
                except Exception:  # noqa: BLE001
                    lcount = 0
                for j in range(min(lcount, 5)):
                    if len(usernames) >= limit:
                        break
                    try:
                        href = links.nth(j).get_attribute("href", timeout=400) or ""
                    except Exception:  # noqa: BLE001
                        href = ""
                    h = _extract_handle(href)
                    if h and h not in seen:
                        seen.add(h)
                        usernames.append(h)

            # Fallback: если cells не нашлись, пройдёмся по всем ссылкам main.
            if not usernames:
                try:
                    links = page.locator("main a[role='link']")
                    lcount = links.count()
                except Exception:  # noqa: BLE001
                    lcount = 0
                for k in range(min(lcount, limit * 6)):
                    if len(usernames) >= limit:
                        break
                    try:
                        href = links.nth(k).get_attribute("href", timeout=400) or ""
                    except Exception:  # noqa: BLE001
                        href = ""
                    h = _extract_handle(href)
                    if h and h not in seen:
                        seen.add(h)
                        usernames.append(h)

            found = len(usernames)
            for h in usernames:
                if _save_lead_if_new(db, org_id, h, "search_users", keywords):
                    saved += 1
                else:
                    skipped += 1
            accounts_svc.log_event(
                db, org_id, acc.id, "audience_search", "info",
                {"keywords": keywords, "found": found, "saved": saved, "skipped": skipped, "segment_id": segment_id},
            )
            db.commit()

            if segment_id:
                from backend.modules.twitter.services import audience_segments as seg_svc
                try:
                    seg_svc.record_run(db, segment_id, saved)
                except Exception:  # noqa: BLE001
                    logger.exception("twitter audience: segment record_run failed")

            return {"ok": True, "found": found, "saved": saved, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        logger.exception("twitter audience scraper failed account_id=%s", account_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["search_users"]
