"""Reddit DOM resilience — locale-aware selectors и shreddit-* fallbacks.

Reddit интерфейс существует в нескольких видах:
  * Старый (old.reddit.com) — table.message-parent, .thing, .author и т.п.
  * Новый редизайн (www.reddit.com) — react-классы (Post, Header, Comment).
  * shreddit-* (новейший roll-out) — custom elements: shreddit-post, shreddit-comment,
    rs-app-message-input, faceplate-tracker и т.п.

И всё это локализовано: «Send», «Отправить», «送信», «Enviar», «Comment»,
«Комментарий», «コメント», «Comentar» и т.д.

Этот модуль централизует:
  * `LOCALIZED_SEND_TEXTS` — все известные тексты «отправить» (sorted by frequency).
  * `LOCALIZED_COMMENT_TEXTS` — варианты «комментировать».
  * `LOCALIZED_LOGIN_HINTS` / `LOCALIZED_SUSPEND_HINTS` — паттерны страниц-блокеров.
  * `build_text_selectors(...)` — генерирует список селекторов `:has-text(...)` для
    набора локализованных слов.
"""

from __future__ import annotations

from collections.abc import Iterable

# ── Send / Submit ──────────────────────────────────────────


LOCALIZED_SEND_TEXTS: tuple[str, ...] = (
    "send",
    "send message",
    "send invitation",
    "send now",
    "submit",
    "Отправить",
    "отправить",
    "Послать",
    "Send",
    "Enviar",
    "Envoyer",
    "Senden",
    "Inviare",
    "送信",
    "送る",
    "発送",
    "送出",
    "傳送",
    "보내기",
    "전송",
    "Wyślij",
    "Skicka",
    "Lähetä",
    "Verstuur",
    "Salin",
    "Kirim",
    "Gönder",
    "İlet",
    "ارسال",
    "ارسل",
    "إرسال",
    "שליחה",
    "שלח",
    "Изпрати",
    "Надіслати",
    "Pošlji",
    "Poslat",
    "Slati",
)


LOCALIZED_COMMENT_TEXTS: tuple[str, ...] = (
    "comment",
    "Комментировать",
    "Комментарий",
    "Ответить",
    "Reply",
    "reply",
    "Comentar",
    "Commenter",
    "Kommentieren",
    "Commenta",
    "コメント",
    "回复",
    "댓글",
    "Skomentuj",
    "Kommentera",
    "Kommentit",
    "Reageer",
    "Komentar",
    "Yorum yap",
    "تعليق",
    "הגב",
)


# ── Page-level hints (case-insensitive substring match) ──


LOCALIZED_LOGIN_HINTS: tuple[str, ...] = (
    # url-paths
    "/login",
    "/register",
    # page texts
    "log in to reddit",
    "create account",
    "войти в reddit",
    "Войти",
    "регистрация",
    "Iniciar sesión",
    "se connecter",
    "ログインする",
    "ログイン",
    "登录",
    "登入",
    "로그인",
)

LOCALIZED_SUSPEND_HINTS: tuple[str, ...] = (
    "your account has been suspended",
    "this account has been suspended",
    "permanently suspended",
    "/account-suspended",
    "ваш аккаунт заблокирован",
    "учётная запись заблокирована",
    "cuenta ha sido suspendida",
    "アカウントは停止されました",
    "您的帐户已被停用",
)

LOCALIZED_RATE_LIMIT_HINTS: tuple[str, ...] = (
    "you are doing that too much",
    "rate limit",
    "try again in",
    "слишком частые запросы",
    "вы делаете это слишком часто",
    "превышен лимит",
    "demasiadas peticiones",
    "trop de requêtes",
    "リクエストが多すぎ",
    "请求过多",
)


# ── Helpers ────────────────────────────────────────────────


def build_text_selectors(
    base_tag: str,
    texts: Iterable[str],
    *,
    extra_attrs: str = "",
) -> tuple[str, ...]:
    """Создать список Playwright-селекторов `tag[extra_attrs]:has-text("...")`.

    Возвращает кортеж в исходном порядке `texts` (приоритет → ранние первыми).
    """
    out: list[str] = []
    for t in texts:
        t = (t or "").strip()
        if not t:
            continue
        # Двойные кавычки внутри текста экранируем заменой на `\"`.
        safe = t.replace('"', '\\"')
        out.append(f"{base_tag}{extra_attrs}:has-text(\"{safe}\")")
    return tuple(out)


def page_has_any_hint(text: str, hints: Iterable[str]) -> bool:
    """Case-insensitive substring match по списку фраз/паттернов URL."""
    if not text:
        return False
    s = text.lower()
    return any((h or "").lower() in s for h in hints)


# Готовые объединённые наборы — для drop-in замены в sender'ах.


SEND_BUTTON_SELECTORS_LOCALIZED: tuple[str, ...] = (
    # data-attrs / aria — самые надёжные.
    "button[type='submit']:not([disabled])",
    "button[aria-label='Send']",
    "button[aria-label='Send message']",
    "button[aria-label*='Send' i]",
    "button[aria-label*='Отправить' i]",
    "[data-testid='send-button']",
    "[data-test-id='send-button']",
    # И только потом — по тексту (мутабельный).
    *build_text_selectors("button", LOCALIZED_SEND_TEXTS),
)


COMMENT_SUBMIT_SELECTORS_LOCALIZED: tuple[str, ...] = (
    "button[type='submit']:not([disabled])",
    "button[aria-label='Comment']",
    "button[aria-label*='Comment' i]",
    "button[aria-label*='Reply' i]",
    "button[aria-label*='Комментировать' i]",
    "button[slot='submit-button']",
    "[data-testid='comment-submit-button']",
    *build_text_selectors("button", LOCALIZED_COMMENT_TEXTS),
)


# Расширенные textbox / composer / chat-overlay селекторы.


COMMENT_TEXTBOX_SELECTORS_LOCALIZED: tuple[str, ...] = (
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true'][aria-label*='comment' i]",
    "div[contenteditable='true'][aria-label*='Комментарий' i]",
    "textarea[name='text']",
    "textarea[placeholder*='comment' i]",
    "textarea[placeholder*='Комментарий' i]",
    "textarea[placeholder*='Прокомментируйте' i]",
    "div.public-DraftEditor-content",
    "shreddit-composer textarea",
    "shreddit-composer div[contenteditable='true']",
    "shreddit-comment-composer textarea",
    "shreddit-comment-composer div[contenteditable='true']",
)


CHAT_TEXTBOX_SELECTORS_LOCALIZED: tuple[str, ...] = (
    "div[contenteditable='true'][role='textbox']",
    "div.public-DraftEditor-content",
    "rs-app-message-input div[contenteditable='true']",
    "[data-testid='message-input']",
    "div[contenteditable='true'][aria-label*='Message' i]",
    "div[contenteditable='true'][aria-label*='Сообщение' i]",
)


# Признаки залогиненности (header user-menu) — расширенный список.


LOGGED_IN_HEADER_SELECTORS: tuple[str, ...] = (
    "header [aria-label*='Account']",
    "header [aria-label*='Profile' i]",
    "header [aria-label*='Профиль' i]",
    "header [data-testid='user-drawer-button']",
    "button#expand-user-drawer-button",
    "header img[alt*='avatar' i]",
    "header faceplate-avatar",
    "#USER_DROPDOWN_ID",
    "a[href*='/submit']",
    "a[href*='/message/inbox']",  # ссылка на inbox видна только залогиненным
)


__all__ = [
    "CHAT_TEXTBOX_SELECTORS_LOCALIZED",
    "COMMENT_SUBMIT_SELECTORS_LOCALIZED",
    "COMMENT_TEXTBOX_SELECTORS_LOCALIZED",
    "LOCALIZED_COMMENT_TEXTS",
    "LOCALIZED_LOGIN_HINTS",
    "LOCALIZED_RATE_LIMIT_HINTS",
    "LOCALIZED_SEND_TEXTS",
    "LOCALIZED_SUSPEND_HINTS",
    "LOGGED_IN_HEADER_SELECTORS",
    "SEND_BUTTON_SELECTORS_LOCALIZED",
    "build_text_selectors",
    "page_has_any_hint",
]
