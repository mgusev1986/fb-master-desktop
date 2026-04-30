"""Оплата продления ключа через NOWPayments (invoice + IPN + опрос статуса)."""

from __future__ import annotations

import logging
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.database import get_db
from backend.models import BillingRenewalOrder
from backend.services.desktop_device import effective_device_fingerprint
from backend.services.i18n import get_lang, select_template
from backend.services.nowpayments_billing import (
    apply_ipn_to_order,
    create_invoice,
    new_order_id,
    pop_plain_key_for_poll,
    verify_nowpayments_ipn,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])
webhook_router = APIRouter(tags=["webhooks"])


def _billing_return_paths(billing_source: str) -> tuple[str, str]:
    """(cancel_url path + query base, redirect path for errors)."""
    src = (billing_source or "").strip().lower()
    if src == "buy":
        return "/buy", "/buy"
    if src == "purchase":
        return "/purchase", "/purchase"
    return "/auth/unlock", "/auth/unlock"


@router.get("/buy")
async def buy_page(request: Request):
    """
    В окне FB Master Desktop локальный /buy бесполезен (нет релизов и NOWPayments в .env).
    Перенаправляем на /purchase — там ссылка на оплату и скачивание на проде.
    """
    ua = request.headers.get("user-agent") or ""
    if "FBMasterDesktop" in ua and app_config.is_local_loopback_app_base():
        q = request.url.query
        loc = "/purchase" + (f"?{q}" if q else "")
        return RedirectResponse(loc, status_code=303)
    templates = request.app.state.templates
    err = (request.query_params.get("error") or "").strip()
    buy_error = unquote(err) if err else ""
    return templates.TemplateResponse(
        select_template(request, "buy.html"),
        {
            "request": request,
            "lang": get_lang(request),
            "buy_error": buy_error,
            "nowpayments_enabled": app_config.nowpayments_enabled(),
            "download_urls": app_config.desktop_buy_page_download_urls(for_paid_flow=False),
            "np_price_30": app_config.NOWPAYMENTS_PRICE_USD_30,
            "np_price_90": app_config.NOWPAYMENTS_PRICE_USD_90,
            "np_price_180": app_config.NOWPAYMENTS_PRICE_USD_180,
            "np_price_365": app_config.NOWPAYMENTS_PRICE_USD_365,
            "np_price_test": app_config.NOWPAYMENTS_PRICE_USD_TEST,
            "np_test_option_label": app_config.nowpayments_test_tariff_option_label(),
            "np_test_enabled": app_config.nowpayments_test_tariff_enabled(),
            "access_key_required": app_config.fb_master_access_key_required(),
            "lavatop_enabled": app_config.lavatop_enabled(),
            "lavatop_price_usd_30": app_config.lavatop_price_usd(30),
            "lavatop_price_usd_90": app_config.lavatop_price_usd(90),
            "lavatop_price_usd_180": app_config.lavatop_price_usd(180),
            "lavatop_price_usd_365": app_config.lavatop_price_usd(365),
        },
    )


@router.get("/download/dev")
async def download_dev_page(request: Request):
    """
    Страница скачивания только для разработчика: отдельные артефакты из static/releases/dev/
    и переменные FB_DESKTOP_DEV_* — не путать с клиентским /buy.
    """
    templates = request.app.state.templates
    latest = app_config.desktop_dev_update_latest_version()
    notes = (app_config.desktop_dev_update_release_notes() or "").strip()
    manifest_url = f"{app_config.public_app_base_url().rstrip('/')}/api/public/desktop-update-dev"
    return templates.TemplateResponse(
        "download_dev.html",
        {
            "request": request,
            "download_urls": app_config.desktop_dev_download_page_urls(for_paid_flow=False),
            "dev_latest_version": latest,
            "dev_release_notes": notes,
            "dev_manifest_url": manifest_url,
        },
    )


@router.get("/purchase")
async def purchase_page(request: Request):
    """Только оплата NOWPayments — для ссылки «Нет ключа?» из приложения (без страницы скачивания /buy)."""
    templates = request.app.state.templates
    err = (request.query_params.get("error") or "").strip()
    purchase_error = unquote(err) if err else ""
    return templates.TemplateResponse(
        select_template(request, "billing/purchase.html"),
        {
            "request": request,
            "lang": get_lang(request),
            "purchase_error": purchase_error,
            "nowpayments_enabled": app_config.nowpayments_enabled(),
            "remote_checkout_url": app_config.remote_https_checkout_url_for_local_app(),
            "np_price_30": app_config.NOWPAYMENTS_PRICE_USD_30,
            "np_price_90": app_config.NOWPAYMENTS_PRICE_USD_90,
            "np_price_180": app_config.NOWPAYMENTS_PRICE_USD_180,
            "np_price_365": app_config.NOWPAYMENTS_PRICE_USD_365,
            "np_price_test": app_config.NOWPAYMENTS_PRICE_USD_TEST,
            "np_test_option_label": app_config.nowpayments_test_tariff_option_label(),
            "np_test_enabled": app_config.nowpayments_test_tariff_enabled(),
            "lavatop_enabled": app_config.lavatop_enabled(),
            "lavatop_price_usd_30": app_config.lavatop_price_usd(30),
            "lavatop_price_usd_90": app_config.lavatop_price_usd(90),
            "lavatop_price_usd_180": app_config.lavatop_price_usd(180),
            "lavatop_price_usd_365": app_config.lavatop_price_usd(365),
        },
    )


@router.post("/billing/nowpayments/create")
async def billing_nowpayments_create(
    request: Request,
    db: Session = Depends(get_db),
    duration: str = Form("365"),
    device_id: str = Form(""),
    billing_source: str = Form("unlock"),
):
    device_id = effective_device_fingerprint(request, device_id)
    cancel_path, err_path = _billing_return_paths(billing_source)
    if not app_config.nowpayments_enabled():
        return RedirectResponse(
            err_path + "?error=" + quote("Онлайн-оплата не настроена."),
            status_code=303,
        )
    d = (duration or "").strip()
    if d == "365":
        # 2.69: тариф изменён на «35 дней — 100 USD». Значение поля формы остаётся "365"
        # для обратной совместимости (в шаблоне purchase.html и старых ссылках).
        # NOWPAYMENTS_PRICE_USD_365 теперь содержит цену 35-дневного тарифа (100 USD).
        days, price = 35, app_config.NOWPAYMENTS_PRICE_USD_365
    elif d == "test":
        if not app_config.nowpayments_test_tariff_enabled():
            return RedirectResponse(
                err_path + "?error=" + quote("Тестовый тариф отключён."),
                status_code=303,
            )
        days = app_config.NOWPAYMENTS_TEST_ORDER_DURATION_SENTINEL
        price = app_config.NOWPAYMENTS_PRICE_USD_TEST
    else:
        return RedirectResponse(
            err_path + "?error=" + quote("Доступен только тариф 35 дней."),
            status_code=303,
        )
    base = app_config.public_app_base_url()
    oid = new_order_id()
    invoice_description = None
    if d == "test":
        invoice_description = (
            f"FB Master, тестовый доступ {app_config.NOWPAYMENTS_TEST_DURATION_MINUTES} мин."
        )
    row = BillingRenewalOrder(
        np_order_id=oid,
        duration_days=days,
        device_fingerprint=(device_id or "").strip()[:128] or None,
        price_amount=str(price),
        price_currency="usd",
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    ipn_url = f"{base}/webhooks/nowpayments"
    success_url = f"{base}/billing/nowpayments/return?billing_token={quote(oid, safe='')}"
    cancel_url = f"{base.rstrip('/')}{cancel_path}"

    inv, err = create_invoice(
        order_id=oid,
        price_amount=str(price),
        price_currency="usd",
        duration_days=days,
        ipn_callback_url=ipn_url,
        success_url=success_url,
        cancel_url=cancel_url,
        order_description=invoice_description,
    )
    if not inv:
        db.delete(row)
        db.commit()
        logger.error("NOWPayments create failed: %s", err)
        return RedirectResponse(
            err_path + "?error=" + quote(f"Платёжная система недоступна: {err[:120]}"),
            status_code=303,
        )

    iid = inv.get("id")
    row.np_invoice_id = str(iid) if iid is not None else None
    row.status = "invoice_created"
    db.add(row)
    db.commit()

    inv_url = str(inv.get("invoice_url") or "").strip()
    if not inv_url:
        db.delete(row)
        db.commit()
        return RedirectResponse(
            err_path + "?error=" + quote("NOWPayments не вернул ссылку на оплату."),
            status_code=303,
        )

    request.session["billing_order_token"] = oid
    return RedirectResponse(inv_url, status_code=303)


@router.get("/billing/nowpayments/return")
async def billing_nowpayments_return(request: Request):
    token = (request.query_params.get("billing_token") or request.query_params.get("token") or "").strip()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "billing/nowpayments_return.html",
        {
            "request": request,
            "billing_token": token,
            "nowpayments_enabled": app_config.nowpayments_enabled(),
        },
    )


@router.get("/billing/nowpayments/status")
def billing_nowpayments_status(
    db: Session = Depends(get_db),
    billing_token: str = "",
):
    token = (billing_token or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "no_token"}, status_code=400)

    order = db.query(BillingRenewalOrder).filter(BillingRenewalOrder.np_order_id == token).first()
    if not order:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    if order.status == "failed":
        return JSONResponse({"ok": False, "error": "payment_failed", "np_status": order.last_np_status})

    plain = pop_plain_key_for_poll(db, order)
    if plain:
        body: dict = {
            "ok": True,
            "ready": True,
            "access_key": plain,
            "download_urls": app_config.desktop_buy_page_download_urls(for_paid_flow=True),
        }
        if order.duration_days == app_config.NOWPAYMENTS_TEST_ORDER_DURATION_SENTINEL:
            body["access_minutes"] = app_config.NOWPAYMENTS_TEST_DURATION_MINUTES
        else:
            body["duration_days"] = order.duration_days
        return JSONResponse(body)

    return JSONResponse(
        {
            "ok": True,
            "ready": False,
            "status": order.status,
            "np_status": order.last_np_status,
        }
    )


@webhook_router.post("/webhooks/nowpayments")
async def nowpayments_ipn_webhook(request: Request, db: Session = Depends(get_db)):
    from fastapi import HTTPException

    if not app_config.NOWPAYMENTS_IPN_SECRET:
        raise HTTPException(status_code=503, detail="ipn_disabled")
    body = await request.body()
    sig = request.headers.get("x-nowpayments-sig")
    data = verify_nowpayments_ipn(body, sig, app_config.NOWPAYMENTS_IPN_SECRET)
    if not data:
        raise HTTPException(status_code=403, detail="bad_signature")
    try:
        apply_ipn_to_order(db, data)
    except Exception:
        logger.exception("NOWPayments IPN handler")
        raise HTTPException(status_code=500, detail="handler_error")
    return JSONResponse({"ok": True})
