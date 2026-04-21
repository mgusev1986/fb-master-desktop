#!/usr/bin/env bash
# Перед electron-builder: убедиться, что в resources-fb-master-backend лежит Chromium Playwright.
# Иначе клиенту придётся ставить браузер вручную, а «Войти в Facebook» не найдёт бинарник.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RES="$ROOT/desktop/fb-master-desktop/resources-fb-master-backend"
PB="$RES/playwright-browsers"

if [[ ! -d "$PB" ]]; then
  echo "ОШИБКА: нет каталога $PB" >&2
  echo "Выполните: ./scripts/prepare-desktop-backend-bundle.sh и установите Chromium (см. комментарии в скрипте)." >&2
  exit 1
fi

if find "$PB" -type f \( -path '*/MacOS/Chromium' -o -path '*/chrome-linux/chrome' -o -path '*/chrome-win/chrome.exe' \) 2>/dev/null | head -1 | grep -q .; then
  echo ">>> Chromium для десктоп-сборки: OK ($PB)"
  exit 0
fi

echo "ОШИБКА: в $PB не найден исполняемый Chromium." >&2
echo "Из каталога resources-fb-master-backend с активированным venv выполните:" >&2
echo "  export PLAYWRIGHT_BROWSERS_PATH=\"\$(pwd)/playwright-browsers\"" >&2
echo "  playwright install chromium" >&2
exit 1
