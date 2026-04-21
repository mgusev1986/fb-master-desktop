"""Reddit Master — SQLAlchemy-модели.

Все таблицы с префиксом `reddit_`. FK на существующие FB-таблицы не
используются — для кросс-модульной CRM-воронки используется общая
`crm_stages` (без FK, хранится как Integer без relationship).

Таблицы создаются в общем `Base.metadata` → `init_db()` создаёт их
автоматически при старте. При выключенном `FB_MASTER_REDDIT_MODULE_ENABLED`
таблицы всё равно создаются, но остаются пустыми.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
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


# ── Accounts ────────────────────────────────────────────


class RedditAccount(Base):
    __tablename__ = "reddit_accounts"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)

    label = Column(String(160), nullable=False, default="")
    username_snapshot = Column(String(160), nullable=True, index=True)
    reddit_user_id = Column(String(80), nullable=True, index=True)  # t2_xxxx

    # OAuth2 (script или installed-app, refresh token живёт долго):
    oauth_refresh_token_enc = Column(Text, nullable=True)
    oauth_scope = Column(String(400), nullable=True)
    oauth_expires_at = Column(DateTime(timezone=True), nullable=True)
    oauth_client_profile_id = Column(Integer, nullable=True)  # ссылка на записанный client в Settings

    status = Column(String(40), nullable=False, default="disconnected")
    # disconnected | connected | needs_reauth | limited | restricted | cooldown

    capabilities_json = Column(JSON, nullable=True)  # {"post": true, "comment": true, "chat": true, "dm": true}
    karma_link = Column(Integer, nullable=True)
    karma_comment = Column(Integer, nullable=True)
    account_age_days = Column(Integer, nullable=True)
    email_verified = Column(Boolean, nullable=True)
    is_suspended = Column(Boolean, nullable=False, default=False)
    is_employee = Column(Boolean, nullable=True)

    rate_limit_state_json = Column(JSON, nullable=True)  # {"bucket": {"remaining": N, "reset_at": iso}}
    cooldown_until = Column(DateTime(timezone=True), nullable=True)
    last_readiness_probe_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    notes = Column(Text, nullable=True)

    # ── Browser-profile mode (как FB Master / LinkedIn Master) ──
    # auth_mode = "oauth" | "browser_profile". При импорте cookies автоматически
    # переключается в "browser_profile". OAuth-аккаунты по-прежнему работают.
    auth_mode = Column(String(24), nullable=False, default="oauth", index=True)
    profile_dir = Column(String(512), nullable=True)  # persistent Chrome-профиль
    cookies_json = Column(JSON, nullable=True)  # [{"name":"reddit_session","value":"...","domain":".reddit.com",...}, ...]
    cookies_imported_at = Column(DateTime(timezone=True), nullable=True)

    proxy_enabled = Column(Boolean, nullable=False, default=False)
    proxy_url = Column(String(512), nullable=True)
    proxy_username = Column(String(255), nullable=True)
    proxy_password = Column(String(255), nullable=True)

    stealth_user_agent = Column(String(512), nullable=True)
    stealth_locale = Column(String(32), nullable=True)
    stealth_timezone_id = Column(String(64), nullable=True)
    stealth_viewport_w = Column(Integer, nullable=True)
    stealth_viewport_h = Column(Integer, nullable=True)

    session_ok = Column(Boolean, nullable=True)
    last_login_check_at = Column(DateTime(timezone=True), nullable=True)
    login_blocked_at = Column(DateTime(timezone=True), nullable=True)
    login_blocked_reason = Column(String(512), nullable=True)

    cap_messages_per_day = Column(Integer, nullable=True)
    cap_comments_per_day = Column(Integer, nullable=True)
    cap_posts_per_day = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


Index("ix_reddit_accounts_org_status", RedditAccount.organization_id, RedditAccount.status)


class RedditAccountDayUsage(Base):
    """Дневные счётчики действий для browser-mode (UTC). Аналог LinkedInAccountDayUsage."""

    __tablename__ = "reddit_account_day_usage"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    usage_date = Column(String(10), nullable=False)  # YYYY-MM-DD UTC
    messages_sent = Column(Integer, nullable=False, default=0)
    comments_posted = Column(Integer, nullable=False, default=0)
    posts_published = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("account_id", "usage_date", name="uq_reddit_account_day"),
    )


class RedditReadinessProbe(Base):
    __tablename__ = "reddit_readiness_probes"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    performed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)

    overall_ok = Column(Boolean, nullable=False, default=False)
    checks_json = Column(JSON, nullable=True)  # {"age": "ok", "karma": "warn", "chat": "ok", ...}
    warnings_json = Column(JSON, nullable=True)  # ["karma_below_suggested", ...]
    notes = Column(Text, nullable=True)


# ── Subreddits / Audience discovery ─────────────────────


class RedditSubreddit(Base):
    __tablename__ = "reddit_subreddits"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)

    name = Column(String(120), nullable=False, index=True)  # без "r/"
    display_name = Column(String(160), nullable=True)
    subreddit_id = Column(String(40), nullable=True, index=True)  # t5_xxxx

    subscribers = Column(Integer, nullable=True)
    active_users_estimate = Column(Integer, nullable=True)
    posts_last_24h = Column(Integer, nullable=True)
    avg_comments_per_post = Column(Float, nullable=True)

    is_nsfw = Column(Boolean, nullable=False, default=False)
    topics_json = Column(JSON, nullable=True)  # ["finance", "crypto", ...]
    summary = Column(Text, nullable=True)
    rules_summary = Column(Text, nullable=True)
    language_guess = Column(String(8), nullable=True)  # "en" | "ru" | ...

    suitability_score = Column(Float, nullable=True)
    risk_score = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)

    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_reddit_subreddits_org_name"),
    )


class RedditUserProfile(Base):
    __tablename__ = "reddit_user_profiles"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)

    username = Column(String(160), nullable=False, index=True)  # без "u/"
    reddit_user_id = Column(String(40), nullable=True, index=True)

    karma_link = Column(Integer, nullable=True)
    karma_comment = Column(Integer, nullable=True)
    account_age_days = Column(Integer, nullable=True)
    bio = Column(Text, nullable=True)
    verified_flags_json = Column(JSON, nullable=True)  # {"email": true, "premium": false}

    last_visible_action_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_post_at = Column(DateTime(timezone=True), nullable=True)
    last_comment_at = Column(DateTime(timezone=True), nullable=True)

    topics_seen_json = Column(JSON, nullable=True)
    subreddits_seen_json = Column(JSON, nullable=True)
    language_guess = Column(String(8), nullable=True)

    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "username", name="uq_reddit_user_profiles_org_username"),
    )


class RedditPost(Base):
    __tablename__ = "reddit_posts"

    id = Column(Integer, primary_key=True, index=True)
    reddit_post_id = Column(String(40), nullable=False, unique=True, index=True)  # t3_xxxx

    subreddit_name = Column(String(120), nullable=False, index=True)
    author_username = Column(String(160), nullable=True, index=True)
    title = Column(String(320), nullable=False, default="")
    body_excerpt = Column(Text, nullable=True)
    url = Column(String(512), nullable=True)
    permalink = Column(String(512), nullable=True)

    posted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    score = Column(Integer, nullable=True)
    num_comments = Column(Integer, nullable=True)

    raw_snapshot_json = Column(JSON, nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class RedditComment(Base):
    __tablename__ = "reddit_comments"

    id = Column(Integer, primary_key=True, index=True)
    reddit_comment_id = Column(String(40), nullable=False, unique=True, index=True)  # t1_xxxx

    parent_post_id = Column(String(40), nullable=True, index=True)  # t3_xxxx
    subreddit_name = Column(String(120), nullable=False, index=True)
    author_username = Column(String(160), nullable=True, index=True)
    body_excerpt = Column(Text, nullable=True)
    permalink = Column(String(512), nullable=True)

    posted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    score = Column(Integer, nullable=True)

    raw_snapshot_json = Column(JSON, nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class RedditRecentActivitySnapshot(Base):
    """Свёртка активности пользователя за период (recent visible activity).

    Не online-status, только видимые посты/комменты.
    """

    __tablename__ = "reddit_recent_activity_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    user_profile_id = Column(Integer, ForeignKey("reddit_user_profiles.id", ondelete="CASCADE"), index=True, nullable=False)
    taken_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    window_label = Column(String(24), nullable=False, default="24h")  # "1h" | "24h" | "3d" | "7d" | "30d" | "custom"

    latest_post_at = Column(DateTime(timezone=True), nullable=True)
    latest_comment_at = Column(DateTime(timezone=True), nullable=True)
    posts_count = Column(Integer, nullable=False, default=0)
    comments_count = Column(Integer, nullable=False, default=0)
    subreddits_json = Column(JSON, nullable=True)  # ["r/foo", "r/bar"]


class RedditChatCapability(Base):
    __tablename__ = "reddit_chat_capability"

    id = Column(Integer, primary_key=True, index=True)
    user_profile_id = Column(Integer, ForeignKey("reddit_user_profiles.id", ondelete="CASCADE"), index=True, nullable=False, unique=True)

    chat_enabled = Column(Boolean, nullable=True)
    dm_enabled = Column(Boolean, nullable=True)
    reason = Column(String(120), nullable=True)  # "disabled_by_user" | "not_enough_karma" | ...
    checked_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── Leads / Templates ───────────────────────────────────


class RedditLead(Base):
    __tablename__ = "reddit_leads"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)

    user_profile_id = Column(Integer, ForeignKey("reddit_user_profiles.id", ondelete="SET NULL"), index=True, nullable=True)
    username = Column(String(160), nullable=False, index=True)

    source_type = Column(String(40), nullable=False, default="manual")
    # manual | subreddit_scan | post_comments | user_mentions | recent_activity
    source_ref = Column(String(400), nullable=True)  # r/<name> | permalink | query

    activity_timestamp = Column(DateTime(timezone=True), nullable=True, index=True)
    engagement_score = Column(Float, nullable=True)
    topic_tags_json = Column(JSON, nullable=True)
    persona_tags = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)

    status = Column(String(40), nullable=False, default="new")
    # new | contacted | replied | skipped | blocked | failed | dnd
    contact_eligibility_json = Column(JSON, nullable=True)

    owner_account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), index=True, nullable=True)
    assigned_campaign_id = Column(Integer, index=True, nullable=True)  # FK позже через ForeignKey при campaigns

    person_id = Column(Integer, ForeignKey("people.id", ondelete="SET NULL"), nullable=True, index=True)
    crm_stage_id = Column(Integer, ForeignKey("crm_stages.id", ondelete="SET NULL"), nullable=True, index=True)

    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_touch_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "username", name="uq_reddit_leads_org_username"),
    )


class RedditTemplate(Base):
    __tablename__ = "reddit_templates"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)

    name = Column(String(200), nullable=False, default="")
    kind = Column(String(40), nullable=False, default="dm")
    # dm | chat_opener | comment | follow_up
    body = Column(Text, nullable=False, default="")
    placeholders_json = Column(JSON, nullable=True)  # ["username", "subreddit", "last_post_title"]
    tone = Column(String(40), nullable=True)  # friendly | formal | casual | expert

    variants_json = Column(JSON, nullable=True)  # list[str]
    variant_mode = Column(String(20), nullable=False, default="random")  # random | sequential

    banned_phrases_json = Column(JSON, nullable=True)
    last_ai_generation_meta_json = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# ── Campaigns / Queue ───────────────────────────────────


class RedditCampaign(Base):
    __tablename__ = "reddit_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)

    name = Column(String(200), nullable=False, default="")
    mode = Column(String(40), nullable=False, default="dm")  # dm | comment
    status = Column(String(40), nullable=False, default="draft")
    # draft | running | paused | finished | cancelled

    audience_query_json = Column(JSON, nullable=True)
    template_id = Column(Integer, ForeignKey("reddit_templates.id", ondelete="SET NULL"), nullable=True, index=True)

    cadence_config_json = Column(JSON, nullable=True)  # {"window_hours": 14, "rest_hours": 10, ...}
    send_window_config_json = Column(JSON, nullable=True)  # {"tz": "Europe/Madrid", "hours": "09-21"}
    per_account_caps_json = Column(JSON, nullable=True)  # {"per_day": 10, "per_hour": 3}

    approval_mode = Column(String(20), nullable=False, default="manual")  # manual | semi | auto
    safety_review_sample_json = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class RedditCampaignQueue(Base):
    __tablename__ = "reddit_campaign_queue"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("reddit_campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    lead_id = Column(Integer, ForeignKey("reddit_leads.id", ondelete="CASCADE"), index=True, nullable=False)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), index=True, nullable=True)

    payload_body = Column(Text, nullable=True)
    payload_meta_json = Column(JSON, nullable=True)
    review_state = Column(String(40), nullable=False, default="pending_review")
    # pending_review | approved | sent | failed | skipped | rejected
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    reason_code = Column(String(80), nullable=True)

    scheduled_for = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    lead = relationship("RedditLead", lazy="joined")
    account = relationship("RedditAccount", lazy="joined")

    __table_args__ = (
        Index("ix_reddit_campaign_queue_review_state_scheduled", "review_state", "scheduled_for"),
    )


# ── Sequences (Agent Mode) ──────────────────────────────


class RedditSequence(Base):
    __tablename__ = "reddit_sequences"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)

    name = Column(String(200), nullable=False, default="")
    status = Column(String(40), nullable=False, default="draft")
    # draft | active | paused | archived

    nodes_json = Column(JSON, nullable=True)  # list[SequenceNode]
    edges_json = Column(JSON, nullable=True)
    approval_gates_json = Column(JSON, nullable=True)
    safety_caps_json = Column(JSON, nullable=True)
    timezone = Column(String(60), nullable=False, default="Europe/Madrid")

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class RedditSequenceRun(Base):
    __tablename__ = "reddit_sequence_runs"

    id = Column(Integer, primary_key=True, index=True)
    sequence_id = Column(Integer, ForeignKey("reddit_sequences.id", ondelete="CASCADE"), index=True, nullable=False)
    lead_id = Column(Integer, ForeignKey("reddit_leads.id", ondelete="CASCADE"), index=True, nullable=False)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), index=True, nullable=True)

    current_node_id = Column(String(80), nullable=True)
    state = Column(String(40), nullable=False, default="scheduled")
    # scheduled | waiting | awaiting_approval | completed | failed | cancelled

    run_state_json = Column(JSON, nullable=True)
    last_error = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


# ── Conversations / Messages / Comments / Actions ───────


class RedditConversation(Base):
    __tablename__ = "reddit_conversations"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), index=True, nullable=False)

    counterpart_username = Column(String(160), nullable=False, index=True)
    thread_kind = Column(String(20), nullable=False, default="pm")
    # pm (private message) | chat (modern chat)
    reddit_thread_id = Column(String(80), nullable=True, index=True)

    subject = Column(String(300), nullable=True)
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_message_from = Column(String(12), nullable=True)  # "us" | "them"
    state = Column(String(40), nullable=False, default="open")  # open | archived | blocked

    crm_stage_id = Column(Integer, ForeignKey("crm_stages.id", ondelete="SET NULL"), nullable=True)
    lead_id = Column(Integer, ForeignKey("reddit_leads.id", ondelete="SET NULL"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class RedditMessageDraft(Base):
    __tablename__ = "reddit_message_drafts"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("reddit_conversations.id", ondelete="CASCADE"), index=True, nullable=False)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), index=True, nullable=True)

    body = Column(Text, nullable=False, default="")
    ai_meta_json = Column(JSON, nullable=True)
    context_ref = Column(String(400), nullable=True)

    review_state = Column(String(40), nullable=False, default="pending_review")
    # pending_review | approved | sent | failed | skipped | rejected
    review_mode = Column(String(20), nullable=False, default="manual")  # manual | semi | auto
    scheduled_for = Column(DateTime(timezone=True), nullable=True, index=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    reason_code = Column(String(80), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class RedditMessage(Base):
    """Сообщения в диалоге: входящие из /message/inbox и отправленные исходящие."""

    __tablename__ = "reddit_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("reddit_conversations.id", ondelete="CASCADE"), index=True, nullable=False)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), index=True, nullable=True)

    direction = Column(String(8), nullable=False)  # "in" | "out"
    body = Column(Text, nullable=False, default="")
    subject = Column(String(300), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    reddit_message_id = Column(String(64), unique=True, nullable=True, index=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    draft_id = Column(Integer, ForeignKey("reddit_message_drafts.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class RedditCommentDraft(Base):
    __tablename__ = "reddit_comment_drafts"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), index=True, nullable=True)

    target_ref = Column(String(400), nullable=False)  # permalink поста / комментария
    target_kind = Column(String(20), nullable=False, default="comment")  # post | comment

    body = Column(Text, nullable=False, default="")
    ai_meta_json = Column(JSON, nullable=True)

    review_state = Column(String(40), nullable=False, default="pending_review")
    review_mode = Column(String(20), nullable=False, default="manual")
    scheduled_for = Column(DateTime(timezone=True), nullable=True, index=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    reddit_comment_id = Column(String(40), nullable=True)  # после публикации

    toxicity_risk_score = Column(Float, nullable=True)
    last_error = Column(Text, nullable=True)
    reason_code = Column(String(80), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class RedditActionAttempt(Base):
    """История попыток выполнить действие (DM/comment/chat/etc)."""

    __tablename__ = "reddit_action_attempts"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), index=True, nullable=False)

    action_kind = Column(String(40), nullable=False, index=True)  # send_dm | send_chat | publish_comment | upvote | ...
    target_ref = Column(String(400), nullable=True)
    source_id = Column(Integer, nullable=True)  # message_draft / comment_draft / campaign_queue id
    source_kind = Column(String(40), nullable=True)

    attempted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    outcome = Column(String(20), nullable=False, default="pending")  # pending | ok | failed | skipped | rate_limited
    reason_code = Column(String(80), nullable=True)
    detail = Column(Text, nullable=True)
    payload_json = Column(JSON, nullable=True)


# ── Compliance / Rate-limit ─────────────────────────────


class RedditComplianceEvent(Base):
    __tablename__ = "reddit_compliance_events"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), index=True, nullable=True)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), nullable=True, index=True)

    event_type = Column(String(60), nullable=False, index=True)
    # soft_warning | hard_stop | rate_limit_hit | chat_restricted | karma_threshold_breach | ...
    severity = Column(String(20), nullable=False, default="info")  # info | warn | error
    payload_json = Column(JSON, nullable=True)
    at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


class RedditRateLimitState(Base):
    __tablename__ = "reddit_rate_limit_state"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    bucket = Column(String(40), nullable=False, index=True)  # api | dm | chat | comment

    remaining = Column(Integer, nullable=True)
    reset_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "bucket", name="uq_reddit_rate_limit_account_bucket"),
    )


__all__ = [
    "RedditAccount",
    "RedditActionAttempt",
    "RedditCampaign",
    "RedditCampaignQueue",
    "RedditChatCapability",
    "RedditComment",
    "RedditCommentDraft",
    "RedditComplianceEvent",
    "RedditConversation",
    "RedditLead",
    "RedditMessageDraft",
    "RedditPost",
    "RedditRateLimitState",
    "RedditReadinessProbe",
    "RedditRecentActivitySnapshot",
    "RedditSequence",
    "RedditSequenceRun",
    "RedditSubreddit",
    "RedditTemplate",
    "RedditUserProfile",
]
