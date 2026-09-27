FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

COPY bot_app.py /app/bot_app.py
COPY register_webhook.py /app/register_webhook.py
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh \
    && mkdir -p /app/downloads

EXPOSE 10000

CMD ["/app/start.sh"]
