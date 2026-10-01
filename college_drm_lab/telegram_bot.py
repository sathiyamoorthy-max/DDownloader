from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import telebot
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LAB_BASE_URL = os.getenv("LAB_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
LAB_USER = os.getenv("LAB_USER", "student")

ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

if not ALLOWED_USER_IDS:
    raise RuntimeError(
        "ALLOWED_USER_IDS is required for this cybersecurity demo bot. "
        "Do not run the attack-demo commands as a public bot."
    )

host = (urlparse(LAB_BASE_URL).hostname or "").lower()
if host not in {"127.0.0.1", "localhost", "::1", "lab-server"}:
    raise RuntimeError(
        "LAB_BASE_URL must point to the local lab only "
        "(localhost/127.0.0.1/lab-server)."
    )

bot = telebot.TeleBot(BOT_TOKEN)
http = requests.Session()


def allowed(message) -> bool:
    return bool(message.from_user and message.from_user.id in ALLOWED_USER_IDS)


def guard(message) -> bool:
    if allowed(message):
        return True
    bot.reply_to(message, "⛔ இந்த college lab bot private.")
    return False


def _post(path: str, *, json=None, headers=None):
    response = http.post(
        f"{LAB_BASE_URL}{path}",
        json=json,
        headers=headers,
        timeout=15,
    )
    return response


def _get(path: str, *, params=None, headers=None):
    response = http.get(
        f"{LAB_BASE_URL}{path}",
        params=params,
        headers=headers,
        timeout=15,
    )
    return response


@bot.message_handler(commands=["start", "help"])
def start(message):
    if not guard(message):
        return

    bot.reply_to(
        message,
        (
            "🧪 College DRM / Entitlement Cyber Lab\n\n"
            "இந்த bot real PocketFM/OTT service-ஐ target செய்யாது. "
            "Local intentionally vulnerable lab மட்டும்.\n\n"
            "/attackdemo - fake client payment flag + exposed lab key demo\n"
            "/securecheck - patched entitlement check fake unlock-ஐ reject செய்வது\n"
            "/legitdemo - lab credit மூலம் legitimate unlock + playback\n"
            "/health - local lab status"
        ),
    )


@bot.message_handler(commands=["health"])
def health(message):
    if not guard(message):
        return

    try:
        response = _get("/health")
        response.raise_for_status()
        bot.reply_to(message, f"✅ Lab online\n{response.json()}")
    except Exception as exc:
        bot.reply_to(message, f"❌ Lab unavailable: {exc}")


@bot.message_handler(commands=["attackdemo"])
def attack_demo(message):
    """
    Demonstrates two intentionally-created local vulnerabilities:
    1. trusting a client-controlled payment flag;
    2. exposing raw AES key material to a playback token.
    """
    if not guard(message):
        return

    status = bot.reply_to(
        message,
        "🧪 Local vulnerable flow தொடங்குகிறது...",
    )

    try:
        unlock = _post(
            "/vuln/unlock",
            json={"episode_id": 2, "client_paid": True},
        )
        unlock.raise_for_status()
        payload = unlock.json()
        token = payload["media_token"]

        encrypted = _get(
            "/vuln/media/2",
            params={"token": token},
        )
        encrypted.raise_for_status()

        key_response = _get(
            "/vuln/key/2",
            params={"token": token},
        )
        key_response.raise_for_status()
        key_payload = key_response.json()

        key = base64.b64decode(key_payload["key_b64"])
        nonce = base64.b64decode(key_payload["nonce_b64"])
        aad = base64.b64decode(key_payload["aad_b64"])

        clear = AESGCM(key).decrypt(
            nonce,
            encrypted.content,
            aad,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "lab_paid_episode.mp3"
            output.write_bytes(clear)

            bot.edit_message_text(
                (
                    "⚠️ Vulnerable local lab exploited.\n"
                    "Client-paid flag trusted → entitlement bypassed.\n"
                    "Playback token exposed raw lab key → sample decrypted.\n\n"
                    "இது intentionally vulnerable local sample மட்டுமே."
                ),
                message.chat.id,
                status.message_id,
            )

            with output.open("rb") as audio:
                bot.send_audio(
                    message.chat.id,
                    audio,
                    title="College Lab Paid Episode",
                    performer="Synthetic Test Audio",
                    caption="🧪 Local lab attack demonstration",
                )

    except Exception as exc:
        bot.edit_message_text(
            f"❌ Attack demo failed: {str(exc)[:1000]}",
            message.chat.id,
            status.message_id,
        )


@bot.message_handler(commands=["securecheck"])
def secure_check(message):
    if not guard(message):
        return

    try:
        response = _post(
            "/secure/unlock",
            json={"episode_id": 2, "client_paid": True},
            headers={"X-Lab-User": "attacker"},
        )

        if response.status_code == 402:
            bot.reply_to(
                message,
                (
                    "✅ Patched API blocked the same fake-unlock idea.\n"
                    "Server-side entitlement check returned payment_required.\n"
                    "Client-supplied paid=true is ignored."
                ),
            )
            return

        bot.reply_to(
            message,
            f"ℹ️ Secure endpoint returned {response.status_code}: {response.text[:500]}",
        )

    except Exception as exc:
        bot.reply_to(message, f"❌ Secure check failed: {exc}")


@bot.message_handler(commands=["legitdemo"])
def legitimate_demo(message):
    if not guard(message):
        return

    headers = {"X-Lab-User": LAB_USER}

    try:
        payment = _post(
            "/secure/pay",
            json={"episode_id": 2},
            headers=headers,
        )
        payment.raise_for_status()

        unlock = _post(
            "/secure/unlock",
            json={"episode_id": 2, "client_paid": True},
            headers=headers,
        )
        unlock.raise_for_status()

        playback = _get(
            "/secure/play/2",
            headers=headers,
        )
        playback.raise_for_status()

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "authorized_lab_episode.mp3"
            output.write_bytes(playback.content)

            with output.open("rb") as audio:
                bot.send_audio(
                    message.chat.id,
                    audio,
                    title="Authorized College Lab Episode",
                    performer="Synthetic Test Audio",
                    caption=(
                        "✅ Secure flow: server-side payment → entitlement → playback. "
                        "Raw key was never returned to the bot."
                    ),
                )

    except Exception as exc:
        bot.reply_to(message, f"❌ Legit demo failed: {str(exc)[:1000]}")


if __name__ == "__main__":
    print(f"College lab Telegram bot started; target={LAB_BASE_URL}")
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        skip_pending=True,
    )
