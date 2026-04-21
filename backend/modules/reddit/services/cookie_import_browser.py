"""Парсер импортируемых Reddit-аккаунтов (browser-mode).

Аналог `backend/modules/linkedin/services/cookie_import.py`, но требует
наличия как минимум одного из сессионных cookies Reddit:
  * `reddit_session` (legacy web flow),
  * `token_v2` (новый flow с 2024+, JWT в cookie).

Поддерживаемые форматы (по строкам, одна строка = один аккаунт):

1) JSON-объект:
   {"label":"u/myaccount", "cookies":[...], "proxy_url":"http://user:pass@host:port",
    "user_agent":"Mozilla/5.0 ...", "username":"myaccount"}

2) Чистый массив cookies:
   [{"name":"reddit_session","value":"...","domain":".reddit.com"}, ...]

3) TSV: "label\\tcookies_json[\\tproxy_url]"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


_REDDIT_SESSION_COOKIES = ("reddit_session", "token_v2")


@dataclass
class ParsedRedditAccount:
    label: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    proxy_url: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    user_agent: str | None = None
    username: str | None = None
    error: str | None = None


def _normalize_cookies(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        value = c.get("value")
        if not name or value is None:
            continue
        domain = (c.get("domain") or ".reddit.com").strip() or ".reddit.com"
        path = (c.get("path") or "/").strip() or "/"
        out.append(
            {
                "name": name,
                "value": str(value),
                "domain": domain,
                "path": path,
                "expires": c.get("expires") or c.get("expirationDate"),
                "httpOnly": bool(c.get("httpOnly", c.get("http_only", False))),
                "secure": bool(c.get("secure", True)),
                "sameSite": c.get("sameSite") or c.get("same_site") or "None",
            }
        )
    return out


def _has_session_cookie(cookies: list[dict[str, Any]]) -> bool:
    names = {(c.get("name") or "").lower() for c in cookies}
    return any(s in names for s in _REDDIT_SESSION_COOKIES)


def _split_proxy_url(url: str) -> tuple[str, str | None, str | None]:
    s = (url or "").strip()
    if not s:
        return "", None, None
    if "://" in s:
        scheme, rest = s.split("://", 1)
    else:
        scheme, rest = "http", s
    if "@" in rest:
        creds, host = rest.rsplit("@", 1)
        if ":" in creds:
            user, pw = creds.split(":", 1)
        else:
            user, pw = creds, None
        return f"{scheme}://{host}", user, pw
    return f"{scheme}://{rest}", None, None


def parse_text(blob: str) -> list[ParsedRedditAccount]:
    items: list[ParsedRedditAccount] = []
    if not blob:
        return items
    for raw_line in blob.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            items.append(_parse_line(line))
        except Exception as e:  # noqa: BLE001
            items.append(ParsedRedditAccount(label=line[:60], error=str(e)))
    return items


def _parse_line(line: str) -> ParsedRedditAccount:
    # TSV
    if "\t" in line and not line.startswith("{") and not line.startswith("["):
        parts = line.split("\t")
        label = parts[0].strip()
        cookies_raw = parts[1].strip() if len(parts) > 1 else ""
        proxy_raw = parts[2].strip() if len(parts) > 2 else ""
        cookies = _normalize_cookies(json.loads(cookies_raw)) if cookies_raw else []
        clean_proxy, pu, pp = _split_proxy_url(proxy_raw) if proxy_raw else ("", None, None)
        if not _has_session_cookie(cookies):
            raise ValueError("В cookies нет ни reddit_session, ни token_v2 — это не активная сессия")
        return ParsedRedditAccount(
            label=label or "reddit",
            cookies=cookies,
            proxy_url=clean_proxy or None,
            proxy_username=pu,
            proxy_password=pp,
        )

    # Чистый массив cookies
    if line.startswith("["):
        cookies = _normalize_cookies(json.loads(line))
        if not _has_session_cookie(cookies):
            raise ValueError("В cookies нет ни reddit_session, ни token_v2")
        return ParsedRedditAccount(label="reddit-account", cookies=cookies)

    # JSON-объект
    if line.startswith("{"):
        obj = json.loads(line)
        cookies = _normalize_cookies(obj.get("cookies") or obj.get("storage_state", {}).get("cookies") or [])
        if not _has_session_cookie(cookies):
            raise ValueError("В cookies нет ни reddit_session, ни token_v2")
        clean_proxy, pu, pp = _split_proxy_url(obj.get("proxy_url") or "") if obj.get("proxy_url") else ("", None, None)
        username = (obj.get("username") or "").strip().lstrip("u/").lstrip("/")
        label_default = f"u/{username}" if username else "reddit-account"
        return ParsedRedditAccount(
            label=str(obj.get("label") or label_default).strip(),
            cookies=cookies,
            proxy_url=clean_proxy or obj.get("proxy_url") or None,
            proxy_username=pu or obj.get("proxy_username"),
            proxy_password=pp or obj.get("proxy_password"),
            user_agent=obj.get("user_agent"),
            username=username or None,
        )

    raise ValueError("Неизвестный формат строки. Ожидается JSON-объект, массив cookies или TSV.")


__all__ = ["ParsedRedditAccount", "parse_text"]
