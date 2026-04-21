#!/bin/bash
cd "$(dirname "$0")" || exit 1

PORT=8765
PIDS=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)

if [[ -z "$PIDS" ]]; then
  echo "Сервер не запущен: на порту $PORT никто не слушает."
else
  echo "Останавливаю процесс(ы) на порту $PORT: $PIDS"
  kill $PIDS 2>/dev/null
  sleep 1
  PIDS=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
  if [[ -n "$PIDS" ]]; then
    echo "Принудительное завершение…"
    kill -9 $PIDS 2>/dev/null
  fi
  echo "Сервер остановлен."
fi

echo ""
read -r -p "Нажмите Enter, чтобы закрыть окно… "
