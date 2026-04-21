"""Instagram Playwright session — persistent profile + cookies + proxy.

Особенность IG: рекомендуется mobile UA (Instagram лучше принимает мобильную
сессию web-версии — меньше детекции).
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
from pathlib import Path
from typing import Any, Iterator

from backend.modules.instagram.models import InstagramAccount

logger = logging.getLogger(__name__)


# Mobile UAs (Instagram реагирует мягче).
_DEFAULT_USER_AGENTS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
)
_DEFAULT_VIEWPORT = (414, 896)  # mobile viewport
_DEFAULT_LOCALE = "en-US"
_DEFAULT_TIMEZONE = "America/Los_Angeles"

try:
    from backend.services.fb_stealth_engine import STEALTH_CHROMIUM_BASE_ARGS as _STEALTH_ARGS
except Exception:  # noqa: BLE001
    _STEALTH_ARGS = (
        "--disable-blink-features=AutomationControlled",
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


def _proxy_kwargs(account: InstagramAccount) -> dict[str, Any] | None:
    if not account.proxy_enabled or not account.proxy_url:
        return None
    proxy: dict[str, Any] = {"server": account.proxy_url}
    if account.proxy_username:
        proxy["username"] = account.proxy_username
    if account.proxy_password:
        proxy["password"] = account.proxy_password
    return proxy


def _normalize_cookies_for_playwright(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".instagram.com"
        path = c.get("path") or "/"
        cookie: dict[str, Any] = {
            "name": str(name), "value": str(value),
            "domain": str(domain), "path": str(path),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
        }
        same_site = c.get("sameSite") or "None"
        if same_site in ("Strict", "Lax", "None"):
            cookie["sameSite"] = same_site
        expires = c.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = float(expires)
        out.append(cookie)
    return out


class InstagramBrowser:
    def __init__(self, account: InstagramAccount, *, headless: bool | None = None, slow_mo_ms: int = 0) -> None:
        self.account = account
        self.headless = (
            headless if headless is not None
            else (os.getenv("FB_MASTER_INSTAGRAM_HEADLESS", "0").strip() in ("1", "true", "yes"))
        )
        self.slow_mo_ms = slow_mo_ms
        self._pw = None
        self._context = None
        self._cookies_restored = False

    def __enter__(self) -> "InstagramBrowser":
        from playwright.sync_api import sync_playwright

        from backend.modules.instagram.runtime.human_behavior import (
            antidetect_enabled, jittered_viewport,
        )

        profile_dir = Path(self.account.profile_dir or "")
        if not str(profile_dir):
            raise RuntimeError("InstagramBrowser: account.profile_dir is empty")
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
            # Включаем эмуляцию touch — для mobile UA это уместно.
            "has_touch": True,
            "is_mobile": True,
            "device_scale_factor": 2.0,
        }
        if proxy:
            launch_kwargs["proxy"] = proxy

        try:
            self._context = self._pw.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
        except Exception:
            self._safe_stop_pw()
            raise

        try:
            self._context.add_init_script(_STEALTH_INIT_JS)
        except Exception:  # noqa: BLE001
            logger.exception("InstagramBrowser: add_init_script failed (non-fatal)")

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
            logger.exception("InstagramBrowser.add_cookies failed for account_id=%s", getattr(self.account, "id", None))

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("InstagramBrowser used outside `with` block")
        return self._context

    def new_page(self):
        return self.context.new_page()

    def is_logged_in(self, page) -> bool:
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        if "/accounts/login" in url or "/accounts/emailsignup" in url:
            return False
        try:
            from backend.modules.instagram.runtime.dom_resilience import LOGGED_IN_SELECTORS

            combined = ", ".join(LOGGED_IN_SELECTORS)
            if page.locator(combined).first.is_visible(timeout=2500):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False


@contextlib.contextmanager
def open_session(account: InstagramAccount, *, headless: bool | None = None) -> Iterator[InstagramBrowser]:
    with InstagramBrowser(account, headless=headless) as s:
        yield s


__all__ = ["InstagramBrowser", "open_session"]
