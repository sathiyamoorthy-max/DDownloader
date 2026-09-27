import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request
import telebot
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))

MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "49")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

MAX_VIDEO_HEIGHT = max(144, int(os.getenv("MAX_VIDEO_HEIGHT", "1080")))
MAX_CONCURRENT_DOWNLOADS = max(
    1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
)
RATE_LIMIT_SECONDS = max(
    0, int(os.getenv("RATE_LIMIT_SECONDS", "5"))
)

_raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {
    int(part.strip())
    for part in _raw_allowed.split(",")
    if part.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is missing. Add it in Render Environment."
    )

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = BASE_DIR / "downloads"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

WEBHOOK_SECRET = hashlib.sha256(
    ("ddownloader-render:" + BOT_TOKEN).encode("utf-8")
).hexdigest()

WEBHOOK_PATH = f"/telegram/{WEBHOOK_SECRET}"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("DDownloaderRender")

bot = telebot.TeleBot(
    BOT_TOKEN,
    threaded=True,
    num_threads=4,
    parse_mode=None,
)

app = Flask(__name__)

# One active download per Telegram user.
_user_locks_guard = threading.Lock()
_user_locks = {}

# Global concurrency cap; important on small Render instances.
_download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

# Webhook requests should return quickly.
_update_pool = ThreadPoolExecutor(max_workers=8)

# Very small in-memory status store. Render disk/process is ephemeral.
_jobs_guard = threading.Lock()
_jobs = {}

# Per-user basic rate limit.
_rate_guard = threading.Lock()
_last_request_at = {}

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


# ============================================================
# Utility helpers
# ============================================================

def allowed_user(message) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return bool(
        message.from_user
        and message.from_user.id in ALLOWED_USER_IDS
    )


def user_lock(user_id: int):
    with _user_locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


def rate_limited(user_id: int) -> bool:
    if RATE_LIMIT_SECONDS <= 0:
        return False

    now = time.time()
    with _rate_guard:
        last = _last_request_at.get(user_id, 0.0)
        if now - last < RATE_LIMIT_SECONDS:
            return True
        _last_request_at[user_id] = now
        return False


def extract_url(text: str):
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(").,]}>\"'")


def validate_public_http_url(url: str) -> None:
    """
    Basic SSRF guard. The bot is meant for public internet media URLs,
    not localhost/private-network resources.
    """
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are supported.")

    host = parsed.hostname
    if not host:
        raise ValueError("Invalid URL hostname.")

    lowered = host.lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local/private URLs are not allowed.")

    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise ValueError("Hostname could not be resolved.")

    for item in addresses:
        ip_text = item[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            raise ValueError("Local/private network URLs are not allowed.")


def manifest_kind(url: str):
    lower = url.lower()
    if re.search(r"\.m3u8(?:$|[?#])", lower):
        return "m3u8"
    if re.search(r"\.mpd(?:$|[?#])", lower):
        return "mpd"
    if re.search(r"\.ism(?:$|[?#])", lower):
        return "ism"
    return None


def newest_media_file(folder: Path):
    allowed = {
        ".mp4", ".mkv", ".webm", ".m4a", ".mp3",
        ".mov", ".ts", ".aac", ".ogg", ".opus",
    }
    candidates = [
        p for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in allowed
        and not p.name.endswith(".part")
    ]

    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


def safe_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__

    lower = text.lower()
    if "drm" in lower or "encrypted" in lower:
        return (
            "This source appears to require DRM/encryption handling. "
            "This Render build only supports non-DRM/public or otherwise "
            "authorized media."
        )

    # Do not flood Telegram with huge yt-dlp/ffmpeg logs.
    if len(text) > 1600:
        text = text[-1600:]

    return text


def set_job(user_id: int, state: str, detail: str = ""):
    with _jobs_guard:
        _jobs[user_id] = {
            "state": state,
            "detail": detail,
            "updated": time.time(),
        }


def get_job(user_id: int):
    with _jobs_guard:
        return dict(_jobs.get(user_id, {}))


def clear_job(user_id: int):
    with _jobs_guard:
        _jobs.pop(user_id, None)


def format_selector() -> str:
    # Prefer MP4-friendly streams where available, then gracefully fall back.
    h = MAX_VIDEO_HEIGHT
    return (
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={h}]+ba/"
        f"b[height<={h}]/b"
    )


def ytdlp_download(url: str, job_dir: Path) -> Path:
    opts = {
        "format": format_selector(),
        "outtmpl": str(job_dir / "%(title).100B_[%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "continuedl": True,
        "nopart": False,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 2,
        "socket_timeout": 30,
        "quiet": False,
        "no_warnings": False,
        "overwrites": True,
    }

    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    result = newest_media_file(job_dir)
    if not result:
        raise RuntimeError(
            "yt-dlp finished but no media file was created."
        )
    return result


def ffmpeg_manifest_fallback(url: str, job_dir: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not installed in the container.")

    output = job_dir / "stream.mp4"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", url,
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c", "copy",
        str(output),
    ]

    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60 * 60 * 4,
    )

    if completed.returncode != 0:
        error = (completed.stderr or "ffmpeg failed").strip()
        raise RuntimeError(error[-1600:])

    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError("ffmpeg did not create a usable output file.")

    return output


def download_media(url: str, job_dir: Path) -> Path:
    validate_public_http_url(url)

    try:
        return ytdlp_download(url, job_dir)
    except DownloadError as first_error:
        # Direct public HLS/DASH/ISM can sometimes work better through ffmpeg.
        if manifest_kind(url):
            logger.warning(
                "yt-dlp failed for manifest, trying ffmpeg fallback: %s",
                first_error,
            )
            return ffmpeg_manifest_fallback(url, job_dir)
        raise


def cleanup_job(job_dir: Path):
    shutil.rmtree(job_dir, ignore_errors=True)


# ============================================================
# Telegram commands
# ============================================================

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if not allowed_user(message):
        bot.reply_to(message, "This bot is private.")
        return

    text = (
        "DDownloader Render Bot ✅\n\n"
        "Send one media URL.\n\n"
        "Commands:\n"
        "/status - current download status\n"
        "/whoami - show your Telegram user ID\n"
        "/help - show this help\n\n"
        f"Max video height: {MAX_VIDEO_HEIGHT}p\n"
        f"Configured Telegram upload limit: {MAX_UPLOAD_MB} MB\n\n"
        "Public/authorized media only. DRM keys/decryption are not supported."
    )
    bot.reply_to(message, text)


@bot.message_handler(commands=["whoami"])
def cmd_whoami(message):
    if not message.from_user:
        return
    bot.reply_to(
        message,
        f"Your Telegram user ID: {message.from_user.id}"
    )


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if not allowed_user(message):
        bot.reply_to(message, "This bot is private.")
        return

    if not message.from_user:
        return

    info = get_job(message.from_user.id)
    if not info:
        bot.reply_to(message, "No active download.")
        return

    state = info.get("state", "unknown")
    detail = info.get("detail", "")
    reply = f"Status: {state}"
    if detail:
        reply += f"\n{detail}"
    bot.reply_to(message, reply)


# ============================================================
# URL handler
# ============================================================

@bot.message_handler(
    func=lambda message: bool(message.text)
    and not message.text.startswith("/")
)
def handle_url(message):
    if not allowed_user(message):
        bot.reply_to(message, "This bot is private.")
        return

    if not message.from_user:
        return

    user_id = message.from_user.id

    if rate_limited(user_id):
        bot.reply_to(
            message,
            f"Please wait {RATE_LIMIT_SECONDS} seconds before another request."
        )
        return

    url = extract_url(message.text or "")
    if not url:
        bot.reply_to(message, "Send a valid http/https media URL.")
        return

    try:
        validate_public_http_url(url)
    except Exception as exc:
        bot.reply_to(message, safe_error(exc))
        return

    lock = user_lock(user_id)
    if not lock.acquire(blocking=False):
        bot.reply_to(
            message,
            "You already have a download running. Use /status."
        )
        return

    job_id = f"{user_id}_{message.message_id}_{uuid.uuid4().hex[:8]}"
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status_msg = None

    try:
        set_job(user_id, "waiting", "Waiting for a download slot…")
        status_msg = bot.reply_to(message, "Waiting for download slot…")

        with _download_slots:
            set_job(user_id, "downloading", "Downloading media…")
            try:
                bot.edit_message_text(
                    "Downloading…",
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                )
            except Exception:
                pass

            bot.send_chat_action(message.chat.id, "typing")
            result = download_media(url, job_dir)

        size = result.stat().st_size
        size_mb = size / (1024 * 1024)

        if size > MAX_UPLOAD_BYTES:
            set_job(
                user_id,
                "too_large",
                f"{size_mb:.1f} MB > {MAX_UPLOAD_MB} MB",
            )
            bot.edit_message_text(
                f"Downloaded successfully: {size_mb:.1f} MB\n"
                f"But it is above this bot's configured upload limit "
                f"({MAX_UPLOAD_MB} MB), so it was not uploaded.",
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
            )
            return

        set_job(user_id, "uploading", f"{size_mb:.1f} MB")

        try:
            bot.edit_message_text(
                f"Uploading… ({size_mb:.1f} MB)",
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
            )
        except Exception:
            pass

        bot.send_chat_action(message.chat.id, "upload_document")

        with result.open("rb") as media:
            bot.send_document(
                chat_id=message.chat.id,
                document=media,
                visible_file_name=result.name,
                caption="Done ✅",
                timeout=600,
            )

        set_job(user_id, "done", result.name)

        try:
            bot.delete_message(
                message.chat.id,
                status_msg.message_id,
            )
        except Exception:
            pass

    except subprocess.TimeoutExpired:
        set_job(user_id, "failed", "Download timed out.")
        if status_msg:
            bot.edit_message_text(
                "Download timed out.",
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
            )

    except Exception as exc:
        logger.exception("Download failed for user %s", user_id)
        message_text = safe_error(exc)
        set_job(user_id, "failed", message_text)

        if status_msg:
            try:
                bot.edit_message_text(
                    "Download failed.\n\n" + message_text,
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                )
            except Exception:
                bot.reply_to(
                    message,
                    "Download failed.\n\n" + message_text,
                )
        else:
            bot.reply_to(message, "Download failed.\n\n" + message_text)

    finally:
        cleanup_job(job_dir)
        # Keep the status briefly for /status, then remove it.
        def expire_status():
            time.sleep(60)
            clear_job(user_id)

        threading.Thread(
            target=expire_status,
            daemon=True,
        ).start()

        lock.release()


# ============================================================
# Render HTTP endpoints + Telegram webhook
# ============================================================

@app.get("/")
def index():
    return jsonify(
        status="ok",
        service="DDownloader Render Telegram Bot",
        webhook=True,
        ffmpeg=bool(shutil.which("ffmpeg")),
        max_upload_mb=MAX_UPLOAD_MB,
        max_video_height=MAX_VIDEO_HEIGHT,
    )


@app.get("/health")
def health():
    return jsonify(status="healthy"), 200


@app.post(WEBHOOK_PATH)
def telegram_webhook():
    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if supplied_secret != WEBHOOK_SECRET:
        return "forbidden", 403

    payload = request.get_json(silent=True)
    if not payload:
        return "bad request", 400

    try:
        update = telebot.types.Update.de_json(payload)

        # Return 200 to Telegram immediately. Processing continues in a worker.
        _update_pool.submit(bot.process_new_updates, [update])
        return "ok", 200
    except Exception:
        logger.exception("Webhook update parse/submit failed")
        return "ok", 200
