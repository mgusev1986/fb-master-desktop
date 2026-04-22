"""Instagram Master — SQLAlchemy-модели.

Все таблицы с префиксом `instagram_`. Архитектура — порт Twitter:
аккаунты подключаются через browser-профиль (Chromium + cookies).
Дополнительно — Instagram-специфичные таблицы:
  * EngagementTask — лайки/комменты/follow по target audience.
  * CompetitorAudienceTask — парсинг аудитории конкурентов.
  * StoryLikeTask — лайки сторисов.
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


class InstagramAccount(Base):
    """Подключённый Instagram-аккаунт (browser-profile mode).

    Cookies: `sessionid` + `csrftoken` + `ds_user_id` обязательные;
    `mid`, `ig_did`, `rur` желательны для аутентичности.
    """

    __tablename__ = "instagram_accounts"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    label = Column(String(255), nullable=False)
    handle = Column(String(80), nullable=True, index=True)
    full_name = Column(String(255), nullable=True)
    bio = Column(String(512), nullable=True)
    profile_url = Column(String(512), nullable=True)
    user_id_snapshot = Column(String(64), nullable=True)  # ds_user_id

    profile_dir = Column(String(512), nullable=False)
    cookies_json = Column(JSON, nullable=True)
    cookies_imported_at = Column(DateTime(timezone=True), nullable=True)

    proxy_enabled = Column(Boolean, nullable=False, default=False)
    proxy_url = Column(String(512), nullable=True)
    proxy_username = Column(String(255), nullable=True)
    proxy_password = Column(String(255), nullable=True)

    # Instagram чувствителен к UA — рекомендуется mobile UA для веб-сессии.
    stealth_user_agent = Column(String(512), nullable=True)
    stealth_locale = Column(String(32), nullable=True)
    stealth_timezone_id = Column(String(64), nullable=True)
    stealth_viewport_w = Column(Integer, nullable=True)
    stealth_viewport_h = Column(Integer, nullable=True)

    status = Column(String(20), nullable=False, default="needs_attention", index=True)
    session_ok = Column(Boolean, nullable=True)
    last_login_check_at = Column(DateTime(timezone=True), nullable=True)
    login_blocked_at = Column(DateTime(timezone=True), nullable=True)
    login_blocked_reason = Column(String(512), nullable=True)

    cap_dms_per_day = Column(Integer, nullable=True)
    cap_comments_per_day = Column(Integer, nullable=True)
    cap_likes_per_day = Column(Integer, nullable=True)
    cap_follows_per_day = Column(Integer, nullable=True)
    cap_story_likes_per_day = Column(Integer, nullable=True)

    notes = Column(Text, nullable=True)

    # Импорт «свой личный аккаунт» через логин+пароль (аналог FBAccount.fb_login_username).
    # Пароль и TOTP шифруются Fernet-ом (см. backend.services.fb_credentials_crypto).
    # При первом открытии (или кнопке «Войти») запускается Playwright-логин,
    # после чего cookies_json заполняется реальной session-парой и status → 'connected'.
    login_username = Column(String(255), nullable=True)
    enc_password = Column(Text, nullable=True)
    enc_totp_secret = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class InstagramAccountDayUsage(Base):
    __tablename__ = "instagram_account_day_usage"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    usage_date = Column(String(10), nullable=False)
    dms_sent = Column(Integer, nullable=False, default=0)
    comments_posted = Column(Integer, nullable=False, default=0)
    likes_done = Column(Integer, nullable=False, default=0)
    follows_done = Column(Integer, nullable=False, default=0)
    story_likes_done = Column(Integer, nullable=False, default=0)
    profiles_visited = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("account_id", "usage_date", name="uq_instagram_account_day"),
    )


# ── Лиды ───────────────────────────────────────────────────


class InstagramLead(Base):
    __tablename__ = "instagram_leads"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    handle = Column(String(80), nullable=False, index=True)
    full_name = Column(String(255), nullable=True)
    bio = Column(String(512), nullable=True)
    is_private = Column(Boolean, nullable=True)
    is_verified = Column(Boolean, nullable=True)
    is_business = Column(Boolean, nullable=True)
    followers_count = Column(Integer, nullable=True)
    following_count = Column(Integer, nullable=True)
    profile_url = Column(String(512), nullable=False)
    avatar_url = Column(String(512), nullable=True)

    source_kind = Column(String(40), nullable=False, default="manual")
    # manual | search_users | hashtag_scan | competitor_likers | competitor_commenters | bulk_import
    source_ref = Column(String(512), nullable=True)  # hashtag, competitor handle, post URL etc.

    tags = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)

    outreach_stage = Column(String(32), nullable=False, default="new", index=True)
    # new | dm_sent | replied | converted | dropped | dm_disabled | followed | liked
    last_outreach_at = Column(DateTime(timezone=True), nullable=True)
    last_signal_at = Column(DateTime(timezone=True), nullable=True)

    owner_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    eligibility_flags = Column(JSON, nullable=True)
    # {"dm_open": true, "private": false, "business": true, ...}

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "handle", name="uq_instagram_leads_org_handle"),
        Index("ix_instagram_leads_org_stage", "organization_id", "outreach_stage"),
    )


# ── Шаблоны ─────────────────────────────────────────────────


class InstagramTemplate(Base):
    __tablename__ = "instagram_templates"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    kind = Column(String(32), nullable=False, default="dm_first_touch")
    # dm_first_touch | dm_follow_up | dm_no_reply | comment | story_reply | warm_reengagement
    body = Column(Text, nullable=False)
    body_variants = Column(JSON, nullable=True)
    placeholders = Column(JSON, nullable=True)
    tone = Column(String(40), nullable=True)
    tags = Column(JSON, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# ── Audience segments ──────────────────────────────────────


class InstagramAudienceSegment(Base):
    """Сохранённый поисковый сегмент: keyword, hashtag или competitor."""

    __tablename__ = "instagram_audience_segments"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    keywords = Column(String(512), nullable=False)  # текст keyword / hashtag (без #) / competitor handle
    search_kind = Column(String(20), nullable=False, default="hashtag")
    # users | hashtag | competitor_followers | competitor_likers | competitor_commenters
    extra_filters = Column(JSON, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_count = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    is_archived = Column(Boolean, nullable=False, default=False)


# ── Кампании рассылки ──────────────────────────────────────


class InstagramOutreachCampaign(Base):
    __tablename__ = "instagram_outreach_campaigns"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    mode = Column(String(40), nullable=False, default="dm")
    # dm | comment | story_reply
    status = Column(String(20), nullable=False, default="draft", index=True)

    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    template_id = Column(Integer, ForeignKey("instagram_templates.id", ondelete="SET NULL"), nullable=True)
    follow_up_template_id = Column(Integer, ForeignKey("instagram_templates.id", ondelete="SET NULL"), nullable=True)

    audience_query = Column(JSON, nullable=True)
    approval_mode = Column(String(20), nullable=False, default="manual")
    daily_cap = Column(Integer, nullable=True)
    schedule_window_json = Column(JSON, nullable=True)

    autoresponder_enabled = Column(Boolean, nullable=False, default=False)
    autoresponder_persona_id = Column(Integer, nullable=True)
    autoresponder_goal = Column(String(40), nullable=True)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)


class InstagramOutreachQueueItem(Base):
    __tablename__ = "instagram_outreach_queue"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("instagram_outreach_campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("instagram_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    draft_body = Column(Text, nullable=False)
    target_post_url = Column(String(512), nullable=True)  # для mode=comment / story_reply
    review_state = Column(String(20), nullable=False, default="pending_review", index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(512), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_instagram_queue_campaign_state", "campaign_id", "review_state"),
    )


# ── Engagement Tasks (Instagram-уникальная фича) ───────────


class InstagramEngagementTask(Base):
    """Задача массового engagement: лайк / коммент / follow / story-like.

    Создаётся пользователем с параметрами:
      kind (like_post | comment_post | follow_user | story_like)
      audience_source: какой набор lead'ов (segment_id или lead_ids)
      template_id (опц., для comment)
    Worker обходит targets с safety-капами и обновляет статус каждого target'а.
    """

    __tablename__ = "instagram_engagement_tasks"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    kind = Column(String(40), nullable=False, default="like_post", index=True)
    # like_post | comment_post | follow_user | story_like

    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    template_id = Column(Integer, ForeignKey("instagram_templates.id", ondelete="SET NULL"), nullable=True)
    segment_id = Column(Integer, ForeignKey("instagram_audience_segments.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(20), nullable=False, default="draft", index=True)
    # draft | active | paused | completed | stopped
    daily_cap = Column(Integer, nullable=True)
    approval_mode = Column(String(20), nullable=False, default="auto")  # auto/manual

    targets_json = Column(JSON, nullable=True)  # список post_url или lead_ids
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)


class InstagramEngagementQueueItem(Base):
    """Один target внутри engagement task."""

    __tablename__ = "instagram_engagement_queue"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("instagram_engagement_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    target_kind = Column(String(20), nullable=False)  # post | user
    target_ref = Column(String(512), nullable=False)  # post_url или handle
    lead_id = Column(Integer, ForeignKey("instagram_leads.id", ondelete="SET NULL"), nullable=True)
    review_state = Column(String(20), nullable=False, default="pending", index=True)
    # pending | done | failed | skipped
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(512), nullable=True)
    done_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── Competitor audience tasks ──────────────────────────────


class InstagramCompetitorAudienceTask(Base):
    """Задача парсинга аудитории конкурента (likers / commenters / followers)."""

    __tablename__ = "instagram_competitor_audience_tasks"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    kind = Column(String(40), nullable=False, default="competitor_likers", index=True)
    # competitor_likers | competitor_commenters | competitor_followers
    competitor_handle = Column(String(80), nullable=True, index=True)  # для followers
    target_post_url = Column(String(512), nullable=True)  # для likers/commenters

    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    max_results = Column(Integer, nullable=True)

    status = Column(String(20), nullable=False, default="pending", index=True)
    # pending | running | completed | failed
    found = Column(Integer, nullable=False, default=0)
    saved = Column(Integer, nullable=False, default=0)
    last_error = Column(String(512), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


# ── Conversations / Inbox ──────────────────────────────────


class InstagramConversation(Base):
    __tablename__ = "instagram_conversations"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("instagram_leads.id", ondelete="SET NULL"), nullable=True, index=True)

    counterpart_handle = Column(String(80), nullable=True, index=True)
    counterpart_full_name = Column(String(255), nullable=True)
    instagram_thread_id = Column(String(64), nullable=True, index=True)

    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_message_from = Column(String(8), nullable=True)
    unread_count = Column(Integer, nullable=False, default=0)
    crm_stage = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class InstagramMessage(Base):
    __tablename__ = "instagram_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("instagram_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    direction = Column(String(8), nullable=False)
    body = Column(Text, nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=False, index=True)
    instagram_message_id = Column(String(64), unique=True, nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    out_source = Column(String(20), nullable=True)  # manual | template | ai_autoresponder
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── AI autoresponder ──────────────────────────────────────


class InstagramAIPersona(Base):
    __tablename__ = "instagram_ai_personas"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    role = Column(String(255), nullable=True)
    company = Column(String(255), nullable=True)
    offer_summary = Column(Text, nullable=True)
    tone = Column(String(40), nullable=True)
    languages = Column(JSON, nullable=True)
    knowledge_base = Column(Text, nullable=True)
    cta_call_link = Column(String(512), nullable=True)
    cta_info_link = Column(String(512), nullable=True)
    forbidden_topics = Column(Text, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class InstagramAIDialogSession(Base):
    __tablename__ = "instagram_ai_dialog_sessions"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("instagram_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("instagram_conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    persona_id = Column(Integer, ForeignKey("instagram_ai_personas.id", ondelete="SET NULL"), nullable=True)

    goal = Column(String(40), nullable=False, default="qualify_lead")
    state = Column(String(20), nullable=False, default="active", index=True)
    approval_mode = Column(String(20), nullable=False, default="manual")

    scratchpad = Column(JSON, nullable=True)
    last_inbound_at = Column(DateTime(timezone=True), nullable=True)
    last_outbound_at = Column(DateTime(timezone=True), nullable=True)
    last_action_at = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=True, index=True)

    msg_count_in = Column(Integer, nullable=False, default=0)
    msg_count_out = Column(Integer, nullable=False, default=0)
    final_outcome = Column(String(40), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class InstagramAIDialogDraft(Base):
    __tablename__ = "instagram_ai_dialog_drafts"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("instagram_ai_dialog_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)
    review_state = Column(String(20), nullable=False, default="pending_review", index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── Compliance ─────────────────────────────────────────────


class InstagramComplianceEvent(Base):
    __tablename__ = "instagram_compliance_events"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    severity = Column(String(10), nullable=False, default="info")
    payload_json = Column(JSON, nullable=True)
    at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


__all__ = [
    "InstagramAccount",
    "InstagramAccountDayUsage",
    "InstagramAIDialogDraft",
    "InstagramAIDialogSession",
    "InstagramAIPersona",
    "InstagramAudienceSegment",
    "InstagramCompetitorAudienceTask",
    "InstagramComplianceEvent",
    "InstagramConversation",
    "InstagramEngagementQueueItem",
    "InstagramEngagementTask",
    "InstagramLead",
    "InstagramMessage",
    "InstagramOutreachCampaign",
    "InstagramOutreachQueueItem",
    "InstagramTemplate",
]
