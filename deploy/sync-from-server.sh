#!/usr/bin/env bash
# Синхронизация локальной копии проекта С СЕРВЕРА (VPS — эталон).
# Не тянет: data/, .git, node_modules, venv, профили браузера, dist,
# resources-fb-master-backend (там свой Python/Chromium под ОС — после синка запустите prepare-desktop-backend-bundle.sh).
#
# Скрипт должен лежать и на сервере в /opt/fb-master/deploy/ — иначе rsync --delete сотрёт локальную копию при следующем pull.
#
#   chmod +x deploy/sync-from-server.sh
#   ./deploy/sync-from-server.sh 46.62.230.106
#
set -euo pipefail

IP="${1:?Укажите IP VPS: $0 IP_СЕРВЕРА}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"

echo ">>> Резервная копия локального .env → .env.backup.$TS (если есть)"
if [[ -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env" "$ROOT/.env.backup.$TS"
fi

echo ">>> rsync с root@${IP}:/opt/fb-master/ → $ROOT/"
rsync -avz --delete --progress \
  --exclude '.git' \
  --exclude 'data/' \
  --exclude 'node_modules' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.venv' \
  --exclude 'browser_profile' \
  --exclude 'browser_profiles' \
  --exclude 'chrome_cdp_user_data' \
  --exclude 'desktop/fb-master-desktop/dist' \
  --exclude 'desktop/fb-master-desktop/.electron-app-data' \
  --exclude 'desktop/fb-master-desktop/resources-fb-master-backend' \
  --exclude 'desktop/messenger2-frame/.electron-app-data' \
  --exclude '.DS_Store' \
  "root@${IP}:/opt/fb-master/" "$ROOT/"

echo ">>> Версия десктопа из .env сервера (FB_DESKTOP_LATEST_VERSION) → npm package"
VER=""
if [[ -f "$ROOT/.env" ]]; then
  VER="$(grep -E '^FB_DESKTOP_LATEST_VERSION=' "$ROOT/.env" | head -1 | cut -d= -f2- | tr -d '\r' | tr -d '[:space:]')"
fi
DESK="$ROOT/desktop/fb-master-desktop"
if [[ -n "${VER}" && -f "$DESK/package.json" ]] && command -v npm >/dev/null 2>&1; then
  (cd "$DESK" && npm pkg set "version=${VER}" && npm install --package-lock-only --ignore-scripts 2>/dev/null) || {
    echo "    (npm pkg set не удался — правьте version в package.json вручную: ${VER})" >&2
  }
  echo "    ${VER} — desktop/fb-master-desktop/package.json (+ lock при успехе npm)"
elif [[ -n "${VER}" && -f "$DESK/package.json" ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    sed -i '' "s/\"version\": \"[^\"]*\"/\"version\": \"${VER}\"/" "$DESK/package.json"
  else
    sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"${VER}\"/" "$DESK/package.json"
  fi
  echo "    ${VER} — только package.json (npm не найден)"
else
  echo "    (нет FB_DESKTOP_LATEST_VERSION или package.json — версию npm не трогаем)"
fi

echo ""
echo ">>> Дальше на Mac (локальный встроенный бэкенд Electron):"
echo "    ./scripts/prepare-desktop-backend-bundle.sh"
echo ""
echo ">>> Готово. Локальный .env совпадает с сервером; предыдущий — в .env.backup.$TS"
