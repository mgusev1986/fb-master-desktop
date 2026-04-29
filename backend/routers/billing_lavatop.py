"""Оплата подписки через LavaTop (invoice + webhook + email-выдача ключа)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.database import get_db
from backend.models import BillingRenewalOrder, LavaWebhookLog
from backend.services import lavatop_billing
from backend.services.desktop_device import effective_device_fingerprint

logger = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])
webhook_router = APIRouter(tags=["webhooks"])

_templates = Jinja2Templates(directory="templates")
_ALLOWED_DAYS = {30, 90, 180, 365}


def _is_valid_email(value: str) -> bool:
    s = (value or "").strip()
    return "@" in s and "." in s.split("@")[-1] and len(s) <= 255


@router.post("/billing/lavatop/create")
async def create_lavatop_invoice(
    request: Request,
    db: Session = Depends(get_db),
    duration_days: int = Form(...),
    customer_email: str = Form(...),
    device_id: str = Form(""),
    billing_source: str = Form("buy"),
):
    """Создаёт LavaTop-счёт + перенаправляет клиента на их checkout-страницу."""
    if not app_config.lavatop_enabled():
        logger.warning("LavaTop create_invoice: provider not enabled (missing API key/creds)")
        return JSONResponse({"ok": False, "error": "lavatop_not_configured"}, status_code=503)

    if duration_days not in _ALLOWED_DAYS:
        return JSONResponse(
            {"ok": False, "error": f"invalid_duration_days={duration_days}"}, status_code=400
        )
    if not _is_valid_email(customer_email):
        return JSONResponse({"ok": False, "error": "invalid_email"}, status_code=400)

    fp = effective_device_fingerprint(request, device_id)
    price_amount = app_config.lavatop_price_usd(duration_days)
    if not price_amount or price_amount == "0":
        logger.error("LavaTop: price not configured for duration=%s", duration_days)
        return JSONResponse({"ok": False, "error": "price_not_configured"}, status_code=500)

    order = BillingRenewalOrder(
        np_order_id=lavatop_billing.new_order_id(),
        duration_days=duration_days,
        device_fingerprint=fp,
        price_amount=price_amount,
        price_currency="usd",
        provider="lavatop",
        customer_email=customer_email.strip().lower(),
        status="pending",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    invoice, err = await lavatop_billing.create_invoice(
        order=order,
        customer_email=customer_email.strip().lower(),
        duration_days=duration_days,
        recurring=app_config.lavatop_recurring_default(),
    )
    if not invoice:
        logger.error(
            "LavaTop: invoice creation failed order_id=%s err=%s", order.np_order_id, err
        )
        order.status = "failed"
        order.last_np_status = f"invoice_error: {err[:80]}"
        db.add(order)
        db.commit()
        return JSONResponse(
            {"ok": False, "error": "invoice_creation_failed", "detail": err}, status_code=502
        )

    db.add(order)
    db.commit()
    return RedirectResponse(invoice["invoice_url"], status_code=303)


@router.get("/billing/lavatop/return")
async def lavatop_return(request: Request):
    """Страница после возврата клиента из LavaTop checkout.

    Ключ выдаётся через email — здесь только показываем «Проверьте почту».
    """
    return _templates.TemplateResponse(
        "billing/lavatop_success.html",
        {"request": request},
    )


@webhook_router.post("/webhooks/lavatop")
async def lavatop_webhook(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_event_type: str | None = Header(default=None, alias="X-Event-Type"),
):
    """Единая точка приёма webhook'ов от LavaTop.

    LavaTop отправляет два типа webhook'ов:
      - "Результат платежа" → payment.success / payment.failed
      - "Регулярный платеж" → subscription.charged / subscription.cancelled

    Всегда возвращаем HTTP 200 (даже при ошибке обработки), чтобы LavaTop
    не повторял доставку в бесконечности. Все события логируются в
    `lava_webhook_logs` для отладки и replay.
    """
    raw_body = await request.body()
    raw_preview = raw_body[:2048].decode("utf-8", errors="replace") if raw_body else ""
    payload: dict
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:  # noqa: BLE001
        payload = {}

    event_type = lavatop_billing._detect_event_type(payload, header=x_event_type)

    log_row = LavaWebhookLog(
        event_type=event_type,
        basic_auth_ok=False,
        processing_status=None,
        payload_json=payload if payload else None,
        raw_body_preview=raw_preview,
    )
    db.add(log_row)
    db.flush()

    if not lavatop_billing.verify_webhook_basic_auth(authorization):
        log_row.basic_auth_ok = False
        log_row.processing_status = "invalid_auth"
        log_row.error_message = "Basic Auth failed"
        db.add(log_row)
        db.commit()
        logger.warning("LavaTop webhook: invalid Basic Auth event=%s", event_type)
        return JSONResponse({"ok": False, "error": "auth_failed"}, status_code=401)

    log_row.basic_auth_ok = True

    if not isinstance(payload, dict) or not payload:
        log_row.processing_status = "error"
        log_row.error_message = "empty or non-JSON body"
        db.add(log_row)
        db.commit()
        return JSONResponse({"ok": True, "warning": "empty_body"}, status_code=200)

    try:
        status, error = await lavatop_billing.apply_webhook(db, event_type, payload, log_row)
    except Exception as e:  # noqa: BLE001
        logger.exception("LavaTop webhook: processing crash event=%s", event_type)
        log_row.processing_status = "error"
        log_row.error_message = f"crash: {e}"
        db.add(log_row)
        db.commit()
        return JSONResponse({"ok": True, "warning": "internal_error"}, status_code=200)

    log_row.processing_status = status
    log_row.error_message = error or None
    db.add(log_row)
    db.commit()

    return JSONResponse({"ok": True, "status": status}, status_code=200)
