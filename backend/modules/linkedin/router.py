"""LinkedIn Master — основной FastAPI router с placeholder-секциями.

Beta: все 15 разделов рендерят `linkedin/_placeholder.html` со списком
шагов разработки. Compliance Center — единственный раздел с реальной
capability-матрицей (рендерится отдельно через `linkedin_compliance_router`).

Подключается в `backend/app_factory.py` ТОЛЬКО при
`FB_MASTER_LINKEDIN_MODULE_ENABLED=1`. При выключенном флаге работает
только COMING_SOON-карточка в launcher.

Дизайн скелета — клон `backend/modules/reddit/router.py` (M4-stage),
чтобы пользователю с первого взгляда был понятен план развития модуля.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/linkedin")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("")
async def linkedin_index(request: Request):
    """Dashboard LinkedIn Master — обзорная страница с шагами onboarding."""
    return _tpl(request).TemplateResponse(
        "linkedin/dashboard.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "linkedin_dashboard",
        },
    )


@router.get("/")
async def linkedin_root(request: Request):
    return await linkedin_index(request)


# ── placeholder-разделы (Beta-skeleton) ─────────────────────

_SECTIONS: dict[str, dict[str, object]] = {
    "accounts": {
        "page_id": "linkedin_accounts_list",
        "title": "LinkedIn-аккаунты",
        "subtitle": "Подключение через официальный OAuth 2.0, контроль статусов, capabilities и cooldown'ов.",
        "icon": "brand-linkedin",
        "text": (
            "Здесь появится подключение LinkedIn-аккаунта (Sign in with LinkedIn) и список "
            "подключённых аккаунтов. Подготовьте client_id / client_secret вашего LinkedIn App "
            "в разделе «Настройки»."
        ),
        "next_steps": [
            "OAuth 2.0 authorization-code flow + refresh-токены",
            "Хранение профиля, scope-grant'ов, member URN",
            "Readiness-check сразу после подключения",
            "Health/lock-state мониторинг по аккаунту",
        ],
    },
    "readiness": {
        "page_id": "linkedin_readiness",
        "title": "Готовность и безопасность",
        "subtitle": "Заполненность профиля, баланс активности, headroom по invitations и messages. Никаких обещаний обхода лимитов.",
        "icon": "checks",
        "text": (
            "Readiness-probe соберёт прозрачный чеклист: profile completeness, photo, headline, "
            "experience, recent posts. Плюс safe daily recommendations и risk banner."
        ),
        "next_steps": [
            "Probe: profile fields, photo, recent activity",
            "Detection: weekly invite headroom, message-rate, restriction warnings",
            "Safe daily recommendation engine (без обхода лимитов)",
            "Account notes & last safety review log",
        ],
    },
    "audience": {
        "page_id": "linkedin_audience",
        "title": "Поиск аудитории",
        "subtitle": "Job title, company, geography, industry, seniority, company size — с saved-сегментами и preview.",
        "icon": "users-group",
        "text": (
            "Wizard фильтров: keywords, job title, function, seniority, geography, company filter. "
            "Preview pane с первыми результатами, bulk select → add to lead list, save segment."
        ),
        "next_steps": [
            "Фильтры аудитории (10+ полей) + persona tags",
            "Preview pane с deduplication",
            "Bulk select → add to lead list",
            "Saved segments + restore",
        ],
    },
    "lead-search": {
        "page_id": "linkedin_lead_search",
        "title": "Поиск лидов",
        "subtitle": "По людям / компаниям / ICP / ключевым словам / источникам / стратегиям.",
        "icon": "search",
        "text": (
            "Универсальный поисковый интерфейс над уже собранной базой и партнёрскими источниками. "
            "Сохранение стратегий и быстрый re-run."
        ),
        "next_steps": [
            "Поиск по людям с фильтрами",
            "Поиск по компаниям (target accounts)",
            "Поиск по ICP / persona / activity signals",
            "Saved strategies",
        ],
    },
    "companies": {
        "page_id": "linkedin_companies",
        "title": "Компании и ICP",
        "subtitle": "Target accounts, buyer personas, decision-maker map, скоринг приоритетов.",
        "icon": "building",
        "text": (
            "Хранилище target companies с расширенным контекстом: industry, size, growth-signals. "
            "Карта decision-maker'ов и карточка ICP."
        ),
        "next_steps": [
            "CRUD target companies + segments",
            "Buyer persona builder",
            "Decision-maker map по компании",
            "Priority scoring + notes",
        ],
    },
    "activity": {
        "page_id": "linkedin_activity",
        "title": "Сигналы активности",
        "subtitle": "Recent posting / commenting / engagement. Online-статус НЕ используется.",
        "icon": "activity",
        "text": (
            "Сигналы видимой активности через Posts API и публичные данные. Окна: 24h / 3d / 7d / 30d / custom. "
            "Сигналы: posted recently, commented, company active, topic active, profile updated."
        ),
        "next_steps": [
            "Сбор signals по спискам leads",
            "Фильтры окон и типов сигналов",
            "Привязка signals к outreach / sequences",
        ],
    },
    "leads": {
        "page_id": "linkedin_leads",
        "title": "Списки лидов",
        "subtitle": "Single source of truth: источник, теги, стадии outreach, AI-summary, связь с CRM и кампаниями.",
        "icon": "address-book",
        "text": (
            "Таблица лидов с bulk-actions, preview-pane, фильтрами по source / stage / tags / signals. "
            "Lead model: full name, headline, company, industry, location, seniority, source query, "
            "AI summary, eligibility, outreach stage, owner account."
        ),
        "next_steps": [
            "CRUD + bulk actions + saved views",
            "Preview pane с recent activity",
            "Linking с общей таблицей people",
            "Compliance-flags на уровне lead",
        ],
    },
    "templates": {
        "page_id": "linkedin_templates",
        "title": "Шаблоны и AI",
        "subtitle": "Invitation, first-touch, follow-up, no-reply, warm re-engagement, comment, persona-specific. AI-вариации.",
        "icon": "template",
        "text": (
            "Редактор с плейсхолдерами {first_name}, {company}, {recent_post_title}, {industry}. "
            "AI-генерация / rewrite / shorten / make more natural / persona-personalize. "
            "Detect duplication, similarity score, banned-phrase highlight."
        ),
        "next_steps": [
            "Редактор шаблонов с плейсхолдерами",
            "AI-вариации + similarity detector",
            "Persona-specific и role-specific шаблоны",
            "Compare variants UI",
        ],
    },
    "outreach": {
        "page_id": "linkedin_outreach",
        "title": "Outreach Builder",
        "subtitle": "Invite / post-accept follow-up / DM / semi-auto assist / comment-engagement campaigns. Default = safe review-based.",
        "icon": "send",
        "text": (
            "Wizard: audience → account assignment → templates → safety limits → schedule → approval mode. "
            "Default approval = manual. Hard-stop на rules: weekly invite cap, restricted accounts."
        ),
        "next_steps": [
            "Campaign wizard + persistence",
            "Daily caps & schedule windows",
            "Review queue с approve/reject на каждое сообщение",
            "Stop rules + deduplication + ownership",
        ],
    },
    "sequences": {
        "page_id": "linkedin_sequences",
        "title": "Сценарии (Agent Mode)",
        "subtitle": "Конструктор цепочек: collect → enrich → score → invite → wait → follow-up → branch on reply.",
        "icon": "brain",
        "text": (
            "Node-editor: collect leads → enrich → score → generate invite draft → manual approve → "
            "queue invite → wait → branch on accepted → generate follow-up → manual approve → ..."
        ),
        "next_steps": [
            "Node model + visual editor",
            "Runtime worker с review-gates",
            "Stop on risk / cap / restricted capability",
            "Run history + audit trail",
        ],
    },
    "conversations": {
        "page_id": "linkedin_conversations",
        "title": "Диалоги / Inbox",
        "subtitle": "Conversation workspace: thread list, detail, AI reply assistant, manual handoff.",
        "icon": "message-circle",
        "text": (
            "Inbox-style layout: thread list ↔ conversation detail ↔ draft area + AI reply assistant. "
            "Stage labels, response state, notes, reminders, follow-up suggestions."
        ),
        "next_steps": [
            "Inbox layout (списки тредов, открытие диалога)",
            "Draft area + AI assistant с manual approve",
            "Stage labels & reminders",
            "Manual-handoff flow для restricted-операций",
        ],
    },
    "analytics": {
        "page_id": "linkedin_analytics",
        "title": "Аналитика",
        "subtitle": "Accounts overview, lead acquisition, segment performance, invite/message/reply stats, queue health.",
        "icon": "chart-line",
        "text": (
            "Dashboards по accounts, segments, campaigns, sequences, templates. Failure reasons, "
            "risk signals, timeline events, queue health."
        ),
        "next_steps": [
            "Сбор метрик из outreach / sequence таблиц",
            "Графики по группам Accounts / Lead acquisition / Outreach / Replies",
            "Failure-reason breakdown",
            "Экспорт CSV",
        ],
    },
    "settings": {
        "page_id": "linkedin_settings",
        "title": "Настройки LinkedIn",
        "subtitle": "OAuth client_id / secret, redirect_uri, approval-mode, daily/weekly caps, timezone.",
        "icon": "settings",
        "text": (
            "Настройки LinkedIn-интеграции. Ключи приложения хранятся в таблице Setting "
            "с ключами linkedin.* (никогда не коммитятся в репо)."
        ),
        "next_steps": [
            "Поля OAuth client_id / client_secret / redirect_uri",
            "Approval mode default + safety caps",
            "Schedule windows / timezone",
            "Compliance-настройки (official_api_only)",
        ],
    },
}


def _render_placeholder(request: Request, section_key: str):
    sec = _SECTIONS[section_key]
    ctx = {
        "request": request,
        "user": request.session.get("user"),
        "page_id": sec["page_id"],
        "page_title": sec["title"],
        "page_subtitle": sec["subtitle"],
        "page_icon": sec["icon"],
        "placeholder_text": sec["text"],
        "placeholder_next_steps": sec["next_steps"],
    }
    return _tpl(request).TemplateResponse("linkedin/_placeholder.html", ctx)


# Реальные роутеры (accounts/leads/templates/outreach/compliance) подключаются в
# app_factory ДО этого placeholder-роутера. Здесь остаются placeholder-handler'ы
# только для разделов, у которых пока нет полноценного UI.

@router.get("/readiness")
async def linkedin_readiness(request: Request):
    return _render_placeholder(request, "readiness")


@router.get("/audience")
async def linkedin_audience(request: Request):
    return _render_placeholder(request, "audience")


@router.get("/lead-search")
async def linkedin_lead_search(request: Request):
    return _render_placeholder(request, "lead-search")


@router.get("/companies")
async def linkedin_companies(request: Request):
    return _render_placeholder(request, "companies")


@router.get("/activity")
async def linkedin_activity(request: Request):
    return _render_placeholder(request, "activity")


@router.get("/analytics")
async def linkedin_analytics(request: Request):
    return _render_placeholder(request, "analytics")


@router.get("/settings")
async def linkedin_settings(request: Request):
    return _render_placeholder(request, "settings")


__all__ = ["router"]
