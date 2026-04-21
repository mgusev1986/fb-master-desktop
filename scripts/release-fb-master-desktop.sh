#!/usr/bin/env bash
# Полный цикл: версия в desktop → prepare bundle → DMG → bundle.zip → (опц.) код на VPS → publish + проверки.
#
# Требования: macOS (arm64 сборка), Node/npm, ssh без пароля на VPS, переменные для notarize (если включены).
#
# Примеры:
#   chmod +x scripts/release-fb-master-desktop.sh
#   ./scripts/release-fb-master-desktop.sh 2.14 root@46.62.230.106 socmaster.pro \
#     "2.14: Улучшения функционала и стабильности работы в FB Master."
#
#   С синхронизацией backend/templates/static на сервер (rsync как в deploy/from-mac.sh, pip, без apt/bootstrap):
#   ./scripts/release-fb-master-desktop.sh --deploy-backend 2.14 root@46.62.230.106 socmaster.pro "2.14: …"
#
# Флаги (можно смешивать с позиционными в конце):
#   --version 2.14
#   --ssh root@46.62.230.106
#   --domain socmaster.pro
#   --notes "текст для FB_DESKTOP_RELEASE_NOTES"
#   --deploy-backend   — залить код в /opt/fb-master и pip install -r requirements.txt
#   --skip-desktop-build — только bump + bundle + (backend) + publish (если артефакты уже собраны)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP="$ROOT/desktop/fb-master-desktop"
PUBLISH="$ROOT/deploy/publish-desktop-release.sh"

VERSION=""
SSH_TARGET=""
DOMAIN=""
NOTES="${FB_DESKTOP_RELEASE_NOTES:-}"
DEPLOY_BACKEND=0
SKIP_DESKTOP_BUILD=0

usage() {
  sed -n '1,35p' "$0" | tail -n +2
}

positional=()
while [[ $# -gt 0 ]]; do
  case "${1:-}" in
    --version|-v)
      VERSION="${2:?}"
      shift 2
      ;;
    --ssh|-s)
      SSH_TARGET="${2:?}"
      shift 2
      ;;
    --domain|-d)
      DOMAIN="${2:?}"
      shift 2
      ;;
    --notes|-n)
      NOTES="${2:?}"
      shift 2
      ;;
    --deploy-backend)
      DEPLOY_BACKEND=1
      shift
      ;;
    --skip-desktop-build)
      SKIP_DESKTOP_BUILD=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Неизвестный флаг: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      positional+=("$1")
      shift
      ;;
  esac
done
positional+=("$@")

if [[ ${#positional[@]} -ge 1 && -z "$VERSION" ]]; then VERSION="${positional[0]}"; fi
if [[ ${#positional[@]} -ge 2 && -z "$SSH_TARGET" ]]; then SSH_TARGET="${positional[1]}"; fi
if [[ ${#positional[@]} -ge 3 && -z "$DOMAIN" ]]; then DOMAIN="${positional[2]}"; fi
if [[ ${#positional[@]} -ge 4 && -z "$NOTES" ]]; then NOTES="${positional[3]}"; fi

if [[ -z "$VERSION" || -z "$SSH_TARGET" || -z "$DOMAIN" ]]; then
  echo "Нужны: ВЕРСИЯ SSH_ЦЕЛЬ ДОМЕН [заметки_релиза]" >&2
  echo "Пример: $0 2.14 root@46.62.230.106 socmaster.pro \"2.14: краткий текст\"" >&2
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Сборка dist:mac:arm64 рассчитана на macOS." >&2
  exit 1
fi

if [[ -z "$NOTES" ]]; then
  echo "Предупреждение: нет текста релиза (задайте 4-м аргументом, --notes или FB_DESKTOP_RELEASE_NOTES)." >&2
  NOTES="${VERSION}: обновление FB Master"
fi

DOM="${DOMAIN#https://}"
DOM="${DOM#http://}"
DOM="${DOM%/}"
BASE_URL="https://${DOM}"

# Маркетинг (2.14) в именах артефактов и .env; npm/electron-builder — полный semver (2.14.0).
MARKETING_VERSION="$VERSION"
PACKAGE_SEMVER="$VERSION"
if [[ "$VERSION" =~ ^[0-9]+\.[0-9]+$ ]]; then
  PACKAGE_SEMVER="${VERSION}.0"
fi

echo ">>> Версия релиза: $MARKETING_VERSION (semver в package.json: $PACKAGE_SEMVER)"
if [[ ! -f "$DESKTOP/package.json" ]]; then
  echo "Нет $DESKTOP/package.json" >&2
  exit 1
fi
(
  cd "$DESKTOP"
  cur="$(node -p "require('./package.json').version" 2>/dev/null || echo "")"
  if [[ "$cur" == "$PACKAGE_SEMVER" ]]; then
    echo ">>> package.json уже $PACKAGE_SEMVER — пропуск npm version"
  else
    npm version "$PACKAGE_SEMVER" --no-git-tag-version
  fi
)

echo ">>> Синхронизация backend/templates/static → resources-fb-master-backend"
chmod +x "$ROOT/scripts/prepare-desktop-backend-bundle.sh"
"$ROOT/scripts/prepare-desktop-backend-bundle.sh"

if [[ "$SKIP_DESKTOP_BUILD" -eq 0 ]]; then
  echo ">>> Electron: dist:mac:arm64 (может занять много времени + notarize)"
  (
    cd "$DESKTOP"
    npm run dist:mac:arm64
  )
  chmod +x "$ROOT/scripts/rename-fbmaster-dmg-marketing.sh"
  "$ROOT/scripts/rename-fbmaster-dmg-marketing.sh"
else
  echo ">>> Пропуск npm run dist:mac:arm64 (--skip-desktop-build)"
fi

echo ">>> ZIP bundle + копия DMG в static/releases"
chmod +x "$ROOT/scripts/zip-mac-arm64-bundle.sh"
"$ROOT/scripts/zip-mac-arm64-bundle.sh" "$MARKETING_VERSION"

if [[ "$DEPLOY_BACKEND" -eq 1 ]]; then
  echo ">>> rsync кода на ${SSH_TARGET}:/opt/fb-master/ (как deploy/from-mac.sh, без bootstrap)"
  rsync -avz --progress \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude 'node_modules' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'data/' \
    --exclude '.env' \
    --exclude 'browser_profile' \
    --exclude 'chrome_cdp_user_data' \
    --exclude 'browser_profiles' \
    --exclude 'desktop/fb-master-desktop/dist' \
    --exclude 'desktop/fb-master-desktop/.electron-app-data' \
    --exclude 'desktop/messenger2-frame/.electron-app-data' \
    --exclude 'static/releases/' \
    "$ROOT/" "${SSH_TARGET}:/opt/fb-master/"
  echo ">>> На сервере: pip install и владелец fbmaster"
  ssh "$SSH_TARGET" bash -s <<'REMOTE'
set -euo pipefail
chown -R fbmaster:fbmaster /opt/fb-master
if [[ -x /opt/fb-master/.venv/bin/pip ]]; then
  sudo -u fbmaster -H bash -lc 'cd /opt/fb-master && .venv/bin/pip install -q -r requirements.txt'
else
  echo "ВНИМАНИЕ: нет /opt/fb-master/.venv — один раз выполните deploy/from-mac.sh или bootstrap на сервере." >&2
fi
REMOTE
fi

if [[ ! -x "$PUBLISH" ]]; then
  chmod +x "$PUBLISH"
fi

echo ">>> Публикация релиза (rsync артефактов, .env desktop-update, удаление старых релизов, restart)"
# publish-desktop-release.sh: $1 ssh, $2 version, $3 domain, $4 optional notes
"$PUBLISH" "$SSH_TARGET" "$MARKETING_VERSION" "$DOMAIN" "$NOTES"

REL_DIR="$ROOT/static/releases"
PREFIX="SOCMASTER"
if [[ ! -f "$REL_DIR/${PREFIX}-${MARKETING_VERSION}-mac-arm64.dmg" && -f "$REL_DIR/FbMaster-${MARKETING_VERSION}-mac-arm64.dmg" ]]; then
  PREFIX="FbMaster"
fi
DMG_NAME="${PREFIX}-${MARKETING_VERSION}-mac-arm64.dmg"
ZIP_NAME="${PREFIX}-${MARKETING_VERSION}-mac-arm64-bundle.zip"
LOCAL_DMG="$REL_DIR/$DMG_NAME"
LOCAL_ZIP="$REL_DIR/$ZIP_NAME"
PUB_DMG="${BASE_URL}/static/releases/${DMG_NAME}"
PUB_ZIP="${BASE_URL}/static/releases/${ZIP_NAME}"

echo ">>> Проверка API и ссылок"
UPDATE_URL="${BASE_URL}/api/public/desktop-update"
JSON=""
for _try in 1 2 3 4 5 6 7 8 9 10; do
  if JSON="$(curl -fsS -m 25 "$UPDATE_URL" 2>/dev/null)"; then
    break
  fi
  if [[ "$_try" -eq 10 ]]; then
    echo "Ошибка: не удалось GET $UPDATE_URL (после рестарта nginx/uvicorn иногда 502 — повторите проверку вручную)" >&2
    exit 1
  fi
  echo ">>> Повтор GET desktop-update через 4 с (попытка $((_try + 1))/10)…" >&2
  sleep 4
done

DMG_NAME="$DMG_NAME" ZIP_NAME="$ZIP_NAME" UPDATE_JSON="$JSON" python3 - "$MARKETING_VERSION" <<'PY' || true
import json, os, sys
from urllib.parse import parse_qs, urlparse

ver = sys.argv[1]
j = json.loads(os.environ["UPDATE_JSON"])
if not j.get("enabled"):
    print("ПРЕДУПРЕЖДЕНИЕ: desktop-update отключён в ответе API:", j, file=sys.stderr)
    raise SystemExit(0)
latest = j.get("latest_version")
urls = j.get("urls") or {}
print("latest_version:", latest)
print("urls keys:", sorted(urls.keys()))
if str(latest) != str(ver):
    print("ПРЕДУПРЕЖДЕНИЕ: latest_version !=", ver, file=sys.stderr)
want_dmg = os.environ["DMG_NAME"]
want_zip = os.environ["ZIP_NAME"]


def check_url(label: str, raw: str, want_file: str) -> None:
    if not raw:
        print("ПРЕДУПРЕЖДЕНИЕ: пустой URL", label, file=sys.stderr)
        return
    u = urlparse(raw)
    qs = parse_qs(u.query)
    fname = (qs.get("f") or [None])[0]
    if u.path.rstrip("/").endswith("/download/release") and fname:
        if fname != want_file:
            print("ПРЕДУПРЕЖДЕНИЕ:", label, "f=", fname, "ожидалось", want_file, file=sys.stderr)
    elif want_file not in (u.path or ""):
        print("ПРЕДУПРЕЖДЕНИЕ:", label, "не похож на ожидаемый файл", want_file, "→", raw[:120], file=sys.stderr)


check_url("darwin_arm64", urls.get("darwin_arm64") or "", want_dmg)
check_url("darwin_arm64_bundle", urls.get("darwin_arm64_bundle") or "", want_zip)
PY

for url in "$PUB_DMG" "$PUB_ZIP"; do
  code="$(curl -fsS -o /dev/null -w '%{http_code}' -I "$url" || echo "err")"
  if [[ "$code" != "200" && "$code" != "302" && "$code" != "301" ]]; then
    echo "ПРЕДУПРЕЖДЕНИЕ: HEAD $url → $code" >&2
  fi
done

echo ""
echo "========== Краткий отчёт =========="
echo "Локально:"
echo "  $LOCAL_DMG"
echo "  $LOCAL_ZIP"
echo ""
echo "Публично:"
echo "  $UPDATE_URL"
echo "  $PUB_DMG"
echo "  $PUB_ZIP"
echo "===================================="
