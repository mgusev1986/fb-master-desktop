"""Чтение cookies Facebook из локального Google Chrome (macOS) для Playwright."""

from __future__ import annotations

import sys
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any


def _chrome_user_data_dir() -> Path:
    return Path.home() / "Library/Application Support/Google/Chrome"


def _discover_cookie_db_files() -> list[Path]:
    """
    В новых Chrome на macOS cookies часто в Profile/Network/Cookies,
    а browser_cookie3 по умолчанию смотрит только на …/Default/Cookies.
    """
    root = _chrome_user_data_dir()
    if not root.is_dir():
        return []

    profiles: list[Path] = []
    default = root / "Default"
    if default.is_dir():
        profiles.append(default)
    profiles.extend(sorted(p for p in root.glob("Profile *") if p.is_dir()))

    out: list[Path] = []
    for prof in profiles:
        network = prof / "Network" / "Cookies"
        legacy = prof / "Cookies"
        if network.is_file():
            out.append(network)
        elif legacy.is_file():
            out.append(legacy)
    return out


def _jar_to_playwright_facebook(jar: CookieJar) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    rows: list[dict[str, Any]] = []

    for c in jar:
        dom_raw = (getattr(c, "domain", None) or "").strip().lower()
        if "facebook" not in dom_raw:
            continue
        name = getattr(c, "name", None)
        value = getattr(c, "value", None)
        if not name or value is None or value == "":
            continue
        path = getattr(c, "path", None) or "/"
        key = (name, dom_raw, path)
        if key in seen:
            continue
        seen.add(key)

        entry: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": ".facebook.com",
            "path": path,
            "secure": bool(c.secure),
        }

        exp = getattr(c, "expires", None)
        if exp is not None and exp > 0:
            entry["expires"] = int(exp)

        rest: dict = getattr(c, "rest", None) or {}
        ho = rest.get("HttpOnly") or rest.get("httponly")
        entry["httpOnly"] = bool(ho)

        ss = rest.get("SameSite") or rest.get("samesite")
        if ss:
            s = str(ss).strip().lower()
            if s == "none":
                entry["sameSite"] = "None"
            elif s == "lax":
                entry["sameSite"] = "Lax"
            elif s == "strict":
                entry["sameSite"] = "Strict"

        rows.append(entry)

    return rows


def facebook_cookies_for_playwright() -> list[dict[str, Any]]:
    """
    Читает Facebook-cookies из всех найденных профилей Chrome (в т.ч. Network/Cookies).
    Основной Chrome должен быть закрыт, иначе SQLite может быть заблокирован.
    """
    try:
        import browser_cookie3
    except ImportError:
        print("[FB Parser] Пакет browser-cookie3 не установлен.", file=sys.stderr)
        return []

    paths = _discover_cookie_db_files()
    if not paths:
        print(
            f"[FB Parser] Нет файлов Cookies в {_chrome_user_data_dir()} "
            "(ожидались Default/Network/Cookies или Profile */…).",
            file=sys.stderr,
        )
        return []

    errors: list[str] = []
    merged: list[dict[str, Any]] = []

    for db_path in paths:
        try:
            jar = browser_cookie3.chrome(cookie_file=str(db_path), domain_name="")
        except Exception as e:
            try:
                short = str(db_path.relative_to(Path.home()))
            except ValueError:
                short = str(db_path)
            errors.append(f"{short}: {e!s}")
            continue
        merged.extend(_jar_to_playwright_facebook(jar))

    if not merged and errors:
        print("[FB Parser] Не удалось расшифровать/открыть cookies Chrome:", file=sys.stderr)
        for line in errors[:5]:
            print(f"  • {line}", file=sys.stderr)
        if len(errors) > 5:
            print(f"  … и ещё {len(errors) - 5}", file=sys.stderr)

    # Дедуп: один URL — самое длинное значение (часто актуальнее)
    by_name: dict[tuple[str, str], dict[str, Any]] = {}
    for row in merged:
        k = (row["name"], row.get("path") or "/")
        if k not in by_name or len(str(row.get("value", ""))) > len(
            str(by_name[k].get("value", ""))
        ):
            by_name[k] = row

    return list(by_name.values())
