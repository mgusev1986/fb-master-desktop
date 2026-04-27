"""Все модели SQLAlchemy для FB Master."""

from __future__ import annotations

import uuid
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


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ── Администраторы ───────────────────────────────────

class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255), nullable=True)
    picture_url = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    last_login_at = Column(DateTime, nullable=True)


# ── Организации (мультитенантность: сотни/тысячи пользователей на одном сервере) ──

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(120), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utcnow)


class OrganizationMember(Base):
    __tablename__ = "organization_members"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("admin_users.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False, default="owner")
    created_at = Column(DateTime, default=_utcnow)

    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_member_user"),)


# ── Ключи доступа (локальная копия / десктоп: один ключ ≈ одно устройство) ──

class AccessKey(Base):
    __tablename__ = "access_keys"

    id = Column(Integer, primary_key=True)
    key_hash = Column(String(128), unique=True, nullable=False, index=True)
    # Расшифровка только владельцем платформы (SECRET_KEY); для отображения в админке «Показать».
    key_plain_enc = Column(Text, nullable=True)
    label = Column(String(255), nullable=True)
    device_fingerprint = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow)
    activated_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True, index=True)
    # UTC; NULL — без срока. Считается от момента выдачи ключа (создания записи).
    expires_at = Column(DateTime, nullable=True, index=True)


# ── Экземпляры программы (десктоп / кабинет): ID установки и последняя активность ──


class ClientMachinePresence(Base):
    """
    Одна строка на файл client_installation_id в DATA_DIR.
    На VPS с общей PostgreSQL даёт владельцу платформы список машин и «онлайн».
    """

    __tablename__ = "client_machine_presence"

    installation_id = Column(String(40), primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    admin_user_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)
    # Заполняется при отчёте с десктопа на VPS (сопоставление с access_keys по device_fingerprint).
    access_key_id = Column(Integer, ForeignKey("access_keys.id"), nullable=True, index=True)
    hostname = Column(String(255), nullable=True)
    desktop_app_version = Column(String(64), nullable=True)
    last_seen_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow)


# ── Оплата продления доступа (NOWPayments) ───────────

class BillingRenewalOrder(Base):
    __tablename__ = "billing_renewal_orders"

    id = Column(Integer, primary_key=True)
    # Тот же id уходит в NOWPayments как order_id и в success_url для опроса статуса
    np_order_id = Column(String(80), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    duration_days = Column(Integer, nullable=False)
    device_fingerprint = Column(String(128), nullable=True)
    price_amount = Column(String(32), nullable=False)
    price_currency = Column(String(16), nullable=False, default="usd")
    np_invoice_id = Column(String(64), nullable=True, index=True)
    np_payment_id = Column(String(64), nullable=True, index=True)
    # pending | invoice_created | paid_ready | fulfilled | failed
    status = Column(String(24), nullable=False, default="pending")
    last_np_status = Column(String(32), nullable=True)
    access_key_id = Column(Integer, ForeignKey("access_keys.id"), nullable=True)
    # Одноразовая выдача ключа после оплаты (Fernet), очищается после первого успешного /status
    pending_plain_key_enc = Column(Text, nullable=True)


# ── Доноры ───────────────────────────────────────────

class Donor(Base):
    __tablename__ = "donors"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    url = Column(String(512), nullable=False)
    name = Column(String(255), nullable=True)
    slug = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    people = relationship("Person", back_populates="donor", lazy="dynamic")


# ── Импорт ───────────────────────────────────────────

class ImportBatch(Base):
    __tablename__ = "import_batches"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    filename = Column(String(512), nullable=False)
    donor_id = Column(Integer, ForeignKey("donors.id"), nullable=True)
    rows_total = Column(Integer, default=0)
    rows_new = Column(Integer, default=0)
    rows_updated = Column(Integer, default=0)
    rows_skipped = Column(Integer, default=0)
    # Список пропусков при ручном импорте CSV/XLSX: {reason, url_raw, name, canonical?, person_id?}
    skip_details_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    # Время последнего прогона парсера по этому донору (для одной общей партии parser:…)
    updated_at = Column(DateTime, nullable=True)


# ── База контактов (таблица people) ─────────────────

class Person(Base):
    __tablename__ = "people"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    # Глобально уникальный URL (упрощает миграцию SQLite; на PostgreSQL при необходимости — составной ключ org+url)
    canonical_url = Column(String(512), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    donor_id = Column(Integer, ForeignKey("donors.id"), nullable=True)
    import_batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=True)
    raw_meta = Column(JSON, nullable=True)

    crm_stage = Column(String(50), default="new")
    crm_notes = Column(Text, nullable=True)
    crm_stage_changed_at = Column(DateTime, nullable=True)
    # Отложенное первое/возобновляющее сообщение в Messenger (воронка «Написать позже» + AI)
    messenger_followup_at = Column(DateTime, nullable=True)
    messenger_followup_prompt = Column(Text, nullable=True)
    messenger_followup_status = Column(String(20), nullable=True)  # pending | processing | done | failed | cancelled
    messenger_followup_last_error = Column(String(512), nullable=True)

    parsed_at = Column(DateTime, nullable=True)
    # True — в списке друзей FB показан плейсхолдер закрытого/скрытого профиля (напр. «Facebook User»).
    fb_profile_restricted = Column(Boolean, default=False, nullable=False)
    language_segment = Column(String(20), nullable=True)
    language_confidence = Column(Integer, nullable=True)
    language_checked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    donor = relationship("Donor", back_populates="people")
    crm_activities = relationship("CRMActivity", back_populates="person", lazy="dynamic")

    __table_args__ = (
        Index("ix_people_crm_stage", "crm_stage"),
        Index("ix_people_language_segment", "language_segment"),
    )


# ── Стадии воронки CRM (slug = Person.crm_stage) ─────

class CRMStage(Base):
    __tablename__ = "crm_stages"

    id = Column(Integer, primary_key=True)
    slug = Column(String(50), unique=True, nullable=False, index=True)
    label = Column(String(255), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    is_builtin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_utcnow)


# ── Facebook-аккаунты ────────────────────────────────

class FBAccount(Base):
    __tablename__ = "fb_accounts"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    label = Column(String(255), nullable=False)
    profile_dir = Column(String(512), nullable=False)
    proxy_enabled = Column(Boolean, default=False)
    proxy_url = Column(String(512), nullable=True)
    proxy_username = Column(String(255), nullable=True)
    proxy_password = Column(String(255), nullable=True)
    proxy_tunnel_blocked = Column(Boolean, nullable=False, default=False)
    proxy_tunnel_blocked_at = Column(DateTime, nullable=True)
    proxy_tunnel_last_error = Column(String(512), nullable=True)
    proxy_tunnel_last_check_at = Column(DateTime, nullable=True)
    # Срок аренды прокси: дата/время окончания у провайдера (UTC); остановка автоматизации за 1 ч до этого момента.
    proxy_lease_ends_at = Column(DateTime, nullable=True)
    # Legacy (до proxy_lease_ends_at): покупка + дни — читается миграцией и сервисом proxy_lease для старых строк.
    proxy_lease_purchased_at = Column(DateTime, nullable=True)
    proxy_lease_days = Column(Integer, nullable=True)
    session_ok = Column(Boolean, nullable=True)
    last_login_check_at = Column(DateTime, nullable=True)
    login_blocked_at = Column(DateTime, nullable=True)
    login_blocked_reason = Column(String(512), nullable=True)
    # Резерв сессии Playwright (cookies + origins) в БД — дублирует профиль на диске
    session_state_json = Column(Text, nullable=True)
    session_saved_at = Column(DateTime, nullable=True)
    # Автоимпорт / автологин (пароль и TOTP — Fernet, см. fb_credentials_crypto)
    fb_login_username = Column(String(255), nullable=True)
    enc_password = Column(Text, nullable=True)
    enc_totp_secret = Column(Text, nullable=True)
    birth_hint = Column(String(64), nullable=True)
    # Профиль «антидетекта» для Playwright (locale / TZ / UA / viewport)
    stealth_locale = Column(String(32), nullable=True)
    stealth_timezone_id = Column(String(64), nullable=True)
    stealth_user_agent = Column(String(512), nullable=True)
    stealth_viewport_w = Column(Integer, nullable=True)
    stealth_viewport_h = Column(Integer, nullable=True)
    # Опционально: подключение Playwright по CDP к внешнему антидетект-браузеру (не встроенный Chromium)
    playwright_cdp_url = Column(String(512), nullable=True)
    # 1..3 — одновременно в работе (Мессенджер, парсер, прогрев и т.д.); NULL — только ожидание / настройка входа
    active_slot = Column(Integer, nullable=True, index=True)
    # «Мягкий» многодневный прогрев купленных аккаунтов (раздел «Прогрев аккаунтов»): cold | warming | ready
    readiness_status = Column(String(20), nullable=False, default="cold", index=True)
    # План и прогресс автопрогрева: duration_days, started_at, ends_at, last_tick_at, ticks_total, day_key, ticks_calendar_day
    natural_warmup_json = Column(JSON, nullable=True)
    # Скрыть карточку на /account-warming (аккаунт остаётся в «Аккаунтах»; прогрев при скрытии останавливается).
    natural_warmup_page_hidden = Column(Boolean, nullable=False, default=False, index=True)
    # Черновик настроек раздела «Оформление аккаунтов» (источник внешнего вида, посты, расписание, ИИ).
    account_branding_draft_json = Column(JSON, nullable=True)
    # Mutex: рассылка держит общий дисковый профиль с встроенным Messenger (Desktop, FB_MASTER_OUTREACH_SHARED_PROFILE).
    outreach_shared_profile_lock_job_id = Column(Integer, nullable=True, index=True)
    outreach_shared_profile_locked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


# ── Шаблоны сообщений ────────────────────────────────

class Template(Base):
    __tablename__ = "templates"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    category = Column(String(50), default="outreach")
    # Несколько текстов в body через VARIANT_SPLIT_LINE; как выбирать при рассылке:
    variant_pick_mode = Column(String(20), default="random")  # random | sequential
    variant_cursor = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# ── «Уже контактировали» ─────────────────────────────

class ContactedPerson(Base):
    __tablename__ = "contacted_people"

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=False)
    canonical_url = Column(String(512), nullable=False, index=True)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=False)
    first_contacted_at = Column(DateTime, default=_utcnow)
    last_message_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("person_id", "fb_account_id", name="uq_contacted_person_account"),
    )


class OrganizationContactedPerson(Base):
    """
    Факт контакта на уровне кабинета (organization): не привязан к живому fb_account_id,
    чтобы пропуски рассылки и воронка не терялись после удаления FB-аккаунта.
    """

    __tablename__ = "organization_contacted_people"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=False, index=True)
    canonical_url = Column(String(512), nullable=False)
    first_contacted_at = Column(DateTime, default=_utcnow)
    last_message_at = Column(DateTime, nullable=True)
    # Без FK: последний известный аккаунт мог быть удалён из fb_accounts.
    last_fb_account_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "person_id", name="uq_org_contacted_person"),
    )


class InstallationContactedProfileUrl(Base):
    """
    Глобальный по установке (вся БД) реестр канонических URL профиля FB, которым уже уходил исходящий контакт.
    Не ссылается на people.id: сохраняет пропуски после удаления/переимпорта контактов (новый person_id).
    """

    __tablename__ = "installation_contacted_profile_urls"

    id = Column(Integer, primary_key=True)
    canonical_url = Column(String(512), nullable=False, unique=True, index=True)
    first_contacted_at = Column(DateTime, default=_utcnow)
    last_message_at = Column(DateTime, nullable=True)

    __table_args__ = ()


# ── Суточные лимиты действий по FB-аккаунту (ротация рассылки / прогрева / сценариев) ──


class FBAccountDayUsage(Base):
    """Счётчики за календарный день UTC (сброс при смене даты)."""

    __tablename__ = "fb_account_day_usage"

    id = Column(Integer, primary_key=True)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=False, index=True)
    usage_date = Column(String(10), nullable=False)  # YYYY-MM-DD UTC
    outreach_actions = Column(Integer, default=0, nullable=False)
    warmup_actions = Column(Integer, default=0, nullable=False)
    sequence_actions = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("fb_account_id", "usage_date", name="uq_fb_account_usage_date"),
    )


# ── Задачи (jobs) и события ──────────────────────────

class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    job_type = Column(String(50), nullable=False, index=True)
    status = Column(String(20), default="queued", index=True)
    correlation_id = Column(String(36), default=_new_uuid, index=True)
    config_snapshot = Column(JSON, nullable=True)
    progress_json = Column(JSON, nullable=True)
    error_summary = Column(Text, nullable=True)
    created_by_admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    events = relationship("JobEvent", back_populates="job", lazy="dynamic")

    __table_args__ = (
        Index("ix_jobs_type_started", "job_type", "started_at"),
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    event_type = Column(String(80), nullable=False)
    severity = Column(String(10), default="info")
    person_id = Column(Integer, ForeignKey("people.id"), nullable=True)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=True)
    outcome = Column(String(10), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    payload = Column(JSON, nullable=True)
    ts = Column(DateTime, default=_utcnow)

    job = relationship("Job", back_populates="events")

    __table_args__ = (
        Index("ix_job_events_job_ts", "job_id", "ts"),
    )


# ── CRM-активности ───────────────────────────────────

class CRMActivity(Base):
    __tablename__ = "crm_activities"

    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=False)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=True)
    activity_type = Column(String(50), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    person = relationship("Person", back_populates="crm_activities")

    __table_args__ = (
        Index("ix_crm_activities_person", "person_id", "created_at"),
    )


# ── Рассылка (кампания + очередь) ─────────────────────

class OutreachCampaign(Base):
    __tablename__ = "outreach_campaigns"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(20), default="draft")
    fb_account_ids = Column(JSON, nullable=True)
    person_ids = Column(JSON, nullable=True)
    template_id = Column(Integer, ForeignKey("templates.id"), nullable=True)
    messages_per_account = Column(Integer, default=20)
    config = Column(JSON, nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class OutreachQueue(Base):
    __tablename__ = "outreach_queue"

    id = Column(Integer, primary_key=True)
    outreach_campaign_id = Column(Integer, ForeignKey("outreach_campaigns.id"), nullable=True, index=True)
    campaign_name = Column(String(255), nullable=True)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=False)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=False)
    template_id = Column(Integer, ForeignKey("templates.id"), nullable=True)
    message_text = Column(Text, nullable=True)
    like_first = Column(Boolean, default=False)
    add_friend_first = Column(Boolean, default=False)
    # «Реакция на Reel/Story получателя перед ЛС» — генерирует уведомление в Messenger
    # получателя; используется в т.ч. как мягкий обход E2EE-pending (пара отправитель ↔
    # получатель, где получатель ещё не открывал новый Messenger).
    react_reel_first = Column(Boolean, default=False)
    status = Column(String(20), default="queued")
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    processed_at = Column(DateTime, nullable=True)


# ── Прогрев: кампания + очередь ───────────────────────

class WarmupCampaign(Base):
    __tablename__ = "warmup_campaigns"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(20), default="draft")
    fb_account_ids = Column(JSON, nullable=True)
    person_ids = Column(JSON, nullable=True)
    actions_per_account = Column(Integer, default=10)
    config = Column(JSON, nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class WarmupQueue(Base):
    __tablename__ = "warmup_queue"

    id = Column(Integer, primary_key=True)
    warmup_campaign_id = Column(Integer, ForeignKey("warmup_campaigns.id"), nullable=True, index=True)
    campaign_name = Column(String(255), nullable=True)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=False)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=False)
    actions = Column(JSON, nullable=True)
    status = Column(String(20), default="queued")
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    processed_at = Column(DateTime, nullable=True)


# ── Сценарии по дням ──────────────────────────────────

class SequenceCampaign(Base):
    __tablename__ = "sequence_campaigns"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(20), default="draft")
    fb_account_ids = Column(JSON, nullable=True)
    person_filter = Column(JSON, nullable=True)
    config = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    steps = relationship("SequenceStep", back_populates="campaign", order_by="SequenceStep.step_index")


class SequenceStep(Base):
    __tablename__ = "sequence_steps"

    id = Column(Integer, primary_key=True)
    sequence_id = Column(Integer, ForeignKey("sequence_campaigns.id"), nullable=False)
    step_index = Column(Integer, nullable=False)
    day_offset = Column(Integer, nullable=False, default=0)
    action_type = Column(String(50), nullable=False)
    config = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    campaign = relationship("SequenceCampaign", back_populates="steps")


class SequenceEnrollment(Base):
    __tablename__ = "sequence_enrollments"

    id = Column(Integer, primary_key=True)
    sequence_id = Column(Integer, ForeignKey("sequence_campaigns.id"), nullable=False)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=False)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=True, index=True)
    current_step_index = Column(Integer, default=0)
    status = Column(String(20), default="active")
    last_run_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


# ── Мессенджер ────────────────────────────────────────

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    fb_account_id = Column(Integer, ForeignKey("fb_accounts.id"), nullable=False)
    peer_url = Column(String(512), nullable=True)
    peer_name = Column(String(255), nullable=True)
    avatar_url = Column(String(512), nullable=True)
    last_snippet = Column(Text, nullable=True)
    last_at = Column(DateTime, nullable=True)
    unread_count = Column(Integer, default=0)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=True)
    is_archived = Column(Boolean, default=False, nullable=False)
    is_pinned = Column(Boolean, default=False, nullable=False)
    local_unread = Column(Boolean, default=False, nullable=False)
    notif_muted_forever = Column(Boolean, default=False, nullable=False)
    notif_muted_until = Column(DateTime, nullable=True)
    ai_assistant_enabled = Column(Boolean, default=False, nullable=False)
    ai_extra_instructions = Column(Text, nullable=True)
    ai_last_transcript_fingerprint = Column(String(64), nullable=True)
    ai_reply_steps_used = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_utcnow)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    direction = Column(String(10), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class MessengerSendQueue(Base):
    """Очередь исходящих из UI: одна сессия Playwright — сообщения обрабатываются по FIFO."""

    __tablename__ = "messenger_send_queue"
    __table_args__ = (Index("ix_messenger_send_queue_status_id", "status", "id"),)

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="queued")  # queued, processing, sent, failed
    messenger_job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    error_summary = Column(Text, nullable=True)
    created_by_admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    processed_at = Column(DateTime, nullable=True)
    ai_transcript_fingerprint = Column(String(64), nullable=True)


class MessengerAIMemoryChunk(Base):
    """Фрагменты переписки для RAG (поиск по словам + приоритет текущего чата)."""

    __tablename__ = "messenger_ai_memory_chunks"
    __table_args__ = (
        Index("ix_m2_mem_org_conv", "organization_id", "conversation_id"),
        Index("ix_m2_mem_org_person", "organization_id", "person_id"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=True, index=True)
    source = Column(String(32), nullable=False)  # message_span | summary
    text = Column(Text, nullable=False)
    first_message_id = Column(Integer, nullable=True)
    last_message_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class MessengerAIConversationSummary(Base):
    """Сжатое представление диалога для самоанализа и подсказок."""

    __tablename__ = "messenger_ai_conversation_summaries"

    conversation_id = Column(Integer, ForeignKey("conversations.id"), primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    summary_text = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class MessengerAIPromptDraft(Base):
    """Черновик правки глобального промпта после самоанализа (применяется вручную)."""

    __tablename__ = "messenger_ai_prompt_drafts"
    __table_args__ = (Index("ix_m2_draft_org_status", "organization_id", "status"),)

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)
    source_conversation_ids = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending | merged | dismissed
    created_at = Column(DateTime, default=_utcnow)
    resolved_at = Column(DateTime, nullable=True)


# ── AI-агент ──────────────────────────────────────────

class AIAgentRun(Base):
    __tablename__ = "ai_agent_runs"

    id = Column(Integer, primary_key=True)
    provider = Column(String(20), nullable=False)
    model = Column(String(100), nullable=False)
    context_type = Column(String(20), nullable=False)
    person_id = Column(Integer, ForeignKey("people.id"), nullable=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True, index=True)
    tokens_prompt = Column(Integer, nullable=True)
    tokens_completion = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    outcome = Column(String(20), nullable=True)
    error = Column(Text, nullable=True)
    input_preview = Column(Text, nullable=True)
    output_preview = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


# ── Автопоиск аудитории (Discovery) ───────────────────

class DiscoveryTask(Base):
    __tablename__ = "discovery_tasks"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    keywords = Column(Text, nullable=False)
    search_types = Column(JSON, nullable=False)
    max_results_per_type = Column(Integer, default=50)
    language_hint = Column(String(10), nullable=True)
    status = Column(String(20), default="pending")
    results_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)
    ended_at = Column(DateTime, nullable=True)

    results = relationship("DiscoveryResult", back_populates="task", cascade="all, delete-orphan")


class DiscoveryResult(Base):
    __tablename__ = "discovery_results"
    __table_args__ = (
        Index("ix_disc_res_task_type", "task_id", "result_type"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("discovery_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    result_type = Column(String(20), nullable=False)
    url = Column(String(512), nullable=False)
    name = Column(String(512), nullable=True)
    description = Column(Text, nullable=True)
    member_count = Column(Integer, nullable=True)
    category = Column(String(255), nullable=True)
    relevance_score = Column(Integer, nullable=True)
    is_approved = Column(Boolean, default=False)
    donor_id = Column(Integer, ForeignKey("donors.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    task = relationship("DiscoveryTask", back_populates="results")


# ── Настройки (key-value JSON) ────────────────────────

class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# Подписи и порядок стадий воронки — в таблице CRMStage (сиды: crm_stages_registry.BUILTIN_CRM_STAGES).
