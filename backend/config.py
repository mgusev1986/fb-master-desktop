"""Конфигурация FB Master. Все секреты и пути — через переменные окружения."""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Не ищем .env выше корня проекта/бандла: иначе desktop-backend может случайно
# подхватить чужой DATABASE_URL / SUPABASE_* из родительской папки и уйти в remote DB.
load_dotenv(BASE_DIR / ".env")


def _bundled_chromium_executable(browsers_root: Path) -> Path | None:
    """
    Ищет исполняемый Chromium внутри каталога Playwright (playwright install chromium).
    Учитывает текущую ОС — не путает chrome-linux с Mac/Win.
    """
    if not browsers_root.is_dir():
        return None
    sysname = platform.system()
    try:
        for f in browsers_root.rglob("*"):
            if not f.is_file():
                continue
            sp = str(f).replace("\\", "/")
            if sysname == "Darwin":
                if f.name == "Chromium" and "/Chromium.app/Contents/MacOS/" in sp:
                    return f
            elif sysname == "Linux":
                if f.name == "chrome" and "/chrome-linux/" in sp:
                    return f
            elif sysname == "Windows":
                if f.name.lower() == "chrome.exe" and "/chrome-win" in sp.lower().replace("\\", "/"):
                    return f
    except OSError:
        return None
    return None


def _playwright_browsers_root_usable(root: Path | None) -> bool:
    return root is not None and _bundled_chromium_executable(root) is not None


def reapply_playwright_browsers_path() -> None:
    """
    Подставить PLAYWRIGHT_BROWSERS_PATH на ./playwright-browsers рядом с приложением,
    если переменная пуста, указывает на нерабочий каталог или без бинарника под эту ОС.
    Идемпотентно — безопасно вызывать перед запуском Playwright.
    """
    bundle = (BASE_DIR / "playwright-browsers").resolve()
    env_raw = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    env_root = Path(env_raw).expanduser().resolve() if env_raw else None

    if _playwright_browsers_root_usable(env_root):
        return
    if _playwright_browsers_root_usable(bundle):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundle)


reapply_playwright_browsers_path()


def _env_path(name: str, default_relative: str) -> Path:
    """
    Путь из переменной окружения: абсолютный — как есть; относительный — от корня проекта
    (не от текущего каталога процесса — важно для systemd/docker на сервере).
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return (BASE_DIR / default_relative).resolve()
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (BASE_DIR / p).resolve()


# ── Сервер ───────────────────────────────────────────
APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:8000")
# Только для OAuth: если пусто, redirect_uri = текущий Host из запроса (локально без mismatch).
# На VPS за nginx: https://ваш-домен.ru (без слэша в конце)
OAUTH_REDIRECT_BASE: str = os.getenv("OAUTH_REDIRECT_BASE", "").strip()
SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-change-me")
BO_HOST: str = os.getenv("BO_HOST", "127.0.0.1")
BO_PORT: int = int(os.getenv("BO_PORT", "8000"))


# ── Пути ─────────────────────────────────────────────
DATA_DIR = _env_path("DATA_DIR", "data")
DB_PATH = DATA_DIR / "app.db"


def _normalize_database_url(url: str) -> str:
    """
    Приводит URL к виду, который понимает SQLAlchemy 2.
    Supabase и другие сервисы часто отдают postgresql:// или postgres:// без драйвера —
    подставляем postgresql+psycopg:// (пакет psycopg v3).
    """
    u = (url or "").strip()
    low = u.lower()
    if low.startswith("sqlite:"):
        return u
    if low.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://") :]
        low = u.lower()
    if low.startswith("postgresql://"):
        if any(x in low for x in ("+psycopg", "+psycopg2", "+asyncpg")):
            return u
        return "postgresql+psycopg://" + u[len("postgresql://") :]
    return u


# Supabase Dashboard часто копируют в отдельную переменную — принимаем оба имени.
_db_url_raw = (
    (os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL") or "").strip()
)
DATABASE_URL: str = (
    _normalize_database_url(_db_url_raw) if _db_url_raw else f"sqlite:///{DB_PATH}"
)

# Supabase Auth (вход в кабинет): Project URL и anon key из Dashboard → Settings → API
SUPABASE_URL: str = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_ANON_KEY: str = (os.getenv("SUPABASE_ANON_KEY") or "").strip()

# Локально вести себя как на сервере: без dev-login, headless, без подстановки cookies из Chrome
LOCAL_SIMULATE_PRODUCTION: bool = os.getenv("LOCAL_SIMULATE_PRODUCTION", "").lower() in (
    "1",
    "true",
    "yes",
)
# Cookie сессии только по HTTPS (за nginx / TLS)
SESSION_COOKIE_SECURE: bool = os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def web_cabinet_gate_slug() -> str:
    """Секретный сегмент пути: вход только по URL …/gate/<slug> и паролю. Без .env — отключено."""
    s = (os.getenv("FB_MASTER_WEB_CABINET_GATE_SLUG") or "").strip().strip("/")
    if "/" in s:
        return ""
    return s


def web_cabinet_gate_password() -> str:
    return os.getenv("FB_MASTER_WEB_CABINET_GATE_PASSWORD") or ""


def web_cabinet_gate_enabled() -> bool:
    return bool(web_cabinet_gate_slug() and web_cabinet_gate_password())


# Десктоп с локальным бэкендом: активация ключа запросом на VPS (https://домен без слэша). Пусто — только локальная БД.
FB_MASTER_LICENSE_API_BASE: str = (os.getenv("FB_MASTER_LICENSE_API_BASE") or "").strip().rstrip("/")
# Полный URL POST для пульса «онлайн» (по умолчанию = LICENSE_API_BASE + /api/public/client-presence). Пусто — не задавать, если не нужен отдельный хост.
FB_MASTER_PRESENCE_REPORT_URL: str = (os.getenv("FB_MASTER_PRESENCE_REPORT_URL") or "").strip()


def client_presence_remote_post_url() -> str:
    """Куда десктоп (SQLite) шлёт client-presence; пустая строка — не слать."""
    raw = (FB_MASTER_PRESENCE_REPORT_URL or "").strip()
    if raw:
        return raw.rstrip("/")
    base = (FB_MASTER_LICENSE_API_BASE or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/api/public/client-presence"

# Параллельные Playwright-задачи (разные FB-аккаунты на мощном сервере)
PLAYWRIGHT_MAX_CONCURRENT: int = max(1, int(os.getenv("PLAYWRIGHT_MAX_CONCURRENT", "3")))


def proxy_health_check_interval_sec() -> float:
    """Период фоновой проверки прокси у FB-аккаунтов (секунды)."""
    raw = (os.getenv("PROXY_HEALTH_CHECK_INTERVAL_SEC") or "20").strip()
    try:
        v = float(raw)
    except ValueError:
        return 20.0
    return max(5.0, min(300.0, v))


BROWSER_PROFILES_DIR = _env_path("BROWSER_PROFILES_DIR", "browser_profiles")
BASE_FB_DIR = _env_path("BASE_FB_DIR", "Base FB")
LOG_DIR = DATA_DIR / "logs"
LOG_DIAGNOSTICS_DIR = LOG_DIR / "diagnostics"


def ensure_runtime_directories() -> None:
    """
    Создать каталоги для SQLite, логов, профилей Chromium и выгрузок парсера.
    Вызывается при загрузке конфигурации — одинаково на Mac и на сервере.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    BASE_FB_DIR.mkdir(parents=True, exist_ok=True)


ensure_runtime_directories()

# Локально: подставлять cookies Facebook из Google Chrome только для «пустого» аккаунта
# (нет папки профиля и нет снимка в БД). Иначе откроется личный FB вместо купленного.
# На VPS = false.
FB_LOCAL_CHROME_COOKIES: bool = (
    os.getenv("FB_LOCAL_CHROME_COOKIES", "false").lower() in ("1", "true", "yes")
)

# Подключение Playwright к уже запущенному Chromium (антидетект AdsPower, Dolphin, GoLogin,
# или Google Chrome с --remote-debugging-port). Тогда отпечаток и прокси задаются в том браузере.
# Пример: http://127.0.0.1:9222 или ws://127.0.0.1:9222/devtools/browser/...
# Переопределяется полем «CDP URL» у конкретного FB-аккаунта (если заполнено).
FB_CDP_ENDPOINT: str = (os.getenv("FB_CDP_ENDPOINT") or "").strip()


def effective_fb_cdp_endpoint(account_cdp_url: str | None = None) -> str | None:
    """URL для connect_over_cdp: сначала из карточки аккаунта, иначе из FB_CDP_ENDPOINT."""
    u = (account_cdp_url or "").strip()
    if u:
        return u
    return FB_CDP_ENDPOINT or None


def dev_login_allowed() -> bool:
    """Режим «Войти как Developer» (только если не имитация прода и не явный запрет)."""
    if os.getenv("DISABLE_DEV_LOGIN", "").lower() in ("1", "true", "yes"):
        return False
    if LOCAL_SIMULATE_PRODUCTION:
        return False
    return True


def fb_master_no_auth() -> bool:
    """
    True — без экрана входа: автоматически подставляется локальный оператор (см. local_operator_session).
    Не включайте на публичном сервере в интернете.
    """
    return os.getenv("FB_MASTER_NO_AUTH", "").lower() in ("1", "true", "yes")


def fb_master_access_key_required() -> bool:
    """
    True — перед кабинетом нужен одноразовый ключ доступа; после ввода на устройстве запоминается в сессии.
    Ключи выдаёт владелец: «Платформа» → ключи или скрипт scripts/create_access_key.py.
    """
    return os.getenv("FB_MASTER_ACCESS_KEY_REQUIRED", "").lower() in ("1", "true", "yes")


def fb_master_web_cabinet_email_login_disabled() -> bool:
    """
    True — убрать веб-вход через Google / Supabase / email / Developer (/auth/login и OAuth).
    В кабинет только через оплаченный ключ на /auth/unlock (рекомендуется вместе с FB_MASTER_ACCESS_KEY_REQUIRED=1).
    Секретная страница /gate/<slug> с паролем — как раньше; после неё редирект на ввод ключа.
    Раздел «Платформа» для владельца (email из FB_MASTER_OWNER_EMAILS) при включённом флаге
    через веб без OAuth недоступен — используйте десктоп или временно отключите флаг.
    """
    return os.getenv("FB_MASTER_WEB_CABINET_EMAIL_LOGIN_DISABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


def fb_master_operator_after_access_key() -> bool:
    """
    True — при уже валидном ключе в сессии автоматически создаётся пользователь-оператор без OAuth.
    Лаунчер Electron подставляет для клиентского DMG; на публичном VPS обычно не задают.
    """
    return os.getenv("FB_MASTER_OPERATOR_AFTER_ACCESS_KEY", "").lower() in ("1", "true", "yes")


def fb_master_show_logout_in_ui() -> bool:
    """False — скрыть кнопку «Выйти» в сайдбаре (десктопный клиент, один пользователь на ПК)."""
    return os.getenv("FB_MASTER_HIDE_LOGOUT", "").lower() not in ("1", "true", "yes")


def fb_master_hide_infra_settings() -> bool:
    """
    True — на странице «Система → Настройки» не показывать блоки инфраструктуры:
    Supabase Auth, прямой Google OAuth / allowlist, пути к данным (профили, Base FB).
    POST на /system/settings/save не применяет эти поля (защита от подделки формы).
    Лаунчер десктопа задаёт FB_MASTER_HIDE_INFRA_SETTINGS=1 по умолчанию; на VPS обычно не включают.
    """
    return os.getenv("FB_MASTER_HIDE_INFRA_SETTINGS", "").lower() in ("1", "true", "yes")


def fb_master_force_cabinet_client_ui() -> bool:
    """
    True — упрощённые тексты кабинета для всех, включая владельца (без сессии «предпросмотр клиента»).
    Удобно на стенде или если нужно снимать скриншоты клиентского UI. На бою обычно не включают.
    """
    return os.getenv("FB_MASTER_FORCE_CABINET_CLIENT_UI", "").lower() in ("1", "true", "yes")


def effective_fb_local_chrome_cookies() -> bool:
    """При LOCAL_SIMULATE_PRODUCTION отключаем подстановку cookies из Chrome (как на VPS)."""
    if LOCAL_SIMULATE_PRODUCTION:
        return False
    return FB_LOCAL_CHROME_COOKIES

# ── Google OAuth 2.0 ─────────────────────────────────
GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
ALLOWED_GOOGLE_EMAILS: list[str] = [
    e.strip()
    for e in os.getenv("ALLOWED_GOOGLE_EMAILS", "").split(",")
    if e.strip()
]


def _platform_owner_emails() -> frozenset[str]:
    """Email(ы) владельца платформы: раздел «Админ» в сайдбаре и /admin/platform (через запятую)."""
    raw = (os.getenv("FB_MASTER_OWNER_EMAILS") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


PLATFORM_OWNER_EMAILS_SET: frozenset[str] = _platform_owner_emails()

# ── LLM ──────────────────────────────────────────────
def _normalize_openai_api_key(raw: str) -> str:
    """Исправляет частую опечатку: ключ проекта OpenAI должен начинаться с sk-proj-, не k-proj-."""
    k = (raw or "").strip()
    if k.startswith("k-proj-"):
        return "s" + k
    return k


OPENAI_API_KEY: str = _normalize_openai_api_key(os.getenv("OPENAI_API_KEY", ""))
GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")

# ── Логирование ──────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS: int = int(os.getenv("LOG_RETENTION_DAYS", "14"))
LOG_LLM_DEBUG: bool = os.getenv("LOG_LLM_DEBUG", "false").lower() == "true"

# Прогрев, парсер друзей, сценарии: headless Chromium (false — видимое окно, отладка на Mac)
WARMUP_HEADLESS: bool = os.getenv("WARMUP_HEADLESS", "true").lower() == "true"


def effective_warmup_headless() -> bool:
    """При LOCAL_SIMULATE_PRODUCTION браузер по умолчанию headless (как на сервере)."""
    if LOCAL_SIMULATE_PRODUCTION:
        return True
    return WARMUP_HEADLESS

# Мессенджер: если Facebook показывает окно PIN / восстановления E2EE, сколько секунд держать
# браузер открытым и перепроверять страницу (время на ручной ввод PIN в этом же окне).
# 0 — сразу ошибка, как раньше (для автоматизации без ручного ввода).
MESSENGER_E2EE_WAIT_SECONDS: int = int(os.getenv("MESSENGER_E2EE_WAIT_SECONDS", "600"))
# Опционально: автоподстановка PIN в окне E2EE Messenger (Playwright). Храните .env в секрете.
MESSENGER_E2EE_PIN: str = (os.getenv("MESSENGER_E2EE_PIN") or "").strip()
# Если MESSENGER_E2EE_PIN пустой: сначала FALLBACK (по умолчанию 777777), если окно не закрылось — FALLBACK_ALT (111111).
MESSENGER_E2EE_PIN_FALLBACK: str = (os.getenv("MESSENGER_E2EE_PIN_FALLBACK") or "777777").strip()
MESSENGER_E2EE_PIN_FALLBACK_ALT: str = (os.getenv("MESSENGER_E2EE_PIN_FALLBACK_ALT") or "111111").strip()


def effective_messenger_e2ee_pins_for_autofill() -> list[str]:
    """
    Цепочка PIN: явный MESSENGER_E2EE_PIN — одна строка; иначе FALLBACK, затем при отличии — FALLBACK_ALT.
    """
    p = (MESSENGER_E2EE_PIN or "").strip()
    if p:
        return [p]
    a = (MESSENGER_E2EE_PIN_FALLBACK or "777777").strip()
    b = (MESSENGER_E2EE_PIN_FALLBACK_ALT or "111111").strip()
    if not a:
        return [b] if b else []
    if not b or a == b:
        return [a]
    return [a, b]


def effective_messenger_e2ee_pin() -> str:
    """Первый PIN из цепочки автоподстановки."""
    xs = effective_messenger_e2ee_pins_for_autofill()
    return xs[0] if xs else ""


def _autostart_messenger2_frame_enabled() -> bool:
    """
    Автозапуск Electron-приложения desktop/fb-master-desktop при старте back-office.
    FB_MASTER_AUTOSTART_DESKTOP или FB_MASTER_AUTOSTART_MESSENGER2_FRAME: 1/true/yes — включить; 0/false/no — выключить.
    Если не задано: на macOS включено, иначе выключено.
    """
    v = (
        os.getenv("FB_MASTER_AUTOSTART_DESKTOP") or os.getenv("FB_MASTER_AUTOSTART_MESSENGER2_FRAME") or ""
    ).strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return sys.platform == "darwin"


AUTOSTART_MESSENGER2_FRAME: bool = _autostart_messenger2_frame_enabled()
# Локальный HTTP health настольного приложения (тот же порт передаётся в Electron через env).
MESSENGER2_FRAME_HEALTH_PORT: int = int(os.getenv("MESSENGER2_FRAME_HEALTH_PORT", "37821"))

# Сценарии по дням: IANA-имя пояса, если в БД нет ключа settings.sequence_timezone
SEQUENCE_TIMEZONE: str = (os.getenv("SEQUENCE_TIMEZONE", "UTC") or "UTC").strip() or "UTC"

# ── Централизованные обновления десктопа (GET /api/public/desktop-update) ──
# Заполните на прод-сервере: пользователи с FB_MASTER_APP_URL на этот хост получат уведомление
# о новой версии и ссылку на установщик. Версия в desktop/fb-master-desktop/package.json и
# FB_DESKTOP_LATEST_VERSION — в формате «2.14», «2.15» (два числовых сегмента; без обязательного .0).
# Сравнение на клиенте — покомпонентно по целым частям (2.1.3 < 2.14; 2.14 < 2.15).
# Версия установленного приложения (Electron): local-backend-launcher выставляет из package.json.
FB_DESKTOP_APP_VERSION: str = (os.getenv("FB_DESKTOP_APP_VERSION") or "").strip()
FB_DESKTOP_LATEST_VERSION: str = (os.getenv("FB_DESKTOP_LATEST_VERSION") or "").strip()
FB_DESKTOP_MIN_VERSION: str = (os.getenv("FB_DESKTOP_MIN_VERSION") or "").strip()
FB_DESKTOP_RELEASE_NOTES: str = (os.getenv("FB_DESKTOP_RELEASE_NOTES") or "").strip()
# Установщики по ОС/архитектуре (HTTPS-ссылки на ваш CDN или /static/releases/...):
FB_DESKTOP_DOWNLOAD_DARWIN_ARM64: str = (os.getenv("FB_DESKTOP_DOWNLOAD_DARWIN_ARM64") or "").strip()
FB_DESKTOP_DOWNLOAD_DARWIN_X64: str = (os.getenv("FB_DESKTOP_DOWNLOAD_DARWIN_X64") or "").strip()
FB_DESKTOP_DOWNLOAD_WIN32_X64: str = (os.getenv("FB_DESKTOP_DOWNLOAD_WIN32_X64") or "").strip()
FB_DESKTOP_DOWNLOAD_LINUX_X64: str = (os.getenv("FB_DESKTOP_DOWNLOAD_LINUX_X64") or "").strip()
# Один DMG для всех Mac — подставится в оба ключа, если ARM64/X64 не заданы по отдельности:
FB_DESKTOP_DOWNLOAD_MAC: str = (os.getenv("FB_DESKTOP_DOWNLOAD_MAC") or "").strip()
FB_DESKTOP_DOWNLOAD_WIN: str = (os.getenv("FB_DESKTOP_DOWNLOAD_WIN") or "").strip()
FB_DESKTOP_DOWNLOAD_LINUX: str = (os.getenv("FB_DESKTOP_DOWNLOAD_LINUX") or "").strip()
# Архив .zip (DMG + инструкция) — URL на /static/releases/… после сборки scripts/zip-mac-arm64-bundle.sh
FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE: str = (
    os.getenv("FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE") or ""
).strip()

# ── Канал «разработчик»: /download/dev + /api/public/desktop-update-dev, файлы в static/releases/dev/ ──
FB_DESKTOP_DEV_LATEST_VERSION: str = (os.getenv("FB_DESKTOP_DEV_LATEST_VERSION") or "").strip()
FB_DESKTOP_DEV_MIN_VERSION: str = (os.getenv("FB_DESKTOP_DEV_MIN_VERSION") or "").strip()
FB_DESKTOP_DEV_RELEASE_NOTES: str = (os.getenv("FB_DESKTOP_DEV_RELEASE_NOTES") or "").strip()
FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64: str = (
    os.getenv("FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64") or ""
).strip()
FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64_BUNDLE: str = (
    os.getenv("FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64_BUNDLE") or ""
).strip()
FB_DESKTOP_DEV_DOWNLOAD_MAC: str = (os.getenv("FB_DESKTOP_DEV_DOWNLOAD_MAC") or "").strip()

# Подписанные ссылки /download/release?f=…&exp=…&sig=…
RELEASE_DOWNLOAD_TTL_PAID: int = max(300, int(os.getenv("FB_RELEASE_TTL_PAID_SECONDS", "172800") or "172800"))
RELEASE_DOWNLOAD_TTL_MANIFEST: int = max(
    300, int(os.getenv("FB_RELEASE_TTL_MANIFEST_SECONDS", "604800") or "604800")
)
RELEASE_DOWNLOAD_TTL_PUBLIC: int = max(300, int(os.getenv("FB_RELEASE_TTL_PUBLIC_SECONDS", "7200") or "7200"))


def _marketing_desktop_version_display(raw: str) -> str:
    """Для подписи в UI: semver 2.14.0 → 2.14; 2.14.1 без изменений."""
    s = (raw or "").strip()
    if not s:
        return s
    m = re.fullmatch(r"(\d+)\.(\d+)\.0", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return s


def desktop_app_version_ui() -> str:
    """
    Подпись версии в сайдбаре и на экране ключа.
    В Electron — из package.json (FB_DESKTOP_APP_VERSION), для отображения без хвоста .0.
    На веб-кабинете/VPS — из FB_DESKTOP_LATEST_VERSION (какая сборка скачивается клиентам).
    """
    if FB_DESKTOP_APP_VERSION:
        return _marketing_desktop_version_display(FB_DESKTOP_APP_VERSION)
    return _marketing_desktop_version_display(FB_DESKTOP_LATEST_VERSION)


def desktop_update_latest_version() -> str:
    return FB_DESKTOP_LATEST_VERSION


def desktop_update_min_version() -> str:
    return FB_DESKTOP_MIN_VERSION


def desktop_update_release_notes() -> str:
    return FB_DESKTOP_RELEASE_NOTES


def desktop_dev_update_latest_version() -> str:
    return FB_DESKTOP_DEV_LATEST_VERSION


def desktop_dev_update_min_version() -> str:
    return FB_DESKTOP_DEV_MIN_VERSION


def desktop_dev_update_release_notes() -> str:
    return FB_DESKTOP_DEV_RELEASE_NOTES


def _static_dev_release_file_path(filename: str) -> Path:
    return (BASE_DIR / "static" / "releases" / "dev" / filename).resolve()


def _dev_buy_page_auto_bundle_url() -> str:
    ver = FB_DESKTOP_DEV_LATEST_VERSION.strip()
    if not ver:
        return ""
    for prefix in ("SOCMASTER", "FbMaster"):
        fn = f"{prefix}-{ver}-mac-arm64-bundle.zip"
        if _static_dev_release_file_path(fn).is_file():
            return f"{public_app_base_url().rstrip('/')}/static/releases/dev/{fn}"
    return ""


def _dev_buy_page_auto_dmg_url() -> str:
    ver = FB_DESKTOP_DEV_LATEST_VERSION.strip()
    if not ver:
        return ""
    for prefix in ("SOCMASTER", "FbMaster"):
        fn = f"{prefix}-{ver}-mac-arm64.dmg"
        if _static_dev_release_file_path(fn).is_file():
            return f"{public_app_base_url().rstrip('/')}/static/releases/dev/{fn}"
    return ""


def desktop_dev_update_download_urls() -> dict[str, str]:
    """Ключи как у клиентского манифеста (darwin_arm64, darwin_arm64_bundle) — канал разработчика."""
    mac_arm = FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64 or FB_DESKTOP_DEV_DOWNLOAD_MAC
    out: dict[str, str] = {}
    bundle = FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64_BUNDLE or _dev_buy_page_auto_bundle_url()
    if bundle:
        out["darwin_arm64_bundle"] = bundle
    if mac_arm:
        out["darwin_arm64"] = mac_arm
    if "darwin_arm64" not in out:
        auto_dmg = _dev_buy_page_auto_dmg_url()
        if auto_dmg:
            out["darwin_arm64"] = auto_dmg
    return out


def desktop_dev_download_page_urls(*, for_paid_flow: bool = False) -> dict[str, str]:
    """Подписанные ссылки для страницы /download/dev."""
    from backend.services.release_download import sign_url_dict

    ttl = RELEASE_DOWNLOAD_TTL_PAID if for_paid_flow else RELEASE_DOWNLOAD_TTL_PUBLIC
    return sign_url_dict(desktop_dev_update_download_urls(), ttl)


def desktop_update_download_urls() -> dict[str, str]:
    """Ключи совпадают с process.platform + '_' + process.arch в Electron."""
    # Apple Silicon: явный URL или общий FB_DESKTOP_DOWNLOAD_MAC (часто arm64 с M‑Mac сборки).
    mac_arm = FB_DESKTOP_DOWNLOAD_DARWIN_ARM64 or FB_DESKTOP_DOWNLOAD_MAC
    # Intel Mac: только явный URL — не подставляем MAC, иначе на /buy две ссылки ведут на arm64.
    mac_x64 = FB_DESKTOP_DOWNLOAD_DARWIN_X64
    win = FB_DESKTOP_DOWNLOAD_WIN32_X64 or FB_DESKTOP_DOWNLOAD_WIN
    linux = FB_DESKTOP_DOWNLOAD_LINUX_X64 or FB_DESKTOP_DOWNLOAD_LINUX
    out: dict[str, str] = {}
    bundle = FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE or _buy_page_auto_bundle_url()
    if bundle:
        out["darwin_arm64_bundle"] = bundle
    if mac_arm:
        out["darwin_arm64"] = mac_arm
    if mac_x64:
        out["darwin_x64"] = mac_x64
    if win:
        out["win32_x64"] = win
    if linux:
        out["linux_x64"] = linux
    return out


def marketing_buy_page_url() -> str:
    """Ссылка «оформить доступ» с экрана ключа: сайт с оплатой (для десктопа — внешний HTTPS)."""
    explicit = (os.getenv("FB_MASTER_MARKETING_BUY_URL") or "").strip()
    if explicit:
        return explicit
    lic = (os.getenv("FB_MASTER_LICENSE_API_BASE") or "").strip().rstrip("/")
    if lic:
        return f"{lic}/buy"
    return f"{public_app_base_url().rstrip('/')}/buy"


def marketing_purchase_page_url() -> str:
    """Страница только оплаты (NOWPayments), без блока скачивания — для ссылки с экрана ключа."""
    explicit = (os.getenv("FB_MASTER_MARKETING_PURCHASE_URL") or "").strip()
    if explicit:
        return explicit
    lic = (os.getenv("FB_MASTER_LICENSE_API_BASE") or "").strip().rstrip("/")
    if lic:
        return f"{lic}/purchase"
    return f"{public_app_base_url().rstrip('/')}/purchase"


def _static_release_file_path(filename: str) -> Path:
    return (BASE_DIR / "static" / "releases" / filename).resolve()


def _buy_page_auto_bundle_url() -> str:
    """
    Если в .env задана FB_DESKTOP_LATEST_VERSION и в static/releases лежит
    SOCMASTER-<версия>-mac-arm64-bundle.zip (или legacy FbMaster-…) —
    подставить публичный URL без FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE.
    """
    ver = FB_DESKTOP_LATEST_VERSION.strip()
    if not ver:
        return ""
    for prefix in ("SOCMASTER", "FbMaster"):
        fn = f"{prefix}-{ver}-mac-arm64-bundle.zip"
        if _static_release_file_path(fn).is_file():
            return f"{public_app_base_url().rstrip('/')}/static/releases/{fn}"
    return ""


def _buy_page_auto_dmg_url() -> str:
    """Аналогично — SOCMASTER/FbMaster-<версия>-mac-arm64.dmg, если нет явных URL в .env."""
    ver = FB_DESKTOP_LATEST_VERSION.strip()
    if not ver:
        return ""
    for prefix in ("SOCMASTER", "FbMaster"):
        fn = f"{prefix}-{ver}-mac-arm64.dmg"
        if _static_release_file_path(fn).is_file():
            return f"{public_app_base_url().rstrip('/')}/static/releases/{fn}"
    return ""


def _buy_page_auto_dmg_x64_url() -> str:
    """v3.0+: Авто-ссылка на Mac Intel (x64) DMG, если нет явного FB_DESKTOP_DOWNLOAD_DARWIN_X64.

    Ищет SOCMASTER-<версия>-mac-x64.dmg в static/releases/.
    """
    ver = FB_DESKTOP_LATEST_VERSION.strip()
    if not ver:
        return ""
    for prefix in ("SOCMASTER", "FbMaster"):
        fn = f"{prefix}-{ver}-mac-x64.dmg"
        if _static_release_file_path(fn).is_file():
            return f"{public_app_base_url().rstrip('/')}/static/releases/{fn}"
    return ""


def _buy_page_auto_bundle_x64_url() -> str:
    """v3.0.5+: Авто-ссылка на Mac Intel (x64) bundle.zip с инструкцией внутри.

    Аналог _buy_page_auto_bundle_url() для arm64. Bundle содержит DMG +
    «1. Инструкция.html» с macOS Gatekeeper helper-инструкцией (3 шага).
    Используется на странице /auth/unlock — Intel-кнопка теперь ведёт на
    bundle вместо raw DMG (чтобы клиент видел инструкцию про «приложение
    повреждено» на свежих macOS Sonoma/Sequoia/Tahoe).
    """
    ver = FB_DESKTOP_LATEST_VERSION.strip()
    if not ver:
        return ""
    for prefix in ("SOCMASTER", "FbMaster"):
        fn = f"{prefix}-{ver}-mac-x64-bundle.zip"
        if _static_release_file_path(fn).is_file():
            return f"{public_app_base_url().rstrip('/')}/static/releases/{fn}"
    return ""


def _buy_page_auto_exe_url() -> str:
    """Авто‑ссылка на Windows‑инсталлятор SOCMASTER‑<версия>‑win‑x64.exe в static/releases/."""
    ver = FB_DESKTOP_LATEST_VERSION.strip()
    if not ver:
        return ""
    for prefix in ("SOCMASTER", "FbMaster"):
        fn = f"{prefix}-{ver}-win-x64.exe"
        if _static_release_file_path(fn).is_file():
            return f"{public_app_base_url().rstrip('/')}/static/releases/{fn}"
    return ""


def desktop_buy_page_download_urls(*, for_paid_flow: bool = False) -> dict[str, str]:
    """
    Ссылки для витрины /buy и после оплаты.
    Apple Silicon (M1–M4): .zip с DMG + HTML «первый запуск» (FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE
    или авто по FB_DESKTOP_LATEST_VERSION + файл в static/releases),
    опционально .dmg (env или авто).
    Работа приложения без ключа — на стороне десктопа (FB_MASTER_ACCESS_KEY_REQUIRED).
    for_paid_flow влияет только на TTL подписанных ссылок.
    """
    out: dict[str, str] = {}
    bundle = FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE or _buy_page_auto_bundle_url()
    if bundle:
        out["darwin_arm64_bundle"] = bundle
    full = dict(desktop_update_download_urls())
    if "darwin_arm64" not in full:
        auto_dmg = _buy_page_auto_dmg_url()
        if auto_dmg:
            full["darwin_arm64"] = auto_dmg
    if "darwin_arm64" in full:
        out["darwin_arm64"] = full["darwin_arm64"]
    # v3.0+: Mac Intel (x64) — для клиентов с MacBook на Intel CPU (2017-2020).
    if "darwin_x64" not in full:
        auto_dmg_x64 = _buy_page_auto_dmg_x64_url()
        if auto_dmg_x64:
            full["darwin_x64"] = auto_dmg_x64
    if "darwin_x64" in full:
        out["darwin_x64"] = full["darwin_x64"]
    # v3.0.5+: x64 bundle.zip с инструкцией (аналогично darwin_arm64_bundle).
    bundle_x64 = _buy_page_auto_bundle_x64_url()
    if bundle_x64:
        out["darwin_x64_bundle"] = bundle_x64
    if "win32_x64" not in full:
        auto_exe = _buy_page_auto_exe_url()
        if auto_exe:
            full["win32_x64"] = auto_exe
    if "win32_x64" in full:
        out["win32_x64"] = full["win32_x64"]

    from backend.services.release_download import sign_url_dict

    ttl = RELEASE_DOWNLOAD_TTL_PAID if for_paid_flow else RELEASE_DOWNLOAD_TTL_PUBLIC
    return sign_url_dict(out, ttl)


def unlock_page_desktop_download_href() -> str:
    """
    Ссылка для кнопки «Скачать приложение» на /auth/unlock, когда в браузере нет формы ключа.
    Прямой скачивание (как после оплаты), без marketing_buy_page_url — он может указывать на /purchase.
    """
    hrefs = unlock_page_desktop_download_hrefs()
    return hrefs.get("mac") or hrefs.get("win") or f"{public_app_base_url().rstrip('/')}/buy"


def unlock_page_desktop_download_hrefs() -> dict[str, str]:
    """
    Ссылки для трёх кнопок «Скачать» (Windows, macOS Apple Silicon, macOS Intel) на /auth/unlock.
    Возвращает словарь с ключами 'mac' (Apple Silicon), 'mac_x64' (Intel) и/или 'win'.
    Ключ может отсутствовать, если для платформы ещё нет сборки.
    """
    out: dict[str, str] = {}
    du = desktop_buy_page_download_urls(for_paid_flow=False) or {}
    for k in ("darwin_arm64_bundle", "darwin_arm64"):
        v = du.get(k)
        if isinstance(v, str) and v.strip():
            out["mac"] = v.strip()
            break
    # v3.0+: Mac Intel (x64) для MacBook на Intel CPU (2017-2020).
    # 3.0.5+: предпочитаем bundle.zip (с инструкцией внутри) над raw DMG —
    # симметрично логике mac (arm64). Без инструкции клиенты на Tahoe видят
    # «приложение повреждено» и не знают что делать.
    for k in ("darwin_x64_bundle", "darwin_x64"):
        v = du.get(k)
        if isinstance(v, str) and v.strip():
            out["mac_x64"] = v.strip()
            break
    win = du.get("win32_x64")
    if isinstance(win, str) and win.strip():
        out["win"] = win.strip()
    return out


# ── NOWPayments (продление ключа доступа) ─────────────
# Кабинет: https://account.nowpayments.io — API key, IPN secret, callback URL в настройках IPN.
NOWPAYMENTS_API_KEY: str = (os.getenv("NOWPAYMENTS_API_KEY") or "").strip()
NOWPAYMENTS_IPN_SECRET: str = (os.getenv("NOWPAYMENTS_IPN_SECRET") or "").strip()
NOWPAYMENTS_SANDBOX: bool = os.getenv("NOWPAYMENTS_SANDBOX", "").lower() in ("1", "true", "yes")
NOWPAYMENTS_PRICE_USD_30: str = (os.getenv("NOWPAYMENTS_PRICE_USD_30") or "100").strip()
NOWPAYMENTS_PRICE_USD_90: str = (os.getenv("NOWPAYMENTS_PRICE_USD_90") or "270").strip()
NOWPAYMENTS_PRICE_USD_180: str = (os.getenv("NOWPAYMENTS_PRICE_USD_180") or "499").strip()
NOWPAYMENTS_PRICE_USD_365: str = (os.getenv("NOWPAYMENTS_PRICE_USD_365") or "865").strip()
# Тестовый тариф ($10 и короткий срок) — показывается в формах только при NOWPAYMENTS_TEST_TARIFF_ENABLED=true
NOWPAYMENTS_PRICE_USD_TEST: str = (os.getenv("NOWPAYMENTS_PRICE_USD_TEST") or "10").strip()
# Допуск на недоплату (в %): если получатель прислал чуть меньше указанной суммы из-за
# проскальзывания курса USDT/USD или комиссии сети — считаем платёж успешным. По умолчанию
# 0.5% (0.50 USDT с $100 — типичный «хвост» NOWPayments). 0 = строгое равенство.
# С 2.92 — fallback, если NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_USD = 0.
def _nowpayments_underpayment_tolerance_pct() -> float:
    raw = (os.getenv("NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_PCT") or "0.5").strip().replace(",", ".")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 0.5
    return max(0.0, min(v, 50.0))


NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_PCT: float = _nowpayments_underpayment_tolerance_pct()


# 2.92: Допуск на недоплату (в USD, абсолютный). Если разница
# (pay_amount − actually_paid) ≤ этого значения — считаем платёж успешным.
# По умолчанию $1.00 (по запросу пользователя — было 0.5%).
# Преимущество: одинакова для $79 ($78 → ОК), $139 ($138 → ОК), $2000 ($1999 → ОК).
# 0 = выключить USD-допуск, использовать только PCT.
def _nowpayments_underpayment_tolerance_usd() -> float:
    raw = (os.getenv("NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_USD") or "1.0").strip().replace(",", ".")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 1.0
    return max(0.0, min(v, 100.0))


NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_USD: float = _nowpayments_underpayment_tolerance_usd()


def _nowpayments_test_duration_minutes() -> int:
    raw = (os.getenv("NOWPAYMENTS_TEST_DURATION_MINUTES") or "3").strip()
    try:
        n = int(raw)
        return max(1, min(n, 24 * 60))
    except ValueError:
        return 3


NOWPAYMENTS_TEST_DURATION_MINUTES: int = _nowpayments_test_duration_minutes()
# В billing_renewal_orders.duration_days: -1 = тестовый доступ по минутам (см. NOWPAYMENTS_TEST_DURATION_MINUTES).
NOWPAYMENTS_TEST_ORDER_DURATION_SENTINEL: int = -1


def nowpayments_test_tariff_option_label() -> str:
    """Подпись опции «тест» в селекте оплаты (склонение «минута»)."""
    m = NOWPAYMENTS_TEST_DURATION_MINUTES
    p = NOWPAYMENTS_PRICE_USD_TEST
    if m % 10 == 1 and m % 100 != 11:
        mw = "минута"
    elif m % 10 in (2, 3, 4) and m % 100 not in (12, 13, 14):
        mw = "минуты"
    else:
        mw = "минут"
    return f"Тест — {p} USD ({m} {mw} доступа)"


def nowpayments_test_tariff_enabled() -> bool:
    return os.getenv("NOWPAYMENTS_TEST_TARIFF_ENABLED", "").lower() in ("1", "true", "yes")


def nowpayments_enabled() -> bool:
    return bool(NOWPAYMENTS_API_KEY and NOWPAYMENTS_IPN_SECRET)


def nowpayments_api_base() -> str:
    return (
        "https://api-sandbox.nowpayments.io"
        if NOWPAYMENTS_SANDBOX
        else "https://api.nowpayments.io"
    )


def public_app_base_url() -> str:
    """Публичный URL без завершающего слэша (ссылки для NOWPayments)."""
    u = (os.getenv("APP_BASE_URL") or "http://localhost:8000").strip().rstrip("/")
    return u


def is_local_loopback_app_base() -> bool:
    """True, если APP_BASE_URL указывает на локальный встроенный бэкенд (Electron)."""
    u = public_app_base_url().lower()
    return u.startswith("http://127.0.0.1") or u.startswith("http://localhost")


def remote_https_checkout_url_for_local_app() -> str | None:
    """
    Для локального кабинета без NOWPayments: HTTPS-URL страницы оплаты на проде
    (из FB_MASTER_LICENSE_API_BASE / FB_MASTER_MARKETING_PURCHASE_URL).
    """
    if not is_local_loopback_app_base():
        return None
    url = marketing_purchase_page_url().strip()
    if not url.lower().startswith("https://"):
        return None
    return url


# ── LavaTop (карты RU/EU + recurring подписки) ────────
# Кабинет: https://app.lava.top/integrations/public-api — API key + два webhook'а
# (Результат платежа + Регулярный платеж) с Basic Auth.
LAVATOP_API_KEY: str = (os.getenv("LAVATOP_API_KEY") or "").strip()
LAVATOP_API_BASE: str = (os.getenv("LAVATOP_API_BASE") or "https://gate.lava.top").strip().rstrip("/")
LAVATOP_WEBHOOK_LOGIN: str = (os.getenv("LAVATOP_WEBHOOK_LOGIN") or "").strip()
LAVATOP_WEBHOOK_PASSWORD: str = (os.getenv("LAVATOP_WEBHOOK_PASSWORD") or "").strip()

# Parent product UUID (общий для всех 4 подписок-тарифов).
LAVATOP_PRODUCT_ID: str = (os.getenv("LAVATOP_PRODUCT_ID") or "").strip()

# Offer (product) IDs создаются вручную в кабинете LavaTop под каждый тариф.
LAVATOP_OFFER_ID_30: str = (os.getenv("LAVATOP_OFFER_ID_30") or "").strip()
LAVATOP_OFFER_ID_90: str = (os.getenv("LAVATOP_OFFER_ID_90") or "").strip()
LAVATOP_OFFER_ID_180: str = (os.getenv("LAVATOP_OFFER_ID_180") or "").strip()
LAVATOP_OFFER_ID_365: str = (os.getenv("LAVATOP_OFFER_ID_365") or "").strip()

# Цены в USD (LavaTop сам конвертирует в RUB/EUR на checkout-странице).
# ВАЖНО: должны совпадать с настройками в кабинете LavaTop, иначе клиент увидит расхождение.
LAVATOP_PRICE_USD_30: str = (os.getenv("LAVATOP_PRICE_USD_30") or "100").strip()
LAVATOP_PRICE_USD_90: str = (os.getenv("LAVATOP_PRICE_USD_90") or "270").strip()
LAVATOP_PRICE_USD_180: str = (os.getenv("LAVATOP_PRICE_USD_180") or "499").strip()
LAVATOP_PRICE_USD_365: str = (os.getenv("LAVATOP_PRICE_USD_365") or "865").strip()


def lavatop_price_usd(duration_days: int) -> str:
    """Цена в USD для тарифа из конфига LavaTop."""
    return {
        30: LAVATOP_PRICE_USD_30,
        90: LAVATOP_PRICE_USD_90,
        180: LAVATOP_PRICE_USD_180,
        365: LAVATOP_PRICE_USD_365,
    }.get(int(duration_days), "0")


def lavatop_offer_id(duration_days: int) -> str:
    return {
        30: LAVATOP_OFFER_ID_30,
        90: LAVATOP_OFFER_ID_90,
        180: LAVATOP_OFFER_ID_180,
        365: LAVATOP_OFFER_ID_365,
    }.get(int(duration_days), "")


def lavatop_recurring_default() -> bool:
    """Подписка с автопродлением включена по умолчанию (решение 2026-04-29)."""
    raw = (os.getenv("LAVATOP_RECURRING_DEFAULT") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def lavatop_enabled() -> bool:
    """LavaTop активен, когда заданы API-ключ и креды webhook-а."""
    return bool(LAVATOP_API_KEY and LAVATOP_WEBHOOK_LOGIN and LAVATOP_WEBHOOK_PASSWORD)


# ── Brevo (Sendinblue) — отправка email с ключом доступа ─────────────────
# https://app.brevo.com/settings/keys/api — 300 писем/день навсегда бесплатно.
BREVO_API_KEY: str = (os.getenv("BREVO_API_KEY") or "").strip()
BREVO_FROM_EMAIL: str = (os.getenv("BREVO_FROM_EMAIL") or "").strip()
BREVO_FROM_NAME: str = (os.getenv("BREVO_FROM_NAME") or "SOCMASTER").strip()


def brevo_api_base() -> str:
    return "https://api.brevo.com"


def brevo_enabled() -> bool:
    return bool(BREVO_API_KEY and BREVO_FROM_EMAIL)


# ── Унифицированный email helper (Brevo приоритетен, Mailgun deprecated) ──
def email_provider_enabled() -> bool:
    """Включён ли хотя бы один email-провайдер (Brevo или Mailgun fallback)."""
    return brevo_enabled() or mailgun_enabled()


# ── Mailgun (DEPRECATED — заменён Brevo, оставлен как fallback) ────────────
MAILGUN_API_KEY: str = (os.getenv("MAILGUN_API_KEY") or "").strip()
MAILGUN_DOMAIN: str = (os.getenv("MAILGUN_DOMAIN") or "").strip().lower()
MAILGUN_FROM_EMAIL: str = (os.getenv("MAILGUN_FROM_EMAIL") or "").strip()
MAILGUN_FROM_NAME: str = (os.getenv("MAILGUN_FROM_NAME") or "SOCMASTER").strip()
MAILGUN_REGION: str = (os.getenv("MAILGUN_REGION") or "us").strip().lower()


def mailgun_api_base() -> str:
    return "https://api.eu.mailgun.net" if MAILGUN_REGION == "eu" else "https://api.mailgun.net"


def mailgun_enabled() -> bool:
    return bool(MAILGUN_API_KEY and MAILGUN_DOMAIN and MAILGUN_FROM_EMAIL)
