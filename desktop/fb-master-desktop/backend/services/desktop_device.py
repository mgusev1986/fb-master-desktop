"""Идентификатор устройства из десктопного Electron (железо), не из браузера."""

from __future__ import annotations

import re

from fastapi import Request

# SHA256 hex от main-процесса (64 символа)
_DESKTOP_HW_FP = re.compile(r"^[a-f0-9]{64}$")


def effective_device_fingerprint(request: Request, form_device_id: str) -> str:
    """
    Десктоп: User-Agent … FBMasterDesktop/VERSION и заголовок X-FB-Master-Device-Id
    (SHA256 от machine-id / железа).

    Браузер: скрытое поле device_id — SHA-256 отпечатка устройства (static/js/fbm-device-fingerprint.js)
    или старый UUID из localStorage для уже выданных ключей.
    """
    ua = (request.headers.get("user-agent") or "").lower()
    if "fbmasterdesktop" not in ua.replace(" ", ""):
        return (form_device_id or "").strip()[:128]
    hdr = (request.headers.get("x-fb-master-device-id") or "").strip().lower()
    if _DESKTOP_HW_FP.match(hdr):
        return hdr[:128]
    return (form_device_id or "").strip()[:128]
