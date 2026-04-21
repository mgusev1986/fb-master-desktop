#!/usr/bin/env bash
# Собирает архив для клиентов: только 1. Инструкция.html и 2. SOCMASTER-ВЕРСИЯ-mac-arm64.dmg
# Результат положить в static/releases и залить на сервер; в .env указать:
#   FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE=https://socmaster.pro/static/releases/SOCMASTER-X.Y.Z-mac-arm64-bundle.zip
#
#   chmod +x scripts/zip-mac-arm64-bundle.sh
#   ./scripts/zip-mac-arm64-bundle.sh 1.0.5
#   Канал разработчика (каталог static/releases/dev/):
#   ./scripts/zip-mac-arm64-bundle.sh 1.0.5 dev

set -euo pipefail
VER="${1:?укажите версию, например 1.0.5}"
CHANNEL="$(echo "${2:-}" | tr '[:upper:]' '[:lower:]')"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DMG_SOC="$ROOT/desktop/fb-master-desktop/dist/SOCMASTER-${VER}-mac-arm64.dmg"
DMG_FB="$ROOT/desktop/fb-master-desktop/dist/FbMaster-${VER}-mac-arm64.dmg"
if [[ -f "$DMG_SOC" ]]; then
  DMG="$DMG_SOC"
elif [[ -f "$DMG_FB" ]]; then
  DMG="$DMG_FB"
  echo "(info: найден старый FbMaster-*.dmg — пакую его в bundle)" >&2
else
  echo "Нет DMG: ни $DMG_SOC, ни $DMG_FB — сначала npm run dist:mac:arm64" >&2
  exit 1
fi
DOC="$ROOT/static/downloads/fb-master-macos-first-run-ru.html"
REL_SUB="releases"
if [[ "$CHANNEL" == "dev" ]]; then
  REL_SUB="releases/dev"
fi
# Имя bundle выбираем по имени исходного DMG, чтобы внешнее наблюдение было консистентно.
DMG_BASENAME="$(basename "$DMG")"
BUNDLE_BASE="${DMG_BASENAME%.dmg}-bundle.zip"
OUT="$ROOT/static/${REL_SUB}/${BUNDLE_BASE}"

NAME1="1. Инструкция.html"
NAME2="2. ${DMG_BASENAME}"

if [[ ! -f "$DOC" ]]; then
  echo "Нет инструкции: $DOC"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp "$DOC" "$TMP/$NAME1"
cp "$DMG" "$TMP/$NAME2"

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
( cd "$TMP" && zip "$OUT" "$NAME1" "$NAME2" )

# Копия DMG рядом с .zip — для прямой ссылки и авто-URL по FB_DESKTOP_LATEST_VERSION (или FB_DESKTOP_DEV_* в dev/)
cp -f "$DMG" "$(dirname "$OUT")/"
echo ">>> $OUT"
unzip -l "$OUT"
ls -lh "$OUT" "$(dirname "$OUT")/$(basename "$DMG")"
