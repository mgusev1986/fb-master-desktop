"""LavaTop billing service.

Создание счёта (invoice) с recurring-подпиской, обработка webhook-событий:
  - "payment.success" / "Результат платежа"  → выдача ключа + email клиенту
  - "subscription.charged" / "Регулярный платёж" → продление ключа + email
  - "subscription.cancelled" → отметить cancelled + email клиенту

Ключевые отличия от NOWPayments:
  - Аутентификация webhook'ов: Basic Auth (LAVATOP_WEBHOOK_LOGIN / PASSWORD),
    не HMAC. Подпись проверяется в `verify_webhook_basic_auth()`.
  - Цена в RUB (по умолчанию). API тариф = lavatop_price_rub(duration_days).
  - При ВКЛ автопродлении (`LAVATOP_RECURRING_DEFAULT=true`) — создаётся
    подписка, а не разовый счёт.

Реальная структура payload'ов от LavaTop уточняется при первом боевом
webhook'е — `apply_webhook()` принимает любые ключевые имена и нормализует
через `_extract()` helper.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session


def _strip_payment_params(url: str) -> str:
    """Удалить query-param `paymentParams` из URL чекаута LavaTop.

    LavaTop при создании invoice через API кладёт в URL base64-состояние
    предзаполненной формы (валюта + метод оплаты). Если оставить —
    пользователь не увидит выбора валюты (RUB/EUR/USD) и PayPal'а.
    После strip'а — открывается универсальный checkout. Остальные query
    (если когда-нибудь появятся) сохраняются.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "paymentParams"]
        return urlunparse(parsed._replace(query=urlencode(kept)))
    except Exception:  # noqa: BLE001
        return url

from backend import config as app_config
from backend.models import (
    AccessKey,
    BillingRenewalOrder,
    LavaSubscription,
    LavaWebhookLog,
)
from backend.services import email as email_service
from backend.services.access_key_crypto import generate_plaintext_key, hash_access_key
from backend.services.fb_credentials_crypto import encrypt_secret
from backend.services.nowpayments_billing import encrypt_one_time_key

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _extract(data: dict, *keys: str) -> str | None:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return str(data[key])
    for nested_key in ("data", "payload", "object", "subscription", "payment", "contract"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested and nested[key] not in (None, ""):
                    return str(nested[key])
    return None


def _detect_event_type(data: dict, header: str | None = None) -> str:
    explicit = (header or "").strip().lower()
    if explicit:
        return explicit
    direct = _extract(data, "event_type", "eventType", "event", "type")
    if direct:
        return direct.strip().lower()
    status = (_extract(data, "status") or "").strip().lower()
    if status in ("success", "completed", "paid"):
        return "payment.success"
    if status in ("failed", "rejected"):
        return "payment.failed"
    if status in ("cancelled", "canceled"):
        return "subscription.cancelled"
    return "unknown"


def new_order_id() -> str:
    return "lava_" + secrets.token_urlsafe(16)


def verify_webhook_basic_auth(authorization_header: str | None) -> bool:
    """Сравнивает заголовок ``Authorization: Basic <b64>`` с конфигом."""
    expected_login = app_config.LAVATOP_WEBHOOK_LOGIN
    expected_pass = app_config.LAVATOP_WEBHOOK_PASSWORD
    if not (expected_login and expected_pass):
        return False
    raw = (authorization_header or "").strip()
    if not raw.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(raw[6:].strip()).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return False
    if ":" not in decoded:
        return False
    login, _, password = decoded.partition(":")
    return secrets.compare_digest(login, expected_login) and secrets.compare_digest(
        password, expected_pass
    )


def _periodicity_for_days(duration_days: int) -> str:
    return {
        30: "MONTHLY",
        90: "PERIOD_90_DAYS",
        180: "PERIOD_180_DAYS",
        365: "PERIOD_YEAR",
    }.get(int(duration_days), "MONTHLY")


async def create_invoice(
    *,
    order: BillingRenewalOrder,
    customer_email: str,
    duration_days: int,
    recurring: bool = True,
) -> tuple[dict | None, str]:
    """Создаёт счёт в LavaTop API.

    Точная схема LavaTop /api/v2/invoice уточнится по первому ответу. Если
    структура отличается — поправим payload и парсинг ответа.
    """
    if not app_config.LAVATOP_API_KEY:
        return None, "lavatop_api_key_missing"
    offer_id = app_config.lavatop_offer_id(duration_days)
    if not offer_id:
        return None, f"offer_id_missing_for_{duration_days}_days"

    payload: dict[str, Any] = {
        "email": customer_email,
        "offerId": offer_id,
        "currency": "RUB",
        "periodicity": _periodicity_for_days(duration_days),
        "buyerLanguage": "RU",
        "clientUtm": {
            "utm_source": "socmaster",
            "utm_campaign": f"lavatop_{duration_days}d",
            "external_order_id": order.np_order_id,
        },
    }
    if not recurring:
        payload["subscription"] = False

    url = f"{app_config.LAVATOP_API_BASE}/api/v2/invoice"
    headers = {
        "X-Api-Key": app_config.LAVATOP_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                logger.warning(
                    "LavaTop POST invoice %s body=%s",
                    r.status_code,
                    r.text[:500],
                )
                return None, f"http_{r.status_code}: {r.text[:200]}"
            data = r.json()
            raw_invoice_url = (
                data.get("paymentUrl") or data.get("payment_url") or data.get("url") or ""
            ).strip()
            # LavaTop возвращает URL вида
            #   /products/<id>/<offer>?paymentParams=<base64-state>
            # paymentParams форсирует валюту RUB + способ "карта" (см. payload
            # currency=RUB выше), что лишает пользователя выбора валюты
            # (RUB/EUR/USD) и PayPal'а в форме оплаты. Убираем этот query —
            # пользователь попадает на универсальную checkout-форму LavaTop.
            # Webhook tracking не страдает: invoice_id и contractId уже
            # сохранены ниже и приходят от LavaTop в IPN независимо от того,
            # какую валюту/способ выбрал пользователь в форме.
            invoice_url = _strip_payment_params(raw_invoice_url)
            if not invoice_url:
                return None, "no_payment_url_in_response"
            order.lava_invoice_id = (
                str(data.get("id") or data.get("invoiceId") or data.get("invoice_id") or "").strip()
                or None
            )
            order.lava_contract_id = (
                str(data.get("contractId") or data.get("contract_id") or "").strip() or None
            )
            order.status = "invoice_created"
            return {"invoice_url": invoice_url, "raw": data}, ""
    except httpx.HTTPError as e:
        logger.exception("LavaTop create_invoice HTTP error")
        return None, f"http_error: {e}"
    except Exception as e:  # noqa: BLE001
        logger.exception("LavaTop create_invoice unexpected error")
        return None, f"error: {e}"


def _find_order(db: Session, payload: dict) -> BillingRenewalOrder | None:
    invoice_id = _extract(payload, "invoiceId", "invoice_id", "id")
    if invoice_id:
        row = (
            db.query(BillingRenewalOrder)
            .filter(BillingRenewalOrder.lava_invoice_id == invoice_id)
            .first()
        )
        if row:
            return row
    ext = _extract(payload, "external_order_id", "externalOrderId", "order_id", "orderId")
    if ext:
        row = (
            db.query(BillingRenewalOrder)
            .filter(BillingRenewalOrder.np_order_id == ext)
            .first()
        )
        if row:
            return row
    contract_id = _extract(payload, "contractId", "contract_id")
    if contract_id:
        row = (
            db.query(BillingRenewalOrder)
            .filter(BillingRenewalOrder.lava_contract_id == contract_id)
            .first()
        )
        if row:
            return row
    return None


async def apply_webhook(
    db: Session,
    event_type: str,
    payload: dict,
    log_row: LavaWebhookLog,
) -> tuple[str, str]:
    et = (event_type or "").strip().lower()
    if et in ("payment.success", "result.payment", "result_payment", "payment_success"):
        return await _apply_payment_success(db, payload, log_row)
    if et in ("payment.failed", "payment_failed", "result_payment_failed"):
        return _apply_payment_failed(db, payload, log_row)
    if et in (
        "subscription.charged",
        "subscription.recurring.charged",
        "recurring.charged",
        "subscription_charged",
    ):
        return await _apply_subscription_charged(db, payload, log_row)
    if et in ("subscription.cancelled", "subscription_cancelled", "subscription.canceled"):
        return await _apply_subscription_cancelled(db, payload, log_row)
    if et in ("subscription.created", "subscription_created"):
        return "ignored", "subscription.created (no-op)"
    logger.info("LavaTop webhook: unhandled event_type=%r — ignored", et)
    return "ignored", f"unhandled event_type={et}"


async def _apply_payment_success(
    db: Session, payload: dict, log_row: LavaWebhookLog
) -> tuple[str, str]:
    order = _find_order(db, payload)
    if not order:
        return "order_not_found", "no order for invoice/contract"
    if order.access_key_id:
        log_row.order_id = order.id
        return "duplicate", f"order #{order.id} already has access_key_id={order.access_key_id}"

    plain = generate_plaintext_key()
    key_hash = hash_access_key(plain)
    if not key_hash:
        return "error", "empty_key_hash"
    duration_days = int(order.duration_days or 30)
    expires_at = _utc_now() + timedelta(days=max(1, duration_days))
    access_key = AccessKey(
        key_hash=key_hash,
        key_plain_enc=encrypt_secret(plain),
        label=f"LavaTop #{order.id}",
        expires_at=expires_at,
    )
    db.add(access_key)
    db.flush()
    order.access_key_id = access_key.id
    order.pending_plain_key_enc = encrypt_one_time_key(plain)
    order.status = "paid_ready"
    order.last_np_status = "lavatop_paid"

    sub_id = _extract(payload, "subscriptionId", "subscription_id", "contractId", "contract_id")
    customer_email = (
        order.customer_email or _extract(payload, "email", "buyerEmail") or ""
    ).strip()
    if sub_id:
        existing = (
            db.query(LavaSubscription)
            .filter(LavaSubscription.lava_subscription_id == sub_id)
            .first()
        )
        if not existing:
            sub = LavaSubscription(
                lava_subscription_id=sub_id,
                lava_contract_id=_extract(payload, "contractId", "contract_id"),
                customer_email=customer_email or "unknown@socmaster.local",
                access_key_id=access_key.id,
                status="active",
                period_days=duration_days,
                price_amount=str(order.price_amount or ""),
                price_currency=(order.price_currency or "rub").lower(),
                next_billing_at=_utc_now() + timedelta(days=duration_days),
                last_charged_at=_utc_now(),
                charges_count=1,
                last_webhook_payload_json=payload,
            )
            db.add(sub)

    log_row.order_id = order.id
    log_row.lava_subscription_id = sub_id
    db.commit()

    if customer_email:
        try:
            ok, info = await email_service.send_access_key_purchased(
                to=customer_email,
                access_key=plain,
                duration_days=duration_days,
                expires_at_label=expires_at.strftime("%Y-%m-%d %H:%M UTC"),
                unlock_url=f"{app_config.public_app_base_url()}/auth/unlock",
                cancel_subscription_url=None,
            )
            if not ok:
                logger.warning("LavaTop: email failed to=%s info=%s", customer_email, info)
        except Exception:  # noqa: BLE001
            logger.exception("LavaTop: email send error to=%s", customer_email)
    else:
        logger.warning("LavaTop: order #%s has no customer_email — skipped email", order.id)

    return "ok", ""


def _apply_payment_failed(
    db: Session, payload: dict, log_row: LavaWebhookLog
) -> tuple[str, str]:
    order = _find_order(db, payload)
    if not order:
        return "order_not_found", "no order for failed payment"
    order.status = "failed"
    order.last_np_status = "lavatop_failed"
    log_row.order_id = order.id
    db.add(order)
    db.commit()
    return "ok", ""


async def _apply_subscription_charged(
    db: Session, payload: dict, log_row: LavaWebhookLog
) -> tuple[str, str]:
    sub_id = _extract(payload, "subscriptionId", "subscription_id", "contractId", "contract_id")
    if not sub_id:
        return "error", "no subscription_id in payload"
    sub = (
        db.query(LavaSubscription)
        .filter(LavaSubscription.lava_subscription_id == sub_id)
        .first()
    )
    if not sub:
        return "order_not_found", f"no LavaSubscription for sub_id={sub_id}"
    access_key = db.query(AccessKey).filter(AccessKey.id == sub.access_key_id).first()
    if not access_key:
        return "error", f"AccessKey #{sub.access_key_id} not found"

    period = int(sub.period_days or 30)
    base = (
        access_key.expires_at
        if access_key.expires_at and access_key.expires_at > _utc_now()
        else _utc_now()
    )
    new_expires = base + timedelta(days=max(1, period))
    access_key.expires_at = new_expires
    sub.last_charged_at = _utc_now()
    sub.next_billing_at = new_expires
    sub.charges_count = int(sub.charges_count or 0) + 1
    sub.last_webhook_payload_json = payload
    db.add_all([access_key, sub])
    log_row.lava_subscription_id = sub_id
    db.commit()

    if sub.customer_email and "@" in sub.customer_email:
        try:
            await email_service.send_subscription_renewed(
                to=sub.customer_email,
                duration_days=period,
                expires_at_label=new_expires.strftime("%Y-%m-%d %H:%M UTC"),
                cancel_subscription_url=None,
            )
        except Exception:  # noqa: BLE001
            logger.exception("LavaTop: subscription_renewed email failed to=%s", sub.customer_email)
    return "ok", ""


async def _apply_subscription_cancelled(
    db: Session, payload: dict, log_row: LavaWebhookLog
) -> tuple[str, str]:
    sub_id = _extract(payload, "subscriptionId", "subscription_id", "contractId", "contract_id")
    if not sub_id:
        return "error", "no subscription_id in payload"
    sub = (
        db.query(LavaSubscription)
        .filter(LavaSubscription.lava_subscription_id == sub_id)
        .first()
    )
    if not sub:
        return "order_not_found", f"no LavaSubscription for sub_id={sub_id}"
    sub.status = "cancelled"
    sub.cancelled_at = _utc_now()
    sub.last_webhook_payload_json = payload
    log_row.lava_subscription_id = sub_id
    db.add(sub)
    db.commit()

    access_key = db.query(AccessKey).filter(AccessKey.id == sub.access_key_id).first()
    expires_label = (
        access_key.expires_at.strftime("%Y-%m-%d %H:%M UTC")
        if access_key and access_key.expires_at
        else "—"
    )
    if sub.customer_email and "@" in sub.customer_email:
        try:
            await email_service.send_subscription_cancelled(
                to=sub.customer_email,
                expires_at_label=expires_label,
            )
        except Exception:  # noqa: BLE001
            logger.exception("LavaTop: subscription_cancelled email failed to=%s", sub.customer_email)
    return "ok", ""
