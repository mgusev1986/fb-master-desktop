"""Instagram / X DOM resilience — locale-aware selectors.

Instagram использует обфусцированные классы — самые стабильные якоря:
  * `[role="textbox"]` для composer'ов.
  * `<svg aria-label="...">` (Like, Comment, Send) — Instagram использует
    aria-label на SVG, а не на button.
  * `time` для дат, `a[href*='/p/']` для ссылок на посты.
"""

from __future__ import annotations

from collections.abc import Iterable


# Любая кнопка содержит SVG с aria-label локализованным.
LOCALIZED_LIKE_LABELS: tuple[str, ...] = (
    "Like", "Лайк", "Нравится", "好き", "좋아요",
    "Me gusta", "J'aime", "Gefällt mir", "Mi piace",
)
LOCALIZED_UNLIKE_LABELS: tuple[str, ...] = (
    "Unlike", "Убрать отметку", "Не нравится", "いいね済み",
)
LOCALIZED_COMMENT_LABELS: tuple[str, ...] = (
    "Comment", "Прокомментировать", "Комментировать", "コメント", "댓글",
    "Comentar", "Commenter",
)
LOCALIZED_FOLLOW_LABELS: tuple[str, ...] = (
    "Follow", "Подписаться", "フォロー", "팔로우",
    "Seguir", "Suivre", "Folgen",
)
LOCALIZED_FOLLOWING_LABELS: tuple[str, ...] = (
    "Following", "Вы подписаны", "フォロー中", "팔로잉",
    "Siguiendo", "Abonné(e)", "Abonniert", "Segui già",
)
LOCALIZED_SEND_LABELS: tuple[str, ...] = (
    "Send", "Отправить", "送信", "전송", "Enviar", "Envoyer", "Senden",
    "Send message", "Отправить сообщение",
)
LOCALIZED_MESSAGE_LABELS: tuple[str, ...] = (
    "Message", "Написать сообщение", "Сообщение", "メッセージ", "메시지",
    "Mensaje", "Message", "Nachricht",
)
LOCALIZED_LOGIN_HINTS: tuple[str, ...] = (
    "/accounts/login", "/accounts/emailsignup",
    "Log in", "Sign up", "Войти", "Зарегистрироваться",
    "ログイン", "登入", "Iniciar sesión",
)
LOCALIZED_RESTRICTED_HINTS: tuple[str, ...] = (
    "Your account has been disabled",
    "Action Blocked", "Try Again Later",
    "We restrict certain activity",
    "ваш аккаунт заблокирован", "ваш аккаунт отключён",
    "действие заблокировано", "вы пытались",
)


def page_has_any_hint(text: str, hints: Iterable[str]) -> bool:
    if not text:
        return False
    s = text.lower()
    return any((h or "").lower() in s for h in hints)


def build_aria_selectors(labels: Iterable[str]) -> tuple[str, ...]:
    """Создать список селекторов: svg/button[aria-label="<label>"]."""
    out: list[str] = []
    for lbl in labels:
        lbl = (lbl or "").strip()
        if not lbl:
            continue
        safe = lbl.replace('"', '\\"')
        out.append(f'svg[aria-label=\"{safe}\"]')
        out.append(f'button[aria-label=\"{safe}\"]')
        out.append(f'a[aria-label=\"{safe}\"]')
        out.append(f'[role="button"][aria-label=\"{safe}\"]')
    return tuple(out)


# Composed selectors.

LIKE_BUTTON_SELECTORS: tuple[str, ...] = (
    *build_aria_selectors(LOCALIZED_LIKE_LABELS),
    "section svg[aria-label*='Like' i]",
)

UNLIKE_BUTTON_SELECTORS: tuple[str, ...] = (
    *build_aria_selectors(LOCALIZED_UNLIKE_LABELS),
)

COMMENT_FOCUS_BUTTON_SELECTORS: tuple[str, ...] = (
    *build_aria_selectors(LOCALIZED_COMMENT_LABELS),
)

COMMENT_TEXTBOX_SELECTORS: tuple[str, ...] = (
    "textarea[aria-label*='comment' i]",
    "textarea[aria-label*='Комментарий' i]",
    "textarea[placeholder*='comment' i]",
    "textarea[placeholder*='Прокомментировать' i]",
    "form[method='POST'] textarea",
    "div[role='textbox'][contenteditable='true']",
)

COMMENT_SUBMIT_SELECTORS: tuple[str, ...] = (
    "form[method='POST'] button[type='submit']:not([disabled])",
    "button[type='submit']:not([disabled])",
    *(f"button:has-text(\"{lbl}\")" for lbl in ("Post", "Опубликовать", "Publish", "投稿"))
)

FOLLOW_BUTTON_SELECTORS: tuple[str, ...] = (
    *(f'button:has-text("{lbl}")' for lbl in LOCALIZED_FOLLOW_LABELS),
    "header section button._acan",  # legacy class
)

FOLLOWING_INDICATOR_SELECTORS: tuple[str, ...] = (
    *(f'button:has-text("{lbl}")' for lbl in LOCALIZED_FOLLOWING_LABELS),
)

# DM composer in /direct/.
DM_TEXTBOX_SELECTORS: tuple[str, ...] = (
    "div[contenteditable='true'][role='textbox']",
    "textarea[placeholder*='Message' i]",
    "textarea[placeholder*='Сообщение' i]",
    "div[aria-label*='Message' i][contenteditable='true']",
)

DM_SEND_SELECTORS: tuple[str, ...] = (
    *(f'button:has-text("{lbl}")' for lbl in LOCALIZED_SEND_LABELS),
    *build_aria_selectors(LOCALIZED_SEND_LABELS),
    "div[role='button']:has-text('Send')",
    "div[role='button']:has-text('Отправить')",
)

# Profile page Message button.
PROFILE_MESSAGE_BUTTON_SELECTORS: tuple[str, ...] = (
    *(f'button:has-text("{lbl}")' for lbl in LOCALIZED_MESSAGE_LABELS),
    "a[href^='/direct/']",
    "div[role='button']:has-text('Message')",
)

# Stories — кружок аватара пользователя на главной + кнопка лайка сторис.
STORY_AVATAR_SELECTORS: tuple[str, ...] = (
    "header img[alt*='profile picture' i]",
    "header img[alt*='profile photo' i]",
    "main img[alt*='profile picture' i]",
)
STORY_LIKE_BUTTON_SELECTORS: tuple[str, ...] = (
    *build_aria_selectors(("Like", "Лайк")),
    "section svg[aria-label='Like']",
)

# Logged-in markers (header/nav).
LOGGED_IN_SELECTORS: tuple[str, ...] = (
    "a[href='/direct/inbox/']",
    "a[href*='/accounts/edit/']",
    "svg[aria-label*='Home' i]",
    "svg[aria-label*='Главная' i]",
    "nav a[href='/']",
    "img[alt$='profile picture']",
)

# Likes modal — список лайкавших пост.
LIKES_MODAL_USER_LINK_SELECTORS: tuple[str, ...] = (
    "div[role='dialog'] a[href^='/'][role='link']",
    "div[role='dialog'] a[href^='/']:not([href*='/p/']):not([href*='/explore/'])",
)

# Comment authors — внутри поста.
COMMENT_AUTHOR_LINK_SELECTORS: tuple[str, ...] = (
    "ul li a[href^='/'][role='link']",
    "article a[href^='/'][role='link']:not([href*='/p/'])",
)

# Hashtag / search results — посты на странице тега.
HASHTAG_POST_LINK_SELECTORS: tuple[str, ...] = (
    "article a[href^='/p/']",
    "main a[href^='/p/']",
)

# Search users — explore/search.
SEARCH_USER_LINK_SELECTORS: tuple[str, ...] = (
    "div[role='dialog'] a[href^='/']",  # search modal
    "main a[href^='/'][role='link']:not([href^='/p/']):not([href^='/explore/'])",
)


__all__ = [
    "COMMENT_AUTHOR_LINK_SELECTORS",
    "COMMENT_FOCUS_BUTTON_SELECTORS",
    "COMMENT_SUBMIT_SELECTORS",
    "COMMENT_TEXTBOX_SELECTORS",
    "DM_SEND_SELECTORS",
    "DM_TEXTBOX_SELECTORS",
    "FOLLOWING_INDICATOR_SELECTORS",
    "FOLLOW_BUTTON_SELECTORS",
    "HASHTAG_POST_LINK_SELECTORS",
    "LIKES_MODAL_USER_LINK_SELECTORS",
    "LIKE_BUTTON_SELECTORS",
    "LOCALIZED_LOGIN_HINTS",
    "LOCALIZED_RESTRICTED_HINTS",
    "LOGGED_IN_SELECTORS",
    "PROFILE_MESSAGE_BUTTON_SELECTORS",
    "SEARCH_USER_LINK_SELECTORS",
    "STORY_AVATAR_SELECTORS",
    "STORY_LIKE_BUTTON_SELECTORS",
    "UNLIKE_BUTTON_SELECTORS",
    "build_aria_selectors",
    "page_has_any_hint",
]
