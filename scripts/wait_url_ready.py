"""Ожидание HTTP-ответа (для bat/command запуска). Использование: wait_url_ready.py <url> [таймаут_сек]"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: wait_url_ready.py URL [max_seconds]", file=sys.stderr)
        return 2
    url = sys.argv[1]
    max_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    deadline = time.monotonic() + max_sec
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return 0
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.25)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
