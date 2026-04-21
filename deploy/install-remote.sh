#!/usr/bin/env bash
# Установка FB Master на чистый Ubuntu (VPS) одной командой с вашего Mac/Linux.
# На сервере: git clone → deploy/bootstrap-server.sh (nginx, systemd, venv, опционально Playwright).
#
# Перед запуском:
#   1) Сервер Ubuntu 22.04/24.04, SSH root@IP без пароля (ключ).
#   2) DNS: A-запись домена на IP сервера (для Let's Encrypt).
#   3) Задайте URL репозитория (публичный или с токеном для приватного).
#
# Примеры:
#   export GIT_URL=https://github.com/ВАШ_АККАУНТ/ВАШ_РЕПО.git
#   chmod +x deploy/install-remote.sh
#   ./deploy/install-remote.sh root@203.0.113.10 socmaster.pro admin@email.com
#
# Приватный GitHub (fine-grained token с read на репозиторий):
#   export GIT_URL=https://ВАШ_ТОКЕН@github.com/ORG/REPO.git
#
# Другая ветка:
#   export BRANCH=main
#
# Без установки Chromium Playwright на сервере (только API, без парсера в облаке):
#   INSTALL_PLAYWRIGHT=0 ./deploy/install-remote.sh root@IP домен
#
set -euo pipefail

TARGET="${1:?Укажите SSH-цель, например: root@203.0.113.10}"
DOMAIN="${2:?Укажите домен, например: socmaster.pro}"
EMAIL="${3:-}"
GIT_URL="${GIT_URL:-}"
BRANCH="${BRANCH:-main}"
INSTALL_PW="${INSTALL_PLAYWRIGHT:-1}"

if [[ -z "$GIT_URL" ]]; then
  echo "Задайте переменную GIT_URL — откуда клонировать код."
  echo ""
  echo "  export GIT_URL=https://github.com/USER/REPO.git"
  echo "  ./deploy/install-remote.sh root@ВАШ_IP $DOMAIN ваш@email.com"
  exit 1
fi

echo ">>> SSH: $TARGET"
echo ">>> Домен: $DOMAIN"
echo ">>> Ветка: $BRANCH"
echo ">>> Репозиторий: ${GIT_URL//:*@/:****@}" # маскируем токен если user:token@
echo ""

ssh -o StrictHostKeyChecking=accept-new "$TARGET" \
  "DOMAIN=$(printf '%q' "$DOMAIN") \
   GIT_URL=$(printf '%q' "$GIT_URL") \
   BRANCH=$(printf '%q' "$BRANCH") \
   EMAIL=$(printf '%q' "$EMAIL") \
   INSTALL_PW=$(printf '%q' "$INSTALL_PW") \
   bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo ">>> apt: git, базовые пакеты"
apt-get update -y
apt-get install -y git ca-certificates curl

if [[ -d /opt/fb-master/.git ]]; then
  echo ">>> Обновление /opt/fb-master (git fetch + reset)"
  cd /opt/fb-master
  git remote set-url origin "$GIT_URL" 2>/dev/null || git remote add origin "$GIT_URL"
  git fetch origin "$BRANCH" --depth 1
  git checkout -B "$BRANCH" "origin/$BRANCH"
else
  echo ">>> Клонирование в /opt/fb-master"
  rm -rf /opt/fb-master
  git clone --depth 1 -b "$BRANCH" "$GIT_URL" /opt/fb-master
fi

if [[ ! -f /opt/fb-master/main.py ]]; then
  echo "Ошибка: в репозитории нет main.py в корне."
  exit 1
fi

chmod +x /opt/fb-master/deploy/bootstrap-server.sh

export INSTALL_PLAYWRIGHT="$INSTALL_PW"
echo ">>> bootstrap-server.sh (INSTALL_PLAYWRIGHT=$INSTALL_PLAYWRIGHT)"
if [[ -n "$EMAIL" ]]; then
  /opt/fb-master/deploy/bootstrap-server.sh "$DOMAIN" "$EMAIL"
else
  /opt/fb-master/deploy/bootstrap-server.sh "$DOMAIN"
fi

echo ""
echo ">>> Готово. Дальше на сервере: nano /opt/fb-master/.env — секреты, БД, ключи."
echo ">>> Сервис: systemctl status fb-master"
REMOTE

echo ""
echo ">>> Локально всё выполнено."
