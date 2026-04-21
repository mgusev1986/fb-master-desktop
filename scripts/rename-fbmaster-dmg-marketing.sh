#!/usr/bin/env bash
# После electron-builder: SOCMASTER-2.14.0-mac-arm64.dmg → SOCMASTER-2.14-mac-arm64.dmg
# (если patch == 0). Исторически имя скрипта осталось rename-fbmaster-* — работает с
# артефактами SOCMASTER-*. Старый FbMaster-*.dmg (если остался после прошлых сборок)
# переименуется тем же правилом — обратная совместимость.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/desktop/fb-master-desktop/dist"
VER_FULL="$(node -p "require('$ROOT/desktop/fb-master-desktop/package.json').version")"
MARKETING="$(node -e "const v=process.argv[1]; const m=v.match(/^(\d+)\.(\d+)\.0$/); console.log(m?m[1]+'.'+m[2]:v)" "$VER_FULL")"

renamed=0
for prefix in SOCMASTER FbMaster; do
  SRC="$DIST/${prefix}-${VER_FULL}-mac-arm64.dmg"
  DST="$DIST/${prefix}-${MARKETING}-mac-arm64.dmg"
  if [[ ! -f "$SRC" ]]; then
    continue
  fi
  if [[ "$SRC" == "$DST" ]]; then
    echo ">>> DMG уже с маркетинговой версией: $DST"
    renamed=1
    continue
  fi
  rm -f "$DST"
  mv "$SRC" "$DST"
  echo ">>> DMG переименован: $(basename "$DST") (из semver $VER_FULL)"
  renamed=1
done
if [[ "$renamed" == "0" ]]; then
  echo "Нет SOCMASTER/FbMaster DMG в $DIST — пропуск переименования" >&2
fi
