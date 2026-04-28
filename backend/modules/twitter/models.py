"""Twitter / X Master — SQLAlchemy-модели.

Все таблицы с префиксом `twitter_`. Архитектура — порт LinkedIn / Reddit:
аккаунты подключаются через browser-профиль + cookies (auth_token, ct0,
twid). Таблицы создаются в общем `Base.metadata`.
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


# ── Аккаунты ───────────────────────────────────────────────


class TwitterAccount(Base):
    """Подключённый Twitter/X-аккаунт (browser-profile mode).

    Cookies: обязательные `auth_token` и `ct0` (CSRF). Опционально `twid`
    (для логирования id), `kdt`, `att`. Persistent Chromium-профиль на диске.
    """

    __tablename__ = "twitter_accounts"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    label = Column(String(255), nullable=False)
    handle = Column(String(80), nullable=True, index=True)  # @username (без @)
    full_name = Column(String(255), nullable=True)
    bio = Column(String(512), nullable=True)
    profile_url = Column(String(512), nullable=True)
    user_id_snapshot = Column(String(64), nullable=True)  # twid

    profile_dir = Column(String(512), nullable=False)
    cookies_json = Column(JSON, nullable=True)
    cookies_imported_at = Column(DateTime(timezone=True), nullable=True)

    proxy_enabled = Column(Boolean, nullable=False, default=False)
    proxy_url = Column(String(512), nullable=True)
    proxy_username = Column(String(255), nullable=True)
    proxy_password = Column(String(255), nullable=True)
    # 2.96+: дата/время окончания аренды прокси у провайдера (UTC).
    # Аналог FBAccount.proxy_lease_ends_at — вводится в Europe/Madrid, остановка
    # автоматизации за 1 ч до этого момента (см. backend.services.proxy_lease).
    proxy_lease_ends_at = Column(DateTime(timezone=True), nullable=True)

    stealth_user_agent = Column(String(512), nullable=True)
    stealth_locale = Column(String(32), nullable=True)
    stealth_timezone_id = Column(String(64), nullable=True)
    stealth_viewport_w = Column(Integer, nullable=True)
    stealth_viewport_h = Column(Integer, nullable=True)

    status = Column(String(20), nullable=False, default="needs_attention", index=True)
    # connected | needs_attention | restricted | cooldown | disabled
    session_ok = Column(Boolean, nullable=True)
    last_login_check_at = Column(DateTime(timezone=True), nullable=True)
    login_blocked_at = Column(DateTime(timezone=True), nullable=True)
    login_blocked_reason = Column(String(512), nullable=True)

    cap_dms_per_day = Column(Integer, nullable=True)
    cap_replies_per_day = Column(Integer, nullable=True)
    cap_follows_per_day = Column(Integer, nullable=True)

    notes = Column(Text, nullable=True)
    # Импорт «свой личный аккаунт» через логин+пароль (аналог InstagramAccount).
    # Пароль и TOTP шифруются Fernet-ом (см. backend.services.fb_credentials_crypto).
    # При первом открытии (или кнопке «Войти») запускается Playwright-логин,
    # после чего cookies_json заполняется реальной session-парой и status → 'connected'.
    login_username = Column(String(255), nullable=True)
    enc_password = Column(Text, nullable=True)
    enc_totp_secret = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TwitterAccountDayUsage(Base):
    """Дневные счётчики (UTC)."""

    __tablename__ = "twitter_account_day_usage"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("twitter_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    usage_date = Column(String(10), nullable=False)  # YYYY-MM-DD UTC
    dms_sent = Column(Integer, nullable=False, default=0)
    replies_posted = Column(Integer, nullable=False, default=0)
    follows_done = Column(Integer, nullable=False, default=0)
    profiles_visited = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("account_id", "usage_date", name="uq_twitter_account_day"),
    )


# ── Лиды ───────────────────────────────────────────────────


class TwitterLead(Base):
    __tablename__ = "twitter_leads"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    handle = Column(String(80), nullable=False, index=True)  # @username (без @)
    full_name = Column(String(255), nullable=True)
    bio = Column(String(512), nullable=True)
    location = Column(String(255), nullable=True)
    followers_count = Column(Integer, nullable=True)
    following_count = Column(Integer, nullable=True)
    is_verified = Column(Boolean, nullable=True)
    profile_url = Column(String(512), nullable=False)
    avatar_url = Column(String(512), nullable=True)

    source_kind = Column(String(40), nullable=False, default="manual")
    # manual | search_users | search_tweets | followers_scan | bulk_import
    source_ref = Column(String(512), nullable=True)

    tags = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)

    outreach_stage = Column(String(32), nullable=False, default="new", index=True)
    # new | dm_sent | replied | converted | dropped | dm_disabled
    last_outreach_at = Column(DateTime(timezone=True), nullable=True)
    last_signal_at = Column(DateTime(timezone=True), nullable=True)

    owner_account_id = Column(Integer, ForeignKey("twitter_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    eligibility_flags = Column(JSON, nullable=True)  # {"dm_open": true, "verified": false, "protected": false}

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "handle", name="uq_twitter_leads_org_handle"),
        Index("ix_twitter_leads_org_stage", "organization_id", "outreach_stage"),
    )


# ── Шаблоны ─────────────────────────────────────────────────


class TwitterTemplate(Base):
    __tablename__ = "twitter_templates"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    kind = Column(String(32), nullable=False, default="dm_first_touch")
    # dm_first_touch | dm_follow_up | dm_no_reply | reply | quote_tweet | warm_reengagement
    body = Column(Text, nullable=False)
    body_variants = Column(JSON, nullable=True)
    placeholders = Column(JSON, nullable=True)  # ["handle","full_name","bio","recent_tweet","topic"]
    tone = Column(String(40), nullable=True)
    tags = Column(JSON, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# ── Saved audience segments ────────────────────────────────


class TwitterAudienceSegment(Base):
    __tablename__ = "twitter_audience_segments"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    keywords = Column(String(512), nullable=False)
    search_kind = Column(String(20), nullable=False, default="users")  # users | tweets
    extra_filters = Column(JSON, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_count = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    is_archived = Column(Boolean, nullable=False, default=False)


# ── Кампании ───────────────────────────────────────────────


class TwitterOutreachCampaign(Base):
    __tablename__ = "twitter_outreach_campaigns"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    mode = Column(String(40), nullable=False, default="dm")
    # dm | reply | quote_tweet
    status = Column(String(20), nullable=False, default="draft", index=True)
    # draft | active | paused | completed | stopped

    account_id = Column(Integer, ForeignKey("twitter_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    template_id = Column(Integer, ForeignKey("twitter_templates.id", ondelete="SET NULL"), nullable=True)
    follow_up_template_id = Column(Integer, ForeignKey("twitter_templates.id", ondelete="SET NULL"), nullable=True)

    audience_query = Column(JSON, nullable=True)
    approval_mode = Column(String(20), nullable=False, default="manual")
    daily_cap = Column(Integer, nullable=True)
    schedule_window_json = Column(JSON, nullable=True)

    # AI autoresponder integration: при ответе lead'а → создаём AIDialogSession.
    autoresponder_enabled = Column(Boolean, nullable=False, default=False)
    autoresponder_persona_id = Column(Integer, nullable=True)
    autoresponder_goal = Column(String(40), nullable=True)
    # book_call | share_info | qualify_lead | nurture

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)


class TwitterOutreachQueueItem(Base):
    __tablename__ = "twitter_outreach_queue"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("twitter_outreach_campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("twitter_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    draft_body = Column(Text, nullable=False)
    target_tweet_url = Column(String(512), nullable=True)  # для mode=reply
    review_state = Column(String(20), nullable=False, default="pending_review", index=True)
    # pending_review | approved | rejected | sent | failed | skipped
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(512), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_twitter_queue_campaign_state", "campaign_id", "review_state"),
    )


# ── Conversations / Inbox ──────────────────────────────────


class TwitterConversation(Base):
    __tablename__ = "twitter_conversations"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("twitter_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("twitter_leads.id", ondelete="SET NULL"), nullable=True, index=True)

    counterpart_handle = Column(String(80), nullable=True, index=True)
    counterpart_full_name = Column(String(255), nullable=True)
    twitter_conversation_id = Column(String(64), nullable=True, index=True)  # X internal id

    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_message_from = Column(String(8), nullable=True)  # in | out
    unread_count = Column(Integer, nullable=False, default=0)
    crm_stage = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TwitterMessage(Base):
    __tablename__ = "twitter_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("twitter_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    direction = Column(String(8), nullable=False)  # in | out
    body = Column(Text, nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=False, index=True)
    twitter_message_id = Column(String(64), unique=True, nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    # Источник исходящих: manual | template | ai_autoresponder
    out_source = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── AI Autoresponder ──────────────────────────────────────


class TwitterAIPersona(Base):
    """Персонаж для AI-автоответчика — кто отвечает и что предлагает."""

    __tablename__ = "twitter_ai_personas"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    role = Column(String(255), nullable=True)  # «Founder of X», «Sales rep»
    company = Column(String(255), nullable=True)
    offer_summary = Column(Text, nullable=True)  # краткий питч (1-2 абзаца)
    tone = Column(String(40), nullable=True)  # friendly | professional | casual | direct
    languages = Column(JSON, nullable=True)  # ["ru", "en"]
    knowledge_base = Column(Text, nullable=True)  # что персонаж знает (FAQ, продукт, кейсы)
    cta_call_link = Column(String(512), nullable=True)  # Calendly / Cal.com link для созвона
    cta_info_link = Column(String(512), nullable=True)  # ссылка на info-pack / pdf / лендинг
    forbidden_topics = Column(Text, nullable=True)  # темы, которые нельзя обсуждать
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TwitterAIDialogSession(Base):
    """Сессия AI-переписки с конкретным lead'ом.

    State machine:
      active     — нейросеть отвечает.
      paused     — оператор поставил на паузу.
      goal_reached — цель достигнута (lead согласился на созвон / запросил материалы).
      stopped    — lead явно отказался / попросил не писать / 7+ дней молчит.
      failed     — техническая ошибка (несколько раз подряд).
    """

    __tablename__ = "twitter_ai_dialog_sessions"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("twitter_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("twitter_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("twitter_conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    persona_id = Column(Integer, ForeignKey("twitter_ai_personas.id", ondelete="SET NULL"), nullable=True)

    goal = Column(String(40), nullable=False, default="qualify_lead")
    # book_call | share_info | qualify_lead | nurture
    state = Column(String(20), nullable=False, default="active", index=True)
    approval_mode = Column(String(20), nullable=False, default="manual")  # manual | auto

    scratchpad = Column(JSON, nullable=True)  # AI memory: outcomes, signals, attempts
    last_inbound_at = Column(DateTime(timezone=True), nullable=True)
    last_outbound_at = Column(DateTime(timezone=True), nullable=True)
    last_action_at = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=True, index=True)

    msg_count_in = Column(Integer, nullable=False, default=0)
    msg_count_out = Column(Integer, nullable=False, default=0)
    final_outcome = Column(String(40), nullable=True)
    # call_booked | info_shared | qualified | refused | silent | error

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TwitterAIDialogDraft(Base):
    """Черновик AI-ответа (если approval_mode=manual — ждёт approve)."""

    __tablename__ = "twitter_ai_dialog_drafts"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("twitter_ai_dialog_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)  # почему AI выбрал именно такой ответ
    review_state = Column(String(20), nullable=False, default="pending_review", index=True)
    # pending_review | approved | rejected | sent | failed
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── Compliance ─────────────────────────────────────────────


class TwitterComplianceEvent(Base):
    __tablename__ = "twitter_compliance_events"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("twitter_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    severity = Column(String(10), nullable=False, default="info")
    payload_json = Column(JSON, nullable=True)
    at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


__all__ = [
    "TwitterAccount",
    "TwitterAccountDayUsage",
    "TwitterAIDialogDraft",
    "TwitterAIDialogSession",
    "TwitterAIPersona",
    "TwitterAudienceSegment",
    "TwitterComplianceEvent",
    "TwitterConversation",
    "TwitterLead",
    "TwitterMessage",
    "TwitterOutreachCampaign",
    "TwitterOutreachQueueItem",
    "TwitterTemplate",
]
