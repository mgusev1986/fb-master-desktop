"""Instagram Master — навигация и capability матрица.

Отдельные навигационные пункты под Instagram-специфичные фичи:
  * Audience Research — поиск по hashtag, парсинг подписчиков/лайкавших/комментаторов.
  * Engagement — лайки/сторис-лайки/комменты/follow по целевой аудитории.
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

MODULE_ID = "instagram"


def _nav() -> NavigationManifest:
    groups = (
        NavigationGroup(
            id="ig_overview",
            label="Обзор",
            items=(
                NavigationItem(id="ig_dashboard", label="Главная", href="/instagram", icon="layout-dashboard", badge="β"),
                NavigationItem(id="ig_compliance", label="Правила и лимиты", href="/instagram/compliance", icon="shield-check"),
            ),
        ),
        NavigationGroup(
            id="ig_accounts",
            label="Аккаунты",
            items=(
                NavigationItem(id="ig_accounts_list", label="IG-аккаунты", href="/instagram/accounts", icon="brand-instagram"),
                NavigationItem(id="ig_readiness", label="Готовность", href="/instagram/readiness", icon="checks"),
            ),
        ),
        NavigationGroup(
            id="ig_research",
            label="Исследование аудитории",
            items=(
                NavigationItem(id="ig_audience_research", label="Поиск аудитории", href="/instagram/audience", icon="users-group"),
                NavigationItem(id="ig_competitor_audience", label="Парсер конкурентов", href="/instagram/competitor-audience", icon="target"),
                NavigationItem(id="ig_leads", label="Лиды", href="/instagram/leads", icon="address-book"),
            ),
        ),
        NavigationGroup(
            id="ig_comms",
            label="Коммуникация",
            items=(
                NavigationItem(id="ig_templates", label="Шаблоны и AI", href="/instagram/templates", icon="template"),
                NavigationItem(id="ig_outreach", label="Direct рассылка", href="/instagram/outreach", icon="send"),
                NavigationItem(id="ig_conversations", label="Direct / Inbox", href="/instagram/conversations", icon="message-circle"),
                NavigationItem(id="ig_autoresponder", label="AI-автоответчик", href="/instagram/autoresponder", icon="robot"),
            ),
        ),
        NavigationGroup(
            id="ig_engagement",
            label="Engagement",
            items=(
                NavigationItem(id="ig_engagement", label="Лайки / комменты / follow", href="/instagram/engagement", icon="heart"),
                NavigationItem(id="ig_story_likes", label="Лайки сторисов", href="/instagram/story-likes", icon="circle-dot"),
            ),
        ),
        NavigationGroup(
            id="ig_system",
            label="Система",
            items=(
                NavigationItem(id="ig_analytics", label="Аналитика", href="/instagram/analytics", icon="chart-line"),
                NavigationItem(id="ig_settings", label="Настройки", href="/instagram/settings", icon="settings"),
            ),
        ),
    )
    return NavigationManifest(module_id=MODULE_ID, groups=groups, default_route="/instagram")


def _caps() -> CapabilityManifest:
    caps = (
        # ── SUPPORTED ─────────────────────────────────────
        Capability(id="account_connect_browser_profile", label="Подключение IG-аккаунта (Chromium + cookies)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Импорт купленных аккаунтов с cookies (sessionid, csrftoken, ds_user_id), persistent профиль, прокси."),
        Capability(id="multi_account", label="Несколько аккаунтов одновременно",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Каждому аккаунту — свой profile_dir и свой proxy."),
        Capability(id="proxy_per_account", label="Прокси на аккаунт (HTTP/HTTPS/SOCKS)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Mobile/residential прокси крайне рекомендованы для Instagram."),
        Capability(id="cookie_import", label="Импорт cookies (txt/JSON)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Парсер форматов: JSON-объект, массив cookies, TSV. Обязательны: sessionid, csrftoken."),
        Capability(id="audience_search", label="Поиск аудитории по keyword / hashtag / location",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Browser-парсер /explore/search/keyword + /explore/tags/<tag>. Сохраняет handles в leads с дедупом."),
        Capability(id="competitor_audience_scrape", label="Парсер аудитории конкурентов (likers/commenters)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Открывает пост конкурента, забирает username из likes-modal и из comment authors. Сохраняет в leads с тегом конкурента."),
        Capability(id="dm_outreach", label="Direct рассылка",
                   level=CapabilityLevel.SUPPORTED,
                   notes="DM через /direct/inbox/. Default approval=manual. Жёсткий cap 20-30/день для нового аккаунта."),
        Capability(id="post_comment", label="Комментирование постов целевой аудитории",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Открыть пост → comment-input → send. AI-черновик опционально. Manual review каждого комментария."),
        Capability(id="like_targeted", label="Лайкинг постов целевой аудитории",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Лайк на конкретные посты по списку URL или по hashtag/locality (engagement worker)."),
        Capability(id="story_like", label="Лайки сторисов",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Открыть профиль → клик на сторис-кружок → reaction. Эффективный signal-сигнал."),
        Capability(id="follow_unfollow", label="Follow / unfollow по фильтрам",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Follow по leads с safety-капами (max 50/час, 150/день для нового аккаунта). Unfollow для cleanup."),
        Capability(id="lead_lists", label="Списки лидов с CRM-стадиями",
                   level=CapabilityLevel.SUPPORTED,
                   notes="handle, full_name, bio, источник (hashtag/competitor/keyword), engagement signals, outreach stage."),
        Capability(id="templates_ai", label="Шаблоны DM / Comment + AI",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Variations / rewrite / shorten / soften. Плейсхолдеры {handle}, {full_name}, {bio}, {recent_post}."),
        Capability(id="ai_autoresponder", label="AI-автоответчик: переписка до цели",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Нейросеть ведёт Direct-переписку, удерживая цель (book_call / share_info / qualify_lead). Manual или auto approval."),
        Capability(id="inbox_sync", label="Direct sync (preview)",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Парсер /direct/inbox/ — preview новых тредов с дедупликацией."),
        Capability(id="anti_detect", label="Anti-detect helpers",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Mouse jitter, gentle scroll, randomized viewport, human-like typing. Mobile-UA по умолчанию (IG лучше работает с mobile profile)."),
        Capability(id="compliance_center", label="Compliance center",
                   level=CapabilityLevel.SUPPORTED,
                   notes="Прозрачный список supported / partial / restricted / unsupported + safety caps."),

        # ── PARTIAL ───────────────────────────────────────
        Capability(id="story_view_signal", label="Просмотр сторисов как сигнал",
                   level=CapabilityLevel.PARTIAL,
                   notes="UI кнопка есть, runtime через Playwright (открыть и подождать) — следующий milestone."),
        Capability(id="reels_engagement", label="Reels engagement",
                   level=CapabilityLevel.PARTIAL,
                   notes="Лайки/комменты на Reels работают через тот же comment_sender, но scroll-feed специфичный. Skeleton."),
        Capability(id="analytics", label="Аналитика рассылок и engagement",
                   level=CapabilityLevel.PARTIAL,
                   notes="Counters в queue items есть. UI dashboard — следующий milestone."),

        # ── RESTRICTED ────────────────────────────────────
        Capability(id="dm_to_unfollowed", label="DM непринятым (не подписаны на вас)",
                   level=CapabilityLevel.RESTRICTED,
                   notes="Instagram отправляет такие DM в request-папку. Высокий risk shadow-ban за массовую рассылку. Только manual review."),
        Capability(id="bulk_follows", label="Массовый follow (>150/день)",
                   level=CapabilityLevel.RESTRICTED,
                   notes="Жёсткий cap 50/час, 150/день для нового аккаунта. Превышение блокируется на уровне worker'а."),
        Capability(id="hashtag_spam", label="Spam-комменты по hashtag (одинаковый текст)",
                   level=CapabilityLevel.RESTRICTED,
                   notes="AI-similarity check блокирует одинаковые комменты массово. Требуется variants_json или AI-вариации."),

        # ── UNSUPPORTED ───────────────────────────────────
        Capability(id="vote_manipulation", label="Накрутка лайков на свои посты",
                   level=CapabilityLevel.UNSUPPORTED,
                   notes="Запрещено Instagram ToS. Не реализовано."),
        Capability(id="impersonation", label="Имперсонация чужого аккаунта",
                   level=CapabilityLevel.UNSUPPORTED,
                   notes="Запрещено ToS и блокируется на уровне политики приложения."),
        Capability(id="auto_view_profiles_mass", label="Массовый автопросмотр профилей",
                   level=CapabilityLevel.UNSUPPORTED,
                   notes="Spammy паттерн с очень высоким risk-score."),
        Capability(id="dm_blast_to_unfollowers", label="DM blast по чужим followers без context",
                   level=CapabilityLevel.UNSUPPORTED,
                   notes="Гарантированный shadowban. Используйте targeted outreach с personalized templates."),
    )
    return CapabilityManifest(module_id=MODULE_ID, capabilities=caps)


NAVIGATION: NavigationManifest = _nav()
CAPABILITIES: CapabilityManifest = _caps()


__all__ = ["CAPABILITIES", "MODULE_ID", "NAVIGATION"]
