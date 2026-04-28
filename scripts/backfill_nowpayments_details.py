"""Bulk backfill NOWPayments-деталей в BillingRenewalOrder.

Прогоняет все заказы с заполненным np_payment_id и пустыми np_pay_amount
(показатель что детали ещё не сохранены), обращается к
NOWPayments API GET /v1/payment/{payment_id} и сохраняет результат через
_save_np_payment_details(order, data).

Используется ОДИН РАЗ после деплоя 3.0.5 для backfill старых заказов,
которые проходили через apply_ipn_to_order ДО того как функция
_save_np_payment_details начала писать поля. Идемпотентен — повторный
запуск перепишет поля свежими значениями.

Запуск на VPS:
  cd /opt/fb-master && .venv/bin/python scripts/backfill_nowpayments_details.py

Опции:
  --dry-run   только показать какие заказы будут обновлены, без записи
  --all       обновить ВСЕ заказы с np_payment_id (даже если np_pay_amount
              уже заполнен — переписать свежими значениями из NP API)
  --rate N    sleep N секунд между запросами (по умолч 0.5; NP rate-limit
              обычно 30 req/sec, но лучше консервативно)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Добавляем корень проекта в sys.path, чтобы импортировать backend.* без cd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.database import SessionLocal
from backend.models import BillingRenewalOrder
from backend.services.nowpayments_billing import backfill_payment_details


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill NOWPayments-деталей в BillingRenewalOrder.")
    p.add_argument("--dry-run", action="store_true", help="Не сохранять, только показать кандидатов")
    p.add_argument("--all", action="store_true", help="Обновить даже уже заполненные (refresh)")
    p.add_argument("--rate", type=float, default=0.5, help="Sleep (sec) между запросами; default 0.5")
    args = p.parse_args()

    db = SessionLocal()
    try:
        q = db.query(BillingRenewalOrder).filter(BillingRenewalOrder.np_payment_id.isnot(None))
        if not args.all:
            q = q.filter(BillingRenewalOrder.np_pay_amount.is_(None))
        rows = q.order_by(BillingRenewalOrder.id.asc()).all()

        if not rows:
            print("Кандидатов нет. (--all чтобы перезаписать уже заполненные)")
            return 0

        print(f"Найдено заказов: {len(rows)}")
        print(f"Режим: {'DRY-RUN' if args.dry_run else 'WRITE'} | sleep между запросами: {args.rate}s")
        print("─" * 70)

        success = 0
        failed = 0
        for r in rows:
            label = f"order #{r.id} np_order_id={r.np_order_id or '—'} payment_id={r.np_payment_id}"
            if args.dry_run:
                print(f"[DRY] {label}")
                continue
            ok = backfill_payment_details(db, r)
            marker = "✅" if ok else "❌"
            print(f"{marker} {label}")
            if ok:
                success += 1
            else:
                failed += 1
            if args.rate > 0:
                time.sleep(args.rate)

        if args.dry_run:
            print("─" * 70)
            print(f"DRY-RUN: ничего не сохранили. {len(rows)} кандидатов.")
        else:
            print("─" * 70)
            print(f"Готово: {success} обновлено, {failed} с ошибкой из {len(rows)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
