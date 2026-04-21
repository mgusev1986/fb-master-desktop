#!/usr/bin/env bash
# Полный архив проекта в backups/ у корня репозитория (рядом с app.py, data и т.д.).
#
# Включается весь проект: backend, desktop, static, data, .env, профили браузера, .venv, dist и т.д.
# Исключаются только: любые __pycache__, *.pyc, каталог .cache в корне проекта.
#
# Меньший архив без переносимого Python/Chromium и без готового .app:
#   BACKUP_SLIM=1 ./scripts/full-project-backup.sh
#
# Заливка на VPS (путь на сервере замените на свой):
#   scp backups/FB-Parser-Friends-*.tar.gz user@host:/opt/fb-master/backups/

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PARENT="$(cd "$ROOT/.." && pwd)"
NAME="$(basename "$ROOT")"
mkdir -p "$ROOT/backups"
TS="$(date +%Y%m%d-%H%M%S)"
LABEL="full"
if [[ "${BACKUP_SLIM:-0}" == "1" ]]; then
  LABEL="slim"
fi
OUT="$ROOT/backups/FB-Parser-Friends-${LABEL}-${TS}.tar.gz"
# Пишем во временный файл: иначе tar видит растущий .tar.gz внутри дерева и ругается «Can't add archive to itself».
TMP_ARCH="/tmp/FB-Parser-Friends-${LABEL}-${TS}-$$.tar.gz"
trap 'rm -f "$TMP_ARCH"' EXIT

EXCLUDES=(
  --exclude='__pycache__'
  --exclude='*.pyc'
  --exclude="${NAME}/.cache"
)

if [[ "${BACKUP_SLIM:-0}" == "1" ]]; then
  EXCLUDES+=(
    --exclude="${NAME}/.venv"
    --exclude="${NAME}/desktop/fb-master-desktop/dist"
    --exclude="${NAME}/desktop/fb-master-desktop/node_modules"
    --exclude="${NAME}/desktop/fb-master-desktop/resources-fb-master-backend/python-lib"
    --exclude="${NAME}/desktop/fb-master-desktop/resources-fb-master-backend/playwright-browsers"
  )
fi

echo ">>> Архив: $OUT (сборка во временный файл, затем перенос)"
echo ">>> Источник: $PARENT/$NAME"
cd "$PARENT"
tar -czf "$TMP_ARCH" "${EXCLUDES[@]}" "$NAME"
mkdir -p "$ROOT/backups"
mv -f "$TMP_ARCH" "$OUT"
trap - EXIT
ls -lh "$OUT"
echo ">>> Готово."
