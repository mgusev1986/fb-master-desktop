"""LinkedIn Master — навигационный и capability манифесты.

Структура повторяет Reddit Master (`backend/modules/reddit/manifest.py`) —
обе сетевые надстройки сидят на общих контрактах из
`backend/core/modules/manifests.py`. Никакого хардкода в layout
не требуется: sidebar строится `templates/linkedin/base.html` через
Jinja-global `fbm_workspace_nav("linkedin")`.

Capability-матрица намеренно консервативна: LinkedIn не выдаёт публичных
эндпоинтов для "send DM via API" обычным разработчикам, и большая часть
mass-outreach-сценариев у них либо partner-only, либо запрещена ToS.
Поэтому Beta-модуль работает в режиме **manual assist + review-first**,
а недоступные действия честно помечены `restricted`/`unsupported`.
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

MODULE_ID = "linkedin"


def _nav() -> NavigationManifest:
    groups = (
        NavigationGroup(
            id="linkedin_overview",
            label="Обзор",
            items=(
                NavigationItem(
                    id="linkedin_dashboard",
                    label="Главная",
                    href="/linkedin",
                    icon="layout-dashboard",
                    badge="β",
                ),
                NavigationItem(
                    id="linkedin_compliance",
                    label="Правила и лимиты",
                    href="/linkedin/compliance",
                    icon="shield-check",
                ),
            ),
        ),
        NavigationGroup(
            id="linkedin_accounts",
            label="Аккаунты",
            items=(
                NavigationItem(
                    id="linkedin_accounts_list",
                    label="LinkedIn-аккаунты",
                    href="/linkedin/accounts",
                    icon="brand-linkedin",
                ),
                NavigationItem(
                    id="linkedin_readiness",
                    label="Готовность и безопасность",
                    href="/linkedin/readiness",
                    icon="checks",
                ),
            ),
        ),
        NavigationGroup(
            id="linkedin_research",
            label="Исследование",
            items=(
                NavigationItem(
                    id="linkedin_audience",
                    label="Поиск аудитории",
                    href="/linkedin/audience",
                    icon="users-group",
                ),
                NavigationItem(
                    id="linkedin_lead_search",
                    label="Поиск лидов",
                    href="/linkedin/lead-search",
                    icon="search",
                ),
                NavigationItem(
                    id="linkedin_companies",
                    label="Компании и ICP",
                    href="/linkedin/companies",
                    icon="building",
                ),
                NavigationItem(
                    id="linkedin_activity",
                    label="Сигналы активности",
                    href="/linkedin/activity",
                    icon="activity",
                ),
                NavigationItem(
                    id="linkedin_leads",
                    label="Списки лидов",
                    href="/linkedin/leads",
                    icon="address-book",
                ),
            ),
        ),
        NavigationGroup(
            id="linkedin_comms",
            label="Коммуникация",
            items=(
                NavigationItem(
                    id="linkedin_templates",
                    label="Шаблоны и AI",
                    href="/linkedin/templates",
                    icon="template",
                ),
                NavigationItem(
                    id="linkedin_outreach",
                    label="Outreach Builder",
                    href="/linkedin/outreach",
                    icon="send",
                ),
                NavigationItem(
                    id="linkedin_sequences",
                    label="Сценарии (Agent)",
                    href="/linkedin/sequences",
                    icon="brain",
                ),
                NavigationItem(
                    id="linkedin_conversations",
                    label="Диалоги / Inbox",
                    href="/linkedin/conversations",
                    icon="message-circle",
                ),
            ),
        ),
        NavigationGroup(
            id="linkedin_system",
            label="Система",
            items=(
                NavigationItem(
                    id="linkedin_analytics",
                    label="Аналитика",
                    href="/linkedin/analytics",
                    icon="chart-line",
                ),
                NavigationItem(
                    id="linkedin_settings",
                    label="Настройки",
                    href="/linkedin/settings",
                    icon="settings",
                ),
            ),
        ),
    )
    return NavigationManifest(module_id=MODULE_ID, groups=groups, default_route="/linkedin")


def _caps() -> CapabilityManifest:
    """Capability-матрица для LinkedIn в browser-profile режиме (как FB Master).

    Подключение = импорт cookies + persistent Chrome-профиль + опциональный
    proxy. Действия = Playwright-runtime поверх того же профиля. LinkedIn
    видит сессию как обычный браузерный логин.

    Все автоматизируемые операции честно помечены: что уже есть в этом
    milestone (skeleton models / UI), что — partial (UI без runtime), и что
    запрещено на уровне политики приложения (ToS / risk).
    """
    caps = (
        # ── SUPPORTED ─────────────────────────────────────────
        Capability(
            id="account_connect_browser_profile",
            label="Подключение аккаунта через browser-профиль",
            level=CapabilityLevel.SUPPORTED,
            notes="Импорт купленных аккаунтов через txt/JSON с cookies (li_at, JSESSIONID), persistent Chrome-профиль, привязка прокси. Аналог Facebook Master.",
        ),
        Capability(
            id="multi_account",
            label="Несколько аккаунтов одновременно",
            level=CapabilityLevel.SUPPORTED,
            notes="Каждому аккаунту — свой profile_dir и свой proxy. Изоляция сессий и fingerprint'а.",
        ),
        Capability(
            id="proxy_per_account",
            label="Прокси на аккаунт (HTTP/HTTPS/SOCKS)",
            level=CapabilityLevel.SUPPORTED,
            notes="Привязка прокси по аккаунту, аренда с датой окончания, health-check, статус «прокси недоступен».",
        ),
        Capability(
            id="cookie_import",
            label="Импорт cookies / сессий (txt, JSON, EditThisCookie)",
            level=CapabilityLevel.SUPPORTED,
            notes="Парсер форматов: одна строка JSON на аккаунт, массив cookies, EditThisCookie export. Обязательные cookies: li_at, JSESSIONID.",
        ),
        Capability(
            id="lead_lists",
            label="Списки лидов с CRM-стадиями",
            level=CapabilityLevel.SUPPORTED,
            notes="Лиды с источниками, тегами, стадиями outreach (queued / sent / replied / converted), заметками, owner-аккаунтом.",
        ),
        Capability(
            id="templates_ai",
            label="Шаблоны invitation / DM / follow-up + AI",
            level=CapabilityLevel.SUPPORTED,
            notes="Редактор шаблонов с плейсхолдерами {first_name} / {company} / {recent_post_title}, AI-вариации через тот же llm-сервис, что у FB Master.",
        ),
        Capability(
            id="compliance_center",
            label="Compliance & лимиты (центр прозрачности)",
            level=CapabilityLevel.SUPPORTED,
            notes="Прозрачный список supported / partial / restricted / unsupported действий, safety-капы, audit-events.",
        ),

        # ── PARTIAL ───────────────────────────────────────────
        Capability(
            id="dm_outreach_first_degree",
            label="Рассылка DM по 1st-degree connections",
            level=CapabilityLevel.PARTIAL,
            notes="Builder + queue + persistence готовы (этот milestone). Реальная отправка через Playwright-runtime — следующий milestone.",
        ),
        Capability(
            id="invitation_outreach",
            label="Connection invitations с personalized note",
            level=CapabilityLevel.PARTIAL,
            notes="Builder и queue persistence готовы. Runtime отправки через Playwright + LinkedIn /people/connect — следующий milestone. Default daily cap 15-20/day на аккаунт, недельный — 80.",
        ),
        Capability(
            id="audience_finder",
            label="Поиск аудитории / парсинг контактов",
            level=CapabilityLevel.PARTIAL,
            notes="UI фильтров + lead persistence готовы. Реальный scraper LinkedIn Search через Playwright — следующий milestone.",
        ),
        Capability(
            id="sequence_automation",
            label="Сценарии (Agent Mode): invite → wait → DM → branch",
            level=CapabilityLevel.PARTIAL,
            notes="Models + state-machine готовы как у Reddit. Runtime worker (по аналогии с FB sequence_worker) — следующий milestone.",
        ),
        Capability(
            id="conversations_inbox",
            label="Inbox / диалоги",
            level=CapabilityLevel.PARTIAL,
            notes="Conversation persistence готова. Inbox-sync через Playwright + LinkedIn /messaging — следующий milestone. Manual-assist mode уже работает.",
        ),
        Capability(
            id="readiness_probe",
            label="Готовность и health-check аккаунта",
            level=CapabilityLevel.PARTIAL,
            notes="status / session_ok / login_blocked_reason поля есть. Probe через Playwright (открыть feed, проверить навигацию) — следующий milestone.",
        ),
        Capability(
            id="activity_signals",
            label="Сигналы видимой активности (posts / engagement)",
            level=CapabilityLevel.PARTIAL,
            notes="Schema готова. Сборщик через Playwright по lead profiles — следующий milestone. Online-статус не используется (LinkedIn не показывает у себя).",
        ),
        Capability(
            id="analytics",
            label="Аналитика по кампаниям и outreach",
            level=CapabilityLevel.PARTIAL,
            notes="Counters в queue items есть. UI dashboards — следующий milestone.",
        ),

        # ── RESTRICTED ────────────────────────────────────────
        Capability(
            id="dm_to_unconnected",
            label="DM непринятым (НЕ 1st degree)",
            level=CapabilityLevel.RESTRICTED,
            notes="Только через InMail (платно) или после accept invitation. Модуль не делает массовой рассылки чужим — это самый быстрый путь к recovery-mode.",
        ),
        Capability(
            id="post_comment",
            label="Публикация комментариев к чужим постам",
            level=CapabilityLevel.RESTRICTED,
            notes="Comment-assisted flow: AI-черновик → ручной approve → отправка через embedded BrowserView. Без ручного подтверждения каждого комментария — не делаем.",
        ),

        # ── UNSUPPORTED ───────────────────────────────────────
        Capability(
            id="bulk_inmail_spam",
            label="Массовый InMail-спам / автогенерация низкокачественных шаблонов",
            level=CapabilityLevel.UNSUPPORTED,
            notes="Не делаем. AI-вариации проходят similarity / banned-phrase check; одинаковые шаблоны не отправляются массово.",
        ),
        Capability(
            id="online_status",
            label="Онлайн-статус пользователей",
            level=CapabilityLevel.UNSUPPORTED,
            notes="LinkedIn не раскрывает online-индикатор у себя. Используем только recent visible activity.",
        ),
        Capability(
            id="auto_endorse_skills",
            label="Автоматическое подтверждение навыков (endorsements)",
            level=CapabilityLevel.UNSUPPORTED,
            notes="Не реализовано: low-signal action, портит профиль и нарушает дух платформы.",
        ),
        Capability(
            id="auto_view_profiles",
            label="Массовый автопросмотр профилей для «нотификации»",
            level=CapabilityLevel.UNSUPPORTED,
            notes="Не делаем. Это spammy паттерн с высоким risk-score. Естественные просмотры — только в рамках реальных действий.",
        ),
    )
    return CapabilityManifest(module_id=MODULE_ID, capabilities=caps)


NAVIGATION: NavigationManifest = _nav()
CAPABILITIES: CapabilityManifest = _caps()


__all__ = ["CAPABILITIES", "MODULE_ID", "NAVIGATION"]
