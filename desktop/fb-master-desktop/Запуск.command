#!/bin/bash
cd "$(dirname "$0")"
if ! command -v npm >/dev/null 2>&1; then
  osascript -e 'display dialog "Установите Node.js (https://nodejs.org), затем запустите снова." buttons {"OK"}'
  exit 1
fi
if [ ! -d node_modules ]; then
  npm install
fi
export FB_MASTER_APP_URL="${FB_MASTER_APP_URL:-http://localhost:8000/}"
npm start
