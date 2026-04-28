"""NOWPayments: счёт (invoice), проверка IPN, выдача ключа доступа после оплаты."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.models import AccessKey, BillingRenewalOrder
from backend.services.access_key_crypto import generate_plaintext_key, hash_access_key
from backend.services.fb_credentials_crypto import encrypt_secret

logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    raw = hashlib.sha256((app_config.SECRET_KEY + "|fbm_nowpay_fernet_v1").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_one_time_key(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_one_time_key(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def verify_nowpayments_ipn(body: bytes, signature_header: str | None, secret: str) -> dict | None:
    """
    Проверка x-nowpayments-sig: пробуем сырое тело и JSON с сортировкой ключей (как в документации NP).
    """
    if not secret or not signature_header:
        return None
    sign = signature_header.strip().lower()
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    sorted_body = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    for payload in (body, sorted_body):
        digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest().lower()
        if hmac.compare_digest(digest, sign):
            return parsed
    logger.warning("NOWPayments IPN: подпись не сошлась")
    return None


def create_invoice(
    *,
    order_id: str,
    price_amount: str,
    price_currency: str,
    duration_days: int,
    ipn_callback_url: str,
    success_url: str,
    cancel_url: str,
    order_description: str | None = None,
) -> tuple[dict | None, str]:
    url = f"{app_config.nowpayments_api_base()}/v1/invoice"
    try:
        amount = float(price_amount)
    except ValueError:
        return None, "invalid_price"
    desc = (order_description or "").strip()
    if not desc:
        desc = f"FB Master, доступ {duration_days} дн."
    payload = {
        "price_amount": amount,
        "price_currency": price_currency.lower(),
        "order_id": order_id,
        "order_description": desc,
        "ipn_callback_url": ipn_callback_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    headers = {"x-api-key": app_config.NOWPAYMENTS_API_KEY}
    try:
        with httpx.Client(timeout=45.0) as client:
            r = client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                logger.warning("NOWPayments POST invoice %s: %s", r.status_code, r.text[:800])
                return None, (r.text or "")[:400]
            data = r.json()
            if not data.get("invoice_url"):
                return None, "no_invoice_url"
            return data, ""
    except Exception as e:
        logger.exception("NOWPayments invoice HTTP")
        return None, str(e)


def _np_payment_status(data: dict) -> str:
    return (
        (data.get("payment_status") or data.get("invoice_status") or data.get("status") or "")
        .strip()
        .lower()
    )


def find_order_for_ipn(db: Session, data: dict) -> BillingRenewalOrder | None:
    oid = str(data.get("order_id") or "").strip()
    if oid:
        row = db.query(BillingRenewalOrder).filter(BillingRenewalOrder.np_order_id == oid).first()
        if row:
            return row
    inv = data.get("invoice_id") or data.get("id")
    if inv is not None:
        sid = str(inv).strip()
        return (
            db.query(BillingRenewalOrder)
            .filter(BillingRenewalOrder.np_invoice_id == sid)
            .first()
        )
    return None


def fulfill_order_after_payment(db: Session, order: BillingRenewalOrder, np_status: str) -> None:
    """Создаёт ключ и кладёт зашифрованный plain для одноразовой выдачи через /status."""
    if order.status == "fulfilled":
        return
    if order.pending_plain_key_enc:
        order.last_np_status = np_status
        db.add(order)
        db.commit()
        return
    plain = generate_plaintext_key()
    h = hash_access_key(plain)
    if not h:
        logger.error("NOWPayments fulfill: пустой хеш ключа order=%s", order.id)
        return
    dd = int(order.duration_days)
    if dd == app_config.NOWPAYMENTS_TEST_ORDER_DURATION_SENTINEL:
        exp = datetime.now(timezone.utc) + timedelta(
            minutes=max(1, int(app_config.NOWPAYMENTS_TEST_DURATION_MINUTES))
        )
    else:
        exp = datetime.now(timezone.utc) + timedelta(days=max(1, dd))
    pre_fp = (order.device_fingerprint or "").strip()
    pre_bind = pre_fp if len(pre_fp) >= 8 else None
    row = AccessKey(
        key_hash=h,
        key_plain_enc=encrypt_secret(plain),
        label=f"NOWPayments #{order.id}",
        expires_at=exp,
        device_fingerprint=pre_bind,
    )
    db.add(row)
    db.flush()
    order.access_key_id = row.id
    order.pending_plain_key_enc = encrypt_one_time_key(plain)
    order.status = "paid_ready"
    order.last_np_status = np_status
    db.add(order)
    db.commit()
    logger.info("NOWPayments: ключ выдан в очередь order_id=%s access_key_id=%s", order.np_order_id, row.id)


def _underpayment_within_tolerance(data: dict) -> tuple[bool, float]:
    """
    Проверяет, попадает ли недоплата в допуск.
    Возвращает (within_tolerance, ratio_paid). ratio = actually_paid / pay_amount.

    Применяется только для статуса partially_paid: если получатель прислал чуть меньше
    из-за проскальзывания курса USDT/USD или комиссии сети — это всё ещё считается
    успешной оплатой.

    С 2.92 — приоритет USD-допуска (NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_USD, по умолчанию $1).
    Если получено ≥ (pay_amount − $1) → засчитываем как оплачено.
    PCT-допуск (0.5% по умолчанию) — fallback на случай если USD-допуск выключен (=0).
    """
    try:
        pay_amount = float(data.get("pay_amount") or 0)
        actually_paid = float(
            data.get("actually_paid")
            or data.get("pay_amount_received")
            or data.get("outcome_amount")
            or 0
        )
    except (TypeError, ValueError):
        return False, 0.0
    if pay_amount <= 0 or actually_paid <= 0:
        return False, 0.0
    ratio = actually_paid / pay_amount

    # 2.92: USD-допуск в приоритете. Это абсолютная погрешность в долларах
    # (или эквиваленте крипто-актива), одинаково удобная для тарифа $79 и $2000.
    tolerance_usd = float(getattr(app_config, "NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_USD", 1.0))
    if tolerance_usd > 0:
        diff = pay_amount - actually_paid
        return diff <= tolerance_usd, ratio

    # Fallback: процентный допуск (legacy, 0.5%).
    tolerance_pct = float(getattr(app_config, "NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_PCT", 0.5))
    min_ratio = max(0.0, 1.0 - tolerance_pct / 100.0)
    return ratio >= min_ratio, ratio


def apply_ipn_to_order(db: Session, data: dict) -> None:
    order = find_order_for_ipn(db, data)
    if not order:
        logger.warning("NOWPayments IPN: заказ не найден keys=%s", list(data.keys())[:20])
        return
    st = _np_payment_status(data)
    order.last_np_status = st
    pid = data.get("payment_id")
    if pid is not None and not order.np_payment_id:
        order.np_payment_id = str(pid).strip() or order.np_payment_id
    if st == "finished":
        fulfill_order_after_payment(db, order, st)
    elif st == "partially_paid":
        ok, ratio = _underpayment_within_tolerance(data)
        try:
            _pay_amt = float(data.get("pay_amount") or 0)
            _act = float(data.get("actually_paid") or data.get("pay_amount_received") or data.get("outcome_amount") or 0)
            _diff = _pay_amt - _act
        except (TypeError, ValueError):
            _diff = 0.0
        _tol_usd = float(getattr(app_config, "NOWPAYMENTS_UNDERPAYMENT_TOLERANCE_USD", 1.0))
        if ok:
            logger.info(
                "NOWPayments: partially_paid в пределах допуска (недоплата %.4f, порог $%.2f, "
                "ratio %.4f%%) — считаем как finished, order_id=%s",
                _diff,
                _tol_usd,
                ratio * 100.0,
                order.np_order_id,
            )
            fulfill_order_after_payment(db, order, st)
        else:
            logger.warning(
                "NOWPayments: partially_paid вне допуска (недоплата %.4f > $%.2f, "
                "ratio %.4f%%) — ключ не выдан, order_id=%s",
                _diff,
                _tol_usd,
                ratio * 100.0,
                order.np_order_id,
            )
            db.add(order)
            db.commit()
    elif st in ("failed", "expired", "refunded"):
        order.status = "failed"
        db.add(order)
        db.commit()
    else:
        db.add(order)
        db.commit()


def new_order_id() -> str:
    return secrets.token_urlsafe(24)


def pop_plain_key_for_poll(db: Session, order: BillingRenewalOrder) -> str | None:
    """Один раз отдаёт ключ клиенту; переносит статус в fulfilled."""
    enc = (order.pending_plain_key_enc or "").strip()
    if not enc:
        return None
    try:
        plain = decrypt_one_time_key(enc)
    except Exception:
        logger.exception("NOWPayments: не удалось расшифровать ключ order=%s", order.id)
        return None
    order.pending_plain_key_enc = None
    order.status = "fulfilled"
    db.add(order)
    db.commit()
    return plain
