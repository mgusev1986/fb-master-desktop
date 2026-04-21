"""Twitter / X DOM resilience — locale-aware selectors.

X использует data-testid в большинстве кнопок — это самый стабильный
якорь. Дополнительно — aria-label и текстовые fallback'ы для разных локалей.
"""

from __future__ import annotations

from collections.abc import Iterable


# ── Localized texts ───────────────────────────────────────


LOCALIZED_SEND_TEXTS: tuple[str, ...] = (
    "send", "Send", "Отправить", "отправить", "送信", "전송",
    "Enviar", "Envoyer", "Senden", "Inviare", "送出", "傳送", "보내기",
    "Wyślij", "Skicka", "ارسال", "إرسال",
)

LOCALIZED_REPLY_TEXTS: tuple[str, ...] = (
    "Reply", "reply", "Ответить", "Comment", "返信", "답글", "Responder", "Répondre",
    "Antworten", "Rispondi", "回复", "댓글", "Odpowiedz",
)

LOCALIZED_POST_TEXTS: tuple[str, ...] = (
    "Post", "Tweet", "Опубликовать", "Твитнуть", "Postar", "Publier",
    "投稿する", "Posten", "Pubblica", "发布", "게시", "Opublikuj",
)

LOCALIZED_LOGIN_HINTS: tuple[str, ...] = (
    "/i/flow/login", "/login", "/i/flow/signup",
    "Sign in to X", "Sign in to Twitter", "Войти в X", "Войти в Twitter",
    "Log in", "Sign up", "Зарегистрироваться",
)

LOCALIZED_RESTRICTED_HINTS: tuple[str, ...] = (
    "your account is suspended", "account suspended", "/account/access",
    "ваш аккаунт заблокирован", "アカウントは凍結されています", "您的账号已被暂停",
)

LOCALIZED_DM_DISABLED_HINTS: tuple[str, ...] = (
    "you can't message this user",
    "this account doesn't accept messages",
    "you can only send messages to people who follow you",
    "не разрешает получать сообщения",
    "не получает сообщения",
)

LOCALIZED_RATE_LIMIT_HINTS: tuple[str, ...] = (
    "you are over the daily limit", "rate limit exceeded",
    "you've reached the daily limit", "слишком частые запросы",
    "превышен лимит",
)


# ── Helpers ──────────────────────────────────────────────


def build_text_selectors(base_tag: str, texts: Iterable[str], *, extra_attrs: str = "") -> tuple[str, ...]:
    out: list[str] = []
    for t in texts:
        t = (t or "").strip()
        if not t:
            continue
        safe = t.replace('"', '\\"')
        out.append(f"{base_tag}{extra_attrs}:has-text(\"{safe}\")")
    return tuple(out)


def page_has_any_hint(text: str, hints: Iterable[str]) -> bool:
    if not text:
        return False
    s = text.lower()
    return any((h or "").lower() in s for h in hints)


# ── Composed selectors (приоритет: data-testid → aria → text) ─────


# DM composer textbox.
DM_TEXTBOX_SELECTORS: tuple[str, ...] = (
    "div[data-testid='dmComposerTextInput']",
    "div[contenteditable='true'][data-testid='dmComposerTextInput']",
    "div[data-testid='tweetTextarea_0']",  # fallback на reply textarea
    "div[contenteditable='true'][role='textbox']",
)


# DM Send button.
DM_SEND_SELECTORS: tuple[str, ...] = (
    "button[data-testid='dmComposerSendButton']:not([aria-disabled='true'])",
    "button[data-testid='dmComposerSendButton']",
    *build_text_selectors("button", LOCALIZED_SEND_TEXTS),
)


# Reply textbox.
REPLY_TEXTBOX_SELECTORS: tuple[str, ...] = (
    "div[data-testid='tweetTextarea_0']",
    "div[contenteditable='true'][data-testid='tweetTextarea_0']",
    "div[contenteditable='true'][aria-label*='Post text']",
    "div[contenteditable='true'][aria-label*='Reply']",
)


# Reply submit button.
REPLY_SUBMIT_SELECTORS: tuple[str, ...] = (
    "button[data-testid='tweetButton']:not([aria-disabled='true'])",
    "button[data-testid='tweetButton']",
    "button[data-testid='tweetButtonInline']",
    *build_text_selectors("button", LOCALIZED_POST_TEXTS),
    *build_text_selectors("button", LOCALIZED_REPLY_TEXTS),
)


# Logged-in markers (header).
LOGGED_IN_SELECTORS: tuple[str, ...] = (
    "a[data-testid='AppTabBar_Profile_Link']",
    "a[aria-label*='Profile' i]",
    "a[aria-label*='Профиль' i]",
    "[data-testid='SideNav_AccountSwitcher_Button']",
    "[data-testid='SideNav_NewTweet_Button']",
    "a[href='/compose/post']",
    "a[href='/compose/tweet']",
)


# DM thread row in /messages.
DM_THREAD_ROW_SELECTORS: tuple[str, ...] = (
    "[data-testid='conversation']",
    "div[role='listitem'] a[href*='/messages/']",
    "section[role='region'] a[href*='/messages/']",
)


# Search results — User cells.
USER_CELL_SELECTORS: tuple[str, ...] = (
    "[data-testid='UserCell']",
    "[data-testid='cellInnerDiv'] a[href^='/'][role='link']",
)
USER_HANDLE_LINK_SELECTORS: tuple[str, ...] = (
    "a[role='link'][href^='/']",
)


# Profile page — Message button.
PROFILE_MESSAGE_BUTTON_SELECTORS: tuple[str, ...] = (
    "[data-testid='sendDMFromProfile']",
    "a[aria-label*='Message' i][href*='/messages/compose']",
    "button[aria-label*='Message' i]",
)


__all__ = [
    "DM_SEND_SELECTORS",
    "DM_TEXTBOX_SELECTORS",
    "DM_THREAD_ROW_SELECTORS",
    "LOCALIZED_DM_DISABLED_HINTS",
    "LOCALIZED_LOGIN_HINTS",
    "LOCALIZED_POST_TEXTS",
    "LOCALIZED_RATE_LIMIT_HINTS",
    "LOCALIZED_REPLY_TEXTS",
    "LOCALIZED_RESTRICTED_HINTS",
    "LOCALIZED_SEND_TEXTS",
    "LOGGED_IN_SELECTORS",
    "PROFILE_MESSAGE_BUTTON_SELECTORS",
    "REPLY_SUBMIT_SELECTORS",
    "REPLY_TEXTBOX_SELECTORS",
    "USER_CELL_SELECTORS",
    "USER_HANDLE_LINK_SELECTORS",
    "build_text_selectors",
    "page_has_any_hint",
]
