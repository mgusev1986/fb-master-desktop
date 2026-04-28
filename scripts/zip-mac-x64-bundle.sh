#!/usr/bin/env bash
# Собирает архив для клиентов на Mac Intel (x64): только 1. Инструкция.html и
# 2. SOCMASTER-ВЕРСИЯ-mac-x64.dmg.
# Используется когда DMG получен из GitHub Actions runner macos-13 (Intel) — у нас
# на M-маке нет возможности собрать x86_64 Python и Chromium локально.
#
# Куда положить bundle.zip:
#   static/releases/      — основной канал (привязка через FB_DESKTOP_LATEST_VERSION).
#   static/releases/dev/  — канал разработчика (привязка через FB_DESKTOP_DEV_LATEST_VERSION).
#
# В .env на VPS должна быть переменная:
#   FB_DESKTOP_LATEST_VERSION=3.0.5  (если 3.0.5)
# тогда auto-detect в backend/config.py:_buy_page_auto_dmg_x64_url() подхватит
# SOCMASTER-3.0.5-mac-x64.dmg и сделает его доступным как `darwin_x64`.
#
# Применение:
#   chmod +x scripts/zip-mac-x64-bundle.sh
#   ./scripts/zip-mac-x64-bundle.sh 3.0.5            # → static/releases/
#   ./scripts/zip-mac-x64-bundle.sh 3.0.5 dev        # → static/releases/dev/
#
# DMG скрипт ищет в:
#   1) desktop/fb-master-desktop/dist/SOCMASTER-VER-mac-x64.dmg (если собрано локально)
#   2) static/releases/SOCMASTER-VER-mac-x64.dmg                (если уже скачан из GHA)
#   3) static/releases/dev/SOCMASTER-VER-mac-x64.dmg            (если в dev-канале)

set -euo pipefail
VER="${1:?укажите версию, например 3.0.5}"
CHANNEL="$(echo "${2:-}" | tr '[:upper:]' '[:lower:]')"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Поиск DMG в трёх местах.
CANDIDATES=(
  "$ROOT/desktop/fb-master-desktop/dist/SOCMASTER-${VER}-mac-x64.dmg"
  "$ROOT/desktop/fb-master-desktop/dist/FbMaster-${VER}-mac-x64.dmg"
  "$ROOT/static/releases/SOCMASTER-${VER}-mac-x64.dmg"
  "$ROOT/static/releases/dev/SOCMASTER-${VER}-mac-x64.dmg"
)
DMG=""
for path in "${CANDIDATES[@]}"; do
  if [[ -f "$path" ]]; then
    DMG="$path"
    break
  fi
done
if [[ -z "$DMG" ]]; then
  echo "Нет DMG для x64. Проверял пути:" >&2
  for p in "${CANDIDATES[@]}"; do echo "  $p" >&2; done
  echo "" >&2
  echo "Соберите DMG локально (npm run dist:mac:x64 на Intel Mac) или скачайте" >&2
  echo "из GitHub Release (https://github.com/mgusev1986/fb-master-desktop/releases/tag/v${VER})" >&2
  echo "и положите в desktop/fb-master-desktop/dist/ или static/releases/" >&2
  exit 1
fi

DOC="$ROOT/static/downloads/fb-master-macos-first-run-intel-ru.html"
REL_SUB="releases"
if [[ "$CHANNEL" == "dev" ]]; then
  REL_SUB="releases/dev"
fi
DMG_BASENAME="$(basename "$DMG")"
BUNDLE_BASE="${DMG_BASENAME%.dmg}-bundle.zip"
OUT="$ROOT/static/${REL_SUB}/${BUNDLE_BASE}"

NAME1="1. Инструкция.html"
NAME2="2. ${DMG_BASENAME}"

if [[ ! -f "$DOC" ]]; then
  echo "Нет инструкции: $DOC" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp "$DOC" "$TMP/$NAME1"
cp "$DMG" "$TMP/$NAME2"

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
( cd "$TMP" && zip "$OUT" "$NAME1" "$NAME2" )

# Копия DMG рядом с .zip — для прямой ссылки и auto-URL по
# FB_DESKTOP_LATEST_VERSION / FB_DESKTOP_DEV_LATEST_VERSION (config._buy_page_auto_dmg_x64_url).
cp -f "$DMG" "$(dirname "$OUT")/"
echo ">>> $OUT"
unzip -l "$OUT"
ls -lh "$OUT" "$(dirname "$OUT")/$(basename "$DMG")"
