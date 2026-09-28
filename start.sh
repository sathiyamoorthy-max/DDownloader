#!/usr/bin/env sh
set -eu

LOCAL_API_PID=""
LOCAL_API_LOG="/tmp/telegram-bot-api-startup.log"

if [ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ]; then
  echo "Starting embedded Local Telegram Bot API on 127.0.0.1:8081..."

  telegram-bot-api \
    --local \
    --http-ip-address=127.0.0.1 \
    --http-port=8081 \
    --dir=/tmp/telegram-bot-api \
    --temp-dir=/tmp/telegram-bot-api-temp \
    --verbosity=0 \
    >"$LOCAL_API_LOG" 2>&1 &

  LOCAL_API_PID=$!

  READY=0
  i=0
  while [ "$i" -lt 30 ]; do
    if ! kill -0 "$LOCAL_API_PID" 2>/dev/null; then
      echo "Local Telegram Bot API exited during startup."
      echo "Startup log:"
      tail -n 40 "$LOCAL_API_LOG" 2>/dev/null || true
      exit 1
    fi

    if curl -sS --max-time 1 "http://127.0.0.1:8081/" >/dev/null 2>&1; then
      READY=1
      break
    fi

    i=$((i + 1))
    sleep 1
  done

  if [ "$READY" -ne 1 ]; then
    echo "Local Telegram Bot API did not become ready on port 8081."
    tail -n 40 "$LOCAL_API_LOG" 2>/dev/null || true
    exit 1
  fi

  export TELEGRAM_API_BASE_URL="http://127.0.0.1:8081"
  echo "Embedded Local Telegram Bot API is ready."
else
  echo "TELEGRAM_API_ID/HASH not set; using official Telegram Bot API."
  export TELEGRAM_API_BASE_URL="https://api.telegram.org"
fi

python /app/register_webhook.py

exec gunicorn \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers 1 \
  --threads 8 \
  --timeout 0 \
  --access-logfile - \
  --error-logfile - \
  bot_app:app
