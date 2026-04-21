#!/bin/bash
# Один клик: back-office (uvicorn) + окно FB Master Desktop (Electron).
# URL окна = APP_BASE_URL из .env (по умолчанию http://localhost:8000), чтобы совпадал с Google redirect URI.
cd "$(dirname "$0")" || exit 1

# Не поднимать второй Electron из lifespan сервера — мы запускаем его сами.
export FB_MASTER_AUTOSTART_DESKTOP=0
export FB_MASTER_AUTOSTART_MESSENGER2_FRAME=0

if [[ ! -d ".venv" ]]; then
  echo "❌ Нет папки .venv"
  echo "   Создайте: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  read -rp "Enter — выход… "
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  osascript -e 'display dialog "Установите Node.js (LTS) с https://nodejs.org и запустите снова." buttons {"OK"}' 2>/dev/null || echo "Установите Node.js (npm)."
  read -rp "Enter — выход… "
  exit 1
fi

# shellcheck source=/dev/null
source ".venv/bin/activate"

PORT=$(python -c "from backend.config import BO_PORT; print(BO_PORT)")
APP_HOME=$(python -c "from backend.config import APP_BASE_URL; print(APP_BASE_URL.rstrip('/'))")
LOGIN_URL="${APP_HOME}/auth/login"
export FB_MASTER_APP_URL="${APP_HOME}/"

PIDS=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
if [[ -n "$PIDS" ]]; then
  echo "Порт $PORT занят — останавливаю: $PIDS"
  kill $PIDS 2>/dev/null
  sleep 1
  PIDS=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
  [[ -n "$PIDS" ]] && kill -9 $PIDS 2>/dev/null
  sleep 0.5
fi

DESKTOP_DIR="desktop/fb-master-desktop"
if [[ ! -f "$DESKTOP_DIR/main.js" ]]; then
  echo "❌ Нет $DESKTOP_DIR/main.js"
  read -rp "Enter — выход… "
  exit 1
fi

stop_server() {
  local p
  p=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
  [[ -n "$p" ]] && kill $p 2>/dev/null
  sleep 0.4
  p=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
  [[ -n "$p" ]] && kill -9 $p 2>/dev/null
}

trap stop_server EXIT INT TERM HUP

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  FB Master: сервер + Desktop"
echo "  ${APP_HOME}  (APP_BASE_URL из .env)"
echo "  Google redirect URI должен быть: ${APP_HOME}/auth/callback"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

python main.py &

echo "Ожидаю готовность сервера (${LOGIN_URL})…"
READY=0
for _ in $(seq 1 120); do
  if curl -sf "$LOGIN_URL" -o /dev/null; then
    READY=1
    break
  fi
  sleep 0.25
done

if [[ "$READY" -ne 1 ]]; then
  echo "❌ Сервер не ответил: ${LOGIN_URL}"
  exit 1
fi

pushd "$DESKTOP_DIR" >/dev/null || exit 1
[[ ! -d node_modules ]] && npm install
export MESSENGER2_FRAME_HEALTH_PORT="${MESSENGER2_FRAME_HEALTH_PORT:-37821}"
echo ""
echo "Запуск окна FB Master Desktop (после закрытия окна сервер остановится)…"
echo ""
npm start
popd >/dev/null || true

echo ""
read -rp "Готово. Enter — закрыть это окно… "
