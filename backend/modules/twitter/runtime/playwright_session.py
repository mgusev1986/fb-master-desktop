"""Twitter / X Playwright session — persistent profile + cookies + proxy."""

from __future__ import annotations

import contextlib
import logging
import os
import random
from pathlib import Path
from typing import Any, Iterator

from backend.modules.twitter.models import TwitterAccount

logger = logging.getLogger(__name__)


_DEFAULT_USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
_DEFAULT_VIEWPORT = (1440, 900)
_DEFAULT_LOCALE = "en-US"
_DEFAULT_TIMEZONE = "America/Los_Angeles"

try:
    from backend.services.fb_stealth_engine import STEALTH_CHROMIUM_BASE_ARGS as _STEALTH_ARGS
except Exception:  # noqa: BLE001
    _STEALTH_ARGS = (
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
    )

_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


def _proxy_kwargs(account: TwitterAccount) -> dict[str, Any] | None:
    if not account.proxy_enabled or not account.proxy_url:
        return None
    proxy: dict[str, Any] = {"server": account.proxy_url}
    if account.proxy_username:
        proxy["username"] = account.proxy_username
    if account.proxy_password:
        proxy["password"] = account.proxy_password
    return proxy


def _normalize_cookies_for_playwright(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """X живёт на двух доменах: .x.com (новый) и .twitter.com (legacy redirect).
    Дублируем критичные cookies (auth_token, ct0) на оба домена для совместимости."""
    out: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()  # (name, domain)
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".x.com"
        path = c.get("path") or "/"
        cookie: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
            "domain": str(domain),
            "path": str(path),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
        }
        same_site = c.get("sameSite") or "None"
        if same_site in ("Strict", "Lax", "None"):
            cookie["sameSite"] = same_site
        expires = c.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = float(expires)

        key = (cookie["name"], cookie["domain"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(cookie)

        # Дубликат на второй домен для критичных cookies (если ещё не было).
        if name in ("auth_token", "ct0", "twid", "kdt", "att"):
            other_domain = ".twitter.com" if domain == ".x.com" else ".x.com"
            other_key = (name, other_domain)
            if other_key not in seen_keys:
                seen_keys.add(other_key)
                dup = dict(cookie)
                dup["domain"] = other_domain
                out.append(dup)
    return out


class TwitterBrowser:
    def __init__(self, account: TwitterAccount, *, headless: bool | None = None, slow_mo_ms: int = 0) -> None:
        self.account = account
        self.headless = (
            headless
            if headless is not None
            else (os.getenv("FB_MASTER_TWITTER_HEADLESS", "0").strip() in ("1", "true", "yes"))
        )
        self.slow_mo_ms = slow_mo_ms
        self._pw = None
        self._context = None
        self._cookies_restored = False

    def __enter__(self) -> "TwitterBrowser":
        from playwright.sync_api import sync_playwright

        from backend.modules.twitter.runtime.human_behavior import (
            antidetect_enabled,
            jittered_viewport,
        )
        from backend.services.fb_playwright import chromium_launch_kwargs_with_bundle

        profile_dir = Path(self.account.profile_dir or "")
        if not str(profile_dir):
            raise RuntimeError("TwitterBrowser: account.profile_dir is empty")
        profile_dir.mkdir(parents=True, exist_ok=True)

        self._pw = sync_playwright().start()
        ua = self.account.stealth_user_agent or random.choice(_DEFAULT_USER_AGENTS)
        base_w = self.account.stealth_viewport_w or _DEFAULT_VIEWPORT[0]
        base_h = self.account.stealth_viewport_h or _DEFAULT_VIEWPORT[1]
        if antidetect_enabled():
            jw, jh = jittered_viewport(base_w, base_h)
            viewport = {"width": jw, "height": jh}
        else:
            viewport = {"width": base_w, "height": base_h}
        locale_str = self.account.stealth_locale or _DEFAULT_LOCALE
        tz = self.account.stealth_timezone_id or _DEFAULT_TIMEZONE
        proxy = _proxy_kwargs(self.account)

        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "channel": "chromium",
            "args": list(_STEALTH_ARGS),
            "user_agent": ua,
            "viewport": viewport,
            "locale": locale_str,
            "timezone_id": tz,
            "slow_mo": self.slow_mo_ms or 0,
            "ignore_https_errors": True,
        }
        if proxy:
            launch_kwargs["proxy"] = proxy

        try:
            self._context = self._pw.chromium.launch_persistent_context(
                str(profile_dir), **chromium_launch_kwargs_with_bundle(launch_kwargs)
            )
        except Exception:
            self._safe_stop_pw()
            raise

        try:
            self._context.add_init_script(_STEALTH_INIT_JS)
        except Exception:  # noqa: BLE001
            logger.exception("TwitterBrowser: add_init_script failed (non-fatal)")

        self._restore_cookies_if_needed()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context is not None:
                with contextlib.suppress(Exception):
                    self._context.close()
        finally:
            self._safe_stop_pw()

    def _safe_stop_pw(self) -> None:
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
            self._pw = None

    def _restore_cookies_if_needed(self) -> None:
        if self._cookies_restored:
            return
        cookies = _normalize_cookies_for_playwright(self.account.cookies_json or [])
        if not cookies:
            return
        try:
            self._context.add_cookies(cookies)
            self._cookies_restored = True
        except Exception:  # noqa: BLE001
            logger.exception("TwitterBrowser.add_cookies failed for account_id=%s", getattr(self.account, "id", None))

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("TwitterBrowser used outside `with` block")
        return self._context

    def new_page(self):
        return self.context.new_page()

    def is_logged_in(self, page) -> bool:
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        if "/i/flow/login" in url or "/login" in url:
            return False
        try:
            from backend.modules.twitter.runtime.dom_resilience import LOGGED_IN_SELECTORS

            combined = ", ".join(LOGGED_IN_SELECTORS)
            if page.locator(combined).first.is_visible(timeout=2500):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False


@contextlib.contextmanager
def open_session(account: TwitterAccount, *, headless: bool | None = None) -> Iterator[TwitterBrowser]:
    with TwitterBrowser(account, headless=headless) as s:
        yield s


__all__ = ["TwitterBrowser", "open_session"]
