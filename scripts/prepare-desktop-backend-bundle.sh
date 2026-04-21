#!/usr/bin/env bash
# Копирует код FB Master в resources-fb-master-backend для упаковки в Electron.
#
# macOS: в каталог кладётся переносимый Python 3.12 (python-build-standalone) + pip install + Chromium;
#       старый venv из Homebrew удаляется — иначе на компьютерах клиентов нет /opt/homebrew/...
# Linux/Windows: создайте venv вручную в resources-fb-master-backend (см. ниже).
#
#   ./scripts/prepare-desktop-backend-bundle.sh
#   cd desktop/fb-master-desktop && npm run dist:mac:arm64
#
# Windows: venv\Scripts\activate; в launch.json — "executable": "venv\\\\Scripts\\\\python.exe"

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/desktop/fb-master-desktop/resources-fb-master-backend"

mkdir -p "$OUT"
rm -rf "$OUT/backend" "$OUT/templates" "$OUT/static"
rm -f "$OUT/main.py" "$OUT/requirements.txt" "$OUT/launch.json"
mkdir -p "$OUT/backend" "$OUT/templates" "$OUT/static"

rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$ROOT/backend/" "$OUT/backend/"
rsync -a --delete "$ROOT/templates/" "$OUT/templates/"
# Установщики DMG/ZIP лежат в static/releases/ для сайта/VPS — внутрь .app они не нужны и раздувают сборку до десятков ГБ.
rsync -a --delete \
  --exclude 'releases/' \
  "$ROOT/static/" "$OUT/static/"
cp "$ROOT/main.py" "$OUT/"
cp "$ROOT/requirements.txt" "$OUT/"

cat > "$OUT/launch.json" << 'JSON'
{
  "executable": "venv/bin/python3",
  "appModule": "main:app",
  "port": 8799,
  "startTimeoutMs": 180000,
  "licenseApiBase": "https://socmaster.pro",
  "env": {
    "FB_MASTER_ACCESS_KEY_REQUIRED": "1",
    "FB_MASTER_WEB_CABINET_EMAIL_LOGIN_DISABLED": "1"
  }
}
JSON

touch "$OUT/.gitkeep"

PB="$OUT/playwright-browsers"
mkdir -p "$PB"

if [[ "$(uname -s)" == "Darwin" ]]; then
  chmod +x "$ROOT/scripts/ensure-macos-embedded-python.sh"
  if ! "$ROOT/scripts/ensure-macos-embedded-python.sh" "$OUT"; then
    echo ">>> ОШИБКА: не удалось собрать встроенный Python для macOS (сеть, место на диске)." >&2
    exit 1
  fi
elif [[ -x "$OUT/venv/bin/python3" ]]; then
  echo ">>> Playwright: скачиваю Chromium в $PB (venv, не macOS)…"
  if ! env PLAYWRIGHT_BROWSERS_PATH="$PB" "$OUT/venv/bin/python3" -m playwright install chromium; then
    echo ">>> ОШИБКА: playwright install chromium не удался — проверьте сеть и pip install -r requirements.txt в venv." >&2
    exit 1
  fi
else
  echo ">>> ОШИБКА: не macOS и нет $OUT/venv/bin/python3 — без venv Chromium в десктоп-сборку не попадёт." >&2
  echo "    cd \"$OUT\" && python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  echo "    ./scripts/prepare-desktop-backend-bundle.sh" >&2
  exit 1
fi

chmod +x "$ROOT/scripts/verify-desktop-chromium.sh"
"$ROOT/scripts/verify-desktop-chromium.sh"

echo ">>> Обновлено: $OUT"
