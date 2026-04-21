"""Нормализация URL профилей Facebook (импорт, парсер друзей)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# id или vanity-slug группы (латиница, цифры, точка, дефис, подчёркивание)
_GROUP_ID_RE = re.compile(r"^[\w.-]+$")


def _unwrap_facebook_redirect_url(raw: str) -> str:
    """Развернуть ссылки вида l.facebook.com/?u=… (копирование из браузера / мессенджера)."""
    s = (raw or "").strip().split("#", 1)[0].strip()
    for _ in range(4):
        try:
            p = urlparse(s)
        except Exception:
            return (raw or "").strip()
        host = (p.hostname or "").lower()
        if host in ("l.facebook.com", "lm.facebook.com"):
            qs = parse_qs(p.query)
            inner = (qs.get("u") or [None])[0]
            if not inner:
                break
            s = unquote(inner).split("#", 1)[0].strip()
            continue
        break
    return s


def normalize_facebook_group_members_url(raw: str) -> str | None:
    """
    Если ссылка ведёт в группу Facebook — канонический URL списка участников
    (доноры, парсер). Поддерживаются /groups/<id>, /groups/<slug>, /members,
    любые query-параметры (?locale=…, ?sort=…), ссылки через l.facebook.com/?u=…
    """
    raw = _unwrap_facebook_redirect_url((raw or "").strip())
    if not raw:
        return None
    try:
        p = urlparse(raw.split("#", 1)[0])
    except Exception:
        return None
    host = (p.hostname or "").lower().replace("www.", "").replace("m.", "").replace("mbasic.", "")
    if "facebook.com" not in host:
        return None
    parts = [x for x in p.path.strip("/").split("/") if x]
    if len(parts) < 2 or parts[0].lower() != "groups":
        return None
    gid = parts[1]
    if not gid or not _GROUP_ID_RE.match(gid):
        return None
    return f"https://www.facebook.com/groups/{gid}/members"


def normalize_facebook_profile_url(raw: str) -> str | None:
    """Привести URL профиля Facebook к каноническому виду."""
    raw = _unwrap_facebook_redirect_url((raw or "").strip())
    if not raw:
        return None
    try:
        p = urlparse(raw)
    except Exception:
        return None
    host = (p.hostname or "").replace("www.", "").replace("m.", "")
    if "facebook.com" not in host:
        return None

    if p.path.rstrip("/") == "/profile.php" or "profile.php" in p.path:
        qs = parse_qs(p.query)
        uid = (qs.get("id") or [None])[0]
        if uid:
            # CSV/Excel часто склеивают ФИО через «;» прямо в query: id=123;Иван Иванов
            uid = uid.split(";")[0].split(",")[0].strip()
        if uid and uid.isdigit():
            return f"https://www.facebook.com/profile.php?id={uid}"
        return None

    path = p.path.strip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None

    # Новый UI: /profile/61571234567890, /user/61571234567890 (подписчики, друзья)
    if len(segments) >= 2:
        seg0 = segments[0].lower()
        if seg0 in ("profile", "user") and segments[1].strip().isdigit():
            uid = segments[1].strip()
            if len(uid) >= 5:
                return f"https://www.facebook.com/profile.php?id={uid}"
            return None

    username = segments[0]
    skip = {
        "friends",
        "groups",
        "watch",
        "reel",
        "marketplace",
        "events",
        "pages",
        "help",
        "settings",
        "messages",
        "notifications",
        # Иначе /profile без id превращался бы в канонический …/profile (ломает уникальность)
        "profile",
        "user",
    }
    if username.lower() in skip:
        return None
    # Числовой путь — тот же ID, что в URL треда Messenger (/messages/t/<id>).
    if username.isdigit():
        uid = username.strip()
        if len(uid) >= 6:
            return f"https://www.facebook.com/profile.php?id={uid}"
        return None
    return f"https://www.facebook.com/{username}"
