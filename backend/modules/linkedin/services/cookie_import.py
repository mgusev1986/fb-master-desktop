"""Парсер импортируемых LinkedIn-аккаунтов.

Поддерживаемые форматы (по строкам, одна строка = один аккаунт):

1) JSON-объект на строку (рекомендуется):
   {"label":"...", "cookies":[...], "proxy_url":"http://user:pass@host:port",
    "user_agent":"Mozilla/5.0 ...", "li_login_email":"...", "li_login_password":"..."}

2) Просто массив cookies на строку:
   [{"name":"li_at","value":"AQED...", "domain":".linkedin.com", ...}, ...]

3) "label\\tcookies_json[\\tproxy_url]" (FB-style tab-separated).

Cookies должны как минимум содержать `li_at` (главная сессионка LinkedIn).
JSESSIONID нужен для CSRF-защищённых запросов в Playwright runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class ParsedAccount:
    label: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    proxy_url: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    user_agent: str | None = None
    li_login_email: str | None = None
    li_login_password: str | None = None
    public_identifier: str | None = None
    full_name: str | None = None
    error: str | None = None  # если строка не распарсилась


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
        domain = (c.get("domain") or ".linkedin.com").strip() or ".linkedin.com"
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


def _has_li_at(cookies: list[dict[str, Any]]) -> bool:
    return any((c.get("name") or "").lower() == "li_at" for c in cookies)


def _split_proxy_url(url: str) -> tuple[str, str | None, str | None]:
    """Вырезать user:pass из proxy URL, если они там есть.

    Возвращает (clean_url, username, password).
    """
    s = (url or "").strip()
    if not s:
        return "", None, None
    # protocol:// part
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


def parse_text(blob: str) -> list[ParsedAccount]:
    """Парсит входной текст и возвращает список ParsedAccount по строкам."""
    items: list[ParsedAccount] = []
    if not blob:
        return items
    for raw_line in blob.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed = _parse_line(line)
            items.append(parsed)
        except Exception as e:  # noqa: BLE001
            items.append(ParsedAccount(label=line[:60], error=str(e)))
    return items


def _parse_line(line: str) -> ParsedAccount:
    # Tab-separated FB-style?
    if "\t" in line and not line.startswith("{") and not line.startswith("["):
        parts = line.split("\t")
        label = parts[0].strip()
        cookies_raw = parts[1].strip() if len(parts) > 1 else ""
        proxy_raw = parts[2].strip() if len(parts) > 2 else ""
        cookies = _normalize_cookies(json.loads(cookies_raw)) if cookies_raw else []
        clean_proxy, pu, pp = _split_proxy_url(proxy_raw) if proxy_raw else ("", None, None)
        if not _has_li_at(cookies):
            raise ValueError("В cookies нет обязательного li_at")
        return ParsedAccount(
            label=label or "linkedin",
            cookies=cookies,
            proxy_url=clean_proxy or None,
            proxy_username=pu,
            proxy_password=pp,
        )

    if line.startswith("["):
        cookies = _normalize_cookies(json.loads(line))
        if not _has_li_at(cookies):
            raise ValueError("В cookies нет обязательного li_at")
        # Label по li_at вычислить нельзя — берём короткий хеш.
        return ParsedAccount(label="linkedin-account", cookies=cookies)

    if line.startswith("{"):
        obj = json.loads(line)
        cookies = _normalize_cookies(obj.get("cookies") or obj.get("storage_state", {}).get("cookies") or [])
        if not _has_li_at(cookies):
            raise ValueError("В cookies нет обязательного li_at")
        clean_proxy, pu, pp = _split_proxy_url(obj.get("proxy_url") or "") if obj.get("proxy_url") else ("", None, None)
        return ParsedAccount(
            label=str(obj.get("label") or obj.get("li_login_email") or "linkedin").strip(),
            cookies=cookies,
            proxy_url=clean_proxy or obj.get("proxy_url") or None,
            proxy_username=pu or obj.get("proxy_username"),
            proxy_password=pp or obj.get("proxy_password"),
            user_agent=obj.get("user_agent"),
            li_login_email=obj.get("li_login_email"),
            li_login_password=obj.get("li_login_password"),
            public_identifier=obj.get("public_identifier"),
            full_name=obj.get("full_name"),
        )

    raise ValueError("Неизвестный формат строки. Ожидается JSON-объект, массив cookies или TSV.")


__all__ = ["ParsedAccount", "parse_text"]
