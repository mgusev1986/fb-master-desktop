#!/usr/bin/env python3
"""
Создать ключ доступа из командной строки (если ещё нет доступа в админку).

Использование (из корня проекта):
  python scripts/create_access_key.py
  python scripts/create_access_key.py "Клиент ООО Ромашка"
  python scripts/create_access_key.py --duration 30 "Подписка"
  python scripts/create_access_key.py --duration 365
  python scripts/create_access_key.py --duration 90
  python scripts/create_access_key.py --duration forever

Срок (--duration): forever | m3 | 30 | 90 | 180 | 365 (m3 = 3 минуты для теста истечения; остальное — дни).

Перед запуском в .env должен быть корректный SECRET_KEY (тот же, что у запущенного приложения),
иначе хеш ключа не совпадет с проверкой в UI.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from backend.database import SessionLocal, init_db
from backend.models import AccessKey
from backend.services.access_key_crypto import generate_plaintext_key, hash_access_key
from backend.services.fb_credentials_crypto import encrypt_secret


def _expires_at_for_cli(duration: str) -> datetime | None:
    d = (duration or "forever").strip().lower()
    if d not in ("forever", "m3", "30", "90", "180", "365"):
        return None
    now = datetime.now(timezone.utc)
    if d == "m3":
        return now + timedelta(minutes=3)
    if d == "30":
        return now + timedelta(days=30)
    if d == "90":
        return now + timedelta(days=90)
    if d == "180":
        return now + timedelta(days=180)
    if d == "365":
        return now + timedelta(days=365)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Создать ключ доступа FB Master")
    ap.add_argument(
        "--duration",
        choices=("forever", "m3", "30", "90", "180", "365"),
        default="forever",
        help="Срок: forever | m3 (3 мин, тест) | 30 | 90 | 180 | 365 дней",
    )
    ap.add_argument("label", nargs="?", default="", help="Подпись в списке ключей")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        plain = generate_plaintext_key()
        h = hash_access_key(plain)
        if not h:
            print("Ошибка: пустой хеш (проверьте SECRET_KEY).", file=sys.stderr)
            sys.exit(1)
        row = AccessKey(
            key_hash=h,
            key_plain_enc=encrypt_secret(plain),
            label=(args.label or "").strip() or None,
            expires_at=_expires_at_for_cli(args.duration),
        )
        db.add(row)
        db.commit()
        print("Создан ключ доступа. Сохраните — повторно не отобразится:")
        print(plain)
    finally:
        db.close()


if __name__ == "__main__":
    main()
