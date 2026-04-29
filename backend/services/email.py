"""Email-сервис: Brevo (приоритет) с Mailgun-fallback.

Используется в ``backend/services/lavatop_billing.py`` после успешного webhook'а
"Результат платежа" (первая выдача ключа) и "Регулярный платеж" (продление).

Brevo (бывш. Sendinblue) — 300 писем/день навсегда бесплатно, REST API.
Mailgun — fallback если Brevo выключен (deprecated, оставлен на случай миграции).

Не падает при сбое — webhook должен всё равно вернуть HTTP 200, чтобы
провайдер не повторял доставку. Ошибка логируется + фиксируется в
``LavaWebhookLog.error_message`` отдельно.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from backend import config as app_config

logger = logging.getLogger(__name__)


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "email"


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=False,
    )


def render_email_template(template_name: str, context: dict[str, Any]) -> tuple[str, str]:
    """Рендерит HTML- и plain-text-варианты письма.

    Соглашение: для каждого письма создаём два файла в ``templates/email/``:
      - ``<name>.html`` — основной HTML-вариант
      - ``<name>.txt`` — plain-text fallback (для клиентов без HTML)
    """
    env = _jinja_env()
    html = env.get_template(f"{template_name}.html").render(**context)
    try:
        text = env.get_template(f"{template_name}.txt").render(**context)
    except Exception:  # noqa: BLE001
        text = _strip_html(html)
    return html, text


def _strip_html(html: str) -> str:
    """Очень простое преобразование HTML→plaintext fallback."""
    import re

    s = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.IGNORECASE)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    reply_to: str | None = None,
    tags: list[str] | None = None,
) -> tuple[bool, str]:
    """Отправляет email. Brevo приоритетен, Mailgun — fallback.

    Возвращает (ok, error_message_or_id). При выключенных всех провайдерах
    возвращает (False, "no_email_provider"). Webhook handler не падает.
    """
    to_email = (to or "").strip()
    if "@" not in to_email:
        logger.warning("Email: invalid recipient %r — skipped", to_email)
        return False, "invalid_email"

    text_body = text or _strip_html(html)

    if app_config.brevo_enabled():
        return await _send_via_brevo(
            to=to_email, subject=subject, html=html, text=text_body, reply_to=reply_to, tags=tags
        )
    if app_config.mailgun_enabled():
        return await _send_via_mailgun(
            to=to_email, subject=subject, html=html, text=text_body, reply_to=reply_to, tags=tags
        )
    logger.warning("Email: no provider enabled (Brevo/Mailgun) — skipped to=%s subj=%r", to_email, subject)
    return False, "no_email_provider"


async def _send_via_brevo(
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    reply_to: str | None,
    tags: list[str] | None,
) -> tuple[bool, str]:
    """POST https://api.brevo.com/v3/smtp/email с JSON-телом и api-key заголовком."""
    url = f"{app_config.brevo_api_base()}/v3/smtp/email"
    body: dict[str, Any] = {
        "sender": {
            "name": app_config.BREVO_FROM_NAME or "SOCMASTER",
            "email": app_config.BREVO_FROM_EMAIL,
        },
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
        "textContent": text,
    }
    if reply_to:
        body["replyTo"] = {"email": reply_to}
    if tags:
        body["tags"] = list(tags)

    headers = {
        "api-key": app_config.BREVO_API_KEY,
        "content-type": "application/json",
        "accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=body, headers=headers)
            if r.status_code >= 400:
                logger.warning(
                    "Brevo POST /smtp/email %s to=%s body=%s",
                    r.status_code,
                    to,
                    r.text[:500],
                )
                return False, f"brevo_http_{r.status_code}: {r.text[:200]}"
            try:
                resp = r.json()
            except Exception:  # noqa: BLE001
                resp = {}
            msg_id = str(resp.get("messageId") or "").strip()
            logger.info("Brevo: sent to=%s subj=%r id=%s", to, subject, msg_id or "<no-id>")
            return True, msg_id or "ok"
    except httpx.HTTPError as e:
        logger.exception("Brevo HTTP error to=%s", to)
        return False, f"brevo_http_error: {e}"
    except Exception as e:  # noqa: BLE001
        logger.exception("Brevo unexpected error to=%s", to)
        return False, f"brevo_error: {e}"


async def _send_via_mailgun(
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    reply_to: str | None,
    tags: list[str] | None,
) -> tuple[bool, str]:
    """Fallback: Mailgun (deprecated). Используется только если Brevo выключен."""
    from_full = f"{app_config.MAILGUN_FROM_NAME} <{app_config.MAILGUN_FROM_EMAIL}>"
    url = f"{app_config.mailgun_api_base()}/v3/{app_config.MAILGUN_DOMAIN}/messages"
    data: dict[str, Any] = {
        "from": from_full,
        "to": to,
        "subject": subject,
        "html": html,
        "text": text,
    }
    if reply_to:
        data["h:Reply-To"] = reply_to
    if tags:
        data["o:tag"] = list(tags)

    auth = ("api", app_config.MAILGUN_API_KEY)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, data=data, auth=auth)
            if r.status_code >= 400:
                logger.warning(
                    "Mailgun POST messages %s to=%s body=%s",
                    r.status_code,
                    to,
                    r.text[:500],
                )
                return False, f"mailgun_http_{r.status_code}: {r.text[:200]}"
            try:
                resp = r.json()
            except Exception:  # noqa: BLE001
                resp = {}
            msg_id = str(resp.get("id") or "").strip()
            logger.info("Mailgun: sent to=%s subj=%r id=%s", to, subject, msg_id or "<no-id>")
            return True, msg_id or "ok"
    except httpx.HTTPError as e:
        logger.exception("Mailgun HTTP error to=%s", to)
        return False, f"mailgun_http_error: {e}"
    except Exception as e:  # noqa: BLE001
        logger.exception("Mailgun unexpected error to=%s", to)
        return False, f"mailgun_error: {e}"


async def send_access_key_purchased(
    *,
    to: str,
    access_key: str,
    duration_days: int,
    expires_at_label: str,
    unlock_url: str,
    cancel_subscription_url: str | None = None,
) -> tuple[bool, str]:
    """Письмо после первичной оплаты с ключом доступа."""
    html, text = render_email_template(
        "access_key_purchased",
        {
            "access_key": access_key,
            "duration_days": duration_days,
            "expires_at_label": expires_at_label,
            "unlock_url": unlock_url,
            "cancel_subscription_url": cancel_subscription_url,
        },
    )
    return await send_email(
        to=to,
        subject=f"Ваш ключ доступа SOCMASTER ({duration_days} дн.)",
        html=html,
        text=text,
        tags=["lavatop", "access_key_purchased"],
    )


async def send_subscription_renewed(
    *,
    to: str,
    duration_days: int,
    expires_at_label: str,
    cancel_subscription_url: str | None = None,
) -> tuple[bool, str]:
    """Письмо после успешного продления подписки."""
    html, text = render_email_template(
        "subscription_renewed",
        {
            "duration_days": duration_days,
            "expires_at_label": expires_at_label,
            "cancel_subscription_url": cancel_subscription_url,
        },
    )
    return await send_email(
        to=to,
        subject=f"Ваша подписка SOCMASTER продлена на {duration_days} дн.",
        html=html,
        text=text,
        tags=["lavatop", "subscription_renewed"],
    )


async def send_subscription_cancelled(
    *,
    to: str,
    expires_at_label: str,
) -> tuple[bool, str]:
    """Письмо после отмены подписки клиентом."""
    html, text = render_email_template(
        "subscription_cancelled",
        {
            "expires_at_label": expires_at_label,
        },
    )
    return await send_email(
        to=to,
        subject="Подписка SOCMASTER отменена",
        html=html,
        text=text,
        tags=["lavatop", "subscription_cancelled"],
    )
