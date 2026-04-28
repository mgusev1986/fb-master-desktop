#!/usr/bin/env bash
# Вкладывает в resources-fb-master-backend переносимый Python 3.12 (indygreg/python-build-standalone)
# и ставит зависимости через pip --prefix, чтобы на Mac клиента не требовались Homebrew и пути сборщика.
#
#   bash scripts/ensure-macos-embedded-python.sh /path/to/resources-fb-master-backend
#
set -euo pipefail

OUT="${1:?укажите каталог resources-fb-master-backend}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$ROOT/.cache/fbm-embedded-python-macos"
REL="20241219"
VER="3.12.8"

# 3.0.5+: FBM_TARGET_ARCH=x86_64 принудительно собирает x86_64-bundle на arm64-runner
# (нужно когда macos-13 deprecated и кросс-сборка идёт с macos-14 arm64 через Rosetta 2).
# Без переменной — работает по uname -m, как раньше (нативный билд).
ARCH="${FBM_TARGET_ARCH:-$(uname -m)}"
if [[ "$ARCH" == "arm64" ]]; then
  FILE="cpython-${VER}+${REL}-aarch64-apple-darwin-install_only.tar.gz"
elif [[ "$ARCH" == "x86_64" ]]; then
  FILE="cpython-${VER}+${REL}-x86_64-apple-darwin-install_only.tar.gz"
else
  echo "ОШИБКА: неподдерживаемая архитектура Mac: $ARCH" >&2
  exit 1
fi

URL="https://github.com/indygreg/python-build-standalone/releases/download/${REL}/${FILE}"
mkdir -p "$CACHE" "$OUT"

LOCAL_TAR="$CACHE/$FILE"
if [[ ! -f "$LOCAL_TAR" ]]; then
  echo ">>> Скачиваю встроенный Python macOS ($ARCH): $FILE"
  curl -fSL "$URL" -o "$LOCAL_TAR.tmp"
  mv "$LOCAL_TAR.tmp" "$LOCAL_TAR"
fi

echo ">>> Распаковываю Python в $OUT/python"
rm -rf "$OUT/python"
tar -xzf "$LOCAL_TAR" -C "$OUT"

PY="$OUT/python/bin/python3.12"
if [[ ! -x "$PY" ]]; then
  echo "ОШИБКА: после распаковки нет исполняемого $PY" >&2
  exit 1
fi

echo ">>> pip install -r requirements.txt → $OUT/python-lib"
rm -rf "$OUT/python-lib"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -r "$OUT/requirements.txt" --prefix "$OUT/python-lib"

LIB_SITE="$OUT/python-lib/lib/python3.12/site-packages"
RT_SITE="$OUT/python/lib/python3.12/site-packages"
export PYTHONPATH="${LIB_SITE}:${RT_SITE}"

PB="$OUT/playwright-browsers"
mkdir -p "$PB"
echo ">>> Playwright: chromium в $PB"
env PLAYWRIGHT_BROWSERS_PATH="$PB" "$PY" -m playwright install chromium

if ! find "$PB" -type f \( -path '*/MacOS/Chromium' -o -path '*/chrome-linux/chrome' -o -path '*/chrome-win/chrome.exe' \) 2>/dev/null | head -1 | grep -q .; then
  echo "ОШИБКА: после playwright install в $PB нет исполняемого Chromium." >&2
  exit 1
fi

rm -rf "$OUT/venv"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$OUT/.embedded_python"
echo ">>> Встроенный Python готов (venv удалён — не используйте старые инструкции с Homebrew-venv на Mac)."
