import hashlib
import os
import sys
import time

import requests


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
TELEGRAM_API_HOST = os.getenv(
    "TELEGRAM_API_HOST",
    "",
).strip()
TELEGRAM_API_PORT = os.getenv(
    "TELEGRAM_API_PORT",
    "",
).strip()
_CONFIGURED_API_BASE = os.getenv(
    "TELEGRAM_API_BASE_URL",
    "",
).strip().rstrip("/")

if TELEGRAM_API_HOST:
    TELEGRAM_API_BASE_URL = "http://" + TELEGRAM_API_HOST
    if TELEGRAM_API_PORT:
        TELEGRAM_API_BASE_URL += ":" + TELEGRAM_API_PORT
elif _CONFIGURED_API_BASE:
    TELEGRAM_API_BASE_URL = _CONFIGURED_API_BASE
else:
    TELEGRAM_API_BASE_URL = "https://api.telegram.org"

if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is missing.")

if not RENDER_EXTERNAL_URL:
    raise SystemExit(
        "RENDER_EXTERNAL_URL is missing. This must run as a Render Web Service."
    )

secret = hashlib.sha256(
    ("ddownloader-render:" + BOT_TOKEN).encode("utf-8")
).hexdigest()

webhook_url = f"{RENDER_EXTERNAL_URL}/telegram/{secret}"
api = (
    f"{TELEGRAM_API_BASE_URL}/bot{BOT_TOKEN}/setWebhook"
)

# When a Local Bot API endpoint is configured, automatically perform the
# cloud -> local handoff. Repeating logOut is harmless for this startup flow:
# failures are logged and webhook registration still retries against local.
if TELEGRAM_API_BASE_URL != "https://api.telegram.org":
    cloud_logout = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/logOut"
    )
    try:
        response = requests.post(cloud_logout, timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {}
        if data.get("ok"):
            print("Cloud Bot API logout completed automatically.")
        else:
            print(
                "Cloud Bot API logout returned:",
                data.get("description") or response.status_code,
            )
    except Exception as exc:
        print("Cloud Bot API logout check failed:", exc)

payload = {
    "url": webhook_url,
    "secret_token": secret,
    "drop_pending_updates": True,
    "allowed_updates": ["message"],
}

last_error = None

for attempt in range(1, 13):
    try:
        response = requests.post(api, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            raise RuntimeError(str(data))

        print("Telegram webhook configured.")
        print("Render URL:", RENDER_EXTERNAL_URL)
        print("Telegram API base:", TELEGRAM_API_BASE_URL)
        sys.exit(0)

    except Exception as exc:
        last_error = exc
        print(f"Webhook setup attempt {attempt}/12 failed: {exc}")
        time.sleep(min(3 + attempt, 15))

raise SystemExit(f"Unable to configure Telegram webhook: {last_error}")
