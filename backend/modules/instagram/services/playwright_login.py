"""Instagram авто-логин через Playwright (headful). v2.96+

Открывает Chromium через прокси аккаунта, вводит логин/пароль (и, если
задан, 2FA-секрет), дожидается появления критичных cookies (sessionid +
csrftoken + ds_user_id). Сохраняет cookies_json в БД.

Если флоу застревает (captcha / suspicious login / e-mail challenge),
оставляет окно открытым 3 минуты — клиент дорешает вручную, после чего
повторно пытается забрать cookies.

Зеркало `backend.modules.twitter.services.playwright_login` под IG.
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

from backend.modules.instagram.models import InstagramAccount
from backend.modules.instagram.runtime.playwright_session import open_session
from backend.services.fb_credentials_crypto import decrypt_secret

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.instagram.com/accounts/login/"
HOME_URL = "https://www.instagram.com/"
ESSENTIAL_COOKIES = ("sessionid", "csrftoken", "ds_user_id")
# v2.99+: 15 минут на ручной вход. Окно НЕ закрываем сами — ждём, пока
# пользователь либо введёт данные (и появятся cookies), либо сам закроет
# вкладку. Это критично если автоматический ввод селекторов промахнулся
# или IG показал captcha / save-info / 2FA challenge.
MANUAL_WAIT_SEC = 900


def _has_session_cookies(cookies: list[dict[str, Any]]) -> bool:
    names = {str(c.get("name") or "").lower() for c in cookies or []}
    return all(n in names for n in ESSENTIAL_COOKIES)


def run_instagram_login(acc: InstagramAccount) -> tuple[bool, str]:
    """Возвращает (ok, detail). На успехе кладёт cookies в acc.cookies_json
    (роутер сам делает db.commit() после возврата).
    """
    if not acc.login_username or not acc.enc_password:
        return False, "empty_credentials"
    username = acc.login_username.strip().lstrip("@")
    password = decrypt_secret(acc.enc_password) or ""
    if not password:
        return False, "decrypt_failed"
    totp_code = None
    if acc.enc_totp_secret and pyotp:
        secret = decrypt_secret(acc.enc_totp_secret) or ""
        if secret:
            try:
                totp_code = pyotp.TOTP(secret).now()
            except Exception:  # noqa: BLE001
                totp_code = None

    try:
        with open_session(acc, headless=False) as sess:
            page = sess.context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)

            # Шаг 1 — закрыть cookie-banner / разрешения, если показан.
            for sel in (
                'button:has-text("Allow all cookies")',
                'button:has-text("Принять все")',
                'button:has-text("Accept all")',
                'button:has-text("Allow")',
                'button:has-text("Разрешить")',
            ):
                try:
                    if page.locator(sel).first.is_visible(timeout=1500):
                        page.locator(sel).first.click()
                        page.wait_for_timeout(800)
                        break
                except Exception:  # noqa: BLE001
                    pass

            # Шаг 2 — username + password (v2.99+: при неудаче не закрываем
            # окно, а передаём управление пользователю — он введёт сам).
            form_filled = False
            try:
                page.wait_for_selector('input[name="username"]', timeout=20_000)
                page.fill('input[name="username"]', username)
                page.wait_for_timeout(400)
                page.fill('input[name="password"]', password)
                page.wait_for_timeout(400)
                form_filled = True
                # Submit: type=submit или явная кнопка с текстом «Log in» / «Войти».
                clicked = False
                for sel in (
                    'button[type="submit"]',
                    'button:has-text("Log in")',
                    'button:has-text("Войти")',
                ):
                    try:
                        if page.locator(sel).first.is_visible(timeout=1500):
                            page.locator(sel).first.click()
                            clicked = True
                            break
                    except Exception:  # noqa: BLE001
                        pass
                if not clicked:
                    # Fallback — Enter в поле пароля.
                    page.locator('input[name="password"]').first.press("Enter")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "instagram login: автоввод формы не удался — оставляем "
                    "окно открытым на %s сек, пользователь введёт вручную.",
                    MANUAL_WAIT_SEC,
                )
            if form_filled:
                page.wait_for_timeout(4000)

            # Шаг 3 — опциональный 2FA (TOTP). IG показывает поле verificationCode
            # либо input[name="verificationCode"] либо input[type="tel"][maxlength="6"].
            if totp_code:
                for sel in (
                    'input[name="verificationCode"]',
                    'input[autocomplete="one-time-code"]',
                    'input[type="tel"][maxlength="6"]',
                ):
                    try:
                        if page.locator(sel).first.is_visible(timeout=2500):
                            page.fill(sel, totp_code)
                            page.wait_for_timeout(400)
                            for btn_sel in (
                                'button[type="button"]:has-text("Confirm")',
                                'button:has-text("Подтвердить")',
                                'button[type="submit"]',
                            ):
                                try:
                                    if page.locator(btn_sel).first.is_visible(timeout=1500):
                                        page.locator(btn_sel).first.click()
                                        break
                                except Exception:  # noqa: BLE001
                                    pass
                            page.wait_for_timeout(3500)
                            break
                    except Exception:  # noqa: BLE001
                        continue

            # Шаг 4 — ждём критичные cookies. Если 15с нет — возможно captcha
            # / «Save login info» / 2FA — даём клиенту время дорешать вручную
            # (до MANUAL_WAIT_SEC = 15 минут). v2.99+: устойчиво к закрытию
            # вкладки пользователем (page.is_closed()).
            cookies_list: list[dict[str, Any]] = []
            user_closed = False
            quick_deadline = time.time() + 15.0
            while time.time() < quick_deadline:
                try:
                    cookies_list = sess.context.cookies()
                    if _has_session_cookies(cookies_list):
                        break
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1500)

            if not _has_session_cookies(cookies_list):
                logger.info(
                    "instagram login: ждём ручной вход (вы можете ввести данные "
                    "и пройти captcha) — окно открыто на %s сек.",
                    MANUAL_WAIT_SEC,
                )
                manual_deadline = time.time() + MANUAL_WAIT_SEC
                while time.time() < manual_deadline:
                    try:
                        if page.is_closed():
                            user_closed = True
                            logger.info("instagram login: пользователь закрыл вкладку")
                            break
                    except Exception:  # noqa: BLE001
                        user_closed = True
                        break
                    try:
                        cookies_list = sess.context.cookies()
                        if _has_session_cookies(cookies_list):
                            break
                    except Exception:  # noqa: BLE001
                        user_closed = True
                        break
                    try:
                        page.wait_for_timeout(2000)
                    except Exception:  # noqa: BLE001
                        user_closed = True
                        break

            # Финальный read — на случай если пользователь успел залогиниться
            # перед закрытием окна.
            if not _has_session_cookies(cookies_list):
                try:
                    cookies_list = sess.context.cookies()
                except Exception:  # noqa: BLE001
                    pass

            if not _has_session_cookies(cookies_list):
                if user_closed:
                    return False, "closed_by_user_no_cookies"
                return False, "no_sessionid_after_wait"

            # Успех — сохраняем cookies + storage_state.
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
        logger.exception("run_instagram_login")
        return False, f"err:{e!s}"[:200]


__all__ = ["run_instagram_login"]
