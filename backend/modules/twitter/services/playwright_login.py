"""Twitter/X авто-логин через Playwright (headful).

Открывает Chromium через прокси аккаунта, вводит логин/пароль (и, если
задан, 2FA-секрет), дожидается появления критичных cookies (auth_token +
ct0). Экспортирует storage_state() и сохраняет cookies_json в БД.

Если флоу застревает (captcha / unusual activity / e-mail challenge),
оставляет окно открытым 3 минуты, чтобы клиент мог дорешать вручную, —
после чего повторно пытается забрать cookies.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

try:
    import pyotp  # type: ignore
except Exception:  # noqa: BLE001
    pyotp = None  # type: ignore[assignment]

from backend.modules.twitter.models import TwitterAccount
from backend.modules.twitter.runtime.playwright_session import open_session
from backend.services.fb_credentials_crypto import decrypt_secret

logger = logging.getLogger(__name__)

LOGIN_URL = "https://x.com/i/flow/login"
HOME_URL = "https://x.com/home"
ESSENTIAL_COOKIES = ("auth_token", "ct0")
# v2.99+: 15 минут на ручной вход. Окно НЕ закрываем сами — ждём, пока
# пользователь либо введёт данные (и появятся cookies), либо сам закроет
# вкладку. Это критично если автоматический ввод селекторов промахнулся
# или X показал capcha / e-mail challenge.
MANUAL_WAIT_SEC = 900


def _has_session_cookies(cookies: list[dict[str, Any]]) -> bool:
    names = {str(c.get("name") or "").lower() for c in cookies or []}
    return all(n in names for n in ESSENTIAL_COOKIES)


def run_twitter_login(acc: TwitterAccount) -> tuple[bool, str]:
    """Возвращает (ok, detail). На успехе кладёт cookies в acc.cookies_json
    (обычно роутер вызывает db.commit после возврата)."""
    if not acc.login_username or not acc.enc_password:
        return False, "empty_credentials"
    username = acc.login_username.strip().lstrip("@")
    password = decrypt_secret(acc.enc_password) or ""
    if not password:
        return False, "decrypt_failed"
    totp = None
    if acc.enc_totp_secret and pyotp:
        secret = decrypt_secret(acc.enc_totp_secret) or ""
        if secret:
            try:
                totp = pyotp.TOTP(secret).now()
            except Exception:  # noqa: BLE001
                totp = None

    try:
        with open_session(acc, headless=False) as sess:
            page = sess.context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)

            # Шаг 1 — поле username.
            try:
                page.wait_for_selector('input[autocomplete="username"]', timeout=20_000)
                page.fill('input[autocomplete="username"]', username)
                # Next button: Twitter меняет селектор; пробуем несколько.
                for sel in (
                    'button:has-text("Next")',
                    'button:has-text("Далее")',
                    'div[role="button"]:has-text("Next")',
                    'div[role="button"]:has-text("Далее")',
                ):
                    try:
                        if page.locator(sel).first.is_visible(timeout=1500):
                            page.locator(sel).first.click()
                            break
                    except Exception:
                        pass
            except Exception:
                pass
            page.wait_for_timeout(2500)

            # Шаг 2 — Twitter иногда спрашивает подтверждение username/phone/email.
            # Если такое поле показано — вставляем login_username ещё раз.
            try:
                if page.locator('input[data-testid="ocfEnterTextTextInput"]').is_visible(timeout=2500):
                    page.fill('input[data-testid="ocfEnterTextTextInput"]', username)
                    for sel in ('button:has-text("Next")', 'button:has-text("Далее")'):
                        try:
                            page.locator(sel).first.click()
                            break
                        except Exception:
                            pass
                    page.wait_for_timeout(2000)
            except Exception:
                pass

            # Шаг 3 — password (v2.99+: при неудаче не закрываем окно, а
            # передаём управление пользователю — он введёт сам).
            password_filled = False
            try:
                page.wait_for_selector('input[name="password"]', timeout=20_000)
                page.fill('input[name="password"]', password)
                password_filled = True
                for sel in (
                    'button[data-testid="LoginForm_Login_Button"]',
                    'button:has-text("Log in")',
                    'button:has-text("Войти")',
                ):
                    try:
                        if page.locator(sel).first.is_visible(timeout=1500):
                            page.locator(sel).first.click()
                            break
                    except Exception:
                        pass
            except Exception:
                logger.warning(
                    "twitter login: поле пароля не найдено за 20с — оставляем окно "
                    "открытым на %s сек, пользователь введёт вручную.",
                    MANUAL_WAIT_SEC,
                )
            if password_filled:
                page.wait_for_timeout(4000)

            # Шаг 4 — опциональный 2FA.
            if totp:
                try:
                    if page.locator('input[data-testid="ocfEnterTextTextInput"]').is_visible(timeout=3000):
                        page.fill('input[data-testid="ocfEnterTextTextInput"]', totp)
                        for sel in ('button:has-text("Next")', 'button:has-text("Далее")'):
                            try:
                                page.locator(sel).first.click()
                                break
                            except Exception:
                                pass
                        page.wait_for_timeout(3500)
                except Exception:
                    pass

            # Ждём появления критичных cookies. Если не пришли за 15с —
            # возможно captcha / e-mail / phone challenge — даём клиенту
            # время дорешать вручную (до MANUAL_WAIT_SEC = 15 минут).
            # v2.99+: чувствительны к закрытию вкладки — если пользователь
            # сам закрыл окно, ловим cookies в момент закрытия.
            cookies_list: list[dict[str, Any]] = []
            user_closed = False
            quick_deadline = time.time() + 15.0
            while time.time() < quick_deadline:
                try:
                    cookies_list = sess.context.cookies()
                    if _has_session_cookies(cookies_list):
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            if not _has_session_cookies(cookies_list):
                logger.info(
                    "twitter login: ждём ручной вход (вы можете ввести данные "
                    "и пройти captcha) — окно открыто на %s сек.",
                    MANUAL_WAIT_SEC,
                )
                manual_deadline = time.time() + MANUAL_WAIT_SEC
                while time.time() < manual_deadline:
                    try:
                        if page.is_closed():
                            user_closed = True
                            logger.info("twitter login: пользователь закрыл вкладку")
                            break
                    except Exception:
                        user_closed = True
                        break
                    try:
                        cookies_list = sess.context.cookies()
                        if _has_session_cookies(cookies_list):
                            break
                    except Exception:
                        # context уже закрыт пользователем — выходим из цикла
                        user_closed = True
                        break
                    # 2-секундный pulse через page.wait_for_timeout (если
                    # page жив); если page закрыт — следующая итерация
                    # сработает is_closed() и выйдет.
                    try:
                        page.wait_for_timeout(2000)
                    except Exception:
                        user_closed = True
                        break

            # Финальный read cookies — на случай если пользователь успел
            # залогиниться прямо перед закрытием окна.
            if not _has_session_cookies(cookies_list):
                try:
                    cookies_list = sess.context.cookies()
                except Exception:
                    pass

            if not _has_session_cookies(cookies_list):
                if user_closed:
                    return False, "closed_by_user_no_cookies"
                return False, "no_auth_token_after_wait"

            # Успех — экспортируем storage_state и кладём в БД.
            try:
                sess.context.storage_state(path=None)
            except Exception:  # noqa: BLE001
                pass
            acc.cookies_json = cookies_list
            acc.cookies_imported_at = datetime.now(timezone.utc)
            acc.status = "connected"
            acc.session_ok = True
            acc.login_blocked_at = None
            acc.login_blocked_reason = None
            return True, "ok"
    except Exception as e:  # noqa: BLE001
        logger.exception("run_twitter_login")
        return False, f"err:{e!s}"[:200]
