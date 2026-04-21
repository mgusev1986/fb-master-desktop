#!/usr/bin/env bash
# С Mac: залить проект на VPS и запустить bootstrap на сервере (одна команда).
#
#   chmod +x deploy/from-mac.sh
#   ./deploy/from-mac.sh ВАШ_IP_VPS socmaster.pro
#   ./deploy/from-mac.sh ВАШ_IP_VPS socmaster.pro ваш@gmail.com
#
#   INSTALL_PLAYWRIGHT=1 ./deploy/from-mac.sh IP домен email
#
# Требуется: ssh root@IP без пароля (SSH-ключ).

set -euo pipefail

IP="${1:?Укажите IP: $0 IP домен [email]}"
DOMAIN="${2:?Укажите домен: $0 IP домен [email]}"
EMAIL="${3:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ">>> rsync → root@${IP}:/opt/fb-master/"
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
  "$ROOT/" "root@${IP}:/opt/fb-master/"

echo ">>> SSH: bootstrap на сервере (.env на сервере не перезаписываем — исключён из rsync; первый раз создастся из .env.example)"
DQ_DOMAIN=$(printf '%q' "$DOMAIN")
DQ_EMAIL=$(printf '%q' "$EMAIL")
IW="${INSTALL_PLAYWRIGHT:-0}"
ssh "root@${IP}" "mkdir -p /opt/fb-master && cd /opt/fb-master && chmod +x deploy/bootstrap-server.sh && INSTALL_PLAYWRIGHT=${IW} ./deploy/bootstrap-server.sh ${DQ_DOMAIN} ${DQ_EMAIL}"

echo ">>> Готово. Если .env ещё не заполняли на сервере: ssh root@${IP} 'nano /opt/fb-master/.env'"
