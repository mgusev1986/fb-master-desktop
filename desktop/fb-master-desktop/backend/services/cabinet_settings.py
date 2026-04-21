"""Переопределения из кабинета (таблица settings) поверх .env — страница «Настройки» и runtime."""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping

import httpx
from fastapi import Request
from sqlalchemy.orm import Session

from backend.services.preview_as_user import preview_as_user_active

from backend.config import (
    ALLOWED_GOOGLE_EMAILS,
    APP_BASE_URL,
    BASE_DIR,
    BASE_FB_DIR,
    BO_HOST,
    BO_PORT,
    BROWSER_PROFILES_DIR,
    DATA_DIR,
    DB_PATH,
    FB_LOCAL_CHROME_COOKIES,
    LOCAL_SIMULATE_PRODUCTION,
    LOG_DIR,
    LOG_LEVEL,
    LOG_RETENTION_DAYS,
    MESSENGER_E2EE_PIN,
    MESSENGER_E2EE_WAIT_SECONDS,
    OAUTH_REDIRECT_BASE,
    OPENAI_API_KEY,
    SEQUENCE_TIMEZONE,
    SESSION_COOKIE_SECURE,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    WARMUP_HEADLESS,
    effective_fb_local_chrome_cookies,
    effective_warmup_headless,
    fb_master_hide_infra_settings,
    GOOGLE_API_KEY,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
)
from backend.models import Setting
from backend.services.natural_warmup_stopwords import NATURAL_WARMUP_TOPIC_STOPWORDS_KEY

logger = logging.getLogger(__name__)

SK_OPENAI = "cabinet_openai_api_key"
SK_GOOGLE_LLM = "cabinet_google_llm_api_key"
# JSON {"fp": sha256hex, "ok": int, "bad": int} — обновляется только при сохранении ключей Gemini (не при каждом GET).
SK_GOOGLE_LLM_HEALTH = "cabinet_google_llm_key_health_v1"
SK_GOOGLE_OAUTH_ID = "cabinet_google_oauth_client_id"
SK_GOOGLE_OAUTH_SECRET = "cabinet_google_oauth_client_secret"
SK_ALLOWED_EMAILS = "cabinet_allowed_google_emails"
SK_OAUTH_REDIRECT = "cabinet_oauth_redirect_base"
SK_BROWSER = "cabinet_browser_profiles_dir"
SK_BASE_FB = "cabinet_base_fb_dir"
SK_SUPABASE_URL = "cabinet_supabase_url"
SK_SUPABASE_ANON_KEY = "cabinet_supabase_anon_key"
# True в БД — показывать окно Chromium (headed); отсутствие ключа — только .env WARMUP_HEADLESS
SK_PLAYWRIGHT_SHOW_BROWSER = "cabinet_playwright_show_browser"
# False в БД — не подтягивать имя контакта из UI Messenger при открытии чата в Desktop.
SK_MESSENGER_SYNC_DISPLAY_NAME = "cabinet_messenger_sync_display_name_from_messenger"
# False в БД — после перезапуска сервера не запускать автоматически прерванные рассылку/прогрев.
SK_AUTOMATION_AUTO_RESUME = "cabinet_automation_auto_resume_after_restart"
# Список стоп-слов для нейрокомментариев при прогреве (темы: секс, эротика, религия…)
SK_NATURAL_WARMUP_TOPIC_STOPWORDS = NATURAL_WARMUP_TOPIC_STOPWORDS_KEY

ALL_CABINET_KEYS = frozenset(
    {
        SK_OPENAI,
        SK_GOOGLE_LLM,
        SK_GOOGLE_OAUTH_ID,
        SK_GOOGLE_OAUTH_SECRET,
        SK_ALLOWED_EMAILS,
        SK_OAUTH_REDIRECT,
        SK_BROWSER,
        SK_BASE_FB,
        SK_SUPABASE_URL,
        SK_SUPABASE_ANON_KEY,
        SK_PLAYWRIGHT_SHOW_BROWSER,
        SK_MESSENGER_SYNC_DISPLAY_NAME,
        SK_AUTOMATION_AUTO_RESUME,
        SK_NATURAL_WARMUP_TOPIC_STOPWORDS,
    }
)


def _norm_openai(k: str) -> str:
    k = (k or "").strip()
    if k.startswith("k-proj-"):
        return "s" + k
    return k


def split_google_llm_api_keys(blob: str) -> list[str]:
    """
    Несколько ключей Gemini: по одному на строку (пустые и строки с # игнорируются).
    Порядок сохраняется; дубликаты отбрасываются.
    """
    if not blob or not str(blob).strip():
        return []
    text = str(blob).replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        k = line.strip()
        if not k or k.startswith("#"):
            continue
        if k not in out:
            out.append(k)
    return out


def google_llm_api_key_list(db: Session) -> list[str]:
    """Все ключи Gemini: из кабинета (мультистрока) или из GOOGLE_API_KEY в .env."""
    s = _get_str(db, SK_GOOGLE_LLM)
    if s:
        return split_google_llm_api_keys(s)
    return split_google_llm_api_keys(GOOGLE_API_KEY)


# Лёгкая проверка ключа: GET v1beta/models (как в Google AI Studio).
_GEMINI_MODELS_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_KEY_CHECK_TIMEOUT_S = 4.0


def _gemini_api_key_list_models_ok(api_key: str) -> bool:
    k = (api_key or "").strip()
    if not k:
        return False
    try:
        with httpx.Client(timeout=_GEMINI_KEY_CHECK_TIMEOUT_S) as client:
            r = client.get(_GEMINI_MODELS_LIST_URL, params={"key": k, "pageSize": "1"})
        return 200 <= r.status_code < 300
    except Exception as e:
        logger.debug("gemini key probe failed: %s", e)
        return False


def google_llm_api_key_health_counts(keys: list[str]) -> tuple[int, int]:
    """
    Сколько ключей Gemini отвечают на listModels (рабочие) и сколько нет (битые/сеть).
    Вызывать только при сохранении ключей (persist_google_llm_health_after_save), не при GET настроек.
    Пустой список → (0, 0). Запросы к Google идут параллельно (ограничение по числу ключей).
    """
    if not keys:
        return 0, 0
    workers = max(1, min(8, len(keys)))
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_gemini_api_key_list_models_ok, k) for k in keys]
        for fut in as_completed(futures):
            if fut.result():
                ok += 1
    return ok, len(keys) - ok


def _get_setting_value(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    return row.value if row else None


def _get_str(db: Session, key: str) -> str:
    v = _get_setting_value(db, key)
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _delete_setting(db: Session, key: str) -> None:
    row = db.get(Setting, key)
    if row:
        db.delete(row)


def _upsert_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def _google_llm_keys_fingerprint(keys: list[str]) -> str:
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def read_google_llm_health_cache(db: Session, keys: list[str]) -> tuple[int | None, int | None]:
    """Счётчики из кэша, если набор ключей совпадает с сохранённым снимком; иначе (None, None)."""
    if not keys:
        return None, None
    raw = _get_str(db, SK_GOOGLE_LLM_HEALTH)
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict) or data.get("fp") != _google_llm_keys_fingerprint(keys):
        return None, None
    ok, bad = data.get("ok"), data.get("bad")
    if isinstance(ok, int) and isinstance(bad, int):
        return ok, bad
    return None, None


def persist_google_llm_health_after_save(db: Session, keys: list[str]) -> None:
    """
    Один проход проверки ключей (listModels) и запись кэша. Вызывать после commit строки ключей,
    чтобы не держать SQLite во время HTTP.
    """
    if not keys:
        _delete_setting(db, SK_GOOGLE_LLM_HEALTH)
        db.commit()
        return
    ok, bad = google_llm_api_key_health_counts(keys)
    payload = json.dumps(
        {"fp": _google_llm_keys_fingerprint(keys), "ok": ok, "bad": bad},
        separators=(",", ":"),
    )
    _upsert_setting(db, SK_GOOGLE_LLM_HEALTH, payload)
    db.commit()


def _resolve_data_path(raw: str) -> Path:
    p = Path(raw.strip()).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (BASE_DIR / p).resolve()


def effective_openai_api_key(db: Session) -> str:
    s = _get_str(db, SK_OPENAI)
    if s:
        return _norm_openai(s)
    return OPENAI_API_KEY


def effective_google_llm_api_key(db: Session) -> str:
    """
    Сырая строка настроек (один или несколько ключей через перевод строки).
    Для вызова LLM используйте split_google_llm_api_keys(...) или google_llm_api_key_list(db).
    """
    s = _get_str(db, SK_GOOGLE_LLM)
    if s:
        return s
    return (GOOGLE_API_KEY or "").strip()


def effective_google_client_id(db: Session) -> str:
    return _get_str(db, SK_GOOGLE_OAUTH_ID) or GOOGLE_CLIENT_ID


def effective_google_client_secret(db: Session) -> str:
    return _get_str(db, SK_GOOGLE_OAUTH_SECRET) or GOOGLE_CLIENT_SECRET


def effective_oauth_redirect_base(db: Session) -> str:
    return _get_str(db, SK_OAUTH_REDIRECT) or OAUTH_REDIRECT_BASE


def effective_allowed_google_emails(db: Session) -> list[str]:
    v = _get_setting_value(db, SK_ALLOWED_EMAILS)
    if isinstance(v, list):
        return [str(x).strip().lower() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [e.strip().lower() for e in v.replace("\n", ",").split(",") if e.strip()]
    return [e.lower() for e in ALLOWED_GOOGLE_EMAILS if e.strip()]


def effective_browser_profiles_dir(db: Session) -> Path:
    s = _get_str(db, SK_BROWSER)
    if s:
        p = _resolve_data_path(s)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return BROWSER_PROFILES_DIR


def effective_base_fb_dir(db: Session) -> Path:
    s = _get_str(db, SK_BASE_FB)
    if s:
        p = _resolve_data_path(s)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return BASE_FB_DIR


def effective_supabase_url(db: Session) -> str:
    s = _get_str(db, SK_SUPABASE_URL)
    if s:
        return s.rstrip("/")
    return SUPABASE_URL


def effective_supabase_anon_key(db: Session) -> str:
    s = _get_str(db, SK_SUPABASE_ANON_KEY)
    if s:
        return s
    return SUPABASE_ANON_KEY


def oauth_callback_url_for_request(request: Request, db: Session) -> str:
    base = effective_oauth_redirect_base(db).strip()
    if base:
        return base.rstrip("/") + "/auth/callback"
    return str(request.base_url).rstrip("/") + "/auth/callback"


def cabinet_overrides_count(db: Session) -> int:
    n = 0
    for k in ALL_CABINET_KEYS:
        v = _get_setting_value(db, k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, list) and not v:
            continue
        if k == SK_PLAYWRIGHT_SHOW_BROWSER and v is not True:
            continue
        if k == SK_MESSENGER_SYNC_DISPLAY_NAME and v is not False:
            continue
        if k == SK_AUTOMATION_AUTO_RESUME and v is not False:
            continue
        n += 1
    return n


def effective_messenger_sync_display_name_from_messenger(db: Session) -> bool:
    """По умолчанию True: при открытии чата в Desktop обновлять Person.display_name из заголовка Messenger."""
    return _get_setting_value(db, SK_MESSENGER_SYNC_DISPLAY_NAME) is not False


def effective_automation_auto_resume_after_restart(db: Session) -> bool:
    """По умолчанию True: после перезапуска снова запускать прерванные рассылку/прогрев (очередь в БД)."""
    return _get_setting_value(db, SK_AUTOMATION_AUTO_RESUME) is not False


def effective_playwright_headless(db: Session | None) -> bool:
    """
    True — Chromium без окна (headless). False — видимое окно (отладка).
    Переопределение из кабинета: «показать окно» → всегда headed, иначе .env / LOCAL_SIMULATE_PRODUCTION.
    """
    if db is not None and _get_setting_value(db, SK_PLAYWRIGHT_SHOW_BROWSER) is True:
        return False
    return effective_warmup_headless()


def _resolved_path_display(db: Session, key: str, default: Path) -> tuple[Path, bool]:
    s = _get_str(db, key)
    if s:
        p = _resolve_data_path(s)
        return p, p.exists()
    return default, default.exists()


def build_settings_page_context(request: Request, db: Session) -> dict[str, Any]:
    openai_cab = bool(_get_str(db, SK_OPENAI))
    gllm_cab = bool(_get_str(db, SK_GOOGLE_LLM))
    eo = effective_openai_api_key(db)
    gemini_keys = google_llm_api_key_list(db)
    gemini_ok, gemini_bad = 0, 0
    gemini_health_known = False
    if gemini_keys:
        co, cb = read_google_llm_health_cache(db, gemini_keys)
        if co is not None and cb is not None:
            gemini_ok, gemini_bad = co, cb
            gemini_health_known = True
    env_o = bool(OPENAI_API_KEY.strip())
    env_g = bool(split_google_llm_api_keys(GOOGLE_API_KEY))

    cid_cab = bool(_get_str(db, SK_GOOGLE_OAUTH_ID))
    sec_cab = bool(_get_str(db, SK_GOOGLE_OAUTH_SECRET))
    e_cid = effective_google_client_id(db)
    e_sec = effective_google_client_secret(db)
    oauth_ready = bool(e_cid.strip() and e_sec.strip())

    allow_eff = effective_allowed_google_emails(db)
    allow_preview = ", ".join(allow_eff[:5])
    if len(allow_eff) > 5:
        allow_preview += "…"

    path_b_in = _get_str(db, SK_BROWSER)
    path_fb_in = _get_str(db, SK_BASE_FB)
    browser_p, browser_ex = _resolved_path_display(db, SK_BROWSER, BROWSER_PROFILES_DIR)
    base_fb_p, base_fb_ex = _resolved_path_display(db, SK_BASE_FB, BASE_FB_DIR)

    oauth_red_cab = bool(_get_str(db, SK_OAUTH_REDIRECT))
    env_ored = bool(OAUTH_REDIRECT_BASE.strip())

    allow_ta = ""
    if _get_setting_value(db, SK_ALLOWED_EMAILS) is not None:
        allow_ta = ", ".join(allow_eff) if allow_eff else ""
    elif ALLOWED_GOOGLE_EMAILS:
        allow_ta = ", ".join(ALLOWED_GOOGLE_EMAILS)

    google_id_input = _get_str(db, SK_GOOGLE_OAUTH_ID)
    oauth_red_input = _get_str(db, SK_OAUTH_REDIRECT)

    return {
        "has_openai_effective": bool(eo.strip()),
        "openai_cabinet": openai_cab,
        "env_openai": env_o,
        "has_google_llm_effective": bool(gemini_keys),
        "google_llm_cabinet": gllm_cab,
        "google_llm_key_count": len(gemini_keys),
        "google_llm_keys_ok": gemini_ok,
        "google_llm_keys_bad": gemini_bad,
        "google_llm_health_known": gemini_health_known,
        "env_google_llm": env_g,
        "google_oauth_ready_effective": oauth_ready,
        "google_oauth_id_cabinet": cid_cab,
        "google_oauth_secret_cabinet": sec_cab,
        "overrides_count": cabinet_overrides_count(db),
        "google_oauth_client_id_input": google_id_input,
        "allowlist_textarea": allow_ta,
        "oauth_redirect_input": oauth_red_input,
        "oauth_callback_preview": oauth_callback_url_for_request(request, db),
        "effective_allowlist_count": len(allow_eff),
        "effective_allowlist_preview": allow_preview or "",
        "oauth_redirect_cabinet": oauth_red_cab,
        "env_oauth_redirect": env_ored,
        "path_browser_profiles_input": path_b_in,
        "path_browser_profiles_cabinet": bool(path_b_in),
        "browser_profiles_exists": browser_ex,
        "browser_profiles_resolved": str(browser_p),
        "path_base_fb_input": path_fb_in,
        "path_base_fb_cabinet": bool(path_fb_in),
        "base_fb_exists": base_fb_ex,
        "base_fb_resolved": str(base_fb_p),
        "runtime_app_base_url": APP_BASE_URL,
        "runtime_bo_host": BO_HOST,
        "runtime_bo_port": BO_PORT,
        "runtime_data_dir": str(DATA_DIR),
        "runtime_db_path": str(DB_PATH),
        "runtime_log_dir": str(LOG_DIR),
        "runtime_log_level": LOG_LEVEL,
        "runtime_log_retention_days": LOG_RETENTION_DAYS,
        "runtime_fb_local_chrome_cookies": effective_fb_local_chrome_cookies(),
        "runtime_warmup_headless": effective_playwright_headless(db),
        "cabinet_playwright_show_browser": _get_setting_value(db, SK_PLAYWRIGHT_SHOW_BROWSER) is True,
        "cabinet_messenger_sync_display_name": effective_messenger_sync_display_name_from_messenger(db),
        "cabinet_automation_auto_resume": effective_automation_auto_resume_after_restart(db),
        "runtime_fb_local_chrome_cookies_env": FB_LOCAL_CHROME_COOKIES,
        "runtime_warmup_headless_env": WARMUP_HEADLESS,
        "runtime_messenger_e2ee_wait_seconds": MESSENGER_E2EE_WAIT_SECONDS,
        "runtime_messenger_e2ee_pin_configured": bool(MESSENGER_E2EE_PIN.strip()),
        "runtime_sequence_timezone_env": SEQUENCE_TIMEZONE,
        "env_browser_profiles": str(BROWSER_PROFILES_DIR),
        "env_base_fb": str(BASE_FB_DIR),
        "env_google_oauth": bool(GOOGLE_CLIENT_ID.strip()),
        "env_google_oauth_secret": bool(GOOGLE_CLIENT_SECRET.strip()),
        "supabase_url_cabinet": bool(_get_str(db, SK_SUPABASE_URL)),
        "supabase_anon_cabinet": bool(_get_str(db, SK_SUPABASE_ANON_KEY)),
        "supabase_url_input": _get_str(db, SK_SUPABASE_URL),
        "supabase_url_effective": effective_supabase_url(db),
        "supabase_auth_ready": bool(
            effective_supabase_url(db).strip() and effective_supabase_anon_key(db).strip()
        ),
        "env_supabase_url": bool(SUPABASE_URL.strip()),
        "env_supabase_anon": bool(SUPABASE_ANON_KEY.strip()),
        "runtime_local_simulate_production": LOCAL_SIMULATE_PRODUCTION,
        "runtime_session_cookie_secure": SESSION_COOKIE_SECURE,
        "settings_hide_infra": fb_master_hide_infra_settings()
        or preview_as_user_active(request),
    }


def _form_str(form: Mapping[str, Any], name: str) -> str:
    v = form.get(name)
    if v is None:
        return ""
    return str(v).strip()


def save_settings_from_form(
    db: Session,
    form: Mapping[str, Any],
    *,
    request: Request | None = None,
    only: str | None = None,
) -> None:
    """
    only=None — все секции (кнопка внизу).
    only='openai' | 'google_llm' — сохранить только этот ключ (поле не пустое).
    only='clear_openai' | 'clear_google_llm' — удалить ключ из кабинета (кнопка «Удалить»).
    """

    def want(part: str) -> bool:
        return only is None or only == part

    saved_google_keys_for_health: list[str] | None = None

    if only == "clear_openai":
        _delete_setting(db, SK_OPENAI)
        db.commit()
        return
    if only == "clear_google_llm":
        _delete_setting(db, SK_GOOGLE_LLM)
        _delete_setting(db, SK_GOOGLE_LLM_HEALTH)
        db.commit()
        return

    if want("openai"):
        key_val = _form_str(form, "openai_api_key")
        if key_val:
            _upsert_setting(db, SK_OPENAI, _norm_openai(key_val))

    if want("google_llm"):
        raw = form.get("google_llm_api_key")
        gk = "" if raw is None else str(raw).strip()
        if gk:
            keys = split_google_llm_api_keys(gk)
            if keys:
                _upsert_setting(db, SK_GOOGLE_LLM, "\n".join(keys))
                saved_google_keys_for_health = keys

    if only is not None and only in ("openai", "google_llm"):
        db.commit()
        if saved_google_keys_for_health is not None:
            persist_google_llm_health_after_save(db, saved_google_keys_for_health)
        return

    hide_infra = fb_master_hide_infra_settings() or (
        request is not None and preview_as_user_active(request)
    )
    if not hide_infra:
        oid = _form_str(form, "google_oauth_client_id")
        if oid:
            _upsert_setting(db, SK_GOOGLE_OAUTH_ID, oid)
        elif form.get("clear_google_oauth_id") == "1":
            _delete_setting(db, SK_GOOGLE_OAUTH_ID)

        sec = _form_str(form, "google_oauth_client_secret")
        if sec:
            _upsert_setting(db, SK_GOOGLE_OAUTH_SECRET, sec)
        elif form.get("clear_google_oauth_secret") == "1":
            _delete_setting(db, SK_GOOGLE_OAUTH_SECRET)

        if form.get("clear_allowed_emails") == "1":
            _delete_setting(db, SK_ALLOWED_EMAILS)
        else:
            raw_a = form.get("allowed_google_emails")
            if raw_a is not None:
                s = str(raw_a).strip()
                if s:
                    parts = [
                        e.strip().lower()
                        for e in s.replace("\n", ",").split(",")
                        if e.strip()
                    ]
                    _upsert_setting(db, SK_ALLOWED_EMAILS, parts)
                elif _get_setting_value(db, SK_ALLOWED_EMAILS) is not None:
                    _upsert_setting(db, SK_ALLOWED_EMAILS, [])

        if form.get("clear_oauth_redirect_base") == "1":
            _delete_setting(db, SK_OAUTH_REDIRECT)
        else:
            rb = _form_str(form, "oauth_redirect_base")
            if rb:
                _upsert_setting(db, SK_OAUTH_REDIRECT, rb)

        if form.get("clear_path_browser_profiles") == "1":
            _delete_setting(db, SK_BROWSER)
        else:
            pb = _form_str(form, "path_browser_profiles")
            if pb:
                _upsert_setting(db, SK_BROWSER, pb)
                _resolve_data_path(pb).mkdir(parents=True, exist_ok=True)

        if form.get("clear_path_base_fb") == "1":
            _delete_setting(db, SK_BASE_FB)
        else:
            pf = _form_str(form, "path_base_fb")
            if pf:
                _upsert_setting(db, SK_BASE_FB, pf)
                _resolve_data_path(pf).mkdir(parents=True, exist_ok=True)

        if form.get("clear_supabase_url") == "1":
            _delete_setting(db, SK_SUPABASE_URL)
        else:
            su = _form_str(form, "supabase_url")
            if su:
                _upsert_setting(db, SK_SUPABASE_URL, su.rstrip("/"))

        sak = _form_str(form, "supabase_anon_key")
        if sak:
            _upsert_setting(db, SK_SUPABASE_ANON_KEY, sak)
        elif form.get("clear_supabase_anon_key") == "1":
            _delete_setting(db, SK_SUPABASE_ANON_KEY)

    if form.get("playwright_show_browser") == "1":
        _upsert_setting(db, SK_PLAYWRIGHT_SHOW_BROWSER, True)
    else:
        _delete_setting(db, SK_PLAYWRIGHT_SHOW_BROWSER)

    if form.get("messenger_sync_display_name_from_messenger") == "1":
        _delete_setting(db, SK_MESSENGER_SYNC_DISPLAY_NAME)
    else:
        _upsert_setting(db, SK_MESSENGER_SYNC_DISPLAY_NAME, False)

    if form.get("automation_auto_resume_after_restart") == "1":
        _delete_setting(db, SK_AUTOMATION_AUTO_RESUME)
    else:
        _upsert_setting(db, SK_AUTOMATION_AUTO_RESUME, False)

    db.commit()
    if saved_google_keys_for_health is not None:
        persist_google_llm_health_after_save(db, saved_google_keys_for_health)
