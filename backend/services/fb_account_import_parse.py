"""Парсинг строки аккаунта (маркетплейсы) и строки прокси host:port:user:pass."""

from __future__ import annotations

import json
import re
from json import JSONDecoder
from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedAccountLine:
    login: str
    password: str
    totp_secret: str | None
    birth_hint: str | None


def normalize_totp_secret(raw: str) -> str:
    """Убрать пробелы в Base32-секрете TOTP."""
    return re.sub(r"\s+", "", raw.strip()).upper()


def parse_account_line(line: str) -> ParsedAccountLine | None:
    """
    Формат: логин:пароль:TOTP_секрет:ДД.ММ.ГГГГ
    TOTP может содержать пробелы между группами.
    Разбор с maxsplit=3 по первым двоеточиям.
    """
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    parts = s.split(":", 3)
    if len(parts) < 3:
        return None
    login = parts[0].strip()
    password = parts[1].strip()
    # Не путать с рекламными строками в .txt (логин FB без пробелов/табов)
    if not login or re.search(r"\s", login):
        return None
    if len(parts) == 3:
        third = parts[2].strip()
        if re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}$", third):
            return ParsedAccountLine(login=login, password=password, totp_secret=None, birth_hint=third)
        return ParsedAccountLine(
            login=login,
            password=password,
            totp_secret=normalize_totp_secret(third) if third else None,
            birth_hint=None,
        )
    # 4 части
    totp_raw = parts[2].strip()
    birth = parts[3].strip()
    totp = normalize_totp_secret(totp_raw) if totp_raw else None
    birth_hint = birth if re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}$", birth) else None
    if not birth_hint and birth:
        # дата в последнем поле не распознана — считаем частью секрета (редко)
        totp = normalize_totp_secret(f"{totp_raw}:{birth}") if totp_raw else None
    return ParsedAccountLine(login=login, password=password, totp_secret=totp, birth_hint=birth_hint)


@dataclass
class ParsedProxyLine:
    scheme: str
    host: str
    port: int
    username: str | None
    password: str | None


def parse_proxy_line(line: str, default_scheme: str = "http") -> ParsedProxyLine | None:
    """
    host:port:user:pass или host:port:user или host:port
    Схему указывают префиксом http:// или socks5:// в начале всей строки (опционально).
    После host:port остаток режется не более чем на два поля (логин и пароль), чтобы пароль мог содержать ':'.
    """
    s = (line or "").strip()
    if not s:
        return None
    scheme = default_scheme
    low = s.lower()
    if low.startswith("socks5://"):
        scheme = "socks5"
        s = s[9:].strip()
    elif low.startswith("http://"):
        scheme = "http"
        s = s[7:].strip()
    elif low.startswith("https://"):
        scheme = "http"
        s = s[8:].strip()

    parts = s.split(":", 3)
    if len(parts) < 2:
        return None
    host = parts[0].strip()
    try:
        port = int(parts[1].strip())
    except ValueError:
        return None
    if not host or port <= 0 or port > 65535:
        return None
    user = parts[2].strip() if len(parts) > 2 else ""
    pw = parts[3].strip() if len(parts) > 3 else ""
    return ParsedProxyLine(
        scheme=scheme,
        host=host,
        port=port,
        username=user or None,
        password=pw or None,
    )


def proxy_url_for_playwright(p: ParsedProxyLine) -> str:
    return f"{p.scheme}://{p.host}:{p.port}"


def normalize_fb_account_proxy_form(
    proxy_url: str,
    proxy_username: str,
    proxy_password: str,
) -> tuple[str | None, str | None, str | None, bool]:
    """
    Разбор вставки одной строкой host:port:user:pass в поле «Адрес прокси» (как выдаёт провайдер).

    Возвращает (url для БД, username, пароль_для_записи_или_None, need_set_password).
    Если need_set_password ложь — пароль в БД не менять (пустое поле формы и пароль не извлечён из строки).
    """
    pu = (proxy_url or "").strip()
    un = (proxy_username or "").strip()
    pwf = (proxy_password or "").strip()

    pp = parse_proxy_line(pu)
    combined = bool(
        pp is not None
        and (pp.username is not None or pp.password is not None)
    )
    if combined:
        out_url = proxy_url_for_playwright(pp)
        out_user = (un or (pp.username or "")).strip() or None
        parsed_pw = (pp.password or "").strip()
        if pwf:
            return out_url, out_user, pwf, True
        if parsed_pw:
            return out_url, out_user, parsed_pw, True
        return out_url, out_user, None, False

    return (pu or None), (un or None), (pwf or None), bool(pwf)


def _expiration_date_to_unix_seconds(raw: Any) -> float | None:
    """Chrome/marketplace JSON: expirationDate в разных форматах.

    Распознаём:
      • Unix seconds (1e9 < x < 1e12) — как есть.
      • Unix milliseconds (1e12 < x < 1e15) — делим на 1000.
      • Windows FILETIME (x > 1e15) — 100-нс тики с 1601-01-01 UTC.
        Именно так Cookie-Editor (Chrome extension) экспортирует cookies в некоторых
        версиях — и это ГЛАВНЫЙ формат в .txt от большинства маркетплейсов (darkstore
        и др.). Раньше мы отбрасывали такие значения → cookies сохранялись без expires,
        инжектились как session-only → FB показывал «Продолжить» picker вместо фида.
    Если результат вне разумного окна (прошлое или больше 10 лет вперёд) — возвращаем
    дефолт 2 года вперёд для критичных FB cookies (иначе они session-only = не
    доживают до перезапуска webview).
    """
    if raw is None:
        return None
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    if x <= 0:
        return None
    sec: float | None = None
    if x > 1e15:
        # Windows FILETIME: 100-ns ticks с 1601-01-01 UTC. Смещение до 1970: 11644473600 сек.
        sec = (x / 1e7) - 11_644_473_600.0
    elif x > 1e12:
        sec = x / 1000.0
    elif x > 1e9:
        sec = x
    if sec is None:
        return None
    # Санити: от минус суток до +10 лет от now.
    import time as _t
    now = _t.time()
    if sec < now - 86_400:
        return None
    if sec > now + 10 * 365 * 86_400:
        # Сентинел Int64_MAX / "never": ограничиваем 2 годами, чтобы Electron считал cookie персистентным.
        return now + 2 * 365 * 86_400
    return sec


def marketplace_cookies_to_storage_state(raw_cookies: list[Any]) -> dict[str, Any]:
    """Массив объектов из выгрузки маркетплейса → storage_state Playwright."""
    cookies: list[dict[str, Any]] = []
    for item in raw_cookies:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if name is None or value is None:
            continue
        domain_s = str(domain or "").strip()
        if not domain_s:
            continue
        path_s = str(item.get("path") or "/").strip() or "/"
        c: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
            "domain": domain_s,
            "path": path_s,
        }
        exp = _expiration_date_to_unix_seconds(
            item.get("expirationDate") if item.get("expirationDate") is not None else item.get("expires")
        )
        if exp is not None:
            c["expires"] = exp
        ho = item.get("httpOnly")
        if ho is None:
            ho = item.get("httponly")
        if ho is not None:
            c["httpOnly"] = bool(ho)
        sec = item.get("secure")
        if sec is None:
            sec = item.get("is_secure")
        if sec is not None:
            c["secure"] = bool(sec)
        ss = item.get("sameSite") if item.get("sameSite") is not None else item.get("samesite")
        if ss is not None and str(ss).strip():
            _ss_val = str(ss).strip()
            if _ss_val in ("Strict", "Lax", "None"):
                c["sameSite"] = _ss_val
            # "Unspecified" / "no_restriction" / etc → omit (Playwright rejects them)
        cookies.append(c)
    return {"cookies": cookies, "origins": []}


@dataclass
class ParsedCookieMarketplaceLine:
    """Формат: телефон/логин:пароль:User-Agent|[{cookie},...] (авторизация по cookie)."""

    login: str
    password: str
    user_agent: str
    storage_state: dict[str, Any]
    fb_user_id: str | None


def parse_cookie_marketplace_line(line: str) -> ParsedCookieMarketplaceLine | None:
    """
    Разбор строки вида: login:password:Mozilla/5.0 ...|[{...cookie...}]
    Пароль и UA не должны содержать символ | ; первые два ':' отделяют логин и пароль, до '|' — UA.
    """
    s = (line or "").strip()
    if "|" not in s:
        return None
    left, right = s.split("|", 1)
    left = left.strip()
    right = right.strip()
    if not right.startswith("["):
        return None
    i1 = left.find(":")
    if i1 < 0:
        return None
    i2 = left.find(":", i1 + 1)
    if i2 < 0:
        return None
    login = left[:i1].strip()
    password = left[i1 + 1 : i2].strip()
    user_agent = left[i2 + 1 :].strip()
    if not login or not password or not user_agent:
        return None
    try:
        raw_cookies = json.loads(right)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_cookies, list) or not raw_cookies:
        return None
    if not any(
        isinstance(c, dict) and "facebook" in str(c.get("domain", "")).lower() for c in raw_cookies
    ):
        return None
    storage = marketplace_cookies_to_storage_state(raw_cookies)
    if not storage.get("cookies"):
        return None
    fb_uid: str | None = None
    for c in raw_cookies:
        if isinstance(c, dict) and c.get("name") == "c_user":
            fb_uid = (c.get("value") or "").strip() or None
            break
    return ParsedCookieMarketplaceLine(
        login=login,
        password=password,
        user_agent=user_agent,
        storage_state=storage,
        fb_user_id=fb_uid,
    )


# Типовый Chrome UA, если в строке импорта нет своего (формат uid|pass|токен|[cookies]).
_DEFAULT_COOKIE_IMPORT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Если в куках мобильный viewport (wd / m_pixel_ratio), подставляем мобильный Chrome — ближе к сессии.
_DEFAULT_COOKIE_IMPORT_UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 13; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)


def _looks_like_browser_user_agent(seg: str) -> bool:
    s = (seg or "").strip()
    if len(s) < 28:
        return False
    low = s.lower()
    if low in ("ua", "n/a", "none", "-", "unknown"):
        return False
    if "mozilla/5.0" not in low:
        return False
    return "applewebkit" in low or "chrome/" in low or "crios/" in low or "safari/" in low


def _user_agent_from_cookie_hints(raw_cookies: list[Any]) -> str:
    """
    UA для импорта по cookie-подсказкам.

    Всегда возвращаем десктопный UA: FB Master работает в десктопном Chromium,
    а мобильный UA вызывает редирект на m.facebook.com, который не рендерится
    в Playwright (пустой белый экран).  Основные сессионные cookies (xs, c_user,
    datr, sb, fr) действительны и для www.facebook.com.
    """
    return _DEFAULT_COOKIE_IMPORT_UA


def parse_cookie_pipe_token_line(line: str) -> ParsedCookieMarketplaceLine | None:
    """
    Частый формат выгрузки: uid|пароль|EA...длинный_токен|[{...cookie JSON...}]
    UA: при наличии поля с Mozilla в доп. сегментах после токена — берём его; иначе десктоп/мобильный шаблон по кукам wd / m_pixel_ratio.
    """
    s = (line or "").strip()
    if not s or "|[{" not in s:
        return None
    idx = s.find("|[{")
    if idx < 0:
        return None
    prefix = s[:idx].strip()
    json_str = s[idx + 1 :].strip()
    if not json_str.startswith("["):
        return None
    segs = [p.strip() for p in prefix.split("|")]
    if len(segs) < 3:
        return None
    login = segs[0]
    password = segs[1]
    if not login or not password:
        return None
    if re.search(r"[\n\r]", login) or re.search(r"[\n\r]", password):
        return None
    if len(login) > 200 or len(password) > 256:
        return None
    # Типичный выгрузочный uid Facebook (отсекает шапку магазина в одной строке с «|»).
    if not re.fullmatch(r"\d{5,20}", login):
        return None
    try:
        raw_cookies = json.loads(json_str)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_cookies, list) or not raw_cookies:
        return None
    if not any(
        isinstance(c, dict) and "facebook" in str(c.get("domain", "")).lower() for c in raw_cookies
    ):
        return None
    storage = marketplace_cookies_to_storage_state(raw_cookies)
    if not storage.get("cookies"):
        return None
    fb_uid: str | None = None
    for c in raw_cookies:
        if isinstance(c, dict) and c.get("name") == "c_user":
            fb_uid = (c.get("value") or "").strip() or None
            break
    ua = _DEFAULT_COOKIE_IMPORT_UA
    for seg in segs[2:]:
        if _looks_like_browser_user_agent(seg):
            ua = seg.strip()[:512]
            break
    if ua == _DEFAULT_COOKIE_IMPORT_UA:
        ua = _user_agent_from_cookie_hints(raw_cookies)
    return ParsedCookieMarketplaceLine(
        login=login,
        password=password,
        user_agent=ua,
        storage_state=storage,
        fb_user_id=fb_uid,
    )


def _cookie_domain_looks_facebook(domain: str) -> bool:
    d = (domain or "").lower().strip()
    return (
        "facebook" in d
        or "messenger" in d
        or "instagram" in d
        or d.endswith(".fb.com")
        or d == "fb.com"
    )


def parse_cookie_json_storage_blob(text: str) -> ParsedCookieMarketplaceLine | None:
    """
    Целый файл или большая вставка: только JSON — массив cookie-объектов либо
    Playwright storage_state {"cookies":[...], "origins":...}.
    Без префикса логин:пароль:UA — сразу сессия в базу (как выгрузка Cookie-Editor / маркетплейса).
    """
    blob = _strip_import_text_bom((text or "").strip())
    if not blob or len(blob) > 15_000_000:
        return None
    data: Any = None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        i = blob.find("[")
        if i < 0:
            i = blob.find("{")
        if i < 0:
            return None
        try:
            data = json.loads(blob[i:])
        except json.JSONDecodeError:
            return None
    raw_list: list[Any] | None = None
    if isinstance(data, dict):
        c = data.get("cookies")
        if isinstance(c, list):
            raw_list = c
    elif isinstance(data, list):
        raw_list = data
    if not raw_list:
        return None
    if not isinstance(raw_list[0], dict):
        return None
    if not any(
        isinstance(x, dict) and x.get("name") and x.get("value") is not None for x in raw_list
    ):
        return None
    if not any(
        isinstance(c, dict) and _cookie_domain_looks_facebook(str(c.get("domain", "") or ""))
        for c in raw_list
    ):
        return None
    storage = marketplace_cookies_to_storage_state(raw_list)
    if not storage.get("cookies"):
        return None
    fb_uid: str | None = None
    for c in raw_list:
        if isinstance(c, dict) and c.get("name") == "c_user":
            fb_uid = (c.get("value") or "").strip() or None
            break
    login_slot = (fb_uid or "fb_cookie_import").strip()[:80]
    return ParsedCookieMarketplaceLine(
        login=login_slot,
        password="",
        user_agent=_user_agent_from_cookie_hints(raw_list),
        storage_state=storage,
        fb_user_id=fb_uid,
    )


def parse_cookie_semicolon_meta_pipe_line(line: str) -> ParsedCookieMarketplaceLine | None:
    """
    Формат части магазинов: email;пароль|ДР|fb_id|…|Mozilla/…|имя|[{cookies JSON}]
    (ищем сегмент с реальным Mozilla/… среди полей после «|»; иначе UA по кукам wd / m_pixel_ratio).
    """
    s = (line or "").strip()
    if "|[{" not in s or ";" not in s:
        return None
    idx = s.find("|[{")
    json_str = s[idx + 1 :].strip()
    prefix = s[:idx].strip()
    if not json_str.startswith("["):
        return None
    pipe_parts = [p.strip() for p in prefix.split("|")]
    head = pipe_parts[0] if pipe_parts else prefix
    if ";" not in head:
        return None
    a, b = head.split(";", 1)
    login, password = a.strip(), b.strip()
    if not login or not password:
        return None
    if re.search(r"[\n\r]", login) or re.search(r"[\n\r]", password):
        return None
    if len(login) > 200 or len(password) > 256:
        return None
    ua_from_line: str | None = None
    for seg in pipe_parts[1:]:
        if _looks_like_browser_user_agent(seg):
            ua_from_line = seg.strip()[:512]
            break
    try:
        raw_cookies = json.loads(json_str)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_cookies, list) or not raw_cookies:
        return None
    if not any(
        isinstance(c, dict) and "facebook" in str(c.get("domain", "")).lower() for c in raw_cookies
    ):
        return None
    storage = marketplace_cookies_to_storage_state(raw_cookies)
    if not storage.get("cookies"):
        return None
    fb_uid: str | None = None
    for c in raw_cookies:
        if isinstance(c, dict) and c.get("name") == "c_user":
            fb_uid = (c.get("value") or "").strip() or None
            break
    ua = ua_from_line if ua_from_line else _user_agent_from_cookie_hints(raw_cookies)
    return ParsedCookieMarketplaceLine(
        login=login,
        password=password,
        user_agent=ua,
        storage_state=storage,
        fb_user_id=fb_uid,
    )


def _strip_import_text_bom(text: str) -> str:
    if text.startswith("\ufeff"):
        return text[1:]
    return text


def _try_extract_embedded_cookie_import(text: str) -> str:
    """
    Строка аккаунта внутри «шапки» магазина: ищем |[{…}] и берём фрагмент от начала строки до конца JSON.
    Помогает, если весь .txt в одну линию или реклама с символом | ломает разбор целиком.
    """
    pos = 0
    dec = JSONDecoder()
    while True:
        i = text.find("|[{", pos)
        if i < 0:
            return ""
        json_start = i + 1
        if json_start >= len(text) or text[json_start] != "[":
            pos = i + 3
            continue
        try:
            _, end_exc = dec.raw_decode(text, json_start)
        except json.JSONDecodeError:
            pos = i + 3
            continue
        line_start = max(text.rfind("\n", 0, i), text.rfind("\r", 0, i)) + 1
        tail = text[i:end_exc]
        prefix_region = text[line_start:i]
        candidate_full = (prefix_region + tail).strip()
        if parse_cookie_marketplace_line(candidate_full):
            return candidate_full
        if parse_cookie_pipe_token_line(candidate_full):
            return candidate_full
        if parse_cookie_semicolon_meta_pipe_line(candidate_full):
            return candidate_full
        for m in reversed(list(re.finditer(r"\d{5,20}\|", prefix_region))):
            sub = (prefix_region[m.start() :] + tail).strip()
            if parse_cookie_pipe_token_line(sub):
                return sub
        pos = i + 3


def extract_fb_import_line_from_text(text: str) -> str:
    """Первая подходящая строка: cookie-формат или классический логин:пароль:TOTP… (в т.ч. после шапки магазина)."""
    blob = _strip_import_text_bom((text or "").strip())
    if not blob:
        return ""

    def _try_one_line(ln: str) -> str:
        s = ln.strip()
        if not s or s.startswith("#"):
            return ""
        if parse_cookie_marketplace_line(s):
            return s
        if parse_cookie_pipe_token_line(s):
            return s
        if parse_cookie_semicolon_meta_pipe_line(s):
            return s
        # Не путать шапку «Заказ: …» с логином:паролем, если в строке есть cookie-JSON.
        if "|[{" not in s and parse_account_line(s):
            return s
        return ""

    if "\n" not in blob and "\r" not in blob:
        hit = _try_one_line(blob)
        if hit:
            return hit
        return _try_extract_embedded_cookie_import(blob)

    for line in blob.splitlines():
        hit = _try_one_line(line)
        if hit:
            return hit
    return _try_extract_embedded_cookie_import(blob) or ""
