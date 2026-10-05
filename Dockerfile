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
COPY pocketfm_catalog.py /app/pocketfm_catalog.py
COPY kuku_catalog.py /app/kuku_catalog.py
COPY pocketfm_api.py /app/pocketfm_api.py
COPY runtime_checks.py /app/runtime_checks.py
COPY batch_state.py /app/batch_state.py
COPY story_library.py /app/story_library.py
COPY provider_cookies.py /app/provider_cookies.py
COPY encrypted_media.py /app/encrypted_media.py
COPY miniapp_bridge.py /app/miniapp_bridge.py
COPY web /app/web
COPY register_webhook.py /app/register_webhook.py
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh \
    && mkdir -p /app/downloads \
    && mkdir -p /tmp/telegram-bot-api \
    && mkdir -p /tmp/telegram-bot-api-temp

EXPOSE 10000

ENTRYPOINT ["/app/start.sh"]
