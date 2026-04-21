#!/usr/bin/env python3
"""
Запуск Chrome/Chromium с флагами Stealth Shell и remote debugging (CDP).
Пример:
  python stealth_engine/launch_stealth_chromium.py --user-data-dir ./browser_profiles/cdp_test --port 9222

В .env: FB_CDP_ENDPOINT=http://127.0.0.1:9222 — тогда FB Master подключится через Playwright connect_over_cdp.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.fb_stealth_engine import stealth_chromium_launch_args  # noqa: E402


def _find_chrome() -> str | None:
    env = (os.getenv("FB_STEALTH_CHROME_BINARY") or "").strip()
    if env and Path(env).is_file():
        return env
    if sys.platform == "darwin":
        p = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if p.is_file():
            return str(p)
        p = Path("/Applications/Chromium.app/Contents/MacOS/Chromium")
        if p.is_file():
            return str(p)
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        w = shutil.which(name)
        if w:
            return w
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Chromium + Stealth Shell + CDP")
    ap.add_argument("--user-data-dir", required=True, help="Каталог профиля (создастся)")
    ap.add_argument("--port", type=int, default=9222, help="remote-debugging-port")
    ap.add_argument("--url", default="about:blank", help="Стартовый URL")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    binary = _find_chrome()
    if not binary:
        print(
            "Не найден Chrome/Chromium. Укажите путь: FB_STEALTH_CHROME_BINARY=/path/to/chrome",
            file=sys.stderr,
        )
        return 1

    udd = Path(args.user_data_dir).expanduser().resolve()
    udd.mkdir(parents=True, exist_ok=True)

    flags = stealth_chromium_launch_args(window_width=args.width, window_height=args.height)
    argv = [
        binary,
        f"--user-data-dir={udd}",
        f"--remote-debugging-port={args.port}",
        "--no-first-run",
        "--no-default-browser-check",
        *flags,
        args.url,
    ]
    print("Запуск:", binary, "…", flush=True)
    print(f"CDP: http://127.0.0.1:{args.port} → FB_CDP_ENDPOINT или поле CDP у аккаунта", flush=True)
    os.execv(binary, argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
