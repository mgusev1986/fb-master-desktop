"""SQLAlchemy engine, session и базовый класс моделей."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import DATABASE_URL


def _is_sqlite() -> bool:
    return (DATABASE_URL or "").strip().lower().startswith("sqlite")


def _postgres_connect_args(url: str) -> dict:
    """
    Supabase / облачный Postgres:
    - SSL (если не задано в URI).
    - Transaction pooler (Supavisor / PgBouncer): порт 6543 — в т.ч. на хосте db.*.supabase.co;
      shared pooler session mode — *.pooler.supabase.com. Для transaction mode нужно
      отключить server-side prepared statements (prepare_threshold=None).
    """
    args: dict = {}
    low = (url or "").lower()
    try:
        parsed = urlparse(url.replace("postgresql+psycopg", "postgresql", 1))
        qs_keys = {k.lower() for k in parse_qs(parsed.query).keys()}
    except Exception:
        qs_keys = set()
    has_ssl_param = "sslmode" in qs_keys or "ssl" in qs_keys or "sslmode=" in low
    if not has_ssl_param and (
        "supabase.co" in low
        or "supabase.com" in low
        or os.getenv("DATABASE_SSL_REQUIRE", "").lower() in ("1", "true", "yes")
    ):
        args["sslmode"] = "require"
    # Transaction mode: 6543 (в т.ч. postgres://...@db.xxx.supabase.co:6543/...)
    # + принудительно из .env, если строка нестандартная
    if (
        ":6543" in low
        or "pooler.supabase.com" in low
        or os.getenv("DATABASE_DISABLE_PREPARED_STATEMENTS", "").lower()
        in ("1", "true", "yes")
    ):
        args["prepare_threshold"] = None
    # Таймаут TCP (сек.); для облака по умолчанию 15, иначе только если задано явно
    ct_raw = (os.getenv("DB_CONNECT_TIMEOUT") or "").strip()
    if ct_raw.isdigit():
        args["connect_timeout"] = int(ct_raw)
    elif "supabase.co" in low or "supabase.com" in low:
        args["connect_timeout"] = int(os.getenv("DB_CONNECT_TIMEOUT_SUPABASE", "15"))
    return args


if _is_sqlite():
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    # WAL + busy_timeout: критично для desktop-сборки, где параллельно работают
    # ~9 фоновых воркеров (parser, proxy_health_guard, client_presence, reddit/
    # twitter/linkedin/instagram outreach + inbox + autoresponder и т.д.). Без
    # WAL каждый write эксклюзивно лочит БД, а busy_timeout=0 даёт мгновенный
    # `database is locked` вместо ожидания. Парсер на каждом раунде делает
    # progress-commit и тонул на этих локах — отсюда жалобы на «медленный
    # turbo, раньше летало».
    from sqlalchemy import event as _sa_event

    @_sa_event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            # busy_timeout 15s (вместо 5s): на больших парсингах ловим
            # database-is-locked при parallel writes (parser progress +
            # proxy_health_guard + client_presence + module workers). 15с
            # перекрывают типичный peak нагрузки на SQLite в десктопе.
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()
else:
    _pg_connect = _postgres_connect_args(DATABASE_URL)
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
        connect_args=_pg_connect,
    )

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def using_postgresql() -> bool:
    """True, если активен PostgreSQL (в т.ч. Supabase), а не SQLite."""
    return not _is_sqlite()


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: одна сессия на запрос."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_columns(
    table_name: str,
    cols_sqlite: tuple,
    cols_pg: tuple,
    extra_sql_sqlite: list = None,
    extra_sql_pg: list = None,
) -> None:
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            for col, typ in cols_sqlite:
                if col not in names:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col} {typ}"))
            if extra_sql_sqlite:
                for sql in extra_sql_sqlite:
                    conn.execute(text(sql))
    else:
        with engine.begin() as conn:
            for col, typ in cols_pg:
                r = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = :tn "
                        "AND column_name = :cn"
                    ),
                    {"tn": table_name, "cn": col},
                ).scalar()
                if not r:
                    conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" {typ}'))
            if extra_sql_pg:
                for sql in extra_sql_pg:
                    conn.execute(text(sql))


def _migrate_fb_accounts_session_columns() -> None:
    """SQLite/PG: добавить колонки резервной сессии к существующей таблице fb_accounts."""
    _add_columns("fb_accounts",
                 (("session_state_json", "TEXT"), ("session_saved_at", "DATETIME")),
                 (("session_state_json", "JSONB"), ("session_saved_at", "TIMESTAMPTZ")))


def _migrate_warmup_queue_campaign_id() -> None:
    _add_columns("warmup_queue",
                 (("warmup_campaign_id", "INTEGER REFERENCES warmup_campaigns(id)"),),
                 (("warmup_campaign_id", "INTEGER REFERENCES warmup_campaigns(id)"),))


def _migrate_sequence_enrollment_fb_account() -> None:
    _add_columns("sequence_enrollments",
                 (("fb_account_id", "INTEGER REFERENCES fb_accounts(id)"),),
                 (("fb_account_id", "INTEGER REFERENCES fb_accounts(id)"),))


def _migrate_outreach_queue_campaign_id() -> None:
    _add_columns("outreach_queue",
                 (("outreach_campaign_id", "INTEGER REFERENCES outreach_campaigns(id)"),),
                 (("outreach_campaign_id", "INTEGER REFERENCES outreach_campaigns(id)"),))


def _migrate_outreach_queue_react_reel_first() -> None:
    """Колонка `react_reel_first` (Boolean) — «реакция на Reel/Story перед ЛС» в кампании."""
    _add_columns(
        "outreach_queue",
        (("react_reel_first", "BOOLEAN DEFAULT 0"),),
        (("react_reel_first", "BOOLEAN DEFAULT FALSE"),),
    )


def _migrate_ai_agent_run_previews() -> None:
    _add_columns("ai_agent_runs",
                 (("input_preview", "TEXT"), ("output_preview", "TEXT")),
                 (("input_preview", "TEXT"), ("output_preview", "TEXT")))


def _migrate_ai_agent_runs_conversation_id() -> None:
    """Связь прогона LLM с чатом Messenger (самоанализ / отладка)."""
    _add_columns(
        "ai_agent_runs",
        (("conversation_id", "INTEGER REFERENCES conversations(id)"),),
        (("conversation_id", "INTEGER REFERENCES conversations(id)"),),
    )


def _migrate_jobs_progress_json() -> None:
    _add_columns("jobs",
                 (("progress_json", "TEXT"),),
                 (("progress_json", "JSONB"),))


def _migrate_templates_variant_columns() -> None:
    _add_columns("templates",
                 (("variant_pick_mode", "VARCHAR(20) DEFAULT 'random'"), ("variant_cursor", "INTEGER DEFAULT 0")),
                 (("variant_pick_mode", "VARCHAR(20) DEFAULT 'random'"), ("variant_cursor", "INTEGER DEFAULT 0")))


def _migrate_people_fb_profile_restricted() -> None:
    _add_columns("people",
                 (("fb_profile_restricted", "BOOLEAN NOT NULL DEFAULT 0"),),
                 (("fb_profile_restricted", "BOOLEAN NOT NULL DEFAULT false"),))


def _migrate_people_language_columns() -> None:
    _add_columns("people",
                 (("language_segment", "VARCHAR(20)"), ("language_confidence", "INTEGER"), ("language_checked_at", "DATETIME")),
                 (("language_segment", "VARCHAR(20)"), ("language_confidence", "INTEGER"), ("language_checked_at", "TIMESTAMPTZ")),
                 extra_sql_sqlite=["CREATE INDEX IF NOT EXISTS ix_people_language_segment ON people (language_segment)"],
                 extra_sql_pg=["CREATE INDEX IF NOT EXISTS ix_people_language_segment ON people (language_segment)"])


def _migrate_people_messenger_followup_columns() -> None:
    """Плановое AI-сообщение в Messenger из воронки («Написать позже»)."""
    _add_columns(
        "people",
        (
            ("messenger_followup_at", "DATETIME"),
            ("messenger_followup_prompt", "TEXT"),
            ("messenger_followup_status", "VARCHAR(20)"),
            ("messenger_followup_last_error", "VARCHAR(512)"),
        ),
        (
            ("messenger_followup_at", "TIMESTAMPTZ"),
            ("messenger_followup_prompt", "TEXT"),
            ("messenger_followup_status", "VARCHAR(20)"),
            ("messenger_followup_last_error", "VARCHAR(512)"),
        ),
        extra_sql_sqlite=[
            "CREATE INDEX IF NOT EXISTS ix_people_messenger_followup_pending "
            "ON people (messenger_followup_status, messenger_followup_at)"
        ],
        extra_sql_pg=[
            "CREATE INDEX IF NOT EXISTS ix_people_messenger_followup_pending "
            "ON people (messenger_followup_status, messenger_followup_at)"
        ],
    )


def _migrate_import_batches_updated_at() -> None:
    _add_columns("import_batches",
                 (("updated_at", "DATETIME"),),
                 (("updated_at", "TIMESTAMPTZ"),))


def _migrate_conversations_archived() -> None:
    _add_columns("conversations",
                 (("is_archived", "BOOLEAN NOT NULL DEFAULT 0"),),
                 (("is_archived", "BOOLEAN NOT NULL DEFAULT false"),))


def _migrate_conversations_vk_actions() -> None:
    _add_columns("conversations",
                 (("is_pinned", "BOOLEAN NOT NULL DEFAULT 0"), ("local_unread", "BOOLEAN NOT NULL DEFAULT 0"), ("notif_muted_forever", "BOOLEAN NOT NULL DEFAULT 0"), ("notif_muted_until", "DATETIME")),
                 (("is_pinned", "BOOLEAN NOT NULL DEFAULT false"), ("local_unread", "BOOLEAN NOT NULL DEFAULT false"), ("notif_muted_forever", "BOOLEAN NOT NULL DEFAULT false"), ("notif_muted_until", "TIMESTAMPTZ")))


def _migrate_conversations_messenger_ai() -> None:
    """AI-ассистент в Messenger: рубильник, доп. инструкции, отпечаток последнего отвеченного транскрипта."""
    cols_sqlite = (
        ("ai_assistant_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("ai_extra_instructions", "TEXT"),
        ("ai_last_transcript_fingerprint", "VARCHAR(64)"),
    )
    cols_pg = (
        ("ai_assistant_enabled", "BOOLEAN NOT NULL DEFAULT false"),
        ("ai_extra_instructions", "TEXT"),
        ("ai_last_transcript_fingerprint", "VARCHAR(64)"),
    )
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(conversations)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            for col, typ in cols_sqlite:
                if col not in names:
                    conn.execute(text(f"ALTER TABLE conversations ADD COLUMN {col} {typ}"))
        return
    with engine.begin() as conn:
        for col, typ in cols_pg:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'conversations' "
                    "AND column_name = :cn"
                ),
                {"cn": col},
            ).scalar()
            if r:
                continue
            conn.execute(text(f'ALTER TABLE conversations ADD COLUMN "{col}" {typ}'))


def _migrate_conversations_ai_reply_steps() -> None:
    col = "ai_reply_steps_used"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(conversations)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(
                    text("ALTER TABLE conversations ADD COLUMN ai_reply_steps_used INTEGER NOT NULL DEFAULT 0")
                )
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'conversations' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(
                text('ALTER TABLE conversations ADD COLUMN "ai_reply_steps_used" INTEGER NOT NULL DEFAULT 0')
            )


def _migrate_messenger_send_queue_ai_fingerprint() -> None:
    col = "ai_transcript_fingerprint"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(messenger_send_queue)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(text(f"ALTER TABLE messenger_send_queue ADD COLUMN {col} VARCHAR(64)"))
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'messenger_send_queue' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(text(f'ALTER TABLE messenger_send_queue ADD COLUMN "{col}" VARCHAR(64)'))


def _init_crm_stages_seed() -> None:
    """Заполнить crm_stages встроенными стадиями при первом запуске."""
    from backend.services.crm_stages_registry import seed_builtin_stages

    db = SessionLocal()
    try:
        seed_builtin_stages(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _migrate_fb_accounts_automation_columns() -> None:
    """Импорт аккаунтов, шифрованные учётные данные, пресеты stealth для Playwright."""
    cols_sqlite = (
        ("fb_login_username", "TEXT"),
        ("enc_password", "TEXT"),
        ("enc_totp_secret", "TEXT"),
        ("birth_hint", "TEXT"),
        ("stealth_locale", "TEXT"),
        ("stealth_timezone_id", "TEXT"),
        ("stealth_user_agent", "TEXT"),
        ("stealth_viewport_w", "INTEGER"),
        ("stealth_viewport_h", "INTEGER"),
        ("playwright_cdp_url", "TEXT"),
    )
    cols_pg = (
        ("fb_login_username", "VARCHAR(255)"),
        ("enc_password", "TEXT"),
        ("enc_totp_secret", "TEXT"),
        ("birth_hint", "VARCHAR(64)"),
        ("stealth_locale", "VARCHAR(32)"),
        ("stealth_timezone_id", "VARCHAR(64)"),
        ("stealth_user_agent", "VARCHAR(512)"),
        ("stealth_viewport_w", "INTEGER"),
        ("stealth_viewport_h", "INTEGER"),
        ("playwright_cdp_url", "VARCHAR(512)"),
    )
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            for col, typ in cols_sqlite:
                if col not in names:
                    conn.execute(text(f"ALTER TABLE fb_accounts ADD COLUMN {col} {typ}"))
        return
    with engine.begin() as conn:
        for col, typ in cols_pg:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                    "AND column_name = :cn"
                ),
                {"cn": col},
            ).scalar()
            if r:
                continue
            conn.execute(text(f'ALTER TABLE fb_accounts ADD COLUMN "{col}" {typ}'))


def _migrate_access_keys_expires_at() -> None:
    """Срок действия ключа (NULL = бессрочно)."""
    col = "expires_at"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(access_keys)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(text("ALTER TABLE access_keys ADD COLUMN expires_at DATETIME"))
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'access_keys' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(text('ALTER TABLE access_keys ADD COLUMN "expires_at" TIMESTAMPTZ'))


def _migrate_access_keys_key_plain_enc() -> None:
    """Зашифрованный plaintext ключа (Fernet от SECRET_KEY) для повторного показа в админке."""
    col = "key_plain_enc"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(access_keys)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(text("ALTER TABLE access_keys ADD COLUMN key_plain_enc TEXT"))
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'access_keys' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(text('ALTER TABLE access_keys ADD COLUMN "key_plain_enc" TEXT'))


def _migrate_access_keys_admin_note() -> None:
    """Пометка владельца к выданному ключу (админка)."""
    col = "admin_note"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(access_keys)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(text("ALTER TABLE access_keys ADD COLUMN admin_note TEXT"))
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'access_keys' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(text('ALTER TABLE access_keys ADD COLUMN "admin_note" TEXT'))


def _migrate_client_machine_presence_access_key() -> None:
    """Связь строки присутствия с ключом доступа (отчёт с десктопа на VPS)."""
    col = "access_key_id"
    if _is_sqlite():
        with engine.begin() as conn:
            t = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_machine_presence'"
                )
            ).scalar()
            if not t:
                return
            rows = conn.execute(text("PRAGMA table_info(client_machine_presence)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(
                    text(
                        "ALTER TABLE client_machine_presence "
                        "ADD COLUMN access_key_id INTEGER REFERENCES access_keys(id)"
                    )
                )
        return
    with engine.begin() as conn:
        t = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'client_machine_presence'"
            )
        ).scalar()
        if not t:
            return
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'client_machine_presence' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(
                text(
                    'ALTER TABLE client_machine_presence ADD COLUMN "access_key_id" INTEGER '
                    "REFERENCES access_keys(id)"
                )
            )


def _migrate_sequence_campaigns_config() -> None:
    """JSON-настройки сценария (ротация аккаунтов и др.)."""
    col = "config"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(sequence_campaigns)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(text("ALTER TABLE sequence_campaigns ADD COLUMN config TEXT"))
    else:
        with engine.begin() as conn:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'sequence_campaigns' "
                    "AND column_name = :cn"
                ),
                {"cn": col},
            ).scalar()
            if not r:
                conn.execute(text('ALTER TABLE sequence_campaigns ADD COLUMN "config" JSONB'))


def _seed_natural_warmup_topic_stopwords() -> None:
    """Один раз записать в settings стоп-слова для нейрокомментариев при прогреве (если ключа ещё нет)."""
    from backend.models import Setting
    from backend.services.natural_warmup_stopwords import (
        DEFAULT_TOPIC_STOPWORDS,
        NATURAL_WARMUP_TOPIC_STOPWORDS_KEY,
    )

    db = SessionLocal()
    try:
        row = db.get(Setting, NATURAL_WARMUP_TOPIC_STOPWORDS_KEY)
        if row is None:
            db.add(
                Setting(
                    key=NATURAL_WARMUP_TOPIC_STOPWORDS_KEY,
                    value=list(DEFAULT_TOPIC_STOPWORDS),
                )
            )
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _migrate_fb_accounts_outreach_shared_lock() -> None:
    """Mutex общего дискового профиля: рассылка vs встроенный Messenger (Desktop)."""
    _add_columns(
        "fb_accounts",
        (
            ("outreach_shared_profile_lock_job_id", "INTEGER"),
            ("outreach_shared_profile_locked_at", "DATETIME"),
        ),
        (
            ("outreach_shared_profile_lock_job_id", "INTEGER"),
            ("outreach_shared_profile_locked_at", "TIMESTAMPTZ"),
        ),
        extra_sql_sqlite=[
            "CREATE INDEX IF NOT EXISTS ix_fb_accounts_outreach_lock_job ON fb_accounts (outreach_shared_profile_lock_job_id)",
        ],
        extra_sql_pg=[
            "CREATE INDEX IF NOT EXISTS ix_fb_accounts_outreach_lock_job ON fb_accounts (outreach_shared_profile_lock_job_id)",
        ],
    )


def _migrate_fb_accounts_active_slot() -> None:
    """Слоты 1–3 для параллельной работы; NULL = ожидание. Существующим кабинетам — первые 3 аккаунта по id."""
    col = "active_slot"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(text(f"ALTER TABLE fb_accounts ADD COLUMN {col} INTEGER"))
    else:
        with engine.begin() as conn:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                    "AND column_name = :cn"
                ),
                {"cn": col},
            ).scalar()
            if not r:
                conn.execute(text(f'ALTER TABLE fb_accounts ADD COLUMN "{col}" INTEGER'))

    from backend.models import FBAccount

    db = SessionLocal()
    try:
        filled = db.query(FBAccount).filter(FBAccount.active_slot.isnot(None)).count()
        if filled > 0:
            return
        org_ids = [r[0] for r in db.query(FBAccount.organization_id).distinct().all()]
        for oid in org_ids:
            batch = (
                db.query(FBAccount)
                .filter(FBAccount.organization_id == oid)
                .order_by(FBAccount.id.asc())
                .limit(3)
                .all()
            )
            for i, acc in enumerate(batch, start=1):
                acc.active_slot = i
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _migrate_fb_accounts_proxy_tunnel_health() -> None:
    """Флаги и метки мониторинга прокси (proxy_health_guard)."""
    cols_sqlite = (
        ("proxy_tunnel_blocked", "BOOLEAN NOT NULL DEFAULT 0"),
        ("proxy_tunnel_blocked_at", "DATETIME"),
        ("proxy_tunnel_last_error", "TEXT"),
        ("proxy_tunnel_last_check_at", "DATETIME"),
    )
    cols_pg = (
        ("proxy_tunnel_blocked", "BOOLEAN NOT NULL DEFAULT false"),
        ("proxy_tunnel_blocked_at", "TIMESTAMPTZ"),
        ("proxy_tunnel_last_error", "VARCHAR(512)"),
        ("proxy_tunnel_last_check_at", "TIMESTAMPTZ"),
    )
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            for col, typ in cols_sqlite:
                if col not in names:
                    conn.execute(text(f"ALTER TABLE fb_accounts ADD COLUMN {col} {typ}"))
        return
    with engine.begin() as conn:
        for col, typ in cols_pg:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                    "AND column_name = :cn"
                ),
                {"cn": col},
            ).scalar()
            if r:
                continue
            conn.execute(text(f'ALTER TABLE fb_accounts ADD COLUMN "{col}" {typ}'))


def _migrate_fb_accounts_proxy_lease() -> None:
    """Дата покупки прокси и срок в днях (учёт запаса 1 ч в логике приложения)."""
    cols_sqlite = (
        ("proxy_lease_purchased_at", "DATETIME"),
        ("proxy_lease_days", "INTEGER"),
    )
    cols_pg = (
        ("proxy_lease_purchased_at", "TIMESTAMPTZ"),
        ("proxy_lease_days", "INTEGER"),
    )
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            for col, typ in cols_sqlite:
                if col not in names:
                    conn.execute(text(f"ALTER TABLE fb_accounts ADD COLUMN {col} {typ}"))
        return
    with engine.begin() as conn:
        for col, typ in cols_pg:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                    "AND column_name = :cn"
                ),
                {"cn": col},
            ).scalar()
            if r:
                continue
            conn.execute(text(f'ALTER TABLE fb_accounts ADD COLUMN "{col}" {typ}'))


def _migrate_fb_accounts_proxy_lease_ends_at() -> None:
    """Одна метка конца аренды (UTC). Старые «покупка + дни» переносятся в ends_at и очищаются."""
    from datetime import datetime, timedelta, timezone

    def _row_to_utc_dt(val: object) -> datetime | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            dt = val
        else:
            s = str(val).strip().replace(" ", "T", 1)
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    col_sqlite = "proxy_lease_ends_at"
    col_pg = "proxy_lease_ends_at"

    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col_sqlite not in names:
                conn.execute(text(f"ALTER TABLE fb_accounts ADD COLUMN {col_sqlite} DATETIME"))
            q = conn.execute(
                text(
                    "SELECT id, proxy_lease_purchased_at, proxy_lease_days FROM fb_accounts "
                    "WHERE proxy_lease_ends_at IS NULL AND proxy_lease_purchased_at IS NOT NULL "
                    "AND proxy_lease_days IS NOT NULL"
                )
            ).fetchall()
            for rid, pat, days in q:
                try:
                    nd = int(days)
                except (TypeError, ValueError):
                    continue
                if nd <= 0:
                    continue
                p = _row_to_utc_dt(pat)
                if not p:
                    continue
                end = p + timedelta(days=nd)
                end_store = end.astimezone(timezone.utc).replace(tzinfo=None)
                conn.execute(
                    text(
                        "UPDATE fb_accounts SET proxy_lease_ends_at = :e, "
                        "proxy_lease_purchased_at = NULL, proxy_lease_days = NULL WHERE id = :id"
                    ),
                    {"e": end_store, "id": int(rid)},
                )
        return

    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                "AND column_name = :cn"
            ),
            {"cn": col_pg},
        ).scalar()
        if not r:
            conn.execute(text(f'ALTER TABLE fb_accounts ADD COLUMN "{col_pg}" TIMESTAMPTZ'))
        q = conn.execute(
            text(
                "SELECT id, proxy_lease_purchased_at, proxy_lease_days FROM fb_accounts "
                "WHERE proxy_lease_ends_at IS NULL AND proxy_lease_purchased_at IS NOT NULL "
                "AND proxy_lease_days IS NOT NULL"
            )
        ).fetchall()
        for rid, pat, days in q:
            try:
                nd = int(days)
            except (TypeError, ValueError):
                continue
            if nd <= 0:
                continue
            p = _row_to_utc_dt(pat)
            if not p:
                continue
            end = p + timedelta(days=nd)
            conn.execute(
                text(
                    "UPDATE fb_accounts SET proxy_lease_ends_at = :e, "
                    "proxy_lease_purchased_at = NULL, proxy_lease_days = NULL WHERE id = :id"
                ),
                {"e": end, "id": int(rid)},
            )


def _migrate_fb_accounts_login_block_status() -> None:
    """Метка: Meta ограничила обычный login-flow, хотя живая сессия может ещё работать в Messenger."""
    cols_sqlite = (
        ("login_blocked_at", "DATETIME"),
        ("login_blocked_reason", "TEXT"),
    )
    cols_pg = (
        ("login_blocked_at", "TIMESTAMPTZ"),
        ("login_blocked_reason", "VARCHAR(512)"),
    )
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            for col, typ in cols_sqlite:
                if col not in names:
                    conn.execute(text(f"ALTER TABLE fb_accounts ADD COLUMN {col} {typ}"))
        return
    with engine.begin() as conn:
        for col, typ in cols_pg:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                    "AND column_name = :cn"
                ),
                {"cn": col},
            ).scalar()
            if r:
                continue
            conn.execute(text(f'ALTER TABLE fb_accounts ADD COLUMN "{col}" {typ}'))


def _migrate_fb_accounts_readiness_natural_warmup() -> None:
    """Статус готовности аккаунта и JSON плана многодневного «натурального» прогрева."""
    col_r = "readiness_status"
    col_j = "natural_warmup_json"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col_r not in names:
                conn.execute(
                    text(
                        "ALTER TABLE fb_accounts ADD COLUMN readiness_status "
                        "VARCHAR(20) NOT NULL DEFAULT 'cold'"
                    )
                )
            if col_j not in names:
                conn.execute(text("ALTER TABLE fb_accounts ADD COLUMN natural_warmup_json TEXT"))
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                "AND column_name = :cn"
            ),
            {"cn": col_r},
        ).scalar()
        if not r:
            conn.execute(
                text(
                    "ALTER TABLE fb_accounts ADD COLUMN readiness_status "
                    "VARCHAR(20) NOT NULL DEFAULT 'cold'"
                )
            )
        r2 = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                "AND column_name = :cn"
            ),
            {"cn": col_j},
        ).scalar()
        if not r2:
            conn.execute(text("ALTER TABLE fb_accounts ADD COLUMN natural_warmup_json JSONB"))


def _migrate_fb_accounts_natural_warmup_page_hidden() -> None:
    """Флаг «не показывать в разделе прогрева» (карточка убрана со страницы, аккаунт в БД сохраняется)."""
    col = "natural_warmup_page_hidden"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(
                    text(
                        f"ALTER TABLE fb_accounts ADD COLUMN {col} "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(
                text(
                    f'ALTER TABLE fb_accounts ADD COLUMN "{col}" BOOLEAN NOT NULL DEFAULT false'
                )
            )


def _migrate_fb_accounts_account_branding_draft() -> None:
    """JSON-черновик сценария оформления профиля (раздел «Оформление аккаунтов»)."""
    col = "account_branding_draft_json"
    if _is_sqlite():
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(fb_accounts)")).fetchall()
            if not rows:
                return
            names = {r[1] for r in rows}
            if col not in names:
                conn.execute(text(f"ALTER TABLE fb_accounts ADD COLUMN {col} TEXT"))
        return
    with engine.begin() as conn:
        r = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'fb_accounts' "
                "AND column_name = :cn"
            ),
            {"cn": col},
        ).scalar()
        if not r:
            conn.execute(text(f'ALTER TABLE fb_accounts ADD COLUMN "{col}" JSONB'))


def _migrate_import_batches_skip_details() -> None:
    _add_columns("import_batches",
                 (("skip_details_json", "TEXT"),),
                 (("skip_details_json", "JSONB"),))


def _migrate_organization_contacted_people() -> None:
    """
    Таблица organization_contacted_people: учёт «уже писали» на уровне кабинета,
    чтобы после удаления FB-аккаунта не терялись пропуски рассылки и воронка.
    """
    with engine.begin() as conn:
        if _is_sqlite():
            r = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='organization_contacted_people'"
                )
            ).scalar()
            if not r:
                conn.execute(
                    text(
                        """
                        CREATE TABLE organization_contacted_people (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            organization_id INTEGER NOT NULL REFERENCES organizations(id),
                            person_id INTEGER NOT NULL REFERENCES people(id),
                            canonical_url VARCHAR(512) NOT NULL,
                            first_contacted_at DATETIME,
                            last_message_at DATETIME,
                            last_fb_account_id INTEGER,
                            UNIQUE (organization_id, person_id)
                        )
                        """
                    )
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_org_contacted_org "
                    "ON organization_contacted_people (organization_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_org_contacted_person "
                    "ON organization_contacted_people (person_id)"
                )
            )
            # Идемпотентно: таблица могла появиться из create_all до этого шага.
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO organization_contacted_people (
                        organization_id, person_id, canonical_url,
                        first_contacted_at, last_message_at, last_fb_account_id
                    )
                    SELECT fa.organization_id, cp.person_id, cp.canonical_url,
                           cp.first_contacted_at, cp.last_message_at, cp.fb_account_id
                    FROM contacted_people cp
                    INNER JOIN fb_accounts fa ON fa.id = cp.fb_account_id
                    """
                )
            )
        else:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'organization_contacted_people'"
                )
            ).scalar()
            if not r:
                conn.execute(
                    text(
                        """
                        CREATE TABLE organization_contacted_people (
                            id SERIAL PRIMARY KEY,
                            organization_id INTEGER NOT NULL REFERENCES organizations(id),
                            person_id INTEGER NOT NULL REFERENCES people(id),
                            canonical_url VARCHAR(512) NOT NULL,
                            first_contacted_at TIMESTAMPTZ,
                            last_message_at TIMESTAMPTZ,
                            last_fb_account_id INTEGER,
                            CONSTRAINT uq_org_contacted_person UNIQUE (organization_id, person_id)
                        )
                        """
                    )
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_org_contacted_org "
                    "ON organization_contacted_people (organization_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_org_contacted_person "
                    "ON organization_contacted_people (person_id)"
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO organization_contacted_people (
                        organization_id, person_id, canonical_url,
                        first_contacted_at, last_message_at, last_fb_account_id
                    )
                    SELECT fa.organization_id, cp.person_id, cp.canonical_url,
                           cp.first_contacted_at, cp.last_message_at, cp.fb_account_id
                    FROM contacted_people cp
                    INNER JOIN fb_accounts fa ON fa.id = cp.fb_account_id
                    ON CONFLICT (organization_id, person_id) DO NOTHING
                    """
                )
            )


def _migrate_installation_contacted_profile_urls() -> None:
    """
    Глобальный реестр URL профилей: пропуск «уже писали» сохраняется после удаления/переимпорта people
    (новый person_id для того же Facebook URL).
    """
    with engine.begin() as conn:
        if _is_sqlite():
            r = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='installation_contacted_profile_urls'"
                )
            ).scalar()
            if not r:
                conn.execute(
                    text(
                        """
                        CREATE TABLE installation_contacted_profile_urls (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            canonical_url VARCHAR(512) NOT NULL,
                            first_contacted_at DATETIME,
                            last_message_at DATETIME,
                            UNIQUE (canonical_url)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_inst_prof_url "
                        "ON installation_contacted_profile_urls (canonical_url)"
                    )
                )
        else:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'installation_contacted_profile_urls'"
                )
            ).scalar()
            if not r:
                conn.execute(
                    text(
                        """
                        CREATE TABLE installation_contacted_profile_urls (
                            id SERIAL PRIMARY KEY,
                            canonical_url VARCHAR(512) NOT NULL,
                            first_contacted_at TIMESTAMPTZ,
                            last_message_at TIMESTAMPTZ,
                            CONSTRAINT uq_installation_contacted_profile_url UNIQUE (canonical_url)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_inst_prof_url "
                        "ON installation_contacted_profile_urls (canonical_url)"
                    )
                )

    db = SessionLocal()
    try:
        from backend.services.contacted_registry import (
            prune_orphan_installation_contacted_urls,
            sync_installation_contacted_urls_from_legacy_tables,
        )

        sync_installation_contacted_urls_from_legacy_tables(db)
        # Чинит баг до 2.66: mirror_installation_contacted_urls_for_person_ids зеркалил URL
        # ВСЕХ удаляемых people, в т.ч. никогда не контактированных. Из-за этого после
        # «удалил партию импорта → загрузил похожую базу» новые контакты сразу попадали в
        # «Уже контактировали» и блокировали рассылку. Удаляем такие ложные записи на старте.
        try:
            import logging as _logging

            removed = prune_orphan_installation_contacted_urls(db)
            if removed:
                _logging.getLogger(__name__).info(
                    "installation_contacted_profile_urls: prune removed %d orphan URLs",
                    removed,
                )
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).exception(
                "installation_contacted_profile_urls: prune failed"
            )
        db.commit()
    finally:
        db.close()


def _migrate_discovery_tables() -> None:
    """Таблицы автопоиска аудитории: discovery_tasks + discovery_results."""
    with engine.begin() as conn:
        if _is_sqlite():
            r = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_tasks'")
            ).scalar()
            if not r:
                conn.execute(
                    text(
                        """
                        CREATE TABLE discovery_tasks (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            organization_id INTEGER NOT NULL REFERENCES organizations(id),
                            job_id INTEGER REFERENCES jobs(id),
                            keywords TEXT NOT NULL,
                            search_types TEXT NOT NULL,
                            max_results_per_type INTEGER DEFAULT 50,
                            language_hint VARCHAR(10),
                            status VARCHAR(20) DEFAULT 'pending',
                            results_count INTEGER DEFAULT 0,
                            created_at DATETIME,
                            ended_at DATETIME
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_task_org ON discovery_tasks (organization_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_task_job ON discovery_tasks (job_id)"))
            r2 = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_results'")
            ).scalar()
            if not r2:
                conn.execute(
                    text(
                        """
                        CREATE TABLE discovery_results (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id INTEGER NOT NULL REFERENCES discovery_tasks(id) ON DELETE CASCADE,
                            organization_id INTEGER NOT NULL REFERENCES organizations(id),
                            result_type VARCHAR(20) NOT NULL,
                            url VARCHAR(512) NOT NULL,
                            name VARCHAR(512),
                            description TEXT,
                            member_count INTEGER,
                            category VARCHAR(255),
                            relevance_score INTEGER,
                            is_approved BOOLEAN DEFAULT 0,
                            donor_id INTEGER REFERENCES donors(id),
                            created_at DATETIME
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_res_task ON discovery_results (task_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_res_org ON discovery_results (organization_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_res_task_type ON discovery_results (task_id, result_type)"))
        else:
            r = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = 'discovery_tasks'"
                )
            ).scalar()
            if not r:
                conn.execute(
                    text(
                        """
                        CREATE TABLE discovery_tasks (
                            id SERIAL PRIMARY KEY,
                            organization_id INTEGER NOT NULL REFERENCES organizations(id),
                            job_id INTEGER REFERENCES jobs(id),
                            keywords TEXT NOT NULL,
                            search_types JSONB NOT NULL DEFAULT '[]',
                            max_results_per_type INTEGER DEFAULT 50,
                            language_hint VARCHAR(10),
                            status VARCHAR(20) DEFAULT 'pending',
                            results_count INTEGER DEFAULT 0,
                            created_at TIMESTAMPTZ,
                            ended_at TIMESTAMPTZ
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_task_org ON discovery_tasks (organization_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_task_job ON discovery_tasks (job_id)"))
            r2 = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = 'discovery_results'"
                )
            ).scalar()
            if not r2:
                conn.execute(
                    text(
                        """
                        CREATE TABLE discovery_results (
                            id SERIAL PRIMARY KEY,
                            task_id INTEGER NOT NULL REFERENCES discovery_tasks(id) ON DELETE CASCADE,
                            organization_id INTEGER NOT NULL REFERENCES organizations(id),
                            result_type VARCHAR(20) NOT NULL,
                            url VARCHAR(512) NOT NULL,
                            name VARCHAR(512),
                            description TEXT,
                            member_count INTEGER,
                            category VARCHAR(255),
                            relevance_score INTEGER,
                            is_approved BOOLEAN DEFAULT FALSE,
                            donor_id INTEGER REFERENCES donors(id),
                            created_at TIMESTAMPTZ
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_res_task ON discovery_results (task_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_res_org ON discovery_results (organization_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_disc_res_task_type ON discovery_results (task_id, result_type)"))


def _migrate_tenant_organization_columns() -> None:
    """Добавить organization_id к существующим таблицам (апгрейд с однокабинетной схемы)."""
    tenant_tables = (
        "donors",
        "people",
        "fb_accounts",
        "templates",
        "jobs",
        "import_batches",
        "outreach_campaigns",
        "warmup_campaigns",
        "sequence_campaigns",
    )
    with engine.begin() as conn:
        if _is_sqlite():
            for t in tenant_tables:
                rows = conn.execute(text(f"PRAGMA table_info({t})")).fetchall()
                if not rows:
                    continue
                names = {r[1] for r in rows}
                if "organization_id" not in names:
                    conn.execute(
                        text(f"ALTER TABLE {t} ADD COLUMN organization_id INTEGER NOT NULL DEFAULT 1")
                    )
        else:
            for t in tenant_tables:
                r = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = :tn "
                        "AND column_name = 'organization_id'"
                    ),
                    {"tn": t},
                ).scalar()
                if r:
                    continue
                conn.execute(text(f'ALTER TABLE "{t}" ADD COLUMN organization_id INTEGER NOT NULL DEFAULT 1'))


def _maybe_seed_default_organization_for_legacy() -> None:
    """Если в БД уже есть данные без организаций — создать Default (id обычно 1)."""
    from backend.models import AdminUser, Donor, FBAccount, Organization

    db = SessionLocal()
    try:
        if db.query(Organization).count() > 0:
            return
        needs = (
            db.query(Donor).count() > 0
            or db.query(FBAccount).count() > 0
            or db.query(AdminUser).count() > 0
        )
        if not needs:
            return
        db.add(Organization(name="Default", slug="default", is_active=True))
        db.commit()
    finally:
        db.close()


def _backfill_organization_members() -> None:
    from backend.models import AdminUser, Organization, OrganizationMember

    db = SessionLocal()
    try:
        org = db.query(Organization).order_by(Organization.id.asc()).first()
        if not org:
            return
        for u in db.query(AdminUser).all():
            if db.query(OrganizationMember).filter_by(user_id=u.id).first():
                continue
            db.add(OrganizationMember(organization_id=org.id, user_id=u.id, role="owner"))
        db.commit()
    finally:
        db.close()


def init_db() -> None:
    """Создать все таблицы (если ещё не существуют)."""
    from backend.config import ensure_runtime_directories

    ensure_runtime_directories()
    # ── Reddit Master: регистрируем модели в Base.metadata до create_all.
    # Таблицы создаются всегда (безопасно: пустые); фактическое использование
    # управляется FB_MASTER_REDDIT_MODULE_ENABLED.
    try:
        import backend.modules.reddit.models  # noqa: F401
    except Exception:
        # Сбой импорта модели Reddit не должен блокировать старт FB Master.
        import logging as _logging

        _logging.getLogger(__name__).exception("reddit models import failed")
    # ── LinkedIn Master: регистрируем модели в Base.metadata до create_all.
    try:
        import backend.modules.linkedin.models  # noqa: F401
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("linkedin models import failed")
    # ── Twitter / X Master: регистрируем модели в Base.metadata до create_all.
    try:
        import backend.modules.twitter.models  # noqa: F401
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("twitter models import failed")
    # ── Instagram Master: регистрируем модели в Base.metadata до create_all.
    try:
        import backend.modules.instagram.models  # noqa: F401
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("instagram models import failed")

    if _is_sqlite():
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
    Base.metadata.create_all(bind=engine)
    _migrate_tenant_organization_columns()
    _maybe_seed_default_organization_for_legacy()
    _backfill_organization_members()
    _init_crm_stages_seed()
    _migrate_fb_accounts_session_columns()
    _migrate_warmup_queue_campaign_id()
    _migrate_sequence_enrollment_fb_account()
    _migrate_outreach_queue_campaign_id()
    _migrate_outreach_queue_react_reel_first()
    _migrate_ai_agent_run_previews()
    _migrate_ai_agent_runs_conversation_id()
    _migrate_jobs_progress_json()
    _migrate_templates_variant_columns()
    _migrate_people_fb_profile_restricted()
    _migrate_people_language_columns()
    _migrate_people_messenger_followup_columns()
    _migrate_import_batches_updated_at()
    _migrate_import_batches_skip_details()
    _migrate_organization_contacted_people()
    _migrate_installation_contacted_profile_urls()
    _migrate_conversations_archived()
    _migrate_conversations_vk_actions()
    _migrate_conversations_messenger_ai()
    _migrate_conversations_ai_reply_steps()
    _migrate_messenger_send_queue_ai_fingerprint()
    _migrate_fb_accounts_automation_columns()
    # До любого db.query(FBAccount) в миграциях ниже — иначе ORM тянет все колонки модели.
    _migrate_fb_accounts_readiness_natural_warmup()
    _migrate_fb_accounts_natural_warmup_page_hidden()
    _migrate_fb_accounts_account_branding_draft()
    _migrate_fb_accounts_proxy_tunnel_health()
    _migrate_fb_accounts_proxy_lease()
    _migrate_fb_accounts_proxy_lease_ends_at()
    _migrate_fb_accounts_login_block_status()
    _migrate_fb_accounts_outreach_shared_lock()
    _migrate_fb_accounts_active_slot()
    _migrate_access_keys_expires_at()
    _migrate_access_keys_key_plain_enc()
    _migrate_access_keys_admin_note()
    _migrate_client_machine_presence_access_key()
    _migrate_sequence_campaigns_config()
    _migrate_discovery_tables()
    _seed_natural_warmup_topic_stopwords()
    _migrate_reddit_accounts_browser_mode()
    _migrate_instagram_accounts_credentials()


def _migrate_reddit_accounts_browser_mode() -> None:
    """Добавить browser-mode колонки к существующей `reddit_accounts`.

    Безопасно: только ADD COLUMN с дефолтами; OAuth-аккаунты сохраняют поведение.
    """
    cols = (
        ("auth_mode", "VARCHAR(24) NOT NULL DEFAULT 'oauth'"),
        ("profile_dir", "VARCHAR(512)"),
        ("cookies_json", "JSON"),
        ("cookies_imported_at", "TIMESTAMP WITH TIME ZONE"),
        ("proxy_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("proxy_url", "VARCHAR(512)"),
        ("proxy_username", "VARCHAR(255)"),
        ("proxy_password", "VARCHAR(255)"),
        ("stealth_user_agent", "VARCHAR(512)"),
        ("stealth_locale", "VARCHAR(32)"),
        ("stealth_timezone_id", "VARCHAR(64)"),
        ("stealth_viewport_w", "INTEGER"),
        ("stealth_viewport_h", "INTEGER"),
        ("session_ok", "BOOLEAN"),
        ("last_login_check_at", "TIMESTAMP WITH TIME ZONE"),
        ("login_blocked_at", "TIMESTAMP WITH TIME ZONE"),
        ("login_blocked_reason", "VARCHAR(512)"),
        ("cap_messages_per_day", "INTEGER"),
        ("cap_comments_per_day", "INTEGER"),
        ("cap_posts_per_day", "INTEGER"),
    )
    # SQLite: упрощённые типы без JSON (хранится как TEXT).
    cols_sqlite = tuple(
        (c, t.replace("JSON", "TEXT").replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP"))
        for c, t in cols
    )
    _add_columns(
        "reddit_accounts",
        cols_sqlite,
        cols,
        extra_sql_sqlite=[
            "CREATE INDEX IF NOT EXISTS ix_reddit_accounts_auth_mode ON reddit_accounts (auth_mode)",
        ],
        extra_sql_pg=[
            "CREATE INDEX IF NOT EXISTS ix_reddit_accounts_auth_mode ON reddit_accounts (auth_mode)",
        ],
    )


def _migrate_instagram_accounts_credentials() -> None:
    """Добавить поля login_username/enc_password/enc_totp_secret к `instagram_accounts`.

    Нужно для режима «свой личный аккаунт через логин+пароль» (v2.34+).
    Безопасно: только ADD COLUMN nullable — купленные аккаунты (cookies-only)
    продолжают работать.
    """
    _add_columns(
        "instagram_accounts",
        (
            ("login_username", "VARCHAR(255)"),
            ("enc_password", "TEXT"),
            ("enc_totp_secret", "TEXT"),
        ),
        (
            ("login_username", "VARCHAR(255)"),
            ("enc_password", "TEXT"),
            ("enc_totp_secret", "TEXT"),
        ),
    )
