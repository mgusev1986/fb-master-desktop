"""Единая логика каталога Chromium для аккаунта (Messenger и окно «Войти в Facebook»)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from backend.services.cabinet_settings import effective_browser_profiles_dir


def profiles_base_resolved(db: Session) -> Path:
    return effective_browser_profiles_dir(db).resolve()


def resolved_profile_under_base(db: Session, stored: str) -> Path | None:
    """Путь из БД только если он внутри текущего BROWSER_PROFILES_DIR (без создания папок)."""
    if not (stored or "").strip():
        return None
    try:
        p = Path(stored).expanduser().resolve()
        base = profiles_base_resolved(db)
        p.relative_to(base)
        return p
    except (ValueError, OSError):
        return None


def playwright_profile_dir_for_account(db: Session, account_id: int, stored_profile_dir: str) -> Path:
    """
    Каталог user-data для Playwright persistent context.

    1) Путь из БД, если он внутри BROWSER_PROFILES_DIR и папка есть.
    2) Иначе абсолютный путь из БД, если на диске есть каталог (другой нормализованный корень / symlink).
    3) Иначе <base>/fb_account_<id> — создаётся при отсутствии.

    Раньше «Войти в Facebook» при отсутствии (1) сразу отвечал «Папка профиля недоступна», хотя сессия
    в БД была в порядке (импорт cookies) или база профилей сменилась — Messenger уже умел запасные варианты.
    """
    stored = (stored_profile_dir or "").strip()
    p = resolved_profile_under_base(db, stored)
    if p is not None and p.is_dir():
        return p
    if stored:
        try:
            raw = Path(stored).expanduser().resolve()
            if raw.is_dir():
                return raw
        except OSError:
            pass
    fb = profiles_base_resolved(db) / f"fb_account_{account_id}"
    fb.mkdir(parents=True, exist_ok=True)
    return fb
