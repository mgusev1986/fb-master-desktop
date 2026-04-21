"""Подписанные ссылки на файлы в static/releases (без прямого угадывания URL)."""

from __future__ import annotations

import hmac
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from backend.config import SECRET_KEY, public_app_base_url

_RELEASES_PREFIX = "/static/releases/"
_DEV_RELEASES_INFIX = "/static/releases/dev/"
_FILENAME_RE = re.compile(r"^FbMaster-[A-Za-z0-9._+-]+\.(dmg|zip)$")


def _basename_allowed(name: str) -> bool:
    s = (name or "").strip()
    return bool(s and s == Path(s).name and _FILENAME_RE.match(s))


def release_signing_enabled() -> bool:
    return __import__("os").getenv("FB_RELEASE_SIGNED_URLS", "true").lower() not in (
        "0",
        "false",
        "no",
    )


def block_static_releases_path() -> bool:
    return __import__("os").getenv("FB_BLOCK_STATIC_RELEASES", "").lower() in ("1", "true", "yes")


def _releases_dir(static_root: Path) -> Path:
    return (static_root / "releases").resolve()


def is_allowed_release_filename(name: str) -> bool:
    """
    Ключ для подписи и query f:
    - FbMaster-….dmg|zip — файл в static/releases/
    - dev/FbMaster-….dmg|zip — файл в static/releases/dev/ (канал разработчика)
    """
    s = (name or "").strip()
    if not s or ".." in s or "\n" in s:
        return False
    if s.startswith("dev/"):
        rest = s[4:]
        if not rest or rest != Path(rest).name:
            return False
        return _basename_allowed(rest)
    if s != Path(s).name:
        return False
    return _basename_allowed(s)


def verify_release_token(filename: str, exp_str: str, sig: str) -> bool:
    if not is_allowed_release_filename(filename):
        return False
    try:
        exp = int(exp_str)
    except (TypeError, ValueError):
        return False
    if int(time.time()) > exp:
        return False
    msg = f"{filename}|{exp}".encode("utf-8")
    key = (SECRET_KEY or "dev-secret-change-me").encode("utf-8")
    expect = hmac.new(key, msg, "sha256").hexdigest()
    got = (sig or "").strip().lower()
    try:
        return hmac.compare_digest(expect, got)
    except Exception:
        return False


def build_signed_release_url(filename: str, ttl_seconds: int) -> str:
    exp = int(time.time()) + max(60, int(ttl_seconds))
    msg = f"{filename}|{exp}".encode("utf-8")
    key = (SECRET_KEY or "dev-secret-change-me").encode("utf-8")
    sig = hmac.new(key, msg, "sha256").hexdigest()
    base = public_app_base_url().rstrip("/")
    q = urlencode({"f": filename, "exp": str(exp), "sig": sig})
    return f"{base}/download/release?{q}"


def maybe_sign_release_download_url(raw_url: str, ttl_seconds: int) -> str:
    """
    Если URL указывает на /static/releases/<имя> на этом хосте — заменить на подписанный /download/release.
    Внешние CDN не трогаем.
    """
    if not release_signing_enabled() or not (raw_url or "").strip():
        return raw_url
    try:
        u = urlparse(raw_url.strip())
    except Exception:
        return raw_url
    path = u.path or ""
    if _DEV_RELEASES_INFIX in path:
        tail = path.split(_DEV_RELEASES_INFIX, 1)[-1].strip()
        token = f"dev/{tail}" if tail and "/" not in tail else ""
        if token and is_allowed_release_filename(token):
            return build_signed_release_url(token, ttl_seconds)
        return raw_url
    if _RELEASES_PREFIX not in path:
        return raw_url
    name = path.split(_RELEASES_PREFIX, 1)[-1].strip()
    if "/" in name:
        return raw_url
    if not is_allowed_release_filename(name):
        return raw_url
    return build_signed_release_url(name, ttl_seconds)


def sign_url_dict(urls: dict[str, str], ttl_seconds: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in urls.items():
        if isinstance(v, str) and v.strip():
            out[k] = maybe_sign_release_download_url(v, ttl_seconds)
        else:
            out[k] = v
    return out


def resolved_release_file(static_root: Path, filename: str) -> Path | None:
    if not is_allowed_release_filename(filename):
        return None
    releases = _releases_dir(static_root)
    if filename.startswith("dev/"):
        base = (releases / "dev").resolve()
        bn = filename[4:]
        target = (base / bn).resolve()
    else:
        base = releases
        target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target

