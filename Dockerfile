FROM aiogram/telegram-bot-api:latest

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000 \
    PATH="/opt/venv/bin:$PATH"

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-virtualenv \
    ffmpeg \
    ca-certificates \
    curl

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --upgrade pip \
    && /opt/venv/bin/python -m pip install -r /app/requirements.txt

COPY bot_app.py /app/bot_app.py
COPY register_webhook.py /app/register_webhook.py
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh \
    && mkdir -p /app/downloads \
    && mkdir -p /tmp/telegram-bot-api \
    && mkdir -p /tmp/telegram-bot-api-temp

EXPOSE 10000

ENTRYPOINT ["/app/start.sh"]
