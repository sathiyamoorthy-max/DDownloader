#!/usr/bin/env sh
set -eu

LOCAL_API_PID=""

cleanup() {
  if [ -n "$LOCAL_API_PID" ]; then
    kill "$LOCAL_API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ]; then
  echo "Starting Local Telegram Bot API on 127.0.0.1:8081..."

  telegram-bot-api \
    --local \
    --http-ip-address=127.0.0.1 \
    --http-port=8081 \
    --dir=/tmp/telegram-bot-api \
    --temp-dir=/tmp/telegram-bot-api-temp \
    --verbosity=1 &

  LOCAL_API_PID=$!

  # Give the local API process a moment to initialize. Webhook registration
  # below has its own retry loop as well.
  sleep 4
else
  echo "TELEGRAM_API_ID/HASH not set; using official Telegram Bot API."
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
