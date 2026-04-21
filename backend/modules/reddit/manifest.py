"""Reddit Master — navigation и capability манифесты."""

from __future__ import annotations

from backend.core.modules.manifests import (
    Capability,
    CapabilityLevel,
    CapabilityManifest,
    NavigationGroup,
    NavigationItem,
    NavigationManifest,
)

MODULE_ID = "reddit"


def _nav() -> NavigationManifest:
    groups = (
        NavigationGroup(
            id="reddit_overview",
            label="Обзор",
            items=(
                NavigationItem(id="reddit_dashboard", label="Главная", href="/reddit", icon="layout-dashboard"),
                NavigationItem(id="reddit_compliance", label="Правила и лимиты", href="/reddit/compliance", icon="shield-check"),
            ),
        ),
        NavigationGroup(
            id="reddit_accounts",
            label="Аккаунты",
            items=(
                NavigationItem(id="reddit_accounts_list", label="Reddit-аккаунты", href="/reddit/accounts", icon="brand-reddit"),
                NavigationItem(id="reddit_readiness", label="Готовность", href="/reddit/readiness", icon="checks"),
            ),
        ),
        NavigationGroup(
            id="reddit_research",
            label="Исследование",
            items=(
                NavigationItem(id="reddit_subreddits", label="Сабреддиты", href="/reddit/subreddits", icon="radar-2"),
                NavigationItem(id="reddit_audience", label="Поиск аудитории", href="/reddit/audience", icon="users-group"),
                NavigationItem(id="reddit_activity", label="Недавняя активность", href="/reddit/activity", icon="activity"),
                NavigationItem(id="reddit_leads", label="Лиды", href="/reddit/leads", icon="address-book"),
            ),
        ),
        NavigationGroup(
            id="reddit_comms",
            label="Коммуникация",
            items=(
                NavigationItem(id="reddit_templates", label="Шаблоны и AI", href="/reddit/templates", icon="template"),
                NavigationItem(id="reddit_campaigns", label="Кампании", href="/reddit/campaigns", icon="send"),
                NavigationItem(id="reddit_sequences", label="Сценарии (Agent)", href="/reddit/sequences", icon="brain"),
                NavigationItem(id="reddit_conversations", label="Диалоги / Inbox", href="/reddit/conversations", icon="message-circle"),
                NavigationItem(id="reddit_comments", label="Комментарии", href="/reddit/comments", icon="messages"),
            ),
        ),
        NavigationGroup(
            id="reddit_system",
            label="Система",
            items=(
                NavigationItem(id="reddit_analytics", label="Аналитика", href="/reddit/analytics", icon="chart-line"),
                NavigationItem(id="reddit_settings", label="Настройки", href="/reddit/settings", icon="settings"),
            ),
        ),
    )
    return NavigationManifest(module_id=MODULE_ID, groups=groups, default_route="/reddit")


def _caps() -> CapabilityManifest:
    caps = (
        Capability(id="account_connect", label="Подключение Reddit-аккаунта (OAuth2)", level=CapabilityLevel.SUPPORTED,
                   notes="Через официальный Reddit OAuth: поток authorization-code."),
        Capability(id="subreddit_discovery", label="Поиск сабреддитов", level=CapabilityLevel.SUPPORTED,
                   notes="По ключевым словам и популярным через API."),
        Capability(id="audience_search", label="Поиск аудитории (авторы постов и комментариев)", level=CapabilityLevel.SUPPORTED,
                   notes="Сбор лидов из сабреддитов и тредов с метриками кармы и активности."),
        Capability(id="recent_activity", label="Недавняя видимая активность", level=CapabilityLevel.SUPPORTED,
                   notes="Посты и комментарии пользователя через API или browser-парсер за заданное окно времени."),
        Capability(id="send_dm", label="Личные сообщения (PM) и рассылки", level=CapabilityLevel.SUPPORTED,
                   notes="Полноценный мессенджер. Browser-mode (Chromium + cookies) даёт рассылку без OAuth-ограничений; OAuth-mode остаётся как fallback. Manual-подтверждение каждого отправления — встроенная мера безопасности."),
        Capability(id="publish_comment", label="Публикация комментария", level=CapabilityLevel.SUPPORTED,
                   notes="Browser-mode → клик «Comment» → ввод → отправка. OAuth-mode → /api/comment. В обоих режимах — manual review каждого черновика и проверка токсичности."),
        Capability(id="sequence_automation", label="Сценарии (цепочки действий)", level=CapabilityLevel.SUPPORTED,
                   notes="DM → пауза → комментарий → проверка ответа. Каждый шаг подтверждается оператором."),
        Capability(id="account_connect_browser_profile",
                   label="Подключение через browser-профиль (Chromium + cookies)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Импорт купленных аккаунтов с cookies (reddit_session или token_v2), persistent Chromium-профиль на каждый аккаунт, привязка прокси. Reddit видит сессию как обычный браузерный логин."),
        Capability(id="multi_account",
                   label="Несколько аккаунтов одновременно",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Каждому аккаунту — свой profile_dir и свой proxy. Изоляция сессий и fingerprint'а."),
        Capability(id="proxy_per_account",
                   label="Прокси на аккаунт",
                   level=CapabilityLevel.SUPPORTED,
                   notes="HTTP/HTTPS/SOCKS прокси с user:pass, отдельный на каждый аккаунт."),
        Capability(id="cookie_import",
                   label="Импорт cookies / сессий (txt/JSON)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Парсер форматов: одна строка JSON, массив cookies, EditThisCookie export. Обязательная cookie: reddit_session или token_v2."),
        Capability(id="anti_detect",
                   label="Anti-detect (mouse jitter, idle, randomized viewport)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Human-like timings и движения мыши снижают очевидные бот-паттерны. Не претензия на полное обхождение fingerprinting."),
        Capability(id="bulk_dm_unverified", label="Массовая рассылка непроверенным аккаунтам",
                   level=CapabilityLevel.RESTRICTED,
                   notes="Reddit очень быстро ловит spam-сигналы (DM от свежего аккаунта без karma → shadowban за часы). Default approval = manual; рекомендуется minimum karma 100, 14+ дней возраста."),
        Capability(id="vote_manipulation", label="Накрутка голосов (upvote/downvote)",
                   level=CapabilityLevel.UNSUPPORTED,
                   notes="Запрещено Reddit ToS («vote manipulation»). Не реализовано."),
        Capability(id="scraping_private_subreddits", label="Скрейпинг приватных сабреддитов",
                   level=CapabilityLevel.UNSUPPORTED,
                   notes="Запрещено Reddit ToS и недоступно без membership."),
    )
    return CapabilityManifest(module_id=MODULE_ID, capabilities=caps)


NAVIGATION: NavigationManifest = _nav()
CAPABILITIES: CapabilityManifest = _caps()


__all__ = ["CAPABILITIES", "MODULE_ID", "NAVIGATION"]
