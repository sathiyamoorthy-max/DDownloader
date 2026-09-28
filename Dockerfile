FROM aiogram/telegram-bot-api:latest AS botapi

FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

RUN apk add --no-cache \
    ffmpeg \
    ca-certificates \
    libstdc++ \
    openssl \
    curl

COPY --from=botapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

COPY bot_app.py /app/bot_app.py
COPY register_webhook.py /app/register_webhook.py
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh \
    && mkdir -p /app/downloads \
    && mkdir -p /tmp/telegram-bot-api \
    && mkdir -p /tmp/telegram-bot-api-temp

EXPOSE 10000

CMD ["/app/start.sh"]
