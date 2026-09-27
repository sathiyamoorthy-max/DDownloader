#!/usr/bin/env sh
set -eu

python /app/register_webhook.py

exec gunicorn \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers 1 \
  --threads 8 \
  --timeout 0 \
  --access-logfile - \
  --error-logfile - \
  bot_app:app
