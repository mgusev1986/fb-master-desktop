"""Instagram Master module (Beta).

Архитектура — порт Twitter/LinkedIn Master: browser-profile (Chromium +
cookies + Playwright). Instagram-специфичные фичи:
  * Лайкинг по целевой аудитории (по hashtag/locality).
  * Лайки сторисов.
  * Сбор аудитории по лайкам/комментам конкурентов.
  * Follow/unfollow по фильтрам.
  * AI-autoresponder с persona+goal (book_call / share_info / qualify_lead).
"""

from backend.modules.instagram.module_stub import (
    InstagramComingSoonModule,
    instagram_coming_soon_module,
)

__all__ = ["InstagramComingSoonModule", "instagram_coming_soon_module"]
