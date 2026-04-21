"""LinkedIn Master — SQLAlchemy-модели.

Все таблицы с префиксом `linkedin_`. Архитектура — порт Facebook Master:
аккаунты подключаются через browser-профиль (Chrome persistent session +
импорт cookies), кампании рассылки строятся на queue items с review-режимом.

Таблицы создаются в общем `Base.metadata` → `init_db()` импортирует этот
модуль до `Base.metadata.create_all`. При выключенном
`FB_MASTER_LINKEDIN_MODULE_ENABLED` таблицы всё равно создаются, но
остаются пустыми (никаких runtime-побочек).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Аккаунты ────────────────────────────────────────────────


class LinkedInAccount(Base):
    """Подключённый LinkedIn-аккаунт (через browser-профиль).

    Аналог `FBAccount` из основного модуля, упрощённый под LinkedIn-специфику.
    Cookie-based сессия хранится в `cookies_json` (массив объектов
    {name,value,domain,path,expires,httpOnly,secure,sameSite}) И на диске
    в `profile_dir` для Playwright/Electron.
    """

    __tablename__ = "linkedin_accounts"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    label = Column(String(255), nullable=False)
    public_identifier = Column(String(120), nullable=True, index=True)  # u/<vanityName>
    full_name = Column(String(255), nullable=True)
    headline = Column(String(512), nullable=True)
    profile_url = Column(String(512), nullable=True)

    # Где Playwright/Electron держит persistent Chrome-профиль.
    profile_dir = Column(String(512), nullable=False)
    # Сохранённая Playwright storage_state: cookies + origins (Fernet не используем —
    # пользователь сам владеет файлом БД, как у FB).
    cookies_json = Column(JSON, nullable=True)
    cookies_imported_at = Column(DateTime, nullable=True)

    # Прокси (как у FBAccount).
    proxy_enabled = Column(Boolean, default=False, nullable=False)
    proxy_url = Column(String(512), nullable=True)
    proxy_username = Column(String(255), nullable=True)
    proxy_password = Column(String(255), nullable=True)
    proxy_lease_ends_at = Column(DateTime, nullable=True)
    proxy_last_check_at = Column(DateTime, nullable=True)
    proxy_last_error = Column(String(512), nullable=True)

    # Антидетект — UA / locale / timezone / viewport.
    stealth_user_agent = Column(String(512), nullable=True)
    stealth_locale = Column(String(32), nullable=True)
    stealth_timezone_id = Column(String(64), nullable=True)
    stealth_viewport_w = Column(Integer, nullable=True)
    stealth_viewport_h = Column(Integer, nullable=True)

    # Состояние сессии.
    status = Column(String(20), nullable=False, default="needs_attention", index=True)
    # connected | needs_attention | restricted | cooldown | disabled
    session_ok = Column(Boolean, nullable=True)
    last_login_check_at = Column(DateTime, nullable=True)
    login_blocked_at = Column(DateTime, nullable=True)
    login_blocked_reason = Column(String(512), nullable=True)

    # «Купленный» аккаунт — login + password (опц., только для ручного re-login через embedded browser).
    li_login_email = Column(String(255), nullable=True)
    li_login_password = Column(Text, nullable=True)  # хранится plaintext только если сам пользователь вставил; рекомендуем cookies

    # Лимиты по аккаунту (override per-account).
    cap_invitations_per_day = Column(Integer, nullable=True)
    cap_invitations_per_week = Column(Integer, nullable=True)
    cap_messages_per_day = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=_utcnow, nullable=False)


class LinkedInAccountDayUsage(Base):
    """Дневные счётчики действий по аккаунту (UTC). Сброс при смене даты."""

    __tablename__ = "linkedin_account_day_usage"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("linkedin_accounts.id"), nullable=False, index=True)
    usage_date = Column(String(10), nullable=False)  # YYYY-MM-DD UTC
    invitations_sent = Column(Integer, default=0, nullable=False)
    messages_sent = Column(Integer, default=0, nullable=False)
    comments_posted = Column(Integer, default=0, nullable=False)
    profiles_visited = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "usage_date", name="uq_li_account_day"),
    )


# ── Лиды ────────────────────────────────────────────────────


class LinkedInLead(Base):
    """Найденный/импортированный LinkedIn-профиль для outreach."""

    __tablename__ = "linkedin_leads"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    public_identifier = Column(String(120), nullable=True, index=True)
    full_name = Column(String(255), nullable=True)
    first_name = Column(String(120), nullable=True)
    last_name = Column(String(120), nullable=True)
    headline = Column(String(512), nullable=True)
    company = Column(String(255), nullable=True)
    job_title = Column(String(255), nullable=True)
    industry = Column(String(120), nullable=True)
    location = Column(String(255), nullable=True)
    seniority = Column(String(64), nullable=True)
    profile_url = Column(String(512), nullable=False)
    avatar_url = Column(String(512), nullable=True)

    # Источник: search query, audience filter, manual import, csv.
    source_kind = Column(String(40), nullable=False, default="manual")
    source_ref = Column(String(512), nullable=True)
    source_query = Column(JSON, nullable=True)

    tags = Column(JSON, nullable=True)  # ["icp", "warm", ...]
    notes = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)

    # Стадия в outreach: new / queued / invite_sent / invite_accepted / dm_sent / replied / converted / dropped.
    outreach_stage = Column(String(32), nullable=False, default="new", index=True)
    last_outreach_at = Column(DateTime, nullable=True)
    last_signal_at = Column(DateTime, nullable=True)

    owner_account_id = Column(Integer, ForeignKey("linkedin_accounts.id"), nullable=True, index=True)
    eligibility_flags = Column(JSON, nullable=True)  # {"first_degree": true, "open_profile": false, ...}

    created_at = Column(DateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_li_leads_org_stage", "organization_id", "outreach_stage"),
    )


# ── Шаблоны ─────────────────────────────────────────────────


class LinkedInTemplate(Base):
    """Шаблон invitation / DM / follow-up."""

    __tablename__ = "linkedin_templates"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    kind = Column(String(32), nullable=False, default="dm")
    # invitation | dm_first_touch | dm_follow_up | dm_no_reply | comment | warm_reengagement
    body = Column(Text, nullable=False)
    body_variants = Column(JSON, nullable=True)  # AI-сгенерированные вариации
    placeholders = Column(JSON, nullable=True)  # ["first_name", "company", ...]
    tone = Column(String(40), nullable=True)  # professional / casual / direct / warm
    tags = Column(JSON, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, nullable=False)


# ── Кампании рассылки ───────────────────────────────────────


class LinkedInOutreachCampaign(Base):
    """Кампания рассылки DM / invitations.

    Аналог FBOutreachCampaign / RedditCampaign.
    """

    __tablename__ = "linkedin_outreach_campaigns"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    mode = Column(String(40), nullable=False, default="dm_first_degree")
    # dm_first_degree | invitation_with_note | post_accept_follow_up | comment_assist
    status = Column(String(20), nullable=False, default="draft", index=True)
    # draft | active | paused | completed | stopped

    account_id = Column(Integer, ForeignKey("linkedin_accounts.id"), nullable=False, index=True)
    template_id = Column(Integer, ForeignKey("linkedin_templates.id"), nullable=True)
    follow_up_template_id = Column(Integer, ForeignKey("linkedin_templates.id"), nullable=True)

    audience_query = Column(JSON, nullable=True)
    approval_mode = Column(String(20), nullable=False, default="manual")  # manual | semi | auto
    daily_cap = Column(Integer, nullable=True)
    schedule_window_json = Column(JSON, nullable=True)  # {"start":"09:00","end":"19:00","tz":"Europe/Madrid","weekdays":[1,2,3,4,5]}

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)


class LinkedInOutreachQueueItem(Base):
    """Один элемент очереди рассылки: lead + draft + state."""

    __tablename__ = "linkedin_outreach_queue"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("linkedin_outreach_campaigns.id"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("linkedin_leads.id"), nullable=False, index=True)
    draft_body = Column(Text, nullable=False)
    review_state = Column(String(20), nullable=False, default="pending_review", index=True)
    # pending_review | approved | rejected | sent | failed | skipped
    scheduled_for = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(512), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_li_queue_campaign_state", "campaign_id", "review_state"),
    )


# ── Sequences (Agent Mode) ──────────────────────────────────


class LinkedInSequence(Base):
    """Граф sequence: invite → wait → DM → branch on reply ..."""

    __tablename__ = "linkedin_sequences"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    nodes_json = Column(JSON, nullable=False)
    edges_json = Column(JSON, nullable=False)
    approval_gates_json = Column(JSON, nullable=True)
    safety_caps_json = Column(JSON, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class LinkedInSequenceRun(Base):
    """Выполнение sequence для конкретного lead."""

    __tablename__ = "linkedin_sequence_runs"

    id = Column(Integer, primary_key=True)
    sequence_id = Column(Integer, ForeignKey("linkedin_sequences.id"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("linkedin_leads.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("linkedin_accounts.id"), nullable=False, index=True)
    current_node_id = Column(String(64), nullable=True)
    state = Column(String(20), nullable=False, default="active", index=True)
    # active | waiting | paused | completed | stopped | failed
    state_json = Column(JSON, nullable=True)
    next_action_at = Column(DateTime, nullable=True, index=True)
    last_event_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


# ── Conversations / Inbox ───────────────────────────────────


class LinkedInConversation(Base):
    """Тред переписки между нашим аккаунтом и lead."""

    __tablename__ = "linkedin_conversations"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("linkedin_accounts.id"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("linkedin_leads.id"), nullable=True, index=True)
    counterpart_public_identifier = Column(String(120), nullable=True)
    counterpart_full_name = Column(String(255), nullable=True)
    thread_kind = Column(String(20), nullable=False, default="dm")  # dm | inmail | invitation_note
    last_message_at = Column(DateTime, nullable=True, index=True)
    last_message_from = Column(String(8), nullable=True)  # in | out
    unread_count = Column(Integer, nullable=False, default=0)
    crm_stage = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class LinkedInMessage(Base):
    """Одно сообщение в conversation (входящее или исходящее)."""

    __tablename__ = "linkedin_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("linkedin_conversations.id"), nullable=False, index=True)
    direction = Column(String(8), nullable=False)  # in | out
    body = Column(Text, nullable=False)
    sent_at = Column(DateTime, nullable=False, index=True)
    linkedin_message_id = Column(String(64), unique=True, nullable=True)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


# ── Compliance / события безопасности ──────────────────────


class LinkedInComplianceEvent(Base):
    """Audit-event: rate-limit, restricted-detected, cookies-expired, и т.п."""

    __tablename__ = "linkedin_compliance_events"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("linkedin_accounts.id"), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    severity = Column(String(10), nullable=False, default="info")  # info | warn | error
    payload_json = Column(JSON, nullable=True)
    at = Column(DateTime, default=_utcnow, nullable=False, index=True)


class LinkedInAudienceSegment(Base):
    """Сохранённый поисковый сегмент для повторного использования.

    Хранит поисковые фильтры (keywords + опц. company / location / industry / seniority),
    позволяет повторно запустить scraper с теми же параметрами.
    """

    __tablename__ = "linkedin_audience_segments"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    keywords = Column(String(512), nullable=False)
    company = Column(String(255), nullable=True)
    location = Column(String(255), nullable=True)
    industry = Column(String(255), nullable=True)
    title = Column(String(255), nullable=True)
    extra_filters = Column(JSON, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    last_run_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    is_archived = Column(Boolean, nullable=False, default=False)


__all__ = [
    "LinkedInAccount",
    "LinkedInAccountDayUsage",
    "LinkedInAudienceSegment",
    "LinkedInComplianceEvent",
    "LinkedInConversation",
    "LinkedInLead",
    "LinkedInMessage",
    "LinkedInOutreachCampaign",
    "LinkedInOutreachQueueItem",
    "LinkedInSequence",
    "LinkedInSequenceRun",
    "LinkedInTemplate",
]
