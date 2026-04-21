#!/usr/bin/env bash
# Публикация превью-сборки в static/releases/dev/ на VPS + только переменные FB_DESKTOP_DEV_* в .env.
# Клиентский канал (FB_DESKTOP_LATEST_VERSION, /static/releases/*.dmg) не трогается.
#
#   chmod +x deploy/publish-desktop-dev-release.sh
#   ./deploy/publish-desktop-dev-release.sh root@46.62.230.106 2.0.7 socmaster.pro "2.0.7-dev: тест перед релизом"
#
# Локально перед этим: npm run dist:mac:arm64 и ./scripts/zip-mac-arm64-bundle.sh ВЕРСИЯ dev

set -euo pipefail

SSH_TARGET="${1:?Укажите SSH, например root@203.0.113.10}"
VER="${2:?Укажите версию, например 2.0.7}"
DOMAIN_RAW="${3:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REL="$ROOT/static/releases/dev"
REMOTE_DIR="/opt/fb-master/static/releases/dev"

# Префикс по умолчанию — SOCMASTER; если локально только FbMaster-* остался, используем его.
PREFIX="SOCMASTER"
if [[ ! -f "$REL/${PREFIX}-${VER}-mac-arm64.dmg" && -f "$REL/FbMaster-${VER}-mac-arm64.dmg" ]]; then
  PREFIX="FbMaster"
  echo "(info: публикую старые FbMaster-* файлы)" >&2
fi
for f in \
  "${PREFIX}-${VER}-mac-arm64.dmg" \
  "${PREFIX}-${VER}-mac-arm64-bundle.zip"
do
  if [[ ! -f "$REL/$f" ]]; then
    echo "Нет файла: $REL/$f" >&2
    echo "Соберите: ./scripts/zip-mac-arm64-bundle.sh $VER dev" >&2
    exit 1
  fi
done

echo ">>> SSH: очищаю старые FbMaster-* и SOCMASTER-* в $REMOTE_DIR"
ssh "$SSH_TARGET" "mkdir -p '$REMOTE_DIR' && rm -f '$REMOTE_DIR'/FbMaster-*.dmg '$REMOTE_DIR'/FbMaster-*.zip '$REMOTE_DIR'/SOCMASTER-*.dmg '$REMOTE_DIR'/SOCMASTER-*.zip 2>/dev/null || true"

echo ">>> rsync → ${SSH_TARGET}:$REMOTE_DIR/  (prefix=$PREFIX)"
rsync -avz --progress \
  "$REL/${PREFIX}-${VER}-mac-arm64.dmg" \
  "$REL/${PREFIX}-${VER}-mac-arm64-bundle.zip" \
  "${SSH_TARGET}:${REMOTE_DIR}/"

echo ">>> SSH: владелец fbmaster"
ssh "$SSH_TARGET" "chown fbmaster:fbmaster '$REMOTE_DIR'/${PREFIX}-${VER}-mac-arm64.dmg '$REMOTE_DIR'/${PREFIX}-${VER}-mac-arm64-bundle.zip 2>/dev/null || true"

if [[ -n "$DOMAIN_RAW" ]]; then
  DOM="${DOMAIN_RAW#https://}"
  DOM="${DOM#http://}"
  DOM="${DOM%/}"
  BASE="https://${DOM}"
  NOTES_RAW="${FB_DESKTOP_DEV_RELEASE_NOTES:-${4:-}}"
  NOTES_B64=""
  if [[ -n "$NOTES_RAW" ]]; then
    NOTES_B64="$(printf '%s' "$NOTES_RAW" | base64 | tr -d '\n')"
    echo ">>> В .env запишу FB_DESKTOP_DEV_RELEASE_NOTES"
  fi
  echo ">>> SSH: правлю /opt/fb-master/.env — FB_DESKTOP_DEV_LATEST_VERSION=$VER (prefix=$PREFIX)"
  ssh "$SSH_TARGET" \
    "env VER=$(printf '%q' "$VER") BASE=$(printf '%q' "$BASE") PREFIX=$(printf '%q' "$PREFIX") NOTES_B64=$(printf '%q' "$NOTES_B64") python3 -" <<'PY'
import base64
import os
import pathlib

ver = os.environ["VER"]
base = os.environ["BASE"].rstrip("/")
prefix = os.environ.get("PREFIX") or "SOCMASTER"
path = pathlib.Path("/opt/fb-master/.env")
updates = {
    "FB_DESKTOP_DEV_LATEST_VERSION": ver,
    "FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64": f"{base}/static/releases/dev/{prefix}-{ver}-mac-arm64.dmg",
    "FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64_BUNDLE": f"{base}/static/releases/dev/{prefix}-{ver}-mac-arm64-bundle.zip",
}
nb = (os.environ.get("NOTES_B64") or "").strip()
if nb:
    try:
        raw = base64.b64decode(nb).decode("utf-8").strip()
        updates["FB_DESKTOP_DEV_RELEASE_NOTES"] = " ".join(raw.split())
    except Exception:
        pass
path.parent.mkdir(parents=True, exist_ok=True)
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
out: list[str] = []
pending = dict(updates)
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in pending:
        out.append(f"{key}={pending[key]}")
        del pending[key]
    else:
        out.append(line)
for key in (
    "FB_DESKTOP_DEV_LATEST_VERSION",
    "FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64",
    "FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64_BUNDLE",
    "FB_DESKTOP_DEV_RELEASE_NOTES",
):
    if key in pending:
        out.append(f"{key}={pending[key]}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
fi

echo ">>> SSH: systemctl restart fb-master (если сервис есть)"
ssh "$SSH_TARGET" "systemctl restart fb-master 2>/dev/null || true"

echo ">>> Готово: dev-канал FbMaster-${VER}-mac-arm64.* в $REMOTE_DIR"
if [[ -z "$DOMAIN_RAW" ]]; then
  echo ">>> Подсказка: передайте третьим аргументом домен — обновится .env (FB_DESKTOP_DEV_*)." >&2
fi
