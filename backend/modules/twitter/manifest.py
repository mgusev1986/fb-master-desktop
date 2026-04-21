"""Twitter / X Master — навигация и capability матрица.

Архитектура — browser-profile (Chromium + cookies + Playwright), как у
LinkedIn / Reddit. Capability-матрица отражает реальное состояние Beta:
runtime для DM/reply/search готов; AI-autoresponder — отдельная фича.
"""

from __future__ import annotations

from backend.core.modules.manifests import (
    Capability,
    CapabilityLevel,
    CapabilityManifest,
    NavigationGroup,
    NavigationItem,
    NavigationManifest,
)

MODULE_ID = "twitter"


def _nav() -> NavigationManifest:
    groups = (
        NavigationGroup(
            id="twitter_overview",
            label="Обзор",
            items=(
                NavigationItem(id="twitter_dashboard", label="Главная", href="/twitter", icon="layout-dashboard", badge="β"),
                NavigationItem(id="twitter_compliance", label="Правила и лимиты", href="/twitter/compliance", icon="shield-check"),
            ),
        ),
        NavigationGroup(
            id="twitter_accounts",
            label="Аккаунты",
            items=(
                NavigationItem(id="twitter_accounts_list", label="X-аккаунты", href="/twitter/accounts", icon="brand-twitter"),
                NavigationItem(id="twitter_readiness", label="Готовность", href="/twitter/readiness", icon="checks"),
            ),
        ),
        NavigationGroup(
            id="twitter_research",
            label="Исследование",
            items=(
                NavigationItem(id="twitter_audience", label="Поиск аудитории", href="/twitter/audience", icon="users-group"),
                NavigationItem(id="twitter_leads", label="Лиды", href="/twitter/leads", icon="address-book"),
            ),
        ),
        NavigationGroup(
            id="twitter_comms",
            label="Коммуникация",
            items=(
                NavigationItem(id="twitter_templates", label="Шаблоны и AI", href="/twitter/templates", icon="template"),
                NavigationItem(id="twitter_outreach", label="Кампании", href="/twitter/outreach", icon="send"),
                NavigationItem(id="twitter_conversations", label="Диалоги / Inbox", href="/twitter/conversations", icon="message-circle"),
                NavigationItem(id="twitter_autoresponder", label="AI-автоответчик", href="/twitter/autoresponder", icon="robot"),
            ),
        ),
        NavigationGroup(
            id="twitter_system",
            label="Система",
            items=(
                NavigationItem(id="twitter_analytics", label="Аналитика", href="/twitter/analytics", icon="chart-line"),
                NavigationItem(id="twitter_settings", label="Настройки", href="/twitter/settings", icon="settings"),
            ),
        ),
    )
    return NavigationManifest(module_id=MODULE_ID, groups=groups, default_route="/twitter")


def _caps() -> CapabilityManifest:
    caps = (
        # ── SUPPORTED ─────────────────────────────────────
        Capability(
            id="account_connect_browser_profile",
            label="Подключение X-аккаунта через browser-профиль",
            level=CapabilityLevel.SUPPORTED,
            notes="Импорт купленных аккаунтов с cookies (auth_token, ct0), persistent Chromium-профиль, привязка прокси. X видит сессию как обычный браузерный логин.",
        ),
        Capability(
            id="multi_account",
            label="Несколько аккаунтов одновременно",
            level=CapabilityLevel.SUPPORTED,
            notes="Каждому аккаунту — свой profile_dir и свой proxy. Изоляция fingerprint'а.",
        ),
        Capability(
            id="proxy_per_account",
            label="Прокси на аккаунт (HTTP/HTTPS/SOCKS)",
            level=CapabilityLevel.SUPPORTED,
            notes="Привязка прокси по аккаунту с user:pass.",
        ),
        Capability(
            id="cookie_import",
            label="Импорт cookies (txt / JSON / EditThisCookie)",
            level=CapabilityLevel.SUPPORTED,
            notes="Парсер форматов: одна строка JSON, массив cookies, TSV. Обязательны: auth_token, ct0.",
        ),
        Capability(
            id="audience_search",
            label="Поиск аудитории через X Search",
            level=CapabilityLevel.SUPPORTED,
            notes="Browser-парсер /search?q=&f=user — собирает usernames в leads с дедупом.",
        ),
        Capability(
            id="lead_lists",
            label="Списки лидов с CRM-стадиями",
            level=CapabilityLevel.SUPPORTED,
            notes="handle, full_name, bio, source query, outreach stage, owner account.",
        ),
        Capability(
            id="templates_ai",
            label="Шаблоны DM / Reply + AI-генерация",
            level=CapabilityLevel.SUPPORTED,
            notes="Variations / rewrite / shorten / soften. Плейсхолдеры {handle}, {full_name}, {bio}, {recent_tweet}, {topic}.",
        ),
        Capability(
            id="dm_outreach",
            label="Рассылка DM по X (browser-mode)",
            level=CapabilityLevel.SUPPORTED,
            notes="Открытие /messages/compose, ввод текста, send. Default approval=manual. Daily cap 30/день для нового аккаунта.",
        ),
        Capability(
            id="reply_outreach",
            label="Реплаи к твитам (engagement outreach)",
            level=CapabilityLevel.SUPPORTED,
            notes="Открытие tweet-permalink, reply, ввод, send. AI-черновик с проверкой токсичности.",
        ),
        Capability(
            id="inbox_sync",
            label="Inbox sync и conversation history",
            level=CapabilityLevel.SUPPORTED,
            notes="Browser-парсер /messages/, upsert в conversations + messages с дедупликацией.",
        ),
        Capability(
            id="ai_autoresponder",
            label="AI-автоответчик: переписка до цели (созвон / инфо / квалификация)",
            level=CapabilityLevel.SUPPORTED,
            notes="Нейросеть ведёт диалог в DM, удерживая цель сессии (book_call / share_info / qualify_lead). Поддерживает manual review каждого ответа или auto-mode.",
        ),
        Capability(
            id="anti_detect",
            label="Anti-detect helpers",
            level=CapabilityLevel.SUPPORTED,
            notes="Mouse jitter, gentle scroll, randomized viewport, human-like typing.",
        ),
        Capability(
            id="compliance_center",
            label="Compliance center",
            level=CapabilityLevel.SUPPORTED,
            notes="Прозрачный список supported / partial / restricted / unsupported действий + safety caps.",
        ),

        # ── PARTIAL ───────────────────────────────────────
        Capability(
            id="follow_unfollow",
            label="Follow / unfollow по фильтрам",
            level=CapabilityLevel.PARTIAL,
            notes="Skeleton кнопки следования есть; runtime через Playwright — следующий milestone.",
        ),
        Capability(
            id="quote_tweets",
            label="Quote-tweet с шаблоном",
            level=CapabilityLevel.PARTIAL,
            notes="Builder есть (kind=quote_tweet в шаблонах); runtime отправки — следующий milestone.",
        ),
        Capability(
            id="analytics",
            label="Аналитика рассылок и диалогов",
            level=CapabilityLevel.PARTIAL,
            notes="Counters в queue items есть. UI dashboard — следующий milestone.",
        ),

        # ── RESTRICTED ────────────────────────────────────
        Capability(
            id="dm_to_unprotected_only",
            label="DM непроверенным (с протекцией DM-only-followers)",
            level=CapabilityLevel.RESTRICTED,
            notes="X ограничивает DM непринятым. Если у получателя стоит «DM only from followers» — отправка вернётся как `dm_disabled`. Модуль это уважает и не пытается обойти.",
        ),
        Capability(
            id="bulk_follows",
            label="Массовый follow (>100/день)",
            level=CapabilityLevel.RESTRICTED,
            notes="Жёсткий cap 50 follow/день для нового аккаунта (X-soft-limit). Default cap зашит, превышение блокируется на уровне worker'а.",
        ),

        # ── UNSUPPORTED ───────────────────────────────────
        Capability(
            id="auto_likes_spam",
            label="Массовые auto-like'и для нотификаций",
            level=CapabilityLevel.UNSUPPORTED,
            notes="Spammy паттерн с высоким shadowban-risk. Не реализовано.",
        ),
        Capability(
            id="vote_manipulation",
            label="Накрутка poll'ов",
            level=CapabilityLevel.UNSUPPORTED,
            notes="Запрещено X ToS.",
        ),
        Capability(
            id="impersonation",
            label="Имперсонация / клонирование чужого аккаунта",
            level=CapabilityLevel.UNSUPPORTED,
            notes="Запрещено ToS и блокируется на уровне политики приложения.",
        ),
        Capability(
            id="auto_view_profiles",
            label="Массовый автопросмотр профилей",
            level=CapabilityLevel.UNSUPPORTED,
            notes="Не делаем — низкая ценность, высокий risk-score.",
        ),
    )
    return CapabilityManifest(module_id=MODULE_ID, capabilities=caps)


NAVIGATION: NavigationManifest = _nav()
CAPABILITIES: CapabilityManifest = _caps()


__all__ = ["CAPABILITIES", "MODULE_ID", "NAVIGATION"]
