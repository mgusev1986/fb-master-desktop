#!/bin/bash
cd "$(dirname "$0")"
if ! command -v npm >/dev/null 2>&1; then
  osascript -e 'display dialog "Установите Node.js (https://nodejs.org), затем запустите снова." buttons {"OK"}' 
  exit 1
fi
if [ ! -f accounts.json ]; then
  cp accounts.example.json accounts.json
  echo "Создан accounts.json — откройте файл и подставьте полный путь к папке профиля из раздела «Аккаунты» FB Master."
fi
if [ ! -d node_modules ]; then
  npm install
fi
npm start
