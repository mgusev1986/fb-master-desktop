"""Минимальный smoke-тест Playwright — проверяет что Chromium вообще
запускается. Если этот скрипт упадёт — проблема в окружении, не в парсере.

Запуск:
    "/Users/maksimgusev/Desktop/Automatization/DEVELOPMENTS/FB - Master/.venv/bin/python" tools/playwright_smoke.py
"""
from __future__ import annotations

import os
import sys
from playwright.sync_api import sync_playwright


def main():
    print(">>> Тест 1/3: launch (без user_data_dir)", flush=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto("https://example.com", wait_until="domcontentloaded", timeout=15_000)
            title = page.title()
            print(f"     OK title='{title}'", flush=True)
            browser.close()
    except Exception as e:
        print(f"!!! Тест 1 ПРОВАЛЕН: {e}", flush=True)
        return 1

    print(">>> Тест 2/3: launch_persistent_context с пустым профилем", flush=True)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir="/tmp/playwright_smoke_profile",
                headless=False,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://example.com", wait_until="domcontentloaded", timeout=15_000)
            title = page.title()
            print(f"     OK title='{title}'", flush=True)
            ctx.close()
    except Exception as e:
        print(f"!!! Тест 2 ПРОВАЛЕН: {e}", flush=True)
        return 2

    print(">>> Тест 3/3: версия Playwright + arch Chromium", flush=True)
    import playwright
    print(f"     playwright module: {playwright.__file__}", flush=True)
    chromium_path = os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-1148/chrome-mac/Chromium.app/Contents/MacOS/Chromium"
    )
    print(f"     Chromium binary: {chromium_path}", flush=True)
    if os.path.exists(chromium_path):
        import subprocess
        r = subprocess.run(["file", chromium_path], capture_output=True, text=True)
        print(f"     {r.stdout.strip()}", flush=True)
    print(">>> Все тесты прошли", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
