"""LinkedIn audience scraper — поиск людей через `/search/results/people/` и
сохранение результатов в `LinkedInLead`.

Поддерживается лёгкий поиск по строке-ключевому слову + опциональным
фильтрам (компания, локация). Без Sales Navigator: возвращает
ограниченное число результатов (LinkedIn показывает ~10/страница и
требует premium для многих фильтров). Для полноценного поиска по ICP —
рекомендуется ручной impotr URL'ов через `/linkedin/leads`.

Контракт: dict
  {ok: bool, found: int, saved: int, skipped: int, reason_code?: str}
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.runtime.dm_sender import _LOGIN_HINTS, _RESTRICTED_HINTS, _human_pause
from backend.modules.linkedin.runtime.playwright_session import LinkedInBrowser
from backend.modules.linkedin.services import accounts as accounts_svc
from backend.modules.linkedin.services import leads as leads_svc

logger = logging.getLogger(__name__)


_SEARCH_URL = "https://www.linkedin.com/search/results/people/?keywords={kw}"
_RESULT_CARD = "li.reusable-search__result-container, div.reusable-search__entity-result-list > li"
_NAME_LINK = "a span[aria-hidden='true'], .entity-result__title-text a"
_PROFILE_LINK = "a.app-aware-link[href*='/in/']"
_HEADLINE = ".entity-result__primary-subtitle, .t-14.t-black"
_LOCATION = ".entity-result__secondary-subtitle"


def _extract_handle(profile_url: str) -> str | None:
    try:
        path = urllib.parse.urlparse(profile_url).path
        # /in/<vanity>/...
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "in":
            return urllib.parse.unquote(parts[1]).split("?")[0]
    except Exception:  # noqa: BLE001
        pass
    return None


def search_people(
    db: Session,
    account_id: int,
    keywords: str,
    *,
    headless: bool = True,
    max_results: int = 10,
    source_kind: str = "search",
    segment_id: int | None = None,
) -> dict[str, Any]:
    keywords = (keywords or "").strip()
    if not keywords:
        return {"ok": False, "reason_code": "empty_keywords"}
    acc = accounts_svc.get_account(db, account_id)
    if acc is None:
        return {"ok": False, "reason_code": "account_not_found"}
    if not (acc.cookies_json or []):
        return {"ok": False, "reason_code": "no_cookies"}

    org_id = acc.organization_id
    url = _SEARCH_URL.format(kw=urllib.parse.quote_plus(keywords))
    found = 0
    saved = 0
    skipped = 0
    try:
        with LinkedInBrowser(acc, headless=headless) as session:
            page = session.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                accounts_svc.log_event(db, org_id, acc.id, "audience_search", "error", {"phase": "goto", "error": str(e)[:300]})
                db.commit()
                return {"ok": False, "reason_code": "navigation_failed", "details": str(e)[:300]}

            cur = (page.url or "").lower()
            if any(h in cur for h in _RESTRICTED_HINTS):
                return {"ok": False, "reason_code": "account_restricted"}
            if any(h in cur for h in _LOGIN_HINTS):
                return {"ok": False, "reason_code": "login_required"}

            _human_pause(800, 1500)

            # Anti-detect: проскроллить страницу до конца — это естественное поведение
            # пользователя при просмотре поисковой выдачи.
            try:
                from backend.modules.linkedin.runtime.human_behavior import (
                    antidetect_enabled, gentle_scroll, mouse_jitter, random_idle,
                )

                if antidetect_enabled():
                    gentle_scroll(page, total_px=600)
                    mouse_jitter(page, steps=4)
                    random_idle(0.7, 1.6)
            except Exception:  # noqa: BLE001
                pass

            try:
                cards = page.locator(_RESULT_CARD)
                count = cards.count()
            except Exception:  # noqa: BLE001
                count = 0

            limit = min(count, max_results)
            for i in range(limit):
                try:
                    card = cards.nth(i)
                    name = ""
                    profile_url = ""
                    headline = ""
                    location = ""
                    try:
                        name = (card.locator(_NAME_LINK).first.inner_text(timeout=800) or "").strip()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        profile_url = (card.locator(_PROFILE_LINK).first.get_attribute("href", timeout=800) or "").split("?")[0]
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        headline = (card.locator(_HEADLINE).first.inner_text(timeout=800) or "").strip()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        location = (card.locator(_LOCATION).first.inner_text(timeout=800) or "").strip()
                    except Exception:  # noqa: BLE001
                        pass

                    if not profile_url or "/in/" not in profile_url:
                        skipped += 1
                        continue
                    if profile_url.startswith("/"):
                        profile_url = "https://www.linkedin.com" + profile_url
                    handle = _extract_handle(profile_url)
                    found += 1

                    # Дедупликация по profile_url в рамках организации.
                    from backend.modules.linkedin.models import LinkedInLead

                    exists = (
                        db.query(LinkedInLead)
                        .filter(
                            LinkedInLead.organization_id == org_id,
                            LinkedInLead.profile_url == profile_url,
                        )
                        .first()
                    )
                    if exists is not None:
                        skipped += 1
                        continue

                    leads_svc.create_lead(
                        db,
                        org_id,
                        profile_url=profile_url,
                        public_identifier=handle,
                        full_name=name,
                        headline=headline,
                        location=location,
                        source_kind=source_kind,
                        source_ref=keywords,
                    )
                    saved += 1
                except Exception:  # noqa: BLE001
                    logger.exception("audience_scraper card error idx=%d", i)
                    skipped += 1
                    continue

            accounts_svc.log_event(
                db, org_id, acc.id, "audience_search", "info",
                {"keywords": keywords, "found": found, "saved": saved, "skipped": skipped, "segment_id": segment_id},
            )
            db.commit()
            if segment_id:
                from backend.modules.linkedin.services import audience_segments as seg_svc

                try:
                    seg_svc.record_run(db, segment_id, saved)
                except Exception:  # noqa: BLE001
                    logger.exception("audience_scraper: segment record_run failed")
            return {"ok": True, "found": found, "saved": saved, "skipped": skipped}

    except Exception as e:  # noqa: BLE001
        logger.exception("audience_scraper failed for account_id=%s", account_id)
        try:
            accounts_svc.log_event(db, org_id, acc.id, "audience_search", "error", {"phase": "session", "error": str(e)[:300]})
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"ok": False, "reason_code": "session_failed", "details": str(e)[:300]}


__all__ = ["search_people"]
