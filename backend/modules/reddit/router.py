"""Reddit Master — FastAPI router.

M4: все 15 разделов через один router с placeholder-шаблоном. В M5
разделы разбиваются на отдельные файлы (`routers/accounts.py`,
`routers/subreddit_discovery.py`, …) по мере наполнения реальной
логикой.

Роутер подключается в `backend/app_factory.py` только при
`FB_MASTER_REDDIT_MODULE_ENABLED=1`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/reddit")


def _tpl(request: Request):
    return request.app.state.templates


@router.get("")
async def reddit_index(request: Request):
    return _tpl(request).TemplateResponse(
        "reddit/dashboard.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "page_id": "reddit_dashboard",
        },
    )


@router.get("/")
async def reddit_root(request: Request):
    return await reddit_index(request)


# ── placeholder-разделы (M4 skeleton) ───────────────────────

_SECTIONS: dict[str, dict[str, object]] = {
    "accounts": {
        "page_id": "reddit_accounts_list",
        "title": "Reddit-аккаунты",
        "subtitle": "Подключение аккаунтов через официальный OAuth2, контроль статусов, capabilities и cooldown'ов.",
        "icon": "brand-reddit",
        "text": "Здесь появится подключение Reddit-аккаунта и список подключённых. Подготовьте client_id / client_secret вашего Reddit App в разделе «Настройки».",
        "next_steps": [
            "Подключить OAuth2 authorization-code flow",
            "Хранение refresh-токенов + автообновление",
            "Readiness-checks при подключении",
        ],
    },
    "readiness": {
        "page_id": "reddit_readiness",
        "title": "Готовность аккаунта",
        "subtitle": "Возраст, karma, email, chat/DM eligibility — как чеклист. Никаких обещаний обхода лимитов.",
        "icon": "checks",
        "text": "Readiness-probe будет забирать данные через Reddit API и отображать прозрачный чеклист с рекомендациями.",
        "next_steps": [
            "Probe: account age, karma link/comment, verified email",
            "Detection: chat enabled, dm enabled, subreddit bans",
            "Рекомендации безопасного использования",
        ],
    },
    "subreddits": {
        "page_id": "reddit_subreddits",
        "title": "Сабреддиты",
        "subtitle": "Поиск по ключевым словам, темам, похожим сабреддитам, объёму и активности.",
        "icon": "radar-2",
        "text": "Discovery через /subreddits/search + /subreddits/popular + фильтры по размеру/активности/темам. Карточки с suitability и risk score.",
        "next_steps": [
            "Интеграция API поиска",
            "Enrichment: posts_24h, avg_comments_per_post, language_guess",
            "Персональный список отобранных сабреддитов",
        ],
    },
    "audience": {
        "page_id": "reddit_audience",
        "title": "Поиск аудитории",
        "subtitle": "Авторы постов, комментаторы, активные в выбранных сабреддитах — с превью профиля.",
        "icon": "users-group",
        "text": "Поиск по сабреддитам, ключам, тредам. Для каждого профиля — мини-карточка с recent visible activity и capability-check (chat/DM).",
        "next_steps": [
            "Sources: subreddit, post comments, user mentions, topic keyword",
            "Прямой ingest в Leads",
            "Фильтры: karma, account age, language",
        ],
    },
    "activity": {
        "page_id": "reddit_activity",
        "title": "Недавняя активность",
        "subtitle": "Recent visible activity — посты и комментарии за выбранное окно. НЕ online-статус.",
        "icon": "activity",
        "text": "Фильтры времени: 1h / 24h / 3d / 7d / 30d / custom. Сортировка: newest, most active, relevance, engagement. Online-статус Reddit не раскрывает, и мы его не используем.",
        "next_steps": [
            "Scan по спискам пользователей из Leads или ad-hoc",
            "Snapshot'ы в reddit_recent_activity_snapshots",
            "Индикаторы engagement и прошедших cooldown'ов",
        ],
    },
    "leads": {
        "page_id": "reddit_leads",
        "title": "Лиды",
        "subtitle": "Единое хранилище собранных пользователей: источник, теги, статус, связь с CRM и кампаниями.",
        "icon": "address-book",
        "text": "Таблица лидов с bulk-actions, preview-pane, фильтрами по source/status/tags/engagement. Интеграция в общую CRM-воронку.",
        "next_steps": [
            "CRUD + bulk-actions",
            "Фильтры и saved-views",
            "Linking с общей таблицей people",
        ],
    },
    "templates": {
        "page_id": "reddit_templates",
        "title": "Шаблоны и AI",
        "subtitle": "DM, chat-opener, комментарии, follow-up. AI-вариации, tone, banned-phrase detector.",
        "icon": "template",
        "text": "Редактор с плейсхолдерами {username}, {subreddit}, {last_post_title}. AI-генерация вариаций через тот же ai_agent_service, что и FB Master.",
        "next_steps": [
            "Редактор с плейсхолдерами и preview",
            "AI-вариации + anti-duplicate detector",
            "Banned phrase highlight",
        ],
    },
    "campaigns": {
        "page_id": "reddit_campaigns",
        "title": "Кампании",
        "subtitle": "Очереди ЛС и комментариев. Review-before-send, per-account caps, send-window, детекция restricted.",
        "icon": "send",
        "text": "Wizard создания кампании: audience → template → cadence+caps → safety review → approve & schedule. Default approval = manual.",
        "next_steps": [
            "Builder + persistence",
            "Review UI с approve/reject на каждое сообщение",
            "Воркер очереди с обработкой rate-limit'ов",
        ],
    },
    "sequences": {
        "page_id": "reddit_sequences",
        "title": "Сценарии (Agent Mode)",
        "subtitle": "Конструктор цепочек: observe → score → draft → approve → publish → wait → detect reply.",
        "icon": "brain",
        "text": "Node-editor. Все send-шаги помечены обязательным manual review по умолчанию. Hard-stop на небезопасные конфиги.",
        "next_steps": [
            "Node model + editor",
            "Runtime-воркер sequence_worker (по аналогии с FB)",
            "Review-gates на send-действиях",
        ],
    },
    "conversations": {
        "page_id": "reddit_conversations",
        "title": "Диалоги / Inbox",
        "subtitle": "Трёхколоночный inbox: аккаунты → треды → сообщения. Compose с AI-draft и queued-send.",
        "icon": "message-circle",
        "text": "Inbox для PM/chat. Compose → AI draft → manual approve → enqueue. Статусы доставки, follow-up suggestions, CRM-stage change прямо из треда.",
        "next_steps": [
            "Inbox layout и навигация по тредам",
            "Compose с AI-draft и preview",
            "Queued-send с ручным подтверждением",
        ],
    },
    "comments": {
        "page_id": "reddit_comments",
        "title": "Комментарии",
        "subtitle": "Очередь входов в дискуссии. AI-помощник, toxicity risk score, manual review.",
        "icon": "messages",
        "text": "Релевантные треды из discovery/audience, черновики комментариев, toxicity risk, history действий.",
        "next_steps": [
            "Ingest релевантных тредов",
            "Draft + preview + review",
            "Публикация и отслеживание реакций",
        ],
    },
    "analytics": {
        "page_id": "reddit_analytics",
        "title": "Аналитика",
        "subtitle": "Сводка по аккаунтам, очередям, кампаниям, ответам и ошибкам с reason codes.",
        "icon": "chart-line",
        "text": "Dashboard с группами Accounts / Discovery / Leads / Conversations / Comments / Campaigns. Графики и разбор причин отказов.",
        "next_steps": [
            "Сбор метрик из Job/JobEvent и reddit-специфичных таблиц",
            "UI-графики",
            "Экспорт CSV",
        ],
    },
    "compliance": {
        "page_id": "reddit_compliance",
        "title": "Compliance & Limits",
        "subtitle": "Supported / Partial / Restricted / Unsupported + rate-limit counters и предупреждения по аккаунту.",
        "icon": "shield-check",
        "text": "Честное отображение возможностей Reddit API. Live-счётчики bucket'ов rate-limit'ов и hard-stop предупреждения в кампаниях.",
        "next_steps": [
            "Рендер capability-матрицы",
            "Текущее состояние rate-limit по bucket'ам",
            "Список активных compliance-event'ов",
        ],
    },
    "settings": {
        "page_id": "reddit_settings",
        "title": "Настройки Reddit",
        "subtitle": "OAuth client_id / secret, user-agent, approval-mode по умолчанию, timezone для send-window.",
        "icon": "settings",
        "text": "Настройки Reddit-интеграции. Ключи приложения хранятся в таблице Setting с ключами reddit.* (никогда не коммитятся в репо).",
        "next_steps": [
            "Поля OAuth client_id / client_secret / redirect_uri",
            "Reddit user-agent (обязателен по ToS)",
            "Approval mode default, send-window, caps",
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
    return _tpl(request).TemplateResponse("reddit/_placeholder.html", ctx)


__all__ = ["router"]
