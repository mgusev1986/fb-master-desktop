#!/usr/bin/env bash
# Автоматическая подготовка Ubuntu (VPS) под FB Master.
# Запуск на сервере под root ПОСЛЕ того, как проект уже лежит в /opt/fb-master
# (rsync/scp/git clone — см. deploy/YOU_DO_MANUALLY.md).
#
# Использование:
#   chmod +x deploy/bootstrap-server.sh
#   sudo ./deploy/bootstrap-server.sh socmaster.pro
#   sudo ./deploy/bootstrap-server.sh socmaster.pro ваш@email.com
#     → если указан email, попытается certbot (нужны уже работающие A-записи DNS).
#
# Переменные окружения:
#   INSTALL_PLAYWRIGHT=1  — установить Chromium для Playwright (парсер/мессенджер на сервере)
#   SKIP_SSL=1            — не вызывать certbot
#   LETSENCRYPT_EMAIL     — email для Let's Encrypt (если не передан 2-м аргументом)

set -euo pipefail

DOMAIN="${1:-${FB_MASTER_DOMAIN:-}}"
SSL_EMAIL="${2:-${LETSENCRYPT_EMAIL:-}}"

if [[ -z "$DOMAIN" ]]; then
  echo "Укажите домен: sudo $0 socmaster.pro [email@для-certbot]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запустите от root: sudo $0 $DOMAIN"
  exit 1
fi

if [[ ! -f "$APP_DIR/main.py" ]]; then
  echo "Ошибка: не найден $APP_DIR/main.py"
  echo "Скопируйте весь проект FB Master в $APP_DIR и повторите."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get install -y \
  ca-certificates curl git nginx certbot python3-certbot-nginx \
  software-properties-common build-essential

# Python 3.12 + venv
if command -v python3.12 &>/dev/null; then
  apt-get install -y python3.12-venv python3.12-dev 2>/dev/null || apt-get install -y python3.12-venv || true
elif grep -q "22.04" /etc/os-release 2>/dev/null; then
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
  apt-get install -y python3.12 python3.12-venv python3.12-dev
fi

if command -v python3.12 &>/dev/null; then
  PY=python3.12
else
  apt-get install -y python3-venv || true
  PY=python3
fi
echo "Используется интерпретатор: $PY ($($PY --version))"

if ! id fbmaster &>/dev/null; then
  adduser --disabled-password --gecos "" fbmaster
fi

mkdir -p /var/lib/fb-master/data /var/lib/fb-master/browser_profiles "/var/lib/fb-master/Base FB"
chown -R fbmaster:fbmaster /var/lib/fb-master

chown -R fbmaster:fbmaster "$APP_DIR"

sudo -u fbmaster bash -c "cd '$APP_DIR' && $PY -m venv .venv && .venv/bin/pip install -U pip setuptools wheel && .venv/bin/pip install -r requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  if [[ -f "$APP_DIR/.env.example" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chown fbmaster:fbmaster "$APP_DIR/.env"
    echo ""
    echo ">>> Создан $APP_DIR/.env из примера. ОБЯЗАТЕЛЬНО отредактируйте секреты (nano $APP_DIR/.env)"
    echo ""
  fi
fi

if [[ "${INSTALL_PLAYWRIGHT:-0}" == "1" ]]; then
  echo "Установка Playwright Chromium в $APP_DIR/playwright-browsers (не в ~/.cache)…"
  PB="$APP_DIR/playwright-browsers"
  sudo -u fbmaster mkdir -p "$PB"
  sudo -u fbmaster bash -c "cd '$APP_DIR' && export PLAYWRIGHT_BROWSERS_PATH='$PB' && .venv/bin/playwright install-deps chromium || true"
  sudo -u fbmaster bash -c "cd '$APP_DIR' && export PLAYWRIGHT_BROWSERS_PATH='$PB' && .venv/bin/playwright install chromium"
  if sudo -u fbmaster test -f "$APP_DIR/.env" && ! sudo -u fbmaster grep -q '^PLAYWRIGHT_BROWSERS_PATH=' "$APP_DIR/.env" 2>/dev/null; then
    printf '\n# Chromium для Playwright (парсер, вход в FB, рассылка)\nPLAYWRIGHT_BROWSERS_PATH=%s\n' "$PB" | sudo -u fbmaster tee -a "$APP_DIR/.env" >/dev/null
  fi
fi

install -m 0644 "$SCRIPT_DIR/fb-master.service.example" /etc/systemd/system/fb-master.service
systemctl daemon-reload
systemctl enable fb-master

NGINX_SITE="/etc/nginx/sites-available/fb-master"
cat >"$NGINX_SITE" <<NGX
upstream fb_master_app {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};

    client_max_body_size 64m;

    location / {
        proxy_pass http://fb_master_app;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
NGX

ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/fb-master
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# UFW: дополняет фаервол облака, удобен на самом сервере
if command -v ufw &>/dev/null; then
  ufw allow OpenSSH || true
  ufw allow 'Nginx Full' || true
  ufw --force enable || true
fi

systemctl restart fb-master || {
  echo "Сервис fb-master не стартовал — проверьте .env (SECRET_KEY, DATABASE_URL и т.д.): journalctl -u fb-master -n 50 --no-pager"
}

echo ""
echo "=== HTTP готов: http://${DOMAIN} (после DNS) → прокси на приложение ==="
echo ""

if [[ -n "$SSL_EMAIL" && "${SKIP_SSL:-0}" != "1" ]]; then
  echo "Запуск certbot для HTTPS..."
  if certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" \
    --non-interactive --agree-tos -m "$SSL_EMAIL" --redirect; then
    systemctl reload nginx
    echo "HTTPS настроен."
  else
    echo "Certbot не удался (часто DNS ещё не дошёл). Когда A-записи заработают, выполните:"
    echo "  certbot --nginx -d $DOMAIN -d www.$DOMAIN"
  fi
else
  echo "Когда DNS укажет на этот сервер, выпустите сертификат:"
  echo "  certbot --nginx -d $DOMAIN -d www.$DOMAIN"
  [[ -z "$SSL_EMAIL" ]] && echo "  (или повторите этот скрипт с email вторым аргументом)"
fi

echo ""
echo "Полезно: journalctl -u fb-master -f"
echo "Готово."
