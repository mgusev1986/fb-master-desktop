"""
FB Master Stealth Shell — клиентский слой «движка» отпечатка (JS-инъекция + флаги Chromium).

Полноценный уровень AdsPower (нативные правки Canvas/WebGL/WebRTC/TLS в C++ в Chromium) —
отдельный репозиторий и сборка; каркас и инструкция: stealth_engine/chromium_fork/HOWTO.txt
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from typing import Any

from backend.config import BASE_DIR
from backend.models import FBAccount

_FINGERPRINT_TEMPLATE_PATH = BASE_DIR / "stealth_engine" / "fingerprint.inject.js"

_MINIMAL_INIT = """
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
  try { window.chrome = window.chrome || { runtime: {} }; } catch (e) {}
})();
"""

# Базовые флаги Chromium для Playwright / ручного запуска (совпадают с fb_stealth_profile до импорта)
STEALTH_CHROMIUM_BASE_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--disable-notifications",
    # Внешний размер окна переопределяется в fb_stealth_profile (viewport + отступ под UI Chrome).
    "--window-size=1280,1400",
]

# Дополнительные флаги процесса (WebRTC при прокси; не замена TLS/JA3 на уровне сетевого стека)
_EXTRA_CHROMIUM_ARGS = [
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]


def stealth_engine_enabled() -> bool:
    return os.getenv("FB_STEALTH_ENGINE", "1").lower() not in ("0", "false", "no", "off")


def extra_stealth_chromium_args() -> list[str]:
    if not stealth_engine_enabled():
        return []
    return list(_EXTRA_CHROMIUM_ARGS)


def stealth_chromium_launch_args(*, window_width: int = 1280, window_height: int = 1180) -> list[str]:
    """Аргументы для subprocess: Chromium/Chrome с теми же настройками, что и Playwright."""
    args = [a for a in STEALTH_CHROMIUM_BASE_ARGS if not a.startswith("--window-size")]
    args.append(f"--window-size={window_width},{window_height}")
    args.extend(extra_stealth_chromium_args())
    return args


def _platform_from_user_agent(ua: str) -> str:
    u = ua or ""
    if "Windows NT" in u:
        return "Win32"
    if "Mac OS X" in u or "Macintosh" in u:
        return "MacIntel"
    if "Linux" in u and "Android" not in u:
        return "Linux x86_64"
    return "Win32"


def _webgl_pair_for_platform(platform: str) -> tuple[str, str]:
    if platform == "MacIntel":
        return (
            "Google Inc. (Apple)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
        )
    if platform == "Linux x86_64":
        return (
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060/PCIe/SSE2, OpenGL 4.5.0)",
        )
    return (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    )


def _stable_seed(acc: FBAccount | None, ua: str) -> int:
    raw = f"{getattr(acc, 'id', 0)!s}:{ua}:fb-master-stealth"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:8], 16)


def build_stealth_params(acc: FBAccount | None, *, user_agent: str, viewport_w: int, viewport_h: int) -> dict[str, Any]:
    ua = (user_agent or "").strip() or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"
    plat = _platform_from_user_agent(ua)
    vendor, renderer = _webgl_pair_for_platform(plat)
    hw = random.choice([4, 6, 8, 8, 12, 16]) if acc is None else random.choice([4, 6, 8, 8, 12])
    mem = random.choice([4, 8, 8, 16]) if acc is None else random.choice([4, 8, 8])
    seed = _stable_seed(acc, ua)
    return {
        "seed": seed,
        "hwConcurrency": hw,
        "deviceMemory": mem,
        "maxTouchPoints": 0 if "Mobile" not in ua else 5,
        "platform": plat,
        "webglVendor": vendor,
        "webglRenderer": renderer,
        "viewportW": int(viewport_w),
        "viewportH": int(viewport_h),
        "userAgent": ua[:512],
    }


def build_stealth_init_script(
    acc: FBAccount | None,
    *,
    user_agent: str,
    viewport_w: int,
    viewport_h: int,
) -> str:
    if not stealth_engine_enabled():
        return _MINIMAL_INIT

    if not _FINGERPRINT_TEMPLATE_PATH.is_file():
        return _MINIMAL_INIT

    params = build_stealth_params(acc, user_agent=user_agent, viewport_w=viewport_w, viewport_h=viewport_h)
    blob = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    # Вставляем объект JSON до IIFE в шаблоне (отдельная строка для отладки в DevTools)
    prefix = f"window.__FB_STEALTH_PARAMS__ = {blob};\n"
    body = _FINGERPRINT_TEMPLATE_PATH.read_text(encoding="utf-8")
    return prefix + body
