"""Геолокация выходного IP через прокси (HTTP/SOCKS) и сопоставление с пресетом stealth."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from backend.services.fb_account_import_parse import ParsedProxyLine
from backend.services.fb_stealth_profile import DEFAULT_STEALTH_REGION, _REGION_PRESETS

logger = logging.getLogger(__name__)

# ISO 3166-1 alpha-2 → ключ пресета из fb_stealth_profile._REGION_PRESETS
ISO2_TO_STEALTH_REGION: dict[str, str] = {
    "AU": "australia",
    "AT": "austria",
    "AM": "armenia",
    "BD": "bangladesh",
    "BY": "belarus",
    "BE": "belgium",
    "BG": "bulgaria",
    "BR": "brazil",
    "GB": "united_kingdom",
    "UK": "united_kingdom",
    "VN": "vietnam",
    "DE": "germany",
    "HK": "hong_kong",
    "GR": "greece",
    "GE": "georgia",
    "DK": "denmark",
    "EG": "egypt",
    "IL": "israel",
    "IN": "india",
    "ID": "indonesia",
    "IE": "ireland",
    "ES": "spain",
    "IT": "italy",
    "KZ": "kazakhstan",
    "CA": "canada",
    "KG": "kyrgyzstan",
    "CN": "china",
    "CY": "cyprus",
    "LV": "latvia",
    "LT": "lithuania",
    "MY": "malaysia",
    "MA": "morocco",
    "MX": "mexico",
    "MD": "moldova",
    "NG": "nigeria",
    "NL": "netherlands",
    "NO": "norway",
    "AE": "uae",
    "PL": "poland",
    "PT": "portugal",
    "RU": "ru",
    "RO": "romania",
    "SA": "saudi_arabia",
    "RS": "serbia",
    "SG": "singapore",
    "SI": "slovenia",
    "US": "usa",
    "TJ": "tajikistan",
    "TW": "taiwan",
    "TH": "thailand",
    "TM": "turkmenistan",
    "TR": "turkey",
    "UZ": "uzbekistan",
    "UA": "ukraine",
    "PH": "philippines",
    "FI": "finland",
    "FR": "france",
    "CZ": "czech_republic",
    "CL": "chile",
    "CH": "switzerland",
    "SE": "sweden",
    "ZA": "south_africa",
    "KR": "south_korea",
    "JP": "japan",
    "EE": "estonia",
}


def _httpx_proxy_url(p: ParsedProxyLine) -> str:
    if p.username:
        u = quote(p.username, safe="")
        pw = quote(p.password or "", safe="")
        return f"{p.scheme}://{u}:{pw}@{p.host}:{p.port}"
    return f"{p.scheme}://{p.host}:{p.port}"


def _geo_from_ip_api(client: httpx.Client) -> dict[str, Any] | None:
    try:
        r = client.get(
            "http://ip-api.com/json/?fields=status,message,country,countryCode,query",
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.debug("ip-api.com failed", exc_info=True)
        return None
    if data.get("status") != "success":
        return None
    return {
        "ip": data.get("query") or "",
        "country": data.get("country") or "",
        "country_code": (data.get("countryCode") or "").upper(),
        "source": "ip-api.com",
    }


def _geo_from_ipwho(client: httpx.Client) -> dict[str, Any] | None:
    try:
        r = client.get("https://ipwho.is/", timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.debug("ipwho.is failed", exc_info=True)
        return None
    if not data.get("success"):
        return None
    return {
        "ip": data.get("ip") or "",
        "country": data.get("country") or "",
        "country_code": (data.get("country_code") or "").upper(),
        "source": "ipwho.is",
    }


def lookup_geo_through_proxy(p: ParsedProxyLine) -> dict[str, Any]:
    """
    HTTP-запрос к сервису гео через прокси → страна выхода.
    Возвращает dict для JSON (ok, error?, ip, country, country_code, stealth_region, locale, timezone_id, matched, hint).
    """
    proxy_url = _httpx_proxy_url(p)
    out_base: dict[str, Any] = {"ok": False}
    try:
        with httpx.Client(proxy=proxy_url, timeout=25.0, follow_redirects=True) as client:
            geo = _geo_from_ip_api(client) or _geo_from_ipwho(client)
    except httpx.ProxyError as e:
        return {**out_base, "error": f"Ошибка прокси: {e!s}"[:400]}
    except httpx.ConnectError as e:
        return {**out_base, "error": f"Не удалось подключиться к прокси: {e!s}"[:400]}
    except httpx.TimeoutException:
        return {**out_base, "error": "Таймаут при запросе через прокси"}
    except OSError as e:
        err = str(e).lower()
        if "socks" in err or "socksio" in err:
            return {
                **out_base,
                "error": "Для SOCKS5 установите зависимость: pip install 'httpx[socks]'",
            }
        return {**out_base, "error": f"Сеть: {e!s}"[:400]}
    except Exception as e:
        logger.exception("lookup_geo_through_proxy")
        return {**out_base, "error": f"Ошибка: {e!s}"[:400]}

    if not geo or not geo.get("country_code"):
        return {**out_base, "error": "Сервис геолокации не вернул страну"}

    cc = geo["country_code"]
    region_key = ISO2_TO_STEALTH_REGION.get(cc)
    matched = region_key is not None
    if not region_key:
        region_key = DEFAULT_STEALTH_REGION

    preset = _REGION_PRESETS.get(region_key) or _REGION_PRESETS[DEFAULT_STEALTH_REGION]
    hint = (
        f"Пресет «{region_key}» ({preset['locale']}, {preset['timezone_id']}) подставлен под IP {geo.get('ip', '')}."
        if matched
        else f"Код страны {cc} не в списке пресетов; подставлен регион по умолчанию ({DEFAULT_STEALTH_REGION}). Выберите вручную при необходимости."
    )

    return {
        "ok": True,
        "ip": geo.get("ip") or "",
        "country": geo.get("country") or "",
        "country_code": cc,
        "stealth_region": region_key,
        "locale": preset["locale"],
        "timezone_id": preset["timezone_id"],
        "matched": matched,
        "hint": hint,
        "source": geo.get("source") or "",
    }


def parsed_proxy_line_from_account_fields(
    proxy_url: str | None,
    proxy_username: str | None,
    proxy_password: str | None,
) -> ParsedProxyLine | None:
    """Собрать ParsedProxyLine из полей FBAccount (proxy_url как http(s)://host:port)."""
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    low = raw.lower()
    scheme = "http"
    if low.startswith("socks5://"):
        scheme = "socks5"
    elif low.startswith("https://"):
        scheme = "http"
    elif low.startswith("http://"):
        scheme = "http"
    try:
        u = urlparse(raw)
    except Exception:
        return None
    host = u.hostname
    if not host:
        return None
    port = u.port
    if not port:
        port = 1080 if scheme == "socks5" else 80
    return ParsedProxyLine(
        scheme=scheme if scheme in ("http", "socks5") else "http",
        host=host,
        port=int(port),
        username=(proxy_username or "").strip() or None,
        password=(proxy_password or "").strip() or None,
    )
