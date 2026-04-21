#!/usr/bin/env bash
# Каркас: не скачивает Chromium. Скопируйте на машину со сборкой и дополните под свой CI.
set -euo pipefail
echo "1. Установите depot_tools и добавьте в PATH"
echo "2. mkdir chromium-src && cd chromium-src && fetch --nohooks chromium"
echo "3. gclient runhooks"
echo "4. git apply ../stealth_engine/chromium_fork/patches/*.patch  # ваши патчи"
echo "5. gn gen out/Release --args='is_component_build=false is_official_build=true'"
echo "6. autoninja -C out/Release chrome"
echo "7. Запуск: out/Release/Chromium.app/Contents/MacOS/Chromium --remote-debugging-port=9222 --user-data-dir=..."
exit 0
