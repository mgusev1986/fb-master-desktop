#!/bin/bash
cd "$(dirname "$0")" || exit 1

PORT=8765
PIDS=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
if [[ -n "$PIDS" ]]; then
  echo "Останавливаю сервер на порту $PORT…"
  kill $PIDS 2>/dev/null
  sleep 1
  PIDS=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
  [[ -n "$PIDS" ]] && kill -9 $PIDS 2>/dev/null
  sleep 0.5
  echo "Готово."
else
  echo "Сервер уже был остановлен."
fi

echo ""

if [[ ! -d ".venv" ]]; then
  echo "Ошибка: нет папки .venv — сначала настройте окружение (см. Запуск.command)."
  read -r -p "Нажмите Enter, чтобы закрыть окно… "
  exit 1
fi

# shellcheck source=/dev/null
source ".venv/bin/activate"

echo "Запуск сервера… Страница http://127.0.0.1:${PORT} откроется в браузере автоматически."
echo "Остановка: закройте Chromium или нажмите Ctrl+C здесь, либо запустите Остановка.command."
echo ""

python app.py

echo ""
read -r -p "Сервер остановлен. Нажмите Enter, чтобы закрыть окно… "
