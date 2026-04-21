"""Парсер импортируемых Instagram-аккаунтов.

Обязательные cookies: `sessionid`, `csrftoken`, `ds_user_id`.
Без них Instagram CSRF проверка завернёт любую mutation (DM/like/comment).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


_REQUIRED_COOKIES = ("sessionid", "csrftoken", "ds_user_id")


@dataclass
class ParsedInstagramAccount:
    label: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    proxy_url: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    user_agent: str | None = None
    handle: str | None = None
    full_name: str | None = None
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
        domain = (c.get("domain") or ".instagram.com").strip() or ".instagram.com"
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


def _missing_required(cookies: list[dict[str, Any]]) -> list[str]:
    names = {(c.get("name") or "").lower() for c in cookies}
    return [n for n in _REQUIRED_COOKIES if n not in names]


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


def parse_text(blob: str) -> list[ParsedInstagramAccount]:
    items: list[ParsedInstagramAccount] = []
    if not blob:
        return items
    for raw_line in blob.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            items.append(_parse_line(line))
        except Exception as e:  # noqa: BLE001
            items.append(ParsedInstagramAccount(label=line[:60], error=str(e)))
    return items


def _parse_line(line: str) -> ParsedInstagramAccount:
    if "\t" in line and not line.startswith("{") and not line.startswith("["):
        parts = line.split("\t")
        label = parts[0].strip()
        cookies_raw = parts[1].strip() if len(parts) > 1 else ""
        proxy_raw = parts[2].strip() if len(parts) > 2 else ""
        cookies = _normalize_cookies(json.loads(cookies_raw)) if cookies_raw else []
        clean_proxy, pu, pp = _split_proxy_url(proxy_raw) if proxy_raw else ("", None, None)
        miss = _missing_required(cookies)
        if miss:
            raise ValueError(f"Не хватает cookies: {', '.join(miss)}")
        return ParsedInstagramAccount(
            label=label or "instagram", cookies=cookies,
            proxy_url=clean_proxy or None, proxy_username=pu, proxy_password=pp,
        )

    if line.startswith("["):
        cookies = _normalize_cookies(json.loads(line))
        miss = _missing_required(cookies)
        if miss:
            raise ValueError(f"Не хватает cookies: {', '.join(miss)}")
        return ParsedInstagramAccount(label="instagram-account", cookies=cookies)

    if line.startswith("{"):
        obj = json.loads(line)
        cookies = _normalize_cookies(obj.get("cookies") or obj.get("storage_state", {}).get("cookies") or [])
        miss = _missing_required(cookies)
        if miss:
            raise ValueError(f"Не хватает cookies: {', '.join(miss)}")
        clean_proxy, pu, pp = _split_proxy_url(obj.get("proxy_url") or "") if obj.get("proxy_url") else ("", None, None)
        handle = (obj.get("handle") or obj.get("username") or "").strip().lstrip("@").lstrip("/")
        label_default = f"@{handle}" if handle else "instagram-account"
        return ParsedInstagramAccount(
            label=str(obj.get("label") or label_default).strip(),
            cookies=cookies,
            proxy_url=clean_proxy or obj.get("proxy_url") or None,
            proxy_username=pu or obj.get("proxy_username"),
            proxy_password=pp or obj.get("proxy_password"),
            user_agent=obj.get("user_agent"),
            handle=handle or None,
            full_name=obj.get("full_name"),
        )

    raise ValueError("Неизвестный формат строки. Ожидается JSON-объект, массив cookies или TSV.")


__all__ = ["ParsedInstagramAccount", "parse_text"]
