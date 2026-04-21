"""Twitter / X Master module (Beta).

В app_factory регистрируется одна из двух реализаций:
  • `twitter_module` (полный BETA) — при FB_MASTER_TWITTER_MODULE_ENABLED=1;
  • `twitter_coming_soon_module` (COMING_SOON stub) — иначе.

Архитектура — порт LinkedIn / Reddit Master: browser-profile (Chromium +
cookies + Playwright). Дополнительно — AI-autoresponder для DM, чтобы
нейросеть могла поддержать диалог до достижения цели (созвон / запрос
информации / квалификация лида).
"""

from backend.modules.twitter.module_stub import (
    TwitterComingSoonModule,
    twitter_coming_soon_module,
)

__all__ = ["TwitterComingSoonModule", "twitter_coming_soon_module"]
