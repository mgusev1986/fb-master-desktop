"""Пресеты locale/TZ/UA под гео прокси + общие флаги Chromium для Playwright."""

from __future__ import annotations

import locale
import os
import random
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.models import FBAccount

# Регион по умолчанию в форме импорта и при неизвестном ключе пресета (купленные аккаунты).
# Ручное «Добавить аккаунт» не трогает stealth_* в БД — для него берётся locale/TZ компьютера (см. ниже).
DEFAULT_STEALTH_REGION = "turkey"
from backend.services.fb_stealth_engine import (
    STEALTH_CHROMIUM_BASE_ARGS,
    build_stealth_init_script,
    extra_stealth_chromium_args,
)

# Высота вьюпорта ниже этого значения режет интерфейс Facebook (док чата, поле ввода внизу).
_FB_MIN_VIEWPORT_HEIGHT = int(os.environ.get("FB_PLAYWRIGHT_MIN_VIEWPORT_HEIGHT", "1320"))
# Высота окна Chromium = вьюпорт + примерная рамка (вкладки, адресная строка), иначе страница «не влезает».
_FB_CHROMIUM_UI_HEIGHT_PAD = int(os.environ.get("FB_CHROMIUM_UI_HEIGHT_PAD", "168"))

_DEFAULT_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# (key, подпись в UI, BCP-47 locale, IANA timezone) — страны из каталога прокси-провайдера
_STEALTH_REGION_ROWS: list[tuple[str, str, str, str]] = [
    ("australia", "Австралия", "en-AU", "Australia/Sydney"),
    ("austria", "Австрия", "de-AT", "Europe/Vienna"),
    ("armenia", "Армения", "hy-AM", "Asia/Yerevan"),
    ("bangladesh", "Бангладеш", "bn-BD", "Asia/Dhaka"),
    ("belarus", "Беларусь", "be-BY", "Europe/Minsk"),
    ("belgium", "Бельгия", "nl-BE", "Europe/Brussels"),
    ("bulgaria", "Болгария", "bg-BG", "Europe/Sofia"),
    ("brazil", "Бразилия", "pt-BR", "America/Sao_Paulo"),
    ("united_kingdom", "Великобритания", "en-GB", "Europe/London"),
    ("vietnam", "Вьетнам", "vi-VN", "Asia/Ho_Chi_Minh"),
    ("germany", "Германия", "de-DE", "Europe/Berlin"),
    ("hong_kong", "Гонконг", "zh-HK", "Asia/Hong_Kong"),
    ("greece", "Греция", "el-GR", "Europe/Athens"),
    ("georgia", "Грузия", "ka-GE", "Asia/Tbilisi"),
    ("denmark", "Дания", "da-DK", "Europe/Copenhagen"),
    ("egypt", "Египет", "ar-EG", "Africa/Cairo"),
    ("israel", "Израиль", "he-IL", "Asia/Jerusalem"),
    ("india", "Индия", "en-IN", "Asia/Kolkata"),
    ("indonesia", "Индонезия", "id-ID", "Asia/Jakarta"),
    ("ireland", "Ирландия", "en-IE", "Europe/Dublin"),
    ("spain", "Испания", "es-ES", "Europe/Madrid"),
    ("italy", "Италия", "it-IT", "Europe/Rome"),
    ("kazakhstan", "Казахстан", "kk-KZ", "Asia/Almaty"),
    ("canada", "Канада", "en-CA", "America/Toronto"),
    ("kyrgyzstan", "Киргизия", "ky-KG", "Asia/Bishkek"),
    ("china", "Китай", "zh-CN", "Asia/Shanghai"),
    ("cyprus", "Кипр", "el-CY", "Asia/Nicosia"),
    ("latvia", "Латвия", "lv-LV", "Europe/Riga"),
    ("lithuania", "Литва", "lt-LT", "Europe/Vilnius"),
    ("malaysia", "Малайзия", "ms-MY", "Asia/Kuala_Lumpur"),
    ("morocco", "Марокко", "ar-MA", "Africa/Casablanca"),
    ("mexico", "Мексика", "es-MX", "America/Mexico_City"),
    ("moldova", "Молдова", "ro-MD", "Europe/Chisinau"),
    ("nigeria", "Нигерия", "en-NG", "Africa/Lagos"),
    ("netherlands", "Нидерланды", "nl-NL", "Europe/Amsterdam"),
    ("norway", "Норвегия", "nb-NO", "Europe/Oslo"),
    ("uae", "ОАЭ", "ar-AE", "Asia/Dubai"),
    ("poland", "Польша", "pl-PL", "Europe/Warsaw"),
    ("portugal", "Португалия", "pt-PT", "Europe/Lisbon"),
    ("ru", "Россия", "ru-RU", "Europe/Moscow"),
    ("romania", "Румыния", "ro-RO", "Europe/Bucharest"),
    ("saudi_arabia", "Саудовская Аравия", "ar-SA", "Asia/Riyadh"),
    ("serbia", "Сербия", "sr-RS", "Europe/Belgrade"),
    ("singapore", "Сингапур", "en-SG", "Asia/Singapore"),
    ("slovenia", "Словения", "sl-SI", "Europe/Ljubljana"),
    ("usa", "США", "en-US", "America/New_York"),
    ("tajikistan", "Таджикистан", "tg-TJ", "Asia/Dushanbe"),
    ("taiwan", "Тайвань", "zh-TW", "Asia/Taipei"),
    ("thailand", "Таиланд", "th-TH", "Asia/Bangkok"),
    ("turkmenistan", "Туркменистан", "tk-TM", "Asia/Ashgabat"),
    ("turkey", "Турция", "tr-TR", "Europe/Istanbul"),
    ("uzbekistan", "Узбекистан", "uz-UZ", "Asia/Tashkent"),
    ("ukraine", "Украина", "uk-UA", "Europe/Kyiv"),
    ("philippines", "Филиппины", "en-PH", "Asia/Manila"),
    ("finland", "Финляндия", "fi-FI", "Europe/Helsinki"),
    ("france", "Франция", "fr-FR", "Europe/Paris"),
    ("czech_republic", "Чехия", "cs-CZ", "Europe/Prague"),
    ("chile", "Чили", "es-CL", "America/Santiago"),
    ("switzerland", "Швейцария", "de-CH", "Europe/Zurich"),
    ("sweden", "Швеция", "sv-SE", "Europe/Stockholm"),
    ("south_africa", "ЮАР", "en-ZA", "Africa/Johannesburg"),
    ("south_korea", "Южная Корея", "ko-KR", "Asia/Seoul"),
    ("japan", "Япония", "ja-JP", "Asia/Tokyo"),
    ("estonia", "Эстония", "et-EE", "Europe/Tallinn"),
]

_REGION_PRESETS: dict[str, dict[str, Any]] = {
    key: {"locale": loc, "timezone_id": tz, "user_agents": list(_DEFAULT_UAS)}
    for key, _label, loc, tz in _STEALTH_REGION_ROWS
}


def stealth_region_choices() -> list[tuple[str, str]]:
    """Пары (value, label) для выпадающего списка импорта, по алфавиту подписи."""
    rows = sorted(_STEALTH_REGION_ROWS, key=lambda r: r[1].casefold())
    out = [(k, f"{lab} — {loc}, {tz}") for k, lab, loc, tz in rows]
    out.append(("custom", "Свой locale / timezone (поля ниже)"))
    return out


def apply_custom_stealth(acc: FBAccount, locale: str, timezone_id: str) -> None:
    acc.stealth_locale = (locale or "en-US").strip() or "en-US"
    acc.stealth_timezone_id = (timezone_id or "UTC").strip() or "UTC"
    acc.stealth_user_agent = random.choice(_DEFAULT_UAS)
    w, h = _pick_viewport()
    acc.stealth_viewport_w = w
    acc.stealth_viewport_h = h


def apply_region_preset_to_account(acc: FBAccount, region_key: str) -> None:
    key = (region_key or DEFAULT_STEALTH_REGION).strip().lower()
    if key == "custom":
        return
    pr = _REGION_PRESETS.get(key) or _REGION_PRESETS[DEFAULT_STEALTH_REGION]
    ua_list = pr.get("user_agents") or _DEFAULT_UAS
    acc.stealth_locale = pr["locale"]
    acc.stealth_timezone_id = pr["timezone_id"]
    acc.stealth_user_agent = random.choice(ua_list)
    w, h = _pick_viewport()
    acc.stealth_viewport_w = w
    acc.stealth_viewport_h = h


def _pick_viewport() -> tuple[int, int]:
    # Достаточная высота для headless; для headed см. no_viewport в fb_playwright._launch_kwargs_persistent.
    return random.choice([(1280, 1180), (1366, 1200), (1440, 1220), (1536, 1240)])


def system_default_locale_bcp47() -> str:
    """BCP-47 из окружения/ОС — для аккаунтов без пресета импорта (кнопка «Добавить аккаунт»)."""
    loc: str | None = None
    try:
        raw = locale.getlocale(locale.LC_MESSAGES)[0]
        if raw:
            loc = str(raw)
    except Exception:
        pass
    if not loc or loc in ("C", "POSIX") or loc.startswith("C."):
        for env_k in ("LC_ALL", "LANG", "LC_CTYPE"):
            v = (os.environ.get(env_k) or "").strip()
            if v:
                base = v.split("@")[0].split(".")[0]
                if base and base not in ("C", "POSIX"):
                    loc = base
                    break
    if not loc or loc in ("C", "POSIX"):
        return "en-US"
    return loc.replace("_", "-")


def system_default_timezone_iana() -> str:
    """IANA timezone — для аккаунтов без пресета; macOS/Linux обычно даёт именованную зону."""
    tz_env = (os.environ.get("TZ") or "").strip()
    if tz_env:
        if tz_env.startswith(":"):
            tz_env = tz_env[1:]
        if "/" in tz_env:
            try:
                ZoneInfo(tz_env)
                return tz_env
            except (ZoneInfoNotFoundError, ValueError):
                pass
    tzinfo = datetime.now().astimezone().tzinfo
    if tzinfo is not None:
        key = getattr(tzinfo, "key", None)
        if key:
            return str(key)
    seq = (os.getenv("SEQUENCE_TIMEZONE") or "").strip() or "UTC"
    try:
        ZoneInfo(seq)
        return seq
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def _is_mobile_user_agent(ua: str) -> bool:
    low = (ua or "").lower()
    return "mobile" in low and ("android" in low or "iphone" in low)


def stealth_playwright_options_from_account(acc: FBAccount | None) -> dict[str, Any]:
    """Опции для launch_persistent_context: locale, timezone_id, user_agent, viewport, args, init_script."""
    if acc and (acc.stealth_locale or acc.stealth_user_agent):
        loc = (acc.stealth_locale or "en-US").strip()
        tz = (acc.stealth_timezone_id or "UTC").strip()
        ua = (acc.stealth_user_agent or "").strip() or random.choice(_DEFAULT_UAS)
        if _is_mobile_user_agent(ua):
            ua = random.choice(_DEFAULT_UAS)
        vw = int(acc.stealth_viewport_w or 1366)
        vh = int(acc.stealth_viewport_h or 960)
    else:
        # Нет сохранённого пресета (типично после «Добавить аккаунт») — не подмешивать Турцию из формы импорта.
        loc = system_default_locale_bcp47()
        tz = system_default_timezone_iana()
        ua = random.choice(_DEFAULT_UAS)
        vw, vh = _pick_viewport()

    vw = int(vw)
    vh = max(int(vh), _FB_MIN_VIEWPORT_HEIGHT)

    lang_arg = loc.split("-")[0] if "-" in loc else loc
    base_chromium = [a for a in STEALTH_CHROMIUM_BASE_ARGS if not a.startswith("--window-size=")]
    args = (
        base_chromium
        + list(extra_stealth_chromium_args())
        + [
            f"--lang={lang_arg}",
            f"--window-size={vw},{vh + _FB_CHROMIUM_UI_HEIGHT_PAD}",
        ]
    )

    return {
        "locale": loc,
        "timezone_id": tz,
        "user_agent": ua,
        "viewport": {"width": vw, "height": vh},
        "args": args,
        "init_script": build_stealth_init_script(
            acc, user_agent=ua, viewport_w=vw, viewport_h=vh
        ),
    }
