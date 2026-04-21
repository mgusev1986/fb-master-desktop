"""Централизованные feature-флаги multi-workspace shell.

Значения читаются из env так же, как `backend.config.fb_master_*()`.
Все флаги **по умолчанию OFF** — пока лаунчер десктопа не включит их явно,
поведение FB Master не меняется.
"""

from __future__ import annotations

import os


def _bool_env(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def multi_workspace_enabled() -> bool:
    """Мастер-флаг: включать ли launcher и workspace switcher.

    Default=False на сервере (VPS socmaster.pro). Desktop-лаунчер
    (`local-backend-launcher.js`) выставит FB_MASTER_MULTI_WORKSPACE_ENABLED=1
    в момент, когда интеграция будет готова к релизу.
    """
    return _bool_env("FB_MASTER_MULTI_WORKSPACE_ENABLED", default=False)


def reddit_module_enabled() -> bool:
    """Включить Reddit Master module (роуты, sidebar, background workers).

    Default=False до публичного релиза Reddit Master.
    """
    return _bool_env("FB_MASTER_REDDIT_MODULE_ENABLED", default=False)


def reddit_official_api_only() -> bool:
    """Запрет на browser-automation для Reddit.

    Default=False — модуль теперь поддерживает browser-profile mode
    (Chromium + cookies) параллельно с OAuth2/PRAW. Установка в True
    оставляет только официальный API path (OAuth-flow).
    """
    return _bool_env("FB_MASTER_REDDIT_OFFICIAL_API_ONLY", default=False)


def reddit_browser_profile_enabled() -> bool:
    """Browser-profile режим Reddit (Chromium + cookies + Playwright).

    Default=True — основной режим для рассылок без OAuth-ограничений.
    Reddit API остаётся доступным как fallback (OAuth-аккаунты не ломаются).
    """
    return _bool_env("FB_MASTER_REDDIT_BROWSER_PROFILE_ENABLED", default=True)


def reddit_send_approval_mode() -> str:
    """Режим аппрува исходящих действий Reddit: manual | semi | auto.

    Default=manual — каждое отправляемое сообщение/комментарий требует
    явного подтверждения пользователем. `auto` сознательно скрыт из
    стандартного UX и включается отдельным решением организации.
    """
    raw = (os.getenv("FB_MASTER_REDDIT_SEND_APPROVAL") or "").strip().lower()
    if raw in ("manual", "semi", "auto"):
        return raw
    return "manual"


def launcher_is_default_entry() -> bool:
    """После успешной активации ключа показывать launcher, а не сразу FB dashboard.

    Default=False. Включится вместе с `multi_workspace_enabled()`.
    """
    return _bool_env("FB_MASTER_LAUNCHER_DEFAULT", default=False)


# ── LinkedIn Master (Beta) ─────────────────────────────────


def linkedin_module_enabled() -> bool:
    """Включить LinkedIn Master module (роуты, sidebar, capability matrix).

    Default=False до публичного релиза LinkedIn Master.
    Desktop-лаунчер выставляет FB_MASTER_LINKEDIN_MODULE_ENABLED=1
    в момент готовности модуля к beta-выкатке.
    """
    return _bool_env("FB_MASTER_LINKEDIN_MODULE_ENABLED", default=False)


def linkedin_official_api_only() -> bool:
    """Запрет на browser-automation для LinkedIn.

    Default=False — модуль работает по образу Facebook Master через
    browser-profile (Electron BrowserView + cookie import + Playwright).
    Установка в True заставит модуль использовать только official API
    (Sign in with LinkedIn / Marketing API) — но без partner-доступа большая
    часть outreach-сценариев тогда недоступна.
    """
    return _bool_env("FB_MASTER_LINKEDIN_OFFICIAL_API_ONLY", default=False)


def linkedin_browser_profile_enabled() -> bool:
    """Browser-profile режим LinkedIn (как Facebook Master).

    Default=True — основной режим: подключение аккаунта через импорт cookies
    (li_at, JSESSIONID) или через embedded BrowserView, работа через
    persistent Chrome-профиль + Playwright. LinkedIn видит сессию как
    обычный браузерный логин.
    """
    return _bool_env("FB_MASTER_LINKEDIN_BROWSER_PROFILE_ENABLED", default=True)


def linkedin_send_approval_mode() -> str:
    """Режим аппрува исходящих действий LinkedIn: manual | semi | auto.

    Default=manual — каждое отправляемое сообщение / invitation / comment требует
    явного подтверждения пользователем. `auto` сознательно скрыт из стандартного UX
    и включается отдельным решением организации.
    """
    raw = (os.getenv("FB_MASTER_LINKEDIN_SEND_APPROVAL") or "").strip().lower()
    if raw in ("manual", "semi", "auto"):
        return raw
    return "manual"


# ── Twitter / X Master (Beta) ─────────────────────────────


def twitter_module_enabled() -> bool:
    """Включить Twitter/X Master (роуты, sidebar, runtime workers).

    Default=False — desktop-лаунчер выставляет в `1` при готовности beta.
    """
    return _bool_env("FB_MASTER_TWITTER_MODULE_ENABLED", default=False)


def twitter_browser_profile_enabled() -> bool:
    """Browser-profile режим Twitter/X (Chromium + cookies + Playwright).

    Default=True — это основной режим для рассылок без X API rate-limit'ов.
    """
    return _bool_env("FB_MASTER_TWITTER_BROWSER_PROFILE_ENABLED", default=True)


def twitter_official_api_only() -> bool:
    """Принудительный fallback на официальный X API.

    Default=False. Без partner-доступа X API дорогой (Pro $5K/mo), поэтому
    browser-mode основной.
    """
    return _bool_env("FB_MASTER_TWITTER_OFFICIAL_API_ONLY", default=False)


def twitter_send_approval_mode() -> str:
    """Режим аппрува исходящих Twitter: manual | semi | auto.

    Default=manual. `auto` нужен для AI-autoresponder в авто-режиме —
    включается отдельным решением (дополнительный риск shadowban).
    """
    raw = (os.getenv("FB_MASTER_TWITTER_SEND_APPROVAL") or "").strip().lower()
    if raw in ("manual", "semi", "auto"):
        return raw
    return "manual"


def twitter_autoresponder_enabled() -> bool:
    """AI-autoresponder для Twitter/X — нейросеть отвечает в DM до достижения цели.

    Default=False — включается явно и требует настройки персонажа + цели.
    """
    return _bool_env("FB_MASTER_TWITTER_AUTORESPONDER_ENABLED", default=False)


# ── Instagram Master (Beta) ───────────────────────────────


def instagram_module_enabled() -> bool:
    """Включить Instagram Master."""
    return _bool_env("FB_MASTER_INSTAGRAM_MODULE_ENABLED", default=False)


def instagram_browser_profile_enabled() -> bool:
    """Browser-profile режим Instagram (Chromium + cookies + Playwright).

    Default=True — основной режим. Instagram Graph API доступен только
    Business/Creator с partner-доступом, поэтому browser-mode основной.
    """
    return _bool_env("FB_MASTER_INSTAGRAM_BROWSER_PROFILE_ENABLED", default=True)


def instagram_official_api_only() -> bool:
    return _bool_env("FB_MASTER_INSTAGRAM_OFFICIAL_API_ONLY", default=False)


def instagram_send_approval_mode() -> str:
    """Default=manual — Instagram чрезвычайно чувствителен к bot-паттернам."""
    raw = (os.getenv("FB_MASTER_INSTAGRAM_SEND_APPROVAL") or "").strip().lower()
    if raw in ("manual", "semi", "auto"):
        return raw
    return "manual"


def instagram_autoresponder_enabled() -> bool:
    return _bool_env("FB_MASTER_INSTAGRAM_AUTORESPONDER_ENABLED", default=False)


__all__ = [
    "multi_workspace_enabled",
    "reddit_browser_profile_enabled",
    "reddit_module_enabled",
    "reddit_official_api_only",
    "reddit_send_approval_mode",
    "linkedin_browser_profile_enabled",
    "linkedin_module_enabled",
    "linkedin_official_api_only",
    "linkedin_send_approval_mode",
    "twitter_autoresponder_enabled",
    "twitter_browser_profile_enabled",
    "twitter_module_enabled",
    "twitter_official_api_only",
    "twitter_send_approval_mode",
    "instagram_autoresponder_enabled",
    "instagram_browser_profile_enabled",
    "instagram_module_enabled",
    "instagram_official_api_only",
    "instagram_send_approval_mode",
    "launcher_is_default_entry",
]
