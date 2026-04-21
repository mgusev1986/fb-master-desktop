"""LinkedIn Playwright session — persistent profile + cookie restore + proxy + UA.

Использование:

    from backend.modules.linkedin.runtime.playwright_session import LinkedInBrowser

    with LinkedInBrowser(account, headless=False) as session:
        page = session.new_page()
        page.goto("https://www.linkedin.com/feed/")
        if session.is_logged_in(page):
            ...

Контекст-менеджер сам:
  * создаёт `profile_dir`, если его нет;
  * восстанавливает cookies из `account.cookies_json` ДО первой навигации
    (на любой URL `*.linkedin.com`);
  * закрывает context и playwright instance при выходе.

Не использует FB-stealth (там много FB-специфичного init script). Для
LinkedIn включаем только базовые stealth-меры: убираем navigator.webdriver,
синхронизируем locale/timezone, валидный десктопный UA, корректный
viewport и набор Chromium-флагов из FB stealth-engine (общий список без
FB-специфичных preferences).
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from backend.modules.linkedin.models import LinkedInAccount

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

# Базовые stealth-флаги (берём из FB-движка если доступен; иначе — минимальный набор).
try:  # pragma: no cover - import guard
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

# Init-script: убираем navigator.webdriver и chrome.runtime fingerprint.
_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


@dataclass
class LinkedInSessionResult:
    ok: bool
    reason_code: str | None = None
    details: str | None = None


def _proxy_kwargs(account: LinkedInAccount) -> dict[str, Any] | None:
    if not account.proxy_enabled or not account.proxy_url:
        return None
    proxy: dict[str, Any] = {"server": account.proxy_url}
    if account.proxy_username:
        proxy["username"] = account.proxy_username
    if account.proxy_password:
        proxy["password"] = account.proxy_password
    return proxy


def _normalize_cookies_for_playwright(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Подготовка cookies к context.add_cookies (Playwright требует строгие поля)."""
    out: list[dict[str, Any]] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".linkedin.com"
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
        out.append(cookie)
    return out


class LinkedInBrowser:
    """Контекст-менеджер persistent Chrome-профиля для LinkedIn-аккаунта."""

    def __init__(
        self,
        account: LinkedInAccount,
        *,
        headless: bool | None = None,
        slow_mo_ms: int = 0,
    ) -> None:
        self.account = account
        self.headless = (
            headless
            if headless is not None
            else (os.getenv("FB_MASTER_LINKEDIN_HEADLESS", "0").strip() in ("1", "true", "yes"))
        )
        self.slow_mo_ms = slow_mo_ms
        self._pw = None
        self._context = None  # playwright BrowserContext
        self._cookies_restored = False

    # ── lifecycle ─────────────────────────────────────────────

    def __enter__(self) -> "LinkedInBrowser":
        from playwright.sync_api import sync_playwright

        from backend.modules.linkedin.runtime.human_behavior import (
            antidetect_enabled,
            jittered_viewport,
        )

        profile_dir = Path(self.account.profile_dir)
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
                str(profile_dir), **launch_kwargs
            )
        except Exception:
            self._safe_stop_pw()
            raise

        try:
            self._context.add_init_script(_STEALTH_INIT_JS)
        except Exception:  # noqa: BLE001
            logger.exception("LinkedInBrowser: add_init_script failed (non-fatal)")

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

    # ── cookies ───────────────────────────────────────────────

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
            logger.exception(
                "LinkedInBrowser.add_cookies failed for account_id=%s",
                getattr(self.account, "id", None),
            )

    # ── helpers ───────────────────────────────────────────────

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("LinkedInBrowser used outside `with` block")
        return self._context

    def new_page(self):
        return self.context.new_page()

    def is_logged_in(self, page) -> bool:
        """Признак залогиненности: на /feed/ нас не редиректит на /login.

        Метод НЕ открывает страницу сам — вызывайте после goto. Использует
        как итоговый URL страницы, так и наличие фид-навигации.
        """
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        if "/login" in url or "/uas/login" in url or "/checkpoint/" in url:
            return False
        # Дополнительный контроль: наличие глобальной навигации фида.
        try:
            return bool(
                page.locator(
                    "header, nav.global-nav, [data-test-global-nav], a[href*='/feed/']"
                ).first.is_visible(timeout=2000)
            )
        except Exception:  # noqa: BLE001
            return False


# ── удобный one-shot helper для коротких операций ────────────


@contextlib.contextmanager
def open_session(account: LinkedInAccount, *, headless: bool | None = None) -> Iterator[LinkedInBrowser]:
    with LinkedInBrowser(account, headless=headless) as s:
        yield s


__all__ = [
    "LinkedInBrowser",
    "LinkedInSessionResult",
    "open_session",
]
