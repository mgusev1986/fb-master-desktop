"""Playwright: проверка сессии Facebook, тест прокси, окно ручного входа (sync API)."""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.config import BASE_DIR, effective_fb_local_chrome_cookies

logger = logging.getLogger(__name__)

_STORAGE_STATE_TIMEOUT_SEC = 14.0

# Окно «Войти в Facebook»: не открываем сразу корень www — у Meta часто отдельный лимит/редирект на /login
# с текстом «временно заблокированы», при этом чат на /messages/ с теми же cookies может работать
# (как во встроенном Messenger). Лента и настройки по-прежнему доступны из меню внутри FB.
FB_LOGIN_WINDOW_INITIAL_URL = "https://www.facebook.com/messages/"


def playwright_chromium_precheck() -> str | None:
    """
    Перед запуском окна «Войти в Facebook» / автовхода без CDP.
    None — Chromium найден; иначе короткое сообщение для пользователя (flash).
    """
    import os

    from backend.config import BASE_DIR, _bundled_chromium_executable, reapply_playwright_browsers_path

    reapply_playwright_browsers_path()

    browsers_root = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    root_path = Path(browsers_root) if browsers_root else (BASE_DIR / "playwright-browsers")
    bundled = _bundled_chromium_executable(root_path.resolve())
    if bundled is None:
        return (
            "В установке FB Master не найден встроенный Chromium (папка playwright-browsers). "
            "Скачайте полный установщик с сайта и переустановите приложение. "
            "На Mac: xattr -cr \"/Applications/FB Master.app\""
        )

    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            # Playwright 1.49+: channel=\"chromium\" — полный Chromium из PLAYWRIGHT_BROWSERS_PATH,
            # а не отдельный chromium-headless-shell (его нет в бандле клиента).
            # У Browser нет executable_path — только у BrowserType; успех = launch без исключения.
            browser = None
            try:
                browser = p.chromium.launch(headless=True, channel="chromium")
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        logger.exception("playwright_chromium_precheck")
        return (
            f"Не удалось запустить Chromium: {e!s}"[:400]
            + " Переустановите приложение с сайта или на Mac выполните: xattr -cr \"/Applications/FB Master.app\""
        )
    return None


def playwright_goto_failure_hint(exc: BaseException) -> str:
    """
    Короткая подсказка по тексту исключения Playwright/Chromium (net::… при навигации).
    Не заменяет исходную ошибку — дополняет для типичных обрывов через прокси.
    """
    low = str(exc).lower()
    if "err_connection_reset" in low or "err_connection_aborted" in low:
        return (
            " Обычно это прокси или фильтр: соединение с Facebook обрывается по пути. "
            "Проверьте прокси в карточке аккаунта (строка, логин/пароль, тип HTTP/HTTPS), "
            "смените прокси или временно отключите его и повторите проверку со своей сети."
        )
    if "err_connection_refused" in low:
        return " Узел отклонил соединение — проверьте адрес и порт прокси или что сервер прокси запущен."
    if "err_proxy_connection_failed" in low:
        return " Не удалось подключиться к прокси-серверу — проверьте хост, порт и доступность."
    if "err_timed_out" in low or "timeout" in low:
        return " Таймаут — прокси или сеть слишком медленные, либо Facebook временно недоступен."
    if "err_name_not_resolved" in low:
        return " DNS не смог разрешить имя — проверьте сеть и настройки прокси."
    return ""


_CONTEXT_CLOSE_TIMEOUT_SEC = 12.0


def _run_playwright_cleanup_call(
    label: str,
    fn: Callable[[], Any],
    timeout_sec: float,
) -> tuple[bool, Any | None, BaseException | None]:
    """
    Run a best-effort Playwright cleanup call without letting it block automation slots.

    ThreadPoolExecutor used as a context manager waits for the worker on exit even
    after Future.result(timeout=...) times out. If ctx.storage_state()/ctx.close()
    gets stuck, outreach can finish the queue but never release the account/global
    Playwright locks until the app is restarted.
    """
    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - cleanup failures must not block slots.
            result["exc"] = exc

    th = threading.Thread(target=_target, name=f"fbm-playwright-cleanup-{label}", daemon=True)
    th.start()
    th.join(max(0.1, float(timeout_sec)))
    if th.is_alive():
        return False, None, None
    exc = result.get("exc")
    if isinstance(exc, BaseException):
        return True, None, exc
    return True, result.get("value"), None


def _storage_state_with_timeout(ctx: Any, timeout_sec: float = _STORAGE_STATE_TIMEOUT_SEC) -> dict[str, Any] | None:
    """Снимок сессии без бесконечного ожидания (после kill Chromium sync API может зависнуть)."""
    if ctx is None:
        return None

    def _call() -> dict[str, Any]:
        return ctx.storage_state()

    done, value, exc = _run_playwright_cleanup_call("storage-state", _call, timeout_sec)
    if not done:
        logger.warning(
            "ctx.storage_state() превысила %.0f с (account cleanup) — снимок пропущен",
            timeout_sec,
        )
        return None
    if exc is not None:
        logger.debug(
            "storage_state_with_timeout",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return None
    if isinstance(value, dict):
        return value
    return None


def _close_browser_or_context_with_timeout(
    browser: Any,
    ctx: Any,
    timeout_sec: float = _CONTEXT_CLOSE_TIMEOUT_SEC,
) -> None:
    def _call() -> None:
        try:
            if browser is not None:
                browser.close()
            elif ctx is not None:
                ctx.close()
        except Exception:
            pass

    done, _value, exc = _run_playwright_cleanup_call("close-context", _call, timeout_sec)
    if not done:
        logger.error(
            "Закрытие браузера/контекста превысило %.0f с — UI разблокирован, процесс Chromium может остаться в фоне",
            timeout_sec,
        )
    elif exc is not None:
        logger.debug(
            "close_browser_or_context_with_timeout",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


# Один активный «окно входа» на аккаунт
_login_locks: dict[int, threading.Lock] = {}
_active_login: set[int] = set()
_login_threads: dict[int, threading.Thread] = {}
# Запас выше цикла ожидания (~7200 с) + навигация, иначе можно снять флаг при ещё живом потоке и открыть второй Chromium на тот же профиль
_LOGIN_THREAD_MAX_MONO_SEC = 10_800.0


def _lock_for(account_id: int) -> threading.Lock:
    if account_id not in _login_locks:
        _login_locks[account_id] = threading.Lock()
    return _login_locks[account_id]


def _has_storage_cookies(storage_state: dict[str, Any] | None) -> bool:
    if not storage_state or not isinstance(storage_state, dict):
        return False
    c = storage_state.get("cookies")
    return isinstance(c, list) and len(c) > 0


def _sanitize_cookie_for_playwright(c: dict[str, Any]) -> dict[str, Any] | None:
    """Оставляем поля, которые принимает BrowserContext.add_cookies (без partitionKey и пр.)."""
    if not isinstance(c, dict):
        return None
    name = c.get("name")
    val = c.get("value")
    if name is None or val is None:
        return None
    out: dict[str, Any] = {"name": str(name), "value": str(val)}
    url = c.get("url")
    domain = c.get("domain")
    if url:
        out["url"] = str(url).strip()
    elif domain:
        out["domain"] = str(domain).strip()
        out["path"] = str(c.get("path") or "/").strip() or "/"
    else:
        return None
    if c.get("expires") is not None and c.get("expires") != -1:
        try:
            out["expires"] = float(c["expires"])
        except (TypeError, ValueError):
            pass
    if c.get("httpOnly") is not None:
        out["httpOnly"] = bool(c["httpOnly"])
    if c.get("secure") is not None:
        out["secure"] = bool(c["secure"])
    ss = c.get("sameSite")
    if ss is not None and str(ss).strip():
        val = str(ss).strip()
        if val in ("Strict", "Lax", "None"):
            out["sameSite"] = val
        # "Unspecified" and other non-standard values: omit → browser uses default (Lax)
    return out


def apply_storage_state_cookies_to_context(ctx, storage_state: dict[str, Any] | None) -> int:
    """
    Подмешать cookies из снимка БД в persistent-контекст.

    Раньше storage_state передавали в launch только при пустой папке профиля; после появления
    Default/Local State на диске актуальные cookies из БД игнорировались — открывался login.php.
    """
    if not _has_storage_cookies(storage_state):
        return 0
    raw_list = storage_state.get("cookies")
    if not isinstance(raw_list, list):
        return 0
    cleaned: list[dict[str, Any]] = []
    for c in raw_list:
        sc = _sanitize_cookie_for_playwright(c) if isinstance(c, dict) else None
        if sc:
            cleaned.append(sc)
    if not cleaned:
        return 0
    n_ok = 0
    try:
        ctx.add_cookies(cleaned)
        n_ok = len(cleaned)
    except Exception:
        logger.debug("add_cookies batch failed, per-cookie fallback", exc_info=True)
        for one in cleaned:
            try:
                ctx.add_cookies([one])
                n_ok += 1
            except Exception:
                continue
    if n_ok:
        logger.info(
            "Подмешано %s cookie из снимка БД в контекст Playwright (профиль на диске мог устареть)",
            n_ok,
        )
    return n_ok


def _facebook_cookies_from_local_chrome() -> list[dict[str, Any]]:
    mod_path = BASE_DIR / "chrome_cookies.py"
    if not mod_path.is_file():
        return []
    spec = importlib.util.spec_from_file_location("_fb_root_chrome_cookies", mod_path)
    if not spec or not spec.loader:
        return []
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "facebook_cookies_for_playwright", None)
    if not callable(fn):
        return []
    try:
        raw = fn()
    except Exception:
        logger.exception("facebook_cookies_for_playwright")
        return []
    return raw if isinstance(raw, list) else []


def _inject_chrome_cookies_into_context(ctx) -> int:
    items = _facebook_cookies_from_local_chrome()
    if not items:
        return 0
    n_ok = 0
    try:
        ctx.add_cookies(items)
        return len(items)
    except Exception:
        for c in items:
            try:
                ctx.add_cookies([c])
                n_ok += 1
            except Exception:
                continue
        return n_ok


def inject_local_chrome_facebook_cookies_and_reload(
    ctx,
    page,
    *,
    timeout_ms: int = 90_000,
    log_warning_if_zero: bool = True,
) -> int:
    """
    Локальная разработка (FB_LOCAL_CHROME_COOKIES / не в режиме LOCAL_SIMULATE_PRODUCTION): cookies из Chrome,
    добавить в контекст Playwright и перезагрузить страницу. Закройте Chrome полностью (Cmd+Q),
    иначе SQLite с cookies может быть заблокирован.
    """
    if not effective_fb_local_chrome_cookies():
        return 0
    n = _inject_chrome_cookies_into_context(ctx)
    if n <= 0:
        if log_warning_if_zero:
            logger.warning(
                "FB_LOCAL_CHROME_COOKIES=true, но не удалось прочитать/подставить cookies из Chrome "
                "(полностью закройте Google Chrome Cmd+Q и проверьте пакет browser-cookie3)."
            )
        return 0
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        logger.exception("reload after Chrome cookie inject")
    return n


def maybe_reload_after_local_chrome_cookies(
    *,
    ctx,
    page,
    storage_state: dict[str, Any] | None,
    profile_was_empty_before_launch: bool,
    timeout_ms: int = 90_000,
) -> None:
    """
    После первого перехода на facebook.com: подставить cookies из локального Chrome,
    если включена подстановка cookies из Chrome и нет сессии ни на диске (до запуска), ни в storage_state.
    """
    if not effective_fb_local_chrome_cookies():
        return
    if not profile_was_empty_before_launch or _has_storage_cookies(storage_state):
        return
    inject_local_chrome_facebook_cookies_and_reload(
        ctx, page, timeout_ms=timeout_ms, log_warning_if_zero=True
    )


def prime_persistent_context_local_chrome(
    ctx,
    profile_dir: str | Path,
    storage_state: dict[str, Any] | None,
    *,
    profile_was_empty_before_launch: bool,
    timeout_ms: int = 90_000,
) -> None:
    """Первый заход на FB в headless/автоматизации при пустом профиле без снимка в БД."""
    if not effective_fb_local_chrome_cookies():
        return
    if not profile_was_empty_before_launch or _has_storage_cookies(storage_state):
        return
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(
            "https://www.facebook.com/",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
    except Exception:
        logger.exception("prime_persistent_context_local_chrome: goto")
        return
    maybe_reload_after_local_chrome_cookies(
        ctx=ctx,
        page=page,
        storage_state=storage_state,
        profile_was_empty_before_launch=profile_was_empty_before_launch,
        timeout_ms=timeout_ms,
    )


def profile_has_chromium_data(profile_dir: str | Path) -> bool:
    """Есть ли на диске уже данные Chromium (тогда сессия живёт в папке профиля)."""
    p = Path(profile_dir)
    if not p.is_dir():
        return False
    for name in ("Default", "Local State", "SingletonCookie", "first_party_sets.db"):
        if (p / name).exists():
            return True
    try:
        return any(p.iterdir())
    except OSError:
        return False


def chromium_profile_dir_locked(profile_dir: str | Path) -> bool:
    """
    True, если user-data-dir, скорее всего, уже открыт другим процессом Chromium
    (окно «Войти в Facebook», встроенный мессенджер с общим диском и т.п.).
    """
    p = Path(profile_dir)
    if not p.is_dir():
        return False
    for rel in ("SingletonLock", "SingletonSocket"):
        try:
            if (p / rel).exists():
                return True
        except OSError:
            pass
    default = p / "Default"
    if default.is_dir():
        for rel in ("SingletonLock", "SingletonSocket"):
            try:
                if (default / rel).exists():
                    return True
            except OSError:
                pass
    return False


def _playwright_profile_in_use_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(
        part in s
        for part in (
            "target page",
            "context or browser has been closed",
            "browser has been closed",
            "singleton",
            "profile is already in use",
            "failed to create data directory",
        )
    )


def _launch_ephemeral_chromium_for_check(p, bundled: dict[str, Any]) -> tuple[Any, Any]:
    """Отдельный Chromium без persistent user-data-dir (проверка только по cookies из БД)."""
    kw = dict(bundled["kwargs"])
    launch_kwargs: dict[str, Any] = {
        "headless": kw.get("headless", True),
        "args": list(kw.get("args") or []),
        "proxy": kw.get("proxy"),
    }
    ch = kw.get("channel")
    if ch:
        launch_kwargs["channel"] = ch
    browser = p.chromium.launch(**launch_kwargs)
    ctx_parts: dict[str, Any] = {
        "locale": kw.get("locale") or "ru-RU",
        "user_agent": kw.get("user_agent"),
        "viewport": kw.get("viewport") or {"width": 1280, "height": 1200},
        "proxy": kw.get("proxy"),
    }
    tz = kw.get("timezone_id")
    if tz:
        ctx_parts["timezone_id"] = tz
    ctx = browser.new_context(**{k: v for k, v in ctx_parts.items() if v is not None})
    return browser, ctx


def proxy_dict_for_playwright(
    *,
    enabled: bool,
    url: str | None,
    username: str | None,
    password: str | None,
) -> dict[str, Any] | None:
    if not enabled or not (url or "").strip():
        return None
    raw = url.strip()
    proxy: dict[str, Any] = {"server": raw}
    u = (username or "").strip()
    pw = (password or "").strip()
    if u:
        proxy["username"] = u
    if pw:
        proxy["password"] = pw
    return proxy


def page_meta_rate_limited_or_blocked(page: Any) -> tuple[bool, str]:
    """
    Типичные тексты модалок Meta: «временно заблокированы», слишком частые входы и т.п.
    Не путать с обычной формой логина — даёт понятное сообщение в «Проверить сессию».
    """
    try:
        text = page.evaluate(
            """() => {
              const b = document.body;
              if (!b) return '';
              return (b.innerText || '').slice(0, 12000);
            }"""
        )
        if not text or not isinstance(text, str):
            return False, ""
        t = text.lower()
        # Турецкий (как на скрине пользователя)
        if "geçici olarak engellendin" in t or "geçici olarak engellendi" in t:
            return (
                True,
                "Facebook показал ограничение (турецкий интерфейс): аккаунт или IP временно "
                "заблокированы за частое использование функции входа. Подождите несколько часов, "
                "используйте резидентский прокси под ГЕО аккаунта, не нажимайте «Войти» подряд много раз.",
            )
        if "bu özelliği çok sık" in t or "kötüye kullanıyordun" in t:
            return (
                True,
                "Facebook: «слишком часто использовали эту функцию» — временный стоп со стороны Meta, "
                "не ошибка FB Master. Смените IP/прокси, сделайте паузу.",
            )
        # Русский
        if "временно заблокирован" in t and ("функци" in t or "слишком часто" in t or "отключил" in t):
            return (
                True,
                "Facebook временно ограничил вход или функцию (частые попытки). Подождите, смените прокси под регион аккаунта.",
            )
        # Английский
        if ("temporarily blocked" in t or "temporarily disabled" in t) and (
            "too often" in t or "misus" in t or "feature" in t
        ):
            return (
                True,
                "Facebook: temporary block / rate limit (English UI). Wait and use a matching residential proxy; reduce login attempts.",
            )
    except Exception:
        logger.debug("page_meta_rate_limited_or_blocked", exc_info=True)
    return False, ""


def page_has_facebook_remembered_login_gate(page: Any) -> tuple[bool, str]:
    """
    Экран «быстрого входа»: Facebook помнит профиль, но полноценная сессия уже истекла.
    Обычно видны «Продолжить» и «Использовать другой профиль», а затем Facebook просит пароль.
    """
    try:
        url = (page.url or "").lower()
        probe = page.evaluate(
            """() => {
              const text = ((document.body && document.body.innerText) || '').slice(0, 12000);
              const hasPassword = !!document.querySelector(
                'input[name="pass"], input#pass, input[type="password"]'
              );
              const hasContinue = Array.from(
                document.querySelectorAll('button, a, div[role="button"], input[type="submit"], input[type="button"]')
              ).some((el) => {
                const txt = String(el.innerText || el.value || '').trim().toLowerCase();
                return txt === 'continue' || txt === 'продолжить';
              });
              const hasAltProfile = /использовать другой профиль|use another account|use another profile|создать новый аккаунт|create new account/i.test(text);
              return { hasPassword, hasContinue, hasAltProfile };
            }"""
        )
        if not isinstance(probe, dict):
            return False, ""
        if probe.get("hasPassword"):
            return False, ""
        if not probe.get("hasContinue"):
            return False, ""
        if "facebook.com/index.php?next=" not in url and not probe.get("hasAltProfile"):
            return False, ""
        return (
            True,
            "Facebook помнит профиль, но полноценная сессия уже истекла: нажмите «Продолжить», "
            "затем введите пароль. Такое бывает после истечения cookies, смены IP/прокси "
            "или дополнительной проверки Meta.",
        )
    except Exception:
        logger.debug("page_has_facebook_remembered_login_gate", exc_info=True)
    return False, ""


def page_requires_facebook_login(page: Any) -> tuple[bool, str]:
    """
    Эвристика: гостевая страница, чекпоинт или модальное окно входа (как на /friends без сессии).
    Возвращает (True, краткая причина) или (False, '').
    """
    try:
        url = (page.url or "").lower()
        if any(
            x in url
            for x in (
                "/login",
                "/checkpoint",
                "/recover",
                "/device",
                "facebook.com/reg",
            )
        ):
            return True, "Открылась страница входа или проверки безопасности"
        remembered_gate, remembered_msg = page_has_facebook_remembered_login_gate(page)
        if remembered_gate:
            return True, remembered_msg
        guest = page.evaluate(
            """() => {
              const email = document.querySelector(
                'input[name="email"], input#email, input[type="email"][autocomplete="username"]'
              );
              const pass = document.querySelector('input[name="pass"], input[type="password"]#pass');
              if (email && pass) return true;
              return false;
            }"""
        )
        if guest:
            return True, "Видна форма входа Facebook (сессия недействительна или истекла)"
    except Exception:
        logger.debug("page_requires_facebook_login: evaluate failed", exc_info=True)
        return False, ""
    return False, ""


def try_click_facebook_remembered_continue(page: Any) -> bool:
    gate, _msg = page_has_facebook_remembered_login_gate(page)
    if not gate:
        return False
    for sel in (
        'button:has-text("Продолжить")',
        'button:has-text("Continue")',
        'div[role="button"]:has-text("Продолжить")',
        'div[role="button"]:has-text("Continue")',
        'a:has-text("Продолжить")',
        'a:has-text("Continue")',
        'input[type="submit"][value="Continue"]',
        'input[type="submit"][value="Продолжить"]',
        'input[type="button"][value="Continue"]',
        'input[type="button"][value="Продолжить"]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=800):
                continue
            loc.click(timeout=1_500)
            page.wait_for_timeout(700)
            logger.info("Facebook: auto-clicked remembered-login Continue gate")
            return True
        except Exception:
            continue
    return False


def fb_c_user_from_storage_state(storage_state: dict[str, Any] | None) -> str | None:
    """Значение cookie c_user (числовой ID профиля Facebook), если есть."""
    if not isinstance(storage_state, dict):
        return None
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        return None
    for c in cookies:
        if isinstance(c, dict) and c.get("name") == "c_user":
            dom = (c.get("domain") or "").lower()
            if dom and "facebook" not in dom:
                continue
            v = (c.get("value") or "").strip()
            return v or None
    for c in cookies:
        if isinstance(c, dict) and c.get("name") == "c_user":
            v = (c.get("value") or "").strip()
            return v or None
    return None


def _storage_cookie_names(storage_state: dict[str, Any] | None) -> list[str]:
    if not isinstance(storage_state, dict):
        return []
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        return []
    out: list[str] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if name:
            out.append(name)
    return out


def explain_fb_session_drop_after_first_request(
    injected_state: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
) -> str | None:
    """
    Различаем две ситуации:
    1. В импортированном снимке c_user вообще не было.
    2. c_user был, но после первого запроса к Facebook сайт сам сбросил auth-cookies.
    """
    before_c_user = fb_c_user_from_storage_state(injected_state)
    if not before_c_user:
        return None
    after_c_user = fb_c_user_from_storage_state(snapshot)
    if after_c_user:
        return None
    after_names = _storage_cookie_names(snapshot)
    if after_names:
        tail = " После открытия страницы остались только cookies: " + ", ".join(after_names[:8]) + "."
    else:
        tail = " После открытия страницы авторизационные cookies в снимке вообще не остались."
    return (
        "Во входном файле cookie c_user был, но после первого запроса Facebook сбросил авторизационные cookies. "
        "Обычно это значит, что сессия уже истекла, продавец дал неполный/битый вход "
        "или для этой сессии нужен родной IP/прокси и близкий отпечаток браузера."
        + tail
    )


def _looks_like_fb_numeric_user_id(s: str) -> bool:
    """Только длинные числовые id Facebook (не телефон 10–11 цифр)."""
    t = (s or "").strip()
    return t.isdigit() and len(t) >= 15


def evaluate_fb_storage_session(
    storage_state: dict[str, Any] | None,
    *,
    expected_login: str | None = None,
) -> tuple[bool, str]:
    """
    Оценка снимка Playwright storage_state: для «вошли в FB» нужен cookie c_user.
    Если в карточке указан числовой ID (как у маркетплейсов), он должен совпадать с c_user.
    """
    if not isinstance(storage_state, dict):
        return False, "Нет данных снимка сессии"
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list) or len(cookies) == 0:
        return False, "В снимке нет cookies"
    c_user = fb_c_user_from_storage_state(storage_state)
    if not c_user:
        return (
            False,
            "Нет cookie c_user — это не полноценный вход в Facebook (часто так бывает на странице логина)",
        )
    exp = (expected_login or "").strip()
    if _looks_like_fb_numeric_user_id(exp) and c_user != exp:
        return (
            False,
            f"В сессии другой аккаунт Facebook (ID {c_user}), в карточке ожидается {exp}",
        )
    return True, ""


# Убирает инфобар «Браузером Chrome управляет автоматизированное ПО» (частично снижает триггеры Meta)
_PLAYWRIGHT_IGNORE_DEFAULT_ARGS = ["--enable-automation"]


def pregrant_facebook_automation_permissions(ctx) -> None:
    """
    Нативный prompt Chromium «разрешить уведомления» перехватывает фокус и блокирует скролл парсера.
    Предвыдаём notifications для origin Facebook — диалог не показывается (для рассылки/прогрева то же).
    """
    origins = (
        "https://www.facebook.com",
        "https://facebook.com",
        "https://m.facebook.com",
    )
    for origin in origins:
        try:
            ctx.grant_permissions(["notifications"], origin=origin)
        except Exception:
            logger.debug("pregrant_facebook_automation_permissions origin=%s", origin, exc_info=True)


def launch_persistent_context_from_bundle(p, profile_path: str | Path, bundled: dict[str, Any]):
    """launch_persistent_context с ignore_default_args из bundle (_launch_kwargs_persistent)."""
    kwargs = dict(bundled["kwargs"])
    ign = bundled.get("ignore_default_args")
    if ign:
        return p.chromium.launch_persistent_context(
            str(profile_path), ignore_default_args=list(ign), **kwargs
        )
    return p.chromium.launch_persistent_context(str(profile_path), **kwargs)


def _launch_kwargs_persistent(
    *,
    headless: bool,
    proxy: dict[str, Any] | None,
    stealth_bundle: dict[str, Any] | None,
) -> dict[str, Any]:
    """Сборка kwargs для launch_persistent_context (stealth или legacy ru-RU)."""
    if stealth_bundle:
        init_script = stealth_bundle.get("init_script") or ""
        base = {
            "headless": headless,
            "locale": stealth_bundle["locale"],
            "timezone_id": stealth_bundle["timezone_id"],
            "user_agent": stealth_bundle["user_agent"],
            "viewport": stealth_bundle["viewport"],
            "args": stealth_bundle["args"],
            "proxy": proxy,
        }
        if headless:
            base["channel"] = "chromium"
        else:
            # Видимый Chromium: без фиксированного viewport — иначе полоса снизу (composer Messenger)
            # часто оказывается «вне» эмулируемой высоты и оператор видит пустую чёрную зону.
            base["no_viewport"] = True
            base.pop("viewport", None)
            args = list(base.get("args") or [])
            if not any("--start-maximized" in str(a) for a in args):
                args.append("--start-maximized")
            base["args"] = args
        return {
            "kwargs": base,
            "init_script": init_script,
            "ignore_default_args": _PLAYWRIGHT_IGNORE_DEFAULT_ARGS,
        }
    legacy = {
        "headless": headless,
        "locale": "ru-RU",
        "viewport": {"width": 1280, "height": 1380},
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-notifications",
            "--window-size=1280,1580",
        ],
        "proxy": proxy,
    }
    if headless:
        legacy["channel"] = "chromium"
    else:
        legacy["no_viewport"] = True
        legacy.pop("viewport", None)
        legacy["args"] = legacy["args"] + ["--start-maximized"]
    return {
        "kwargs": legacy,
        "init_script": "",
        "ignore_default_args": _PLAYWRIGHT_IGNORE_DEFAULT_ARGS,
    }


def _attach_via_cdp(p, endpoint: str) -> tuple[Any, Any]:
    """
    Подключение к уже запущенному Chromium. Возвращает (browser, context).
    Завершение: browser.close() — отключение Playwright от CDP; сам процесс браузера может остаться.
    """
    ep = (endpoint or "").strip()
    if not ep:
        raise ValueError("пустой CDP endpoint")
    browser = p.chromium.connect_over_cdp(ep)
    if browser.contexts:
        ctx = browser.contexts[0]
    else:
        ctx = browser.new_context()
    return browser, ctx


def check_facebook_session(
    profile_dir: str,
    proxy: dict[str, Any] | None,
    *,
    storage_state: dict[str, Any] | None = None,
    stealth_bundle: dict[str, Any] | None = None,
    timeout_ms: int = 55_000,
    cdp_endpoint: str | None = None,
    expected_login: str | None = None,
    account_id: int | None = None,
) -> tuple[bool | None, str, dict[str, Any] | None]:
    """
    Headless-проверка сессии FB.
    Возвращает (ok, сообщение, storage_state для сохранения в БД при ok=True).
    При cdp_endpoint подключается к внешнему браузеру (антидетект / Chrome с remote debugging).
    """
    from playwright.sync_api import sync_playwright

    cdp = (cdp_endpoint or "").strip()
    path = Path(profile_dir) if (profile_dir or "").strip() else None

    if not cdp:
        if path is None or not path.is_dir():
            return None, "Папка профиля не найдена", None
        profile_was_empty = not profile_has_chromium_data(str(path))
    else:
        profile_was_empty = False
        if proxy:
            logger.warning(
                "CDP: прокси из настроек аккаунта не применяется к уже запущенному браузеру — задайте прокси в профиле внешнего браузера"
            )

    bundled = _launch_kwargs_persistent(headless=True, proxy=proxy, stealth_bundle=stealth_bundle)
    init_s_persistent = bundled["init_script"]
    init_s_cdp = ""
    if stealth_bundle and isinstance(stealth_bundle, dict):
        init_s_cdp = stealth_bundle.get("init_script") or ""

    login_busy = bool(account_id is not None and login_window_busy(int(account_id)))
    profile_busy = bool(path is not None and chromium_profile_dir_locked(path))
    use_ephemeral_disk = (not cdp) and (login_busy or profile_busy)
    if use_ephemeral_disk and not _has_storage_cookies(storage_state):
        return (
            None,
            "Папка профиля занята (окно «Войти в Facebook» или встроенный мессенджер). "
            "Пока окно входа открыто, сессия каждые ~30 с подтягивается в базу — подождите минуту после успешного входа "
            "и снова нажмите «Проверить вход», либо закройте окно Chromium (сессия сохранится при закрытии). "
            "Если открыт мессенджер с тем же профилем — закройте его вкладку или перезагрузите страницу мессенджера.",
            None,
        )

    with sync_playwright() as p:
        browser = None
        ctx = None
        try:
            if cdp:
                try:
                    browser, ctx = _attach_via_cdp(p, cdp)
                except Exception as e:
                    logger.exception("connect_over_cdp (check)")
                    return (
                        None,
                        "CDP: не удалось подключиться к «"
                        + cdp[:80]
                        + "». Запустите профиль антидетекта или Chrome с remote debugging "
                        "(см. .env FB_CDP_ENDPOINT). Ошибка: "
                        + str(e)[:200],
                        None,
                    )
                if init_s_cdp:
                    try:
                        ctx.add_init_script(init_s_cdp)
                    except Exception:
                        logger.debug("add_init_script (check cdp)", exc_info=True)
            elif use_ephemeral_disk:
                try:
                    browser, ctx = _launch_ephemeral_chromium_for_check(p, bundled)
                except Exception as e:
                    logger.exception("launch ephemeral (check, busy profile)")
                    return (
                        None,
                        "Не удалось запустить браузер для проверки. "
                        "Закройте окно входа и мессенджер для этого профиля и повторите. "
                        f"Детали: {e!s}"[:320],
                        None,
                    )
                if init_s_persistent:
                    try:
                        ctx.add_init_script(init_s_persistent)
                    except Exception:
                        logger.debug("add_init_script (check ephemeral)", exc_info=True)
            else:
                init_already_applied = False
                try:
                    ctx = launch_persistent_context_from_bundle(p, path, bundled)
                except Exception as e:
                    logger.exception("launch_persistent_context (check)")
                    if _has_storage_cookies(storage_state) and _playwright_profile_in_use_error(e):
                        logger.warning(
                            "check-session: persistent launch failed, retry ephemeral with DB cookies only"
                        )
                        try:
                            browser, ctx = _launch_ephemeral_chromium_for_check(p, bundled)
                        except Exception as e2:
                            logger.exception("launch ephemeral (check fallback)")
                            return (
                                None,
                                "Профиль занят другим окном браузера — не удалось открыть второй экземпляр. "
                                "Закройте окно «Войти в Facebook» и обновление мессенджера, затем повторите проверку. "
                                f"({e2!s})"[:320],
                                None,
                            )
                        if init_s_persistent:
                            try:
                                ctx.add_init_script(init_s_persistent)
                            except Exception:
                                logger.debug("add_init_script (check fallback)", exc_info=True)
                            init_already_applied = True
                    else:
                        return (
                            None,
                            "Не удалось запустить браузер для проверки. "
                            "Если открыто окно входа в Facebook или мессенджер с тем же профилем — закройте их и повторите. "
                            f"Технически: {e!s}"[:320],
                            None,
                        )

                if init_s_persistent and not init_already_applied:
                    try:
                        ctx.add_init_script(init_s_persistent)
                    except Exception:
                        logger.debug("add_init_script (check)", exc_info=True)

            pregrant_facebook_automation_permissions(ctx)
            apply_storage_state_cookies_to_context(ctx, storage_state)

            snapshot: dict[str, Any] | None = None
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(
                    "https://www.facebook.com/",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if not cdp and not use_ephemeral_disk:
                    if effective_fb_local_chrome_cookies():
                        # Не смешивать с личным Chrome, если уже есть сессия на диске или в снимке БД
                        if profile_was_empty and not _has_storage_cookies(storage_state):
                            inject_local_chrome_facebook_cookies_and_reload(
                                ctx, page, timeout_ms=timeout_ms, log_warning_if_zero=False
                            )
                        else:
                            maybe_reload_after_local_chrome_cookies(
                                ctx=ctx,
                                page=page,
                                storage_state=storage_state,
                                profile_was_empty_before_launch=profile_was_empty,
                                timeout_ms=timeout_ms,
                            )
                    else:
                        maybe_reload_after_local_chrome_cookies(
                            ctx=ctx,
                            page=page,
                            storage_state=storage_state,
                            profile_was_empty_before_launch=profile_was_empty,
                            timeout_ms=timeout_ms,
                        )
                elif not cdp and use_ephemeral_disk:
                    # Сессия только из БД; локальный Chrome и диск профиля не трогаем
                    pass
                else:
                    maybe_reload_after_local_chrome_cookies(
                        ctx=ctx,
                        page=page,
                        storage_state=storage_state,
                        profile_was_empty_before_launch=profile_was_empty,
                        timeout_ms=timeout_ms,
                    )
                page.wait_for_timeout(2500)
                limited, lim_msg = page_meta_rate_limited_or_blocked(page)
                if limited:
                    return False, lim_msg, None
                need_login, login_msg = page_requires_facebook_login(page)
                if need_login:
                    return False, login_msg, None

                try:
                    snapshot = ctx.storage_state()
                except Exception:
                    logger.exception("storage_state after check")
                    snapshot = None
                if storage_state:
                    logger.info(
                        "check-session cookie delta: before=%s after=%s",
                        _storage_cookie_names(storage_state),
                        _storage_cookie_names(snapshot),
                    )
                snap_ok, snap_msg = evaluate_fb_storage_session(
                    snapshot, expected_login=expected_login
                )
                if not snap_ok:
                    dropped_msg = explain_fb_session_drop_after_first_request(
                        storage_state,
                        snapshot,
                    )
                    if dropped_msg:
                        return False, dropped_msg, None
                    return False, snap_msg, None
                return True, "Сессия похожа на активную (есть c_user" + (
                    ", совпадает с ID в карточке" if _looks_like_fb_numeric_user_id((expected_login or "").strip()) else ""
                ) + ")", snapshot
            except Exception as e:
                logger.exception("check_facebook_session")
                base = f"Ошибка: {e!s}"[:280]
                return None, (base + playwright_goto_failure_hint(e))[:520], None
        finally:
            try:
                if browser is not None:
                    browser.close()
                elif ctx is not None:
                    ctx.close()
            except Exception:
                pass


def test_facebook_credentials(
    email: str,
    password: str,
    proxy: dict[str, Any] | None,
    *,
    timeout_ms: int = 75_000,
) -> tuple[bool | None, str]:
    """
    Одноразовая проверка email/пароля во временном Chromium (без профиля на диске).
    Не сохраняет сессию и не подмешивает cookies из локального Chrome.
    """
    from playwright.sync_api import sync_playwright

    email = (email or "").strip()
    password = (password or "").strip()
    if not email or not password:
        return None, "Укажите email (или телефон) и пароль"

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(
                headless=True,
                channel="chromium",
                locale="ru-RU",
                args=["--disable-blink-features=AutomationControlled"],
                proxy=proxy,
            )
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 1200},
                locale="ru-RU",
            )
            page = ctx.new_page()
            page.goto(
                "https://www.facebook.com/login/",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(800)

            email_sel = 'input[name="email"], input#email, input[type="email"]'
            pass_sel = 'input[name="pass"], input#pass, input[type="password"]'
            try:
                page.wait_for_selector(f"{email_sel}, {pass_sel}", timeout=20_000)
            except Exception:
                return None, "Не удалось загрузить форму входа Facebook (сеть или блокировка)"

            page.locator(email_sel).first.fill(email)
            page.locator(pass_sel).first.fill(password)
            page.locator('button[name="login"], button[type="submit"]').first.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(25_000, timeout_ms))
            except Exception:
                pass
            page.wait_for_timeout(4000)

            url = (page.url or "").lower()
            if "/checkpoint" in url or "two_step" in url or "two-factor" in url:
                return (
                    True,
                    "Логин и пароль приняты; Facebook запросил проверку безопасности или 2FA — завершите вход вручную в обычном браузере.",
                )
            if "/login" in url or "/recover" in url:
                err_js = """() => {
                  const box = document.querySelector(
                    '[role="alert"], [data-testid="form_error"], .error_box, #error_box, ._9ay7'
                  );
                  const t = (box && (box.innerText || box.textContent) || '').trim();
                  return t.slice(0, 280);
                }"""
                try:
                    err_txt = page.evaluate(err_js) or ""
                except Exception:
                    err_txt = ""
                if err_txt:
                    return False, f"Facebook вернул ошибку: {err_txt}"
                need, why = page_requires_facebook_login(page)
                if need:
                    return False, why or "Вход не выполнен — проверьте email и пароль"
                return False, "Вход не выполнен"

            need_login, login_msg = page_requires_facebook_login(page)
            if need_login:
                return False, login_msg or "После отправки формы всё ещё виден экран входа"

            return True, "Логин и пароль подошли (тестовый вход без сохранения сессии в FB Master)."
        except Exception as e:
            logger.exception("test_facebook_credentials")
            return None, f"Ошибка проверки: {e!s}"[:400]
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


def test_proxy(
    proxy: dict[str, Any] | None,
    *,
    timeout_ms: int = 25_000,
) -> tuple[bool, str]:
    """Простой HTTP-запрос через прокси (без сохранённого профиля)."""
    from playwright.sync_api import sync_playwright

    if not proxy:
        return False, "Прокси не задан"

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(headless=True, channel="chromium", proxy=proxy)
            page = browser.new_page()
            page.goto(
                "https://api.ipify.org?format=json",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            body = (page.inner_text("body") or "").strip()
            if not body:
                return False, "Пустой ответ"
            return True, body[:500]
        except Exception as e:
            logger.exception("test_proxy")
            return False, f"Не удалось: {e!s}"[:400]
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


def _playwright_login_poll_user_closed(ctx: Any, closed: threading.Event) -> None:
    """
    Обнаружить полное закрытие Chromium пользователем (Dock, Cmd+Q, крестик окна).
    Без этого поток может крутиться в ожидании, а UI считает «окно входа открыто».
    """
    try:
        br = ctx.browser
        if br is not None and not br.is_connected():
            closed.set()
            return
    except Exception:
        closed.set()
        return
    try:
        pgs = list(ctx.pages)
    except Exception:
        closed.set()
        return
    if not pgs:
        closed.set()
        return

    def _page_gone(pg: Any) -> bool:
        try:
            return bool(pg.is_closed())
        except Exception:
            return True

    if all(_page_gone(pg) for pg in pgs):
        closed.set()


_LOGIN_INTERIM_STORAGE_SEC = 30.0


def open_login_window_blocking(
    account_id: int,
    profile_dir: str,
    proxy: dict[str, Any] | None,
    *,
    storage_state: dict[str, Any] | None = None,
    on_closed_storage: Callable[[dict[str, Any] | None], None] | None = None,
    on_interim_storage: Callable[[dict[str, Any] | None], None] | None = None,
    landing_after_warmup: str | None = None,
    stealth_bundle: dict[str, Any] | None = None,
    cdp_endpoint: str | None = None,
) -> None:
    """
    Открывает headed Chromium с постоянным профилем или подключается по CDP к внешнему браузеру.
    После закрытия вкладки снимает storage_state и вызывает on_closed_storage (сохранение в БД).
    Пока окно открыто и видна рабочая сессия (есть c_user), периодически вызывает on_interim_storage —
    чтобы «Проверить вход» мог работать через копию cookies в БД, не закрывая Chromium.
    """
    from playwright.sync_api import sync_playwright

    cdp = (cdp_endpoint or "").strip()
    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    profile_was_empty = not profile_has_chromium_data(str(path))

    if cdp and proxy:
        logger.warning(
            "CDP: прокси из настроек аккаунта не применяется к внешнему браузеру — настройте прокси в профиле антидетекта"
        )

    bundled = _launch_kwargs_persistent(headless=False, proxy=proxy, stealth_bundle=stealth_bundle)
    init_s_persistent = bundled["init_script"]
    init_s_cdp = ""
    if stealth_bundle and isinstance(stealth_bundle, dict):
        init_s_cdp = stealth_bundle.get("init_script") or ""

    with sync_playwright() as p:
        browser = None
        ctx = None
        try:
            if cdp:
                try:
                    browser, ctx = _attach_via_cdp(p, cdp)
                except Exception:
                    logger.exception("connect_over_cdp (login window)")
                    raise
                if init_s_cdp:
                    try:
                        ctx.add_init_script(init_s_cdp)
                    except Exception:
                        logger.debug("add_init_script (login window cdp)", exc_info=True)
            else:
                ctx = launch_persistent_context_from_bundle(p, path, bundled)
                if init_s_persistent:
                    try:
                        ctx.add_init_script(init_s_persistent)
                    except Exception:
                        logger.debug("add_init_script (login window)", exc_info=True)

            pregrant_facebook_automation_permissions(ctx)
            apply_storage_state_cookies_to_context(ctx, storage_state)
            closed = threading.Event()

            def _mark_closed(*_a: Any, **_k: Any) -> None:
                closed.set()

            try:
                ctx.on("close", _mark_closed)
                br_hook = ctx.browser
                if br_hook is not None:
                    try:
                        br_hook.on("disconnected", _mark_closed)
                    except Exception:
                        logger.debug("login window: browser.on(disconnected)", exc_info=True)
                page = ctx.new_page()
                page.on("close", _mark_closed)
                try:
                    page.on("crash", _mark_closed)
                except Exception:
                    logger.debug("login window: page.on(crash)", exc_info=True)
                page.goto(
                    FB_LOGIN_WINDOW_INITIAL_URL,
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
                if effective_fb_local_chrome_cookies() and not cdp:
                    if profile_was_empty and not _has_storage_cookies(storage_state):
                        inject_local_chrome_facebook_cookies_and_reload(
                            ctx, page, timeout_ms=90_000, log_warning_if_zero=True
                        )
                    else:
                        maybe_reload_after_local_chrome_cookies(
                            ctx=ctx,
                            page=page,
                            storage_state=storage_state,
                            profile_was_empty_before_launch=profile_was_empty,
                            timeout_ms=90_000,
                        )
                else:
                    maybe_reload_after_local_chrome_cookies(
                        ctx=ctx,
                        page=page,
                        storage_state=storage_state,
                        profile_was_empty_before_launch=profile_was_empty,
                        timeout_ms=90_000,
                    )
                _land = (landing_after_warmup or "").strip()
                if _land:
                    try:
                        page.goto(_land, wait_until="domcontentloaded", timeout=90_000)
                    except Exception:
                        logger.exception("open_login_window: landing_after_warmup goto")
                last_continue_click = 0.0
                try:
                    if try_click_facebook_remembered_continue(page):
                        last_continue_click = time.monotonic()
                except Exception:
                    logger.debug("login window: initial remembered continue", exc_info=True)
                deadline = time.monotonic() + 7200
                last_interim_save = 0.0
                while not closed.is_set() and time.monotonic() < deadline:
                    time.sleep(0.25)
                    try:
                        _playwright_login_poll_user_closed(ctx, closed)
                    except Exception:
                        logger.debug("login window: poll user closed", exc_info=True)
                        closed.set()
                    if closed.is_set():
                        break
                    try:
                        pg = None
                        for cand in ctx.pages:
                            try:
                                if not cand.is_closed():
                                    pg = cand
                                    break
                            except Exception:
                                continue
                        now_m = time.monotonic()
                        if pg is not None and now_m - last_continue_click >= 2.0:
                            if try_click_facebook_remembered_continue(pg):
                                last_continue_click = now_m
                    except Exception:
                        logger.debug("login window: remembered continue poll", exc_info=True)
                    if on_interim_storage:
                        now_m = time.monotonic()
                        if now_m - last_interim_save >= _LOGIN_INTERIM_STORAGE_SEC:
                            last_interim_save = now_m
                            try:
                                pg = None
                                for cand in ctx.pages:
                                    try:
                                        if not cand.is_closed():
                                            pg = cand
                                            break
                                    except Exception:
                                        continue
                                if pg is not None:
                                    need_l, _ = page_requires_facebook_login(pg)
                                    if not need_l:
                                        snap_i = _storage_state_with_timeout(ctx)
                                        if snap_i and fb_c_user_from_storage_state(snap_i):
                                            on_interim_storage(snap_i)
                            except Exception:
                                logger.debug("login window: interim storage", exc_info=True)
            finally:
                snapshot = _storage_state_with_timeout(ctx)
                _close_browser_or_context_with_timeout(browser, ctx)
                _active_login.discard(account_id)
                if on_closed_storage:
                    try:
                        on_closed_storage(snapshot)
                    except Exception:
                        logger.exception("on_closed_storage callback")
        except Exception:
            _active_login.discard(account_id)
            raise


def compute_totp_for_display(secret: str) -> tuple[str | None, int, int, str]:
    """
    Текущий код TOTP для UI (тот же алгоритм, что Google Authenticator / 2fa.live).
    Возвращает (code, interval_sec, seconds_until_next_code, error_message).
    При успехе error_message пустая строка.
    """
    import time

    import pyotp

    raw = (secret or "").strip().replace(" ", "").upper()
    if not raw:
        return None, 30, 0, "Пустой секрет"
    try:
        totp = pyotp.TOTP(raw)
        code = totp.now()
        interval = int(totp.interval)
        rem = interval - (int(time.time()) % interval)
        if rem <= 0:
            rem = interval
        return code, interval, rem, ""
    except Exception:
        logger.exception("compute_totp_for_display")
        return None, 30, 0, "Некорректный секрет TOTP (ожидается Base32)"


def _try_fill_facebook_2fa(page: Any, totp_secret: str) -> bool:
    """Подставить текущий TOTP, если видно поле кода. Возвращает True, если что-то заполнили."""
    import pyotp

    secret = (totp_secret or "").strip().replace(" ", "").upper()
    if not secret:
        return False
    try:
        code = pyotp.TOTP(secret).now()
    except Exception:
        logger.exception("pyotp.TOTP")
        return False
    selectors = (
        'input[name="approvals_code"]',
        "input#approvals_code",
        'input[autocomplete="one-time-code"]',
        'input[data-testid="approvals_code"]',
        'input[placeholder*="code" i]',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=800):
                continue
            loc.click(timeout=2_000)
            loc.fill("", timeout=1_000)
            loc.fill(code, timeout=3_000)
            page.keyboard.press("Enter")
            return True
        except Exception:
            continue
    return False


def _try_click_trust_browser(page: Any) -> None:
    for sel in (
        'button:has-text("Continue")',
        'button:has-text("Продолжить")',
        'button:has-text("OK")',
        'div[role="button"]:has-text("Continue")',
    ):
        try:
            page.locator(sel).first.click(timeout=900)
            return
        except Exception:
            continue


def open_auto_login_window_blocking(
    account_id: int,
    profile_dir: str,
    proxy: dict[str, Any] | None,
    email: str,
    password: str,
    totp_secret: str | None,
    stealth_bundle: dict[str, Any],
    *,
    on_closed_storage: Callable[[dict[str, Any] | None], None] | None = None,
    cdp_endpoint: str | None = None,
) -> None:
    """
    Headed Chromium или CDP: логин + 2FA (TOTP) + сохранение storage_state.
    Не подмешивает cookies из локального Chrome.
    """
    from playwright.sync_api import sync_playwright

    cdp = (cdp_endpoint or "").strip()
    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True)

    if cdp and proxy:
        logger.warning(
            "CDP: прокси из настроек аккаунта не применяется к внешнему браузеру — настройте прокси в профиле антидетекта"
        )

    bundled = _launch_kwargs_persistent(headless=False, proxy=proxy, stealth_bundle=stealth_bundle)
    init_s_persistent = bundled["init_script"]
    init_s_cdp = stealth_bundle.get("init_script") or "" if isinstance(stealth_bundle, dict) else ""

    with sync_playwright() as p:
        browser = None
        ctx = None
        try:
            if cdp:
                browser, ctx = _attach_via_cdp(p, cdp)
                if init_s_cdp:
                    try:
                        ctx.add_init_script(init_s_cdp)
                    except Exception:
                        logger.debug("add_init_script (auto login cdp)", exc_info=True)
            else:
                ctx = launch_persistent_context_from_bundle(p, path, bundled)
                if init_s_persistent:
                    try:
                        ctx.add_init_script(init_s_persistent)
                    except Exception:
                        logger.debug("add_init_script (auto login)", exc_info=True)

            pregrant_facebook_automation_permissions(ctx)
            apply_storage_state_cookies_to_context(ctx, None)

            closed = threading.Event()

            def _mark_auto_closed(*_a: Any, **_k: Any) -> None:
                closed.set()

            try:
                ctx.on("close", _mark_auto_closed)
                br_auto = ctx.browser
                if br_auto is not None:
                    try:
                        br_auto.on("disconnected", _mark_auto_closed)
                    except Exception:
                        logger.debug("auto login: browser.on(disconnected)", exc_info=True)
                page = ctx.new_page()
                page.goto("https://www.facebook.com/login/", wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(1_200)
                try_click_facebook_remembered_continue(page)
                page.wait_for_timeout(800)

                email_sel = 'input[name="email"], input#email, input[type="email"]'
                pass_sel = 'input[name="pass"], input#pass, input[type="password"]'
                page.wait_for_selector(f"{email_sel}, {pass_sel}", timeout=25_000)
                email_loc = page.locator(email_sel).first
                pass_loc = page.locator(pass_sel).first
                try:
                    if email_loc.count() > 0 and email_loc.is_visible(timeout=800):
                        email_loc.click(timeout=2_000)
                        email_loc.fill((email or "").strip())
                except Exception:
                    logger.debug("auto login: email field not fillable", exc_info=True)
                if pass_loc.count() == 0 or not pass_loc.is_visible(timeout=2_000):
                    raise RuntimeError(
                        "Facebook не показал поле пароля. Попробуйте «Войти в Facebook» вручную: "
                        "если видите экран «Продолжить», нажмите его и затем введите пароль."
                    )
                pass_loc.click(timeout=2_000)
                pass_loc.fill((password or "").strip())
                clicked_submit = False
                for sel in (
                    'button[name="login"]',
                    'button[type="submit"]',
                    'button:has-text("Log in")',
                    'button:has-text("Войти")',
                    'button:has-text("Continue")',
                    'button:has-text("Продолжить")',
                    'div[role="button"]:has-text("Log in")',
                    'div[role="button"]:has-text("Войти")',
                    'div[role="button"]:has-text("Continue")',
                    'div[role="button"]:has-text("Продолжить")',
                ):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() == 0:
                            continue
                        if not loc.is_visible(timeout=800):
                            continue
                        loc.click(timeout=2_000)
                        clicked_submit = True
                        break
                    except Exception:
                        continue
                if not clicked_submit:
                    page.keyboard.press("Enter")

                deadline = time.monotonic() + 240.0
                while time.monotonic() < deadline and not closed.is_set():
                    stop_loop = False
                    try:
                        if page.is_closed():
                            stop_loop = True
                        else:
                            br = ctx.browser
                            if br is not None and not br.is_connected():
                                stop_loop = True
                            else:
                                _playwright_login_poll_user_closed(ctx, closed)
                                if closed.is_set():
                                    stop_loop = True
                    except Exception:
                        stop_loop = True
                    if stop_loop:
                        break
                    page.wait_for_timeout(2_500)
                    try:
                        _try_click_trust_browser(page)
                        if totp_secret:
                            _try_fill_facebook_2fa(page, totp_secret)
                        url = (page.url or "").lower()
                        need, _ = page_requires_facebook_login(page)
                        if (
                            not need
                            and "facebook.com" in url
                            and "/login" not in url
                            and "two_step" not in url
                            and "checkpoint" not in url
                        ):
                            break
                        if "/checkpoint" in url:
                            logger.warning("auto_login: checkpoint — возможно нужен ручной ввод")
                    except Exception:
                        logger.debug("auto_login loop", exc_info=True)

                page.wait_for_timeout(2_000)
            finally:
                snapshot = _storage_state_with_timeout(ctx)
                _close_browser_or_context_with_timeout(browser, ctx)
                _active_login.discard(account_id)
                if on_closed_storage:
                    try:
                        on_closed_storage(snapshot)
                    except Exception:
                        logger.exception("on_closed_storage (auto login)")
        except Exception:
            _active_login.discard(account_id)
            raise


def try_start_auto_login_window(
    account_id: int,
    profile_dir: str,
    proxy: dict[str, Any] | None,
    email: str,
    password: str,
    totp_secret: str | None,
    stealth_bundle: dict[str, Any],
    *,
    on_closed_storage: Callable[[dict[str, Any] | None], None] | None = None,
    cdp_endpoint: str | None = None,
) -> tuple[bool, str]:
    """Фоновый поток: автоматический вход с TOTP."""

    def _run() -> None:
        try:
            open_auto_login_window_blocking(
                account_id,
                profile_dir,
                proxy,
                email,
                password,
                totp_secret,
                stealth_bundle,
                on_closed_storage=on_closed_storage,
                cdp_endpoint=cdp_endpoint,
            )
        except Exception:
            logger.exception("open_auto_login_window account_id=%s", account_id)
        finally:
            _active_login.discard(account_id)
            _login_threads.pop(account_id, None)

    t = threading.Thread(target=_run, name=f"fb-auto-login-{account_id}", daemon=True)
    with _lock_for(account_id):
        if account_id in _active_login:
            thr = _login_threads.get(account_id)
            if thr is None or not thr.is_alive():
                _active_login.discard(account_id)
                _login_threads.pop(account_id, None)
            else:
                return False, "Окно для этого аккаунта уже открыто. Дождитесь завершения."
        _active_login.add(account_id)
        t._fb_login_started_monotonic = time.monotonic()
        _login_threads[account_id] = t
    t.start()
    return True, ""


def try_start_login_window(
    account_id: int,
    profile_dir: str,
    proxy: dict[str, Any] | None,
    *,
    storage_state: dict[str, Any] | None = None,
    on_closed_storage: Callable[[dict[str, Any] | None], None] | None = None,
    on_interim_storage: Callable[[dict[str, Any] | None], None] | None = None,
    landing_after_warmup: str | None = None,
    stealth_bundle: dict[str, Any] | None = None,
    cdp_endpoint: str | None = None,
) -> tuple[bool, str]:
    """Старт фонового потока с окном входа."""

    def _run() -> None:
        try:
            open_login_window_blocking(
                account_id,
                profile_dir,
                proxy,
                storage_state=storage_state,
                on_closed_storage=on_closed_storage,
                on_interim_storage=on_interim_storage,
                landing_after_warmup=landing_after_warmup,
                stealth_bundle=stealth_bundle,
                cdp_endpoint=cdp_endpoint,
            )
        except Exception:
            logger.exception("open_login_window account_id=%s", account_id)
        finally:
            _active_login.discard(account_id)
            _login_threads.pop(account_id, None)

    t = threading.Thread(target=_run, name=f"fb-login-{account_id}", daemon=True)
    with _lock_for(account_id):
        if account_id in _active_login:
            thr = _login_threads.get(account_id)
            if thr is None or not thr.is_alive():
                _active_login.discard(account_id)
                _login_threads.pop(account_id, None)
            else:
                return (
                    False,
                    "Окно входа для этого аккаунта уже открыто (или завершается). "
                    "Если вы уже закрыли Chromium — подождите до ~30 с: сессия дописывается в базу и флаг снимется. "
                    "Иначе закройте окно входа или дождитесь его окончания.",
                )
        _active_login.add(account_id)
        t._fb_login_started_monotonic = time.monotonic()
        _login_threads[account_id] = t
    t.start()
    return True, ""


def login_window_busy(account_id: int) -> bool:
    """
    True только пока жив поток окна входа. Если Chromium закрыли, а флаг остался — сбрасываем.
    """
    if account_id not in _active_login:
        return False
    thr = _login_threads.get(account_id)
    if thr is not None and thr.is_alive():
        started = getattr(thr, "_fb_login_started_monotonic", None)
        if started is not None and (time.monotonic() - float(started)) > _LOGIN_THREAD_MAX_MONO_SEC:
            logger.warning(
                "login_window_busy: поток входа account_id=%s живёт дольше лимита — сбрасываем блокировку UI",
                account_id,
            )
            _active_login.discard(account_id)
            _login_threads.pop(account_id, None)
            return False
        return True
    _active_login.discard(account_id)
    _login_threads.pop(account_id, None)
    logger.info("login_window_busy: сброшен устаревший флаг окна входа для account_id=%s", account_id)
    return False
