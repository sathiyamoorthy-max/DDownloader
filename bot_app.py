import hashlib
import ipaddress
import json
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
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from flask import Flask, jsonify, request
import requests
import telebot
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
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
PROGRESS_UPDATE_SECONDS = max(
    1, int(os.getenv("PROGRESS_UPDATE_SECONDS", "2"))
)

# Optional authenticated access for NON-DECRYPTING tests.
# Secrets are only sent to explicitly allowlisted domains.
AUTH_DOMAINS = {
    item.strip().lower()
    for item in os.getenv("AUTH_DOMAINS", "").split(",")
    if item.strip()
}
AUTH_COOKIE = os.getenv("AUTH_COOKIE", "").strip()
AUTHORIZATION_HEADER = os.getenv("AUTHORIZATION_HEADER", "").strip()
AUTH_REFERER = os.getenv("AUTH_REFERER", "").strip()

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


def auth_headers_for_url(url: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    if not host or not AUTH_DOMAINS:
        return {}

    allowed = any(
        host == domain or host.endswith("." + domain)
        for domain in AUTH_DOMAINS
    )
    if not allowed:
        return {}

    headers = {}
    if AUTH_COOKIE:
        headers["Cookie"] = AUTH_COOKIE
    if AUTHORIZATION_HEADER:
        headers["Authorization"] = AUTHORIZATION_HEADER
    if AUTH_REFERER:
        headers["Referer"] = AUTH_REFERER
    return headers


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


class PublicMediaHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.candidates = []
        self.title = None
        self._in_title = False
        self._title_parts = []

    def _add(self, value):
        if not value:
            return
        value = value.strip()
        if not value:
            return
        absolute = urljoin(self.base_url, value)
        if absolute.startswith(("http://", "https://")):
            self.candidates.append(absolute)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        lower_tag = tag.lower()

        if lower_tag == "title":
            self._in_title = True

        if lower_tag in {"audio", "video", "source"}:
            self._add(attrs.get("src"))

        if lower_tag == "meta":
            key = (
                attrs.get("property")
                or attrs.get("name")
                or ""
            ).lower()
            if key in {
                "og:audio",
                "og:audio:url",
                "og:audio:secure_url",
                "og:video",
                "og:video:url",
                "og:video:secure_url",
                "twitter:player:stream",
            }:
                self._add(attrs.get("content"))

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False
            if self._title_parts:
                self.title = clean_title(
                    " ".join(self._title_parts),
                    "media",
                )

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)


def _collect_jsonld_media(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "contentUrl",
                "embedUrl",
                "uploadUrl",
            } and isinstance(item, str):
                found.append(item)
            else:
                _collect_jsonld_media(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_jsonld_media(item, found)


def extract_public_media_candidates(page_url: str) -> dict:
    """
    Inspect only the publicly returned webpage HTML for openly exposed media
    URLs. This does not log in, use cookies, call private APIs, obtain keys,
    or decrypt protected streams.
    """
    validate_public_http_url(page_url)

    response = requests.get(
        page_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type and "xhtml" not in content_type:
        return {
            "title": clean_title(
                Path(urlparse(response.url).path).stem,
                "media",
            ),
            "candidates": [response.url],
        }

    html = response.text
    parser = PublicMediaHTMLParser(response.url)
    parser.feed(html)

    jsonld_urls = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            _collect_jsonld_media(payload, jsonld_urls)
        except Exception:
            continue

    candidates = []
    seen = set()

    for raw_url in parser.candidates + jsonld_urls:
        candidate = urljoin(response.url, raw_url)
        if candidate in seen:
            continue
        seen.add(candidate)

        try:
            validate_public_http_url(candidate)
        except Exception:
            continue

        lower_path = urlparse(candidate).path.lower()
        if (
            re.search(r"\.(?:mp3|m4a|aac|ogg|opus|wav|mp4|mkv|webm|mov)(?:$|[?#])", candidate, re.I)
            or ".m3u8" in lower_path
            or ".mpd" in lower_path
            or ".ism" in lower_path
        ):
            candidates.append(candidate)

    return {
        "title": parser.title or clean_title(
            Path(urlparse(response.url).path).stem,
            "media",
        ),
        "candidates": candidates[:20],
    }


def download_public_candidate(
    candidate: str,
    job_dir: Path,
    progress_hook=None,
) -> dict:
    kind = manifest_kind(candidate)
    if kind:
        result = ffmpeg_manifest_fallback(candidate, job_dir)
        return result

    return ytdlp_download(
        candidate,
        job_dir,
        progress_hook=progress_hook,
    )


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

    if "unsupported url" in lower:
        return (
            "The normal extractor does not support this page. "
            "Public webpage media fallbacks were also attempted. "
            "If no openly exposed media URL exists, the source may require "
            "login, authorization, or protection handling."
        )

    if "http error 403" in lower or "forbidden" in lower:
        return (
            "The media server refused access (HTTP 403). "
            "The URL may be expired, private, or require authorization."
        )

    if "http error 404" in lower or "not found" in lower:
        return "The media URL was not found or has expired (HTTP 404)."

    # Do not flood Telegram with huge yt-dlp/ffmpeg logs.
    if len(text) > 1200:
        text = text[-1200:]

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


def human_bytes(value) -> str:
    if value is None:
        return "?"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"

    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1

    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.1f} {units[index]}"


def human_speed(value) -> str:
    if not value:
        return "?"
    return f"{human_bytes(value)}/s"


def clean_title(value, fallback="media") -> str:
    title = str(value or fallback).strip().replace("\n", " ")
    title = re.sub(r"\s+", " ", title)
    return title[:180] or fallback


def edit_status(chat_id: int, message_id: int, text: str) -> None:
    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception:
        # Telegram returns an error when the text is unchanged; ignore it.
        pass


def make_download_progress_hook(
    user_id: int,
    chat_id: int,
    status_message_id: int,
):
    last = {"time": 0.0, "percent": -1.0}

    def hook(data):
        status = data.get("status")

        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            speed = data.get("speed")
            eta = data.get("eta")

            percent = None
            if total:
                percent = max(0.0, min(100.0, downloaded * 100.0 / total))

            parts = []
            if percent is not None:
                parts.append(f"{percent:.1f}%")
            parts.append(
                f"{human_bytes(downloaded)} / {human_bytes(total)}"
                if total else human_bytes(downloaded)
            )
            if speed:
                parts.append(human_speed(speed))
            if eta is not None:
                parts.append(f"ETA {int(eta)}s")

            detail = " • ".join(parts)
            set_job(user_id, "downloading", detail)

            now = time.time()
            should_update = (
                now - last["time"] >= PROGRESS_UPDATE_SECONDS
                or (percent is not None and percent >= 99.5)
            )

            if should_update:
                last["time"] = now
                if percent is not None:
                    last["percent"] = percent
                edit_status(
                    chat_id,
                    status_message_id,
                    "⬇️ Downloading\n" + detail,
                )

        elif status == "finished":
            total = data.get("total_bytes") or data.get("downloaded_bytes")
            detail = f"Downloaded {human_bytes(total)}. Processing…"
            set_job(user_id, "processing", detail)
            edit_status(
                chat_id,
                status_message_id,
                "✅ Download complete\nProcessing media…",
            )

    return hook


def send_thumbnail_preview(
    chat_id: int,
    thumbnail_url: str | None,
    title: str,
    size_bytes: int,
) -> None:
    if not thumbnail_url:
        return

    try:
        parsed = urlparse(thumbnail_url)
        if parsed.scheme not in {"http", "https"}:
            return
        bot.send_photo(
            chat_id=chat_id,
            photo=thumbnail_url,
            caption=f"🎵 {clean_title(title)}\n📦 {human_bytes(size_bytes)}",
            timeout=30,
        )
    except Exception:
        logger.info("Thumbnail preview unavailable", exc_info=True)


def upload_document_with_progress(
    chat_id: int,
    path: Path,
    title: str,
    user_id: int,
    status_message_id: int,
) -> None:
    size = path.stat().st_size
    filename = path.name
    mime = "application/octet-stream"
    if path.suffix.lower() == ".mp4":
        mime = "video/mp4"
    elif path.suffix.lower() in {".mp3", ".m4a", ".aac", ".ogg", ".opus"}:
        mime = "audio/mpeg"

    caption = (
        f"✅ Done\n"
        f"🎵 {clean_title(title, path.stem)}\n"
        f"📦 {human_bytes(size)}"
    )

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    last = {"time": 0.0}

    with path.open("rb") as media:
        encoder = MultipartEncoder(
            fields={
                "chat_id": str(chat_id),
                "caption": caption[:1024],
                "document": (filename, media, mime),
            }
        )

        def on_upload(monitor):
            now = time.time()
            if monitor.len:
                percent = max(
                    0.0,
                    min(100.0, monitor.bytes_read * 100.0 / monitor.len),
                )
            else:
                percent = 0.0

            detail = (
                f"{percent:.1f}% • "
                f"{human_bytes(min(monitor.bytes_read, monitor.len))} / "
                f"{human_bytes(monitor.len)}"
            )
            set_job(user_id, "uploading", detail)

            if (
                now - last["time"] >= PROGRESS_UPDATE_SECONDS
                or percent >= 99.5
            ):
                last["time"] = now
                edit_status(
                    chat_id,
                    status_message_id,
                    "⬆️ Uploading to Telegram\n" + detail,
                )

        monitor = MultipartEncoderMonitor(encoder, on_upload)

        response = requests.post(
            api_url,
            data=monitor,
            headers={"Content-Type": monitor.content_type},
            timeout=(30, 600),
        )

    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(
            payload.get("description") or "Telegram upload failed."
        )


def format_selector() -> str:
    # Prefer MP4-friendly streams where available, then gracefully fall back.
    h = MAX_VIDEO_HEIGHT
    return (
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={h}]+ba/"
        f"b[height<={h}]/b"
    )


def ytdlp_download(
    url: str,
    job_dir: Path,
    progress_hook=None,
) -> dict:
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

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    result = newest_media_file(job_dir)
    if not result:
        raise RuntimeError(
            "Download completed but no media file was created."
        )

    if isinstance(info, dict) and info.get("entries"):
        entries = [entry for entry in info.get("entries") or [] if entry]
        if entries:
            info = entries[0]

    info = info if isinstance(info, dict) else {}

    return {
        "path": result,
        "title": clean_title(info.get("title"), result.stem),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
    }

def ffmpeg_manifest_fallback(url: str, job_dir: Path) -> dict:
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

    return {
        "path": output,
        "title": clean_title(Path(urlparse(url).path).stem, "stream"),
        "thumbnail": None,
        "duration": None,
    }


def download_media(
    url: str,
    job_dir: Path,
    progress_hook=None,
) -> dict:
    validate_public_http_url(url)

    first_error = None

    # 1) Normal yt-dlp extractor/generic downloader.
    try:
        return ytdlp_download(
            url,
            job_dir,
            progress_hook=progress_hook,
        )
    except Exception as exc:
        first_error = exc
        logger.warning(
            "Primary downloader failed; trying public fallbacks: %s",
            exc,
        )

    # 2) Direct manifest fallback through ffmpeg.
    if manifest_kind(url):
        try:
            return ffmpeg_manifest_fallback(url, job_dir)
        except Exception as exc:
            first_error = exc

    # 3) Public webpage metadata/HTML fallback. Only media URLs already
    #    exposed in the public page response are considered.
    try:
        page = extract_public_media_candidates(url)
        candidates = page.get("candidates") or []

        for index, candidate in enumerate(candidates, start=1):
            logger.info(
                "Trying public media candidate %s/%s: %s",
                index,
                len(candidates),
                candidate,
            )
            try:
                result = download_public_candidate(
                    candidate,
                    job_dir,
                    progress_hook=progress_hook,
                )
                if not result.get("title") or result.get("title") == "media":
                    result["title"] = page.get("title") or result.get("title")
                return result
            except Exception as candidate_error:
                logger.warning(
                    "Public candidate failed: %s",
                    candidate_error,
                )
                continue

        if not candidates:
            raise RuntimeError(
                "No public direct media URL was exposed by this webpage."
            )
    except Exception as fallback_error:
        logger.warning(
            "Public webpage fallback failed: %s",
            fallback_error,
        )

    raise RuntimeError(
        "The page could not be downloaded as public media. "
        "It may be unsupported, private, authenticated, expired, or protected. "
        f"Original downloader error: {safe_error(first_error)}"
    )


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
        "/status - live download/upload progress\n"
        "/whoami - show your Telegram user ID\n"
        "/help - show this help\n\n"
        f"Max video height: {MAX_VIDEO_HEIGHT}p\n"
        f"Configured Telegram upload limit: {MAX_UPLOAD_MB} MB\n\n"
        "Public/authorized media only. The bot also checks public webpage "
        "metadata for openly exposed direct media URLs. "
        "Login/private/DRM streams and decryption are not supported."
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
            progress_hook = make_download_progress_hook(
                user_id,
                message.chat.id,
                status_msg.message_id,
            )
            media_info = download_media(
                url,
                job_dir,
                progress_hook=progress_hook,
            )
            result = media_info["path"]

        size = result.stat().st_size
        size_mb = size / (1024 * 1024)
        title = media_info.get("title") or result.stem
        thumbnail = media_info.get("thumbnail")

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

        set_job(
            user_id,
            "uploading",
            f"0.0% • {human_bytes(size)}",
        )

        send_thumbnail_preview(
            message.chat.id,
            thumbnail,
            title,
            size,
        )

        edit_status(
            message.chat.id,
            status_msg.message_id,
            f"⬆️ Uploading to Telegram\n0.0% • {human_bytes(size)}",
        )

        bot.send_chat_action(message.chat.id, "upload_document")

        upload_document_with_progress(
            chat_id=message.chat.id,
            path=result,
            title=title,
            user_id=user_id,
            status_message_id=status_msg.message_id,
        )

        set_job(
            user_id,
            "done",
            f"{clean_title(title)} • {human_bytes(size)}",
        )

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
        progress_update_seconds=PROGRESS_UPDATE_SECONDS,
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
