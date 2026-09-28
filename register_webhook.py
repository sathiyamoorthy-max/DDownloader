import hashlib
import os
import sys
import time

import requests


def redact_secret(value: object) -> str:
    text = str(value)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        text = text.replace(token, "<BOT_TOKEN>")
    return text


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_API_HOST = os.getenv("TELEGRAM_API_HOST", "").strip()
TELEGRAM_API_PORT = os.getenv("TELEGRAM_API_PORT", "").strip()
_CONFIGURED_API_BASE = os.getenv(
    "TELEGRAM_API_BASE_URL",
    "",
).strip().rstrip("/")

if _CONFIGURED_API_BASE:
    TELEGRAM_API_BASE_URL = _CONFIGURED_API_BASE
elif TELEGRAM_API_HOST:
    TELEGRAM_API_BASE_URL = "http://" + TELEGRAM_API_HOST
    if TELEGRAM_API_PORT:
        TELEGRAM_API_BASE_URL += ":" + TELEGRAM_API_PORT
elif TELEGRAM_API_ID and TELEGRAM_API_HASH:
    TELEGRAM_API_BASE_URL = "http://127.0.0.1:8081"
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

# When a Local Bot API endpoint is configured, perform a best-effort
# cloud -> local handoff. Any Telegram rate limit here is non-fatal.
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
        print("Cloud Bot API logout check failed:", redact_secret(exc))

payload = {
    "url": webhook_url,
    "secret_token": secret,
    "drop_pending_updates": True,
    "allowed_updates": ["message"],
}

last_error = None
max_attempts = 20

for attempt in range(1, max_attempts + 1):
    retry_after = None
    try:
        response = requests.post(api, json=payload, timeout=30)

        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code == 429:
            parameters = data.get("parameters") or {}
            try:
                retry_after = int(parameters.get("retry_after") or 0)
            except (TypeError, ValueError):
                retry_after = None

            description = data.get("description") or "Too Many Requests"
            raise RuntimeError(
                f"429 Too Many Requests: {description}"
            )

        response.raise_for_status()

        if not data.get("ok"):
            raise RuntimeError(str(data))

        print("Telegram webhook configured.")
        print("Render URL:", RENDER_EXTERNAL_URL)
        print("Telegram API base:", TELEGRAM_API_BASE_URL)
        sys.exit(0)

    except Exception as exc:
        last_error = redact_secret(exc)
        print(
            f"Webhook setup attempt {attempt}/{max_attempts} failed: "
            f"{redact_secret(exc)}"
        )

        if retry_after:
            wait_seconds = min(max(retry_after + 1, 5), 120)
        else:
            wait_seconds = min(5 + attempt * 2, 30)

        print(f"Retrying webhook setup in {wait_seconds}s...")
        time.sleep(wait_seconds)

print(
    "Webhook setup is still pending after retries. "
    "The web service will remain online; redeploy is not required."
)
sys.exit(0)
