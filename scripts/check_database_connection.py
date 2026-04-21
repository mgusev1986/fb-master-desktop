#!/usr/bin/env python3
"""
Проверка подключения к БД из .env (DATABASE_URL или SUPABASE_DATABASE_URL).
Запуск из корня проекта: python3 scripts/check_database_connection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from sqlalchemy import text

from backend.config import DATABASE_URL
from backend.database import engine, using_postgresql


def main() -> int:
    kind = "PostgreSQL" if using_postgresql() else "SQLite"
    # Не печатаем пароль — только тип и хост/путь без credentials
    safe = DATABASE_URL
    if "@" in safe and "://" in safe:
        head, _, tail = safe.partition("://")
        if "@" in tail:
            hostpart = tail.split("@", 1)[-1].split("/")[0]
            safe = f"{head}://***@{hostpart}/…"
    print(f"Режим: {kind}")
    print(f"URL (маскирован): {safe}")
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("OK: SELECT 1 выполнен.")
        return 0
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
