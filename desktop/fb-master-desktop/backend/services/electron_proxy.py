"""Прокси для Electron: Chromium в webview не принимает user:pass внутри proxyRules → отдельно rules + логин."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote, urlparse

from backend.models import FBAccount


def electron_proxy_rules_for_account(acc: FBAccount | None) -> str | None:
    if not acc or not acc.proxy_enabled or not (acc.proxy_url or "").strip():
        return None
    parsed = urlparse(acc.proxy_url.strip())
    if not parsed.hostname:
        return None
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks4"):
        scheme = "http"
    port = parsed.port
    if port is None:
        port = 1080 if scheme.startswith("socks") else 80
    hostport = f"{parsed.hostname}:{port}"
    user = (acc.proxy_username or "").strip()
    pw = (acc.proxy_password or "").strip()
    if user:
        return f"{scheme}://{quote(user, safe='')}:{quote(pw, safe='')}@{hostport}"
    return f"{scheme}://{hostport}"


def electron_partition_proxy_for_webview(acc: FBAccount | None) -> dict[str, Any] | None:
    """
    Конфиг для session.setProxy + on('login') во встроенном webview.
    Без userinfo в строке правил (иначе Chromium: ERR_NO_SUPPORTED_PROXIES -336).
    """
    if not acc or not acc.proxy_enabled or not (acc.proxy_url or "").strip():
        return None
    parsed = urlparse(acc.proxy_url.strip())
    if not parsed.hostname:
        return None
    scheme = (parsed.scheme or "http").lower()
    if scheme == "https":
        scheme = "http"
    if scheme not in ("http", "socks5", "socks4"):
        scheme = "http"
    port = parsed.port
    if port is None:
        port = 1080 if scheme.startswith("socks") else 80

    u_url = unquote(parsed.username) if parsed.username else ""
    p_url = unquote(parsed.password) if parsed.password else ""
    user = (acc.proxy_username or "").strip() or u_url or ""
    password = (acc.proxy_password or "").strip() or p_url or ""

    clean_url = f"{scheme}://{parsed.hostname}:{port}"
    socks_with_auth = scheme.startswith("socks") and bool(user or password)

    return {
        "rules_url": clean_url,
        "username": user or None,
        "password": password or None,
        "socks_with_auth": socks_with_auth,
    }
