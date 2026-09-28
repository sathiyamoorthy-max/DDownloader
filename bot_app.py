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
from contextlib import ExitStack
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from flask import Flask, jsonify, request
import requests
import telebot
from telebot import apihelper
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_API_HOST = os.getenv("TELEGRAM_API_HOST", "").strip()
TELEGRAM_API_PORT = os.getenv("TELEGRAM_API_PORT", "").strip()
_CONFIGURED_API_BASE = os.getenv(
    "TELEGRAM_API_BASE_URL",
    "",
).strip().rstrip("/")

# Simplest Render setup: when API ID + hash are present, start and use the
# Local Bot API server inside this same container on 127.0.0.1:8081.
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

OFFICIAL_TELEGRAM_API = (
    TELEGRAM_API_BASE_URL.lower() == "https://api.telegram.org"
)

REQUESTED_MAX_UPLOAD_MB = max(
    1,
    int(os.getenv("MAX_UPLOAD_MB", "100")),
)

# The hosted Telegram Bot API only accepts bot uploads up to ~50 MB.
# A Local Bot API Server can accept much larger uploads. Keep the hosted API
# safely below its ceiling, while honoring the configured limit in local mode.
MAX_UPLOAD_MB = (
    min(REQUESTED_MAX_UPLOAD_MB, 49)
    if OFFICIAL_TELEGRAM_API
    else REQUESTED_MAX_UPLOAD_MB
)
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

apihelper.API_URL = (
    TELEGRAM_API_BASE_URL + "/bot{0}/{1}"
)
apihelper.FILE_URL = (
    TELEGRAM_API_BASE_URL + "/file/bot{0}/{1}"
)

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

# PocketFM public show selection state.
_pocket_states_guard = threading.Lock()
_pocket_states = {}

# Per-user basic rate limit.
_rate_guard = threading.Lock()
_last_request_at = {}

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


# ============================================================
# Utility helpers
# ============================================================

def get_main_menu():
    markup = ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    markup.row(
        KeyboardButton("📥 Media URL"),
        KeyboardButton("🔍 PocketFM Series"),
    )
    markup.row(
        KeyboardButton("📊 Status"),
        KeyboardButton("ℹ️ Help"),
    )
    return markup


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


def request_headers_for_url(
    url: str,
    accept: str = "*/*",
) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
        "Accept": accept,
    }
    headers.update(auth_headers_for_url(url))
    return headers


def scoped_get(
    url: str,
    *,
    accept: str = "*/*",
    timeout: int = 30,
    stream: bool = False,
    max_redirects: int = 5,
):
    """
    GET with per-hop auth scoping. Auth headers are recalculated after every
    redirect, so secrets are never forwarded to a host that is not explicitly
    allowlisted in AUTH_DOMAINS.
    """
    current = url
    for _ in range(max_redirects + 1):
        validate_public_http_url(current)
        response = requests.get(
            current,
            headers=request_headers_for_url(current, accept),
            timeout=timeout,
            allow_redirects=False,
            stream=stream,
        )

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            if not location:
                return response
            next_url = urljoin(current, location)
            response.close()
            current = next_url
            continue

        return response

    raise RuntimeError("Too many redirects.")


def authenticated_direct_download(
    url: str,
    job_dir: Path,
    progress_hook=None,
) -> dict:
    """
    Download an authorized NON-MANIFEST media file with per-hop scoped auth.
    This does not fetch licenses, keys, or decrypt protected streams.
    """
    response = scoped_get(
        url,
        accept="*/*",
        timeout=60,
        stream=True,
        max_redirects=5,
    )
    response.raise_for_status()

    final_url = response.url or url
    suffix = Path(urlparse(final_url).path).suffix.lower()
    if suffix not in {
        ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav",
        ".mp4", ".mkv", ".webm", ".mov", ".ts",
    }:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "audio/mpeg" in content_type:
            suffix = ".mp3"
        elif "audio/mp4" in content_type or "audio/aac" in content_type:
            suffix = ".m4a"
        elif "video/mp4" in content_type:
            suffix = ".mp4"
        else:
            suffix = ".bin"

    output = job_dir / ("authorized_media" + suffix)
    total = response.headers.get("Content-Length")
    try:
        total_bytes = int(total) if total else None
    except ValueError:
        total_bytes = None

    downloaded = 0
    with output.open("wb") as target:
        for chunk in response.iter_content(256 * 1024):
            if not chunk:
                continue
            target.write(chunk)
            downloaded += len(chunk)
            if progress_hook:
                progress_hook({
                    "status": "downloading",
                    "downloaded_bytes": downloaded,
                    "total_bytes": total_bytes,
                })

    response.close()

    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError("Authorized media download produced an empty file.")

    if progress_hook:
        progress_hook({
            "status": "finished",
            "downloaded_bytes": output.stat().st_size,
            "total_bytes": total_bytes or output.stat().st_size,
        })

    return {
        "path": output,
        "title": clean_title(Path(urlparse(final_url).path).stem, "media"),
        "thumbnail": None,
        "duration": None,
    }


def fetch_for_inspection(url: str):
    return scoped_get(
        url,
        accept="*/*",
        timeout=30,
        stream=False,
        max_redirects=5,
    )


def detect_drm_markers(text: str) -> list[str]:
    lower = (text or "").lower()
    found = []

    if (
        "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" in lower
        or "widevine" in lower
        or "com.widevine.alpha" in lower
    ):
        found.append("Widevine")

    if (
        "9a04f079-9840-4286-ab92-e65be0885f95" in lower
        or "playready" in lower
        or "mspr:pro" in lower
    ):
        found.append("PlayReady")

    if (
        "com.apple.fps" in lower
        or "skd://" in lower
        or "fairplay" in lower
    ):
        found.append("FairPlay")

    if (
        "sample-aes" in lower
        or "sample-aes-ctr" in lower
    ):
        found.append("SAMPLE-AES")

    if (
        "#ext-x-key" in lower
        and "method=none" not in lower
        and not any(
            item in found
            for item in ["FairPlay", "SAMPLE-AES"]
        )
    ):
        found.append("HLS encryption")

    return found


def manifest_type_from_text(url: str, content_type: str, text: str) -> str:
    lower_url = url.lower()
    lower_ct = (content_type or "").lower()
    lower_text = (text or "").lstrip().lower()

    if ".mpd" in lower_url or "dash+xml" in lower_ct or "<mpd" in lower_text:
        return "MPEG-DASH (.mpd)"
    if (
        ".m3u8" in lower_url
        or "mpegurl" in lower_ct
        or lower_text.startswith("#extm3u")
    ):
        return "HLS (.m3u8)"
    if ".ism" in lower_url or "smoothstreamingmedia" in lower_text:
        return "Smooth Streaming (.ism)"
    return "Unknown / webpage"


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

    response = scoped_get(
        page_url,
        accept="text/html,application/xhtml+xml",
        timeout=30,
        stream=False,
        max_redirects=5,
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
        # Inspect the manifest before handing it to ffmpeg. Protected
        # manifests are reported, not decrypted.
        try:
            response = fetch_for_inspection(candidate)
            if response.status_code == 200:
                drm = detect_drm_markers(response.text[:2_000_000])
                if drm:
                    raise RuntimeError(
                        "DRM/protected manifest detected: "
                        + ", ".join(drm)
                    )
        except RuntimeError:
            raise
        except Exception:
            logger.info(
                "Manifest pre-inspection unavailable; continuing normal "
                "non-decrypting download attempt.",
                exc_info=True,
            )

        return ffmpeg_manifest_fallback(candidate, job_dir)

    if auth_headers_for_url(candidate):
        return authenticated_direct_download(
            candidate,
            job_dir,
            progress_hook=progress_hook,
        )

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


def safe_media_filename(title: str, suffix: str) -> str:
    name = clean_title(title, "media")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " - ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:140] or "media") + suffix


def probe_media_streams(path: Path) -> dict:
    fallback_audio = path.suffix.lower() in {
        ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"
    }
    fallback_video = path.suffix.lower() in {
        ".mp4", ".mkv", ".webm", ".mov", ".ts"
    }

    if shutil.which("ffprobe") is None:
        return {
            "has_audio": fallback_audio,
            "has_video": fallback_video,
            "audio_codec": None,
        }

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "stream=codec_type,codec_name",
                "-of", "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError("ffprobe failed")

        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams") or []
        audio_streams = [
            item for item in streams
            if item.get("codec_type") == "audio"
        ]
        return {
            "has_audio": bool(audio_streams),
            "has_video": any(
                item.get("codec_type") == "video"
                for item in streams
            ),
            "audio_codec": (
                audio_streams[0].get("codec_name")
                if audio_streams else None
            ),
        }
    except Exception:
        logger.info("Media probing failed for %s", path, exc_info=True)
        return {
            "has_audio": fallback_audio,
            "has_video": fallback_video,
            "audio_codec": None,
        }


def prepare_audio_container(
    path: Path,
    job_dir: Path,
) -> Path:
    probe = probe_media_streams(path)
    codec = (probe.get("audio_codec") or "").lower()

    if not probe.get("has_audio"):
        return path

    if codec == "mp3":
        suffix = ".mp3"
        codec_args = ["-c:a", "copy"]
    elif codec in {"aac", "alac"}:
        suffix = ".m4a"
        codec_args = ["-c:a", "copy"]
    else:
        suffix = ".m4a"
        codec_args = ["-c:a", "aac", "-b:a", "128k"]

    output = job_dir / ("audio_base" + suffix)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(path),
        "-map", "0:a:0",
        "-vn",
        *codec_args,
        str(output),
    ]

    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60 * 60 * 2,
    )
    if (
        completed.returncode == 0
        and output.exists()
        and output.stat().st_size > 0
    ):
        return output

    raise RuntimeError(
        "Audio preparation failed: "
        + (completed.stderr or "ffmpeg failed")[-900:]
    )


def prepare_telegram_thumbnail(
    thumbnail_url: str | None,
    job_dir: Path,
) -> Path | None:
    if not thumbnail_url or shutil.which("ffmpeg") is None:
        return None

    source = job_dir / "thumbnail_source"
    current = thumbnail_url

    try:
        for _ in range(5):
            validate_public_http_url(current)
            response = requests.get(
                current,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 13) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    )
                },
                timeout=30,
                allow_redirects=False,
                stream=True,
            )

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue

            response.raise_for_status()
            total = 0
            with source.open("wb") as target:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        raise RuntimeError("Thumbnail is too large.")
                    target.write(chunk)
            break
        else:
            return None

        attempts = [
            ("320", "8"),
            ("256", "12"),
            ("220", "16"),
        ]
        for size, quality in attempts:
            output = job_dir / f"thumbnail_{size}.jpg"
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-i", str(source),
                    "-vf",
                    f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                    "-frames:v", "1",
                    "-q:v", quality,
                    str(output),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            if (
                completed.returncode == 0
                and output.exists()
                and 0 < output.stat().st_size <= 190 * 1024
            ):
                return output
    except Exception:
        logger.info("Thumbnail preparation unavailable", exc_info=True)

    return None


def finalize_audio_file(
    path: Path,
    title: str,
    thumbnail_path: Path | None,
    job_dir: Path,
    performer: str | None = None,
) -> Path:
    suffix = ".mp3" if path.suffix.lower() == ".mp3" else ".m4a"
    output = job_dir / safe_media_filename(title, suffix)

    def run(include_cover: bool):
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(path),
        ]
        if include_cover and thumbnail_path:
            cmd.extend(["-i", str(thumbnail_path)])

        cmd.extend(["-map", "0:a:0"])

        if include_cover and thumbnail_path:
            cmd.extend(["-map", "1:v:0"])

        cmd.extend(["-c:a", "copy"])

        if include_cover and thumbnail_path:
            cmd.extend(["-c:v", "mjpeg"])
            if suffix == ".mp3":
                cmd.extend([
                    "-id3v2_version", "3",
                    "-metadata:s:v", "title=Album cover",
                    "-metadata:s:v", "comment=Cover (front)",
                ])
            else:
                cmd.extend([
                    "-disposition:v:0", "attached_pic",
                ])

        cmd.extend([
            "-metadata", f"title={clean_title(title, path.stem)}",
        ])
        if performer:
            cmd.extend([
                "-metadata", f"artist={clean_title(performer, 'PocketFM')}",
            ])
        cmd.append(str(output))

        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60 * 60,
        )

    completed = run(bool(thumbnail_path))
    if completed.returncode != 0 and thumbnail_path:
        completed = run(False)

    if (
        completed.returncode != 0
        or not output.exists()
        or output.stat().st_size <= 0
    ):
        raise RuntimeError(
            "Audio metadata/cover preparation failed: "
            + (completed.stderr or "ffmpeg failed")[-900:]
        )

    return output


def upload_media_with_progress(
    chat_id: int,
    path: Path,
    title: str,
    user_id: int,
    status_message_id: int,
    media_kind: str,
    thumbnail_path: Path | None = None,
    performer: str | None = None,
) -> None:
    size = path.stat().st_size
    suffix = path.suffix.lower()

    if media_kind == "audio":
        method = "sendAudio"
        field_name = "audio"
        mime = (
            "audio/mpeg"
            if suffix == ".mp3"
            else "audio/mp4"
        )
        filename = safe_media_filename(title, suffix or ".m4a")
        caption_icon = "🎵"
    elif media_kind == "video":
        method = "sendVideo"
        field_name = "video"
        mime = "video/mp4"
        filename = safe_media_filename(title, suffix or ".mp4")
        caption_icon = "🎬"
    else:
        method = "sendDocument"
        field_name = "document"
        mime = "application/octet-stream"
        filename = safe_media_filename(title, suffix or ".bin")
        caption_icon = "📄"

    caption = (
        f"✅ Done\n"
        f"{caption_icon} {clean_title(title, path.stem)}\n"
        f"📦 {human_bytes(size)}"
    )

    api_url = (
        f"{TELEGRAM_API_BASE_URL}/bot{BOT_TOKEN}/{method}"
    )
    last = {"time": 0.0}

    with ExitStack() as stack:
        media = stack.enter_context(path.open("rb"))
        fields = {
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            field_name: (filename, media, mime),
        }

        if media_kind == "audio":
            fields["title"] = clean_title(title, path.stem)[:128]
            if performer:
                fields["performer"] = clean_title(
                    performer,
                    "PocketFM",
                )[:64]
        elif media_kind == "video":
            fields["supports_streaming"] = "true"

        if thumbnail_path and thumbnail_path.exists():
            thumb = stack.enter_context(thumbnail_path.open("rb"))
            fields["thumbnail"] = (
                "thumbnail.jpg",
                thumb,
                "image/jpeg",
            )

        encoder = MultipartEncoder(fields=fields)

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
            timeout=(30, 1200),
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


def is_pocketfm_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "pocketfm.com" or host.endswith(".pocketfm.com")


def pocketfm_public_page_info(url: str) -> dict:
    """
    Extract metadata and openly exposed media links from the public PocketFM
    webpage. No private API, key extraction, or decryption is used here.
    """
    validate_public_http_url(url)

    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.pocketfm.com/",
        },
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()

    html = response.text
    normalized = (
        html.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )

    def meta_value(name):
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, re.I)
            if match:
                return match.group(1).strip()
        return None

    title = meta_value("og:title") or clean_title(
        Path(urlparse(response.url).path).stem,
        "Pocket FM",
    )
    thumbnail = meta_value("og:image")

    raw_candidates = re.findall(
        r'https?://[^\s"\'<>]+?\.(?:m3u8|mp3|m4a|aac|mp4)(?:\?[^\s"\'<>]*)?',
        normalized,
        flags=re.I,
    )

    candidates = []
    seen = set()
    for candidate in raw_candidates:
        candidate = candidate.rstrip(").,]}>")
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            validate_public_http_url(candidate)
        except Exception:
            continue
        candidates.append(candidate)

    return {
        "title": clean_title(title, "Pocket FM"),
        "thumbnail": thumbnail,
        "candidates": candidates[:20],
        "final_url": response.url,
    }


def pocketfm_public_download(
    url: str,
    job_dir: Path,
    progress_hook=None,
) -> dict:
    page = pocketfm_public_page_info(url)
    last_error = None

    for candidate in page.get("candidates") or []:
        try:
            result = download_public_candidate(
                candidate,
                job_dir,
                progress_hook=progress_hook,
            )
            result["title"] = page.get("title") or result.get("title")
            result["thumbnail"] = (
                page.get("thumbnail")
                or result.get("thumbnail")
            )
            return result
        except Exception as exc:
            last_error = exc
            logger.warning(
                "PocketFM public candidate failed: %s",
                exc,
            )

    if last_error:
        raise RuntimeError(safe_error(last_error))

    raise RuntimeError(
        "PocketFM page did not expose a public direct media URL."
    )


def pocketfm_public_show_links(url: str) -> tuple[str, list[str]]:
    validate_public_http_url(url)

    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.pocketfm.com/",
        },
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()

    html = response.text.replace("\\/", "/")

    title_match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        html,
        re.I,
    )
    title = (
        clean_title(title_match.group(1), "Pocket FM Series")
        if title_match
        else "Pocket FM Series"
    )

    matches = re.findall(
        r'(?:https?://(?:www\.)?pocketfm\.com)?/episode/[A-Za-z0-9_-]+(?:\?[^\s"\'<>]*)?',
        html,
        flags=re.I,
    )

    links = []
    seen = set()
    for item in matches:
        link = (
            item
            if item.startswith("http")
            else urljoin(response.url, item)
        )
        if link not in seen:
            seen.add(link)
            links.append(link)

    return title, links[:500]


def media_duration_seconds(path: Path):
    if shutil.which("ffprobe") is None:
        return None

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            return None

        duration = float((completed.stdout or "").strip())
        return duration if duration > 0 else None
    except Exception:
        return None


def maybe_compress_audio_for_upload(
    path: Path,
    job_dir: Path,
) -> Path:
    """
    Keep oversized audio as ONE file. Re-encode it to a bitrate calculated
    from the duration so the result fits under the configured Telegram limit.
    No splitting is performed.
    """
    if path.stat().st_size <= MAX_UPLOAD_BYTES:
        return path

    if path.suffix.lower() not in {
        ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"
    }:
        return path

    if shutil.which("ffmpeg") is None:
        return path

    duration = media_duration_seconds(path)
    target_bytes = int(MAX_UPLOAD_BYTES * 0.90)

    # Try progressively lower mono MP3 bitrates until one single file fits.
    bitrate_steps = [
        128, 112, 96, 80, 64, 56, 48, 40,
        32, 28, 24, 20, 16, 12, 8,
    ]

    if duration:
        calculated_kbps = max(
            8,
            int((target_bytes * 8 / duration / 1000) * 0.90),
        )
        candidates = [
            kbps for kbps in bitrate_steps
            if kbps <= calculated_kbps
        ]
        if not candidates:
            candidates = [8]
    else:
        candidates = list(bitrate_steps)

    # Always keep lower bitrate fallbacks available.
    lowest = candidates[-1]
    for kbps in bitrate_steps:
        if kbps < lowest and kbps not in candidates:
            candidates.append(kbps)

    successful = []

    for kbps in candidates:
        output = job_dir / f"{path.stem}_single_{kbps}k.mp3"

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(path),
            "-vn",
            "-ac", "1",
            "-codec:a", "libmp3lame",
            "-b:a", f"{kbps}k",
        ]

        # Very low bitrates are more stable with a lower sample rate.
        if kbps <= 24:
            cmd.extend(["-ar", "16000"])

        cmd.append(str(output))

        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60 * 60 * 2,
        )

        if (
            completed.returncode == 0
            and output.exists()
            and output.stat().st_size > 0
        ):
            successful.append(output)
            if output.stat().st_size <= target_bytes:
                return output

    # Never split. Return the smallest one-file result if compression worked.
    if successful:
        return min(successful, key=lambda p: p.stat().st_size)

    return path


def prepare_upload_files(
    path: Path,
    job_dir: Path,
) -> list[Path]:
    """
    Always return exactly one audio file. Oversized audio is compressed;
    it is never split into parts.
    """
    return [
        maybe_compress_audio_for_upload(
            path,
            job_dir,
        )
    ]

def download_media(
    url: str,
    job_dir: Path,
    progress_hook=None,
) -> dict:
    validate_public_http_url(url)

    first_error = None

    if is_pocketfm_url(url) and "/episode/" in urlparse(url).path.lower():
        try:
            return pocketfm_public_download(
                url,
                job_dir,
                progress_hook=progress_hook,
            )
        except Exception as exc:
            first_error = exc
            logger.warning(
                "PocketFM public extractor failed; trying generic flow: %s",
                exc,
            )

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
        "DDownloader + PocketFM Bot ✅\n\n"
        "Send a public/authorized media URL. PocketFM episode links are "
        "checked with the PocketFM public-page extractor first, then the "
        "general downloader fallback.\n\n"
        "Commands:\n"
        "/status - live download/upload progress\n"
        "/whoami - show your Telegram user ID\n"
        "/inspect <url> - inspect manifest/DRM markers\n"
        "/authstatus - show authorized-download config status\n"
        "/help - show this help\n\n"
        f"Max video height: {MAX_VIDEO_HEIGHT}p\n"
        f"Requested upload limit: {REQUESTED_MAX_UPLOAD_MB} MB\n"
        f"Active upload limit: {MAX_UPLOAD_MB} MB\n"
        f"Telegram API mode: "
        f"{'official' if OFFICIAL_TELEGRAM_API else 'local/custom'}\n\n"
        "Allowlisted authenticated, non-DRM sources can use Render-stored "
        "authorization headers. "
        "Audio-only sources are sent as Telegram audio with episode title "
        "and cover art. Video sources are sent as video with audio. "
        "Large audio files are compressed only when they exceed the "
        "active upload limit. "
        "DRM keys/decryption and protection bypass are not supported."
    )
    bot.reply_to(
        message,
        text,
        reply_markup=get_main_menu(),
    )


@bot.message_handler(commands=["whoami"])
def cmd_whoami(message):
    if not message.from_user:
        return
    bot.reply_to(
        message,
        f"Your Telegram user ID: {message.from_user.id}"
    )


@bot.message_handler(commands=["inspect"])
def cmd_inspect(message):
    if not allowed_user(message):
        bot.reply_to(message, "This bot is private.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Usage: /inspect <url>"
        )
        return

    url = parts[1].strip()
    if not url.startswith(("http://", "https://")):
        bot.reply_to(message, "Send a valid http/https URL.")
        return

    status_message = bot.reply_to(message, "Inspecting…")

    try:
        response = fetch_for_inspection(url)

        content_type = response.headers.get("Content-Type", "")
        body = response.text[:2_000_000]

        drm = detect_drm_markers(body)
        manifest_type = manifest_type_from_text(
            response.url,
            content_type,
            body,
        )

        auth_used = bool(auth_headers_for_url(url))
        auth_text = "yes" if auth_used else "no"

        drm_text = ", ".join(drm) if drm else "No common DRM marker detected"

        result = (
            "Inspection result\n\n"
            f"HTTP: {response.status_code}\n"
            f"Final URL host: {urlparse(response.url).hostname or '?'}\n"
            f"Content-Type: {content_type or '?'}\n"
            f"Type: {manifest_type}\n"
            f"Authenticated request: {auth_text}\n"
            f"DRM/encryption markers: {drm_text}\n\n"
            "This command only inspects metadata/manifest text; "
            "it does not extract keys or decrypt protected media."
        )

        edit_status(
            message.chat.id,
            status_message.message_id,
            result,
        )

    except Exception as exc:
        edit_status(
            message.chat.id,
            status_message.message_id,
            "Inspection failed.\n\n" + safe_error(exc),
        )


@bot.message_handler(commands=["authstatus"])
def cmd_authstatus(message):
    if not allowed_user(message):
        bot.reply_to(message, "This bot is private.")
        return

    configured = bool(
        AUTH_DOMAINS
        and (AUTH_COOKIE or AUTHORIZATION_HEADER or AUTH_REFERER)
    )
    domains = ", ".join(sorted(AUTH_DOMAINS)) if AUTH_DOMAINS else "none"
    bot.reply_to(
        message,
        (
            "Authorized non-DRM download config\n\n"
            f"Configured: {'yes' if configured else 'no'}\n"
            f"Allowlisted domains: {domains}\n"
            f"Cookie present: {'yes' if AUTH_COOKIE else 'no'}\n"
            f"Authorization header present: "
            f"{'yes' if AUTHORIZATION_HEADER else 'no'}\n"
            f"Referer present: {'yes' if AUTH_REFERER else 'no'}\n\n"
            "Secrets are never shown. DRM/license/key bypass is not supported."
        ),
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
# PocketFM UI + download handlers
# ============================================================

@bot.message_handler(func=lambda message: message.text == "📥 Media URL")
def button_media_url(message):
    bot.reply_to(
        message,
        "Send one media URL, or multiple PocketFM /episode/ links "
        "on separate lines.",
    )


@bot.message_handler(func=lambda message: message.text == "🔍 PocketFM Series")
def button_pocket_series(message):
    bot.reply_to(
        message,
        "Send a public PocketFM /show/ link. If episode links are visible "
        "on the public page, I will let you choose a single episode or range.",
    )


@bot.message_handler(func=lambda message: message.text == "📊 Status")
def button_status(message):
    cmd_status(message)


@bot.message_handler(func=lambda message: message.text == "ℹ️ Help")
def button_help(message):
    cmd_start(message)


@bot.message_handler(
    func=lambda message: bool(message.text)
    and "pocketfm.com/show/" in message.text.lower()
)
def handle_pocket_show(message):
    if not allowed_user(message) or not message.from_user:
        return

    url = extract_url(message.text or "")
    if not url:
        bot.reply_to(message, "Send a valid PocketFM show URL.")
        return

    status_message = bot.reply_to(
        message,
        "🔍 Reading public series page…",
    )

    try:
        title, links = pocketfm_public_show_links(url)
        if not links:
            edit_status(
                message.chat.id,
                status_message.message_id,
                "No public episode links were visible on this show page. "
                "You can still send individual public episode URLs.",
            )
            return

        with _pocket_states_guard:
            _pocket_states[message.from_user.id] = {
                "title": title,
                "links": links,
                "created": time.time(),
            }

        edit_status(
            message.chat.id,
            status_message.message_id,
            f"🎧 {title}\n"
            f"Public episode links found: {len(links)}\n\n"
            "Send one number, for example: 7\n"
            "Or a range: 1 15",
        )
        bot.register_next_step_handler(
            status_message,
            process_pocket_range,
        )
    except Exception as exc:
        edit_status(
            message.chat.id,
            status_message.message_id,
            "Series read failed.\n\n" + safe_error(exc),
        )


def process_pocket_range(message):
    if not allowed_user(message) or not message.from_user:
        return

    user_id = message.from_user.id
    with _pocket_states_guard:
        state = dict(_pocket_states.get(user_id, {}))

    links = state.get("links") or []
    if not links:
        bot.reply_to(
            message,
            "Series selection expired. Send the /show/ link again.",
        )
        return

    try:
        numbers = (message.text or "").strip().split()
        if len(numbers) == 1:
            start = end = int(numbers[0])
        elif len(numbers) == 2:
            start, end = map(int, numbers)
        else:
            raise ValueError

        start = max(1, start)
        end = min(len(links), end)
        if end < start:
            raise ValueError

        selected = links[start - 1:end]
    except ValueError:
        bot.reply_to(
            message,
            "Use a number like 7, or a range like 1 15.",
        )
        return

    with _pocket_states_guard:
        _pocket_states.pop(user_id, None)

    process_url_batch(
        message,
        selected,
        batch_label=f"PocketFM episodes {start}-{end}",
    )


def process_one_url(
    message,
    url: str,
    user_id: int,
    sequence_text: str = "",
) -> bool:
    job_id = f"{user_id}_{message.message_id}_{uuid.uuid4().hex[:8]}"
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"{sequence_text}\n" if sequence_text else ""
    status_msg = bot.reply_to(
        message,
        prefix + "Waiting for download slot…",
    )

    try:
        set_job(
            user_id,
            "waiting",
            sequence_text or "Waiting for a download slot…",
        )

        with _download_slots:
            set_job(
                user_id,
                "downloading",
                sequence_text or "Downloading media…",
            )
            edit_status(
                message.chat.id,
                status_msg.message_id,
                prefix + "Downloading…",
            )

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

        title = media_info.get("title") or result.stem
        thumbnail = media_info.get("thumbnail")
        performer = "PocketFM" if is_pocketfm_url(url) else None

        probe = probe_media_streams(result)
        if probe.get("has_video"):
            media_kind = "video"
        elif probe.get("has_audio"):
            media_kind = "audio"
        else:
            media_kind = "document"

        edit_status(
            message.chat.id,
            status_msg.message_id,
            f"{prefix}Preparing Telegram media…",
        )
        set_job(
            user_id,
            "processing",
            (
                "Preparing audio title, cover and file size…"
                if media_kind == "audio"
                else "Preparing video + audio for Telegram…"
            ),
        )

        thumbnail_path = prepare_telegram_thumbnail(
            thumbnail,
            job_dir,
        )

        if media_kind == "audio":
            result = prepare_audio_container(
                result,
                job_dir,
            )

        upload_files = prepare_upload_files(
            result,
            job_dir,
        )

        if media_kind == "audio":
            upload_files = [
                finalize_audio_file(
                    upload_files[0],
                    title,
                    thumbnail_path,
                    job_dir,
                    performer=performer,
                )
            ]

        if any(
            part.stat().st_size > MAX_UPLOAD_BYTES
            for part in upload_files
        ):
            largest_mb = max(
                part.stat().st_size for part in upload_files
            ) / (1024 * 1024)
            set_job(
                user_id,
                "too_large",
                f"{largest_mb:.1f} MB > {MAX_UPLOAD_MB} MB",
            )
            limit_note = ""
            if OFFICIAL_TELEGRAM_API and REQUESTED_MAX_UPLOAD_MB > 49:
                limit_note = (
                    "\n\nLocal Telegram Bot API is not connected yet. "
                    f"Requested limit is {REQUESTED_MAX_UPLOAD_MB} MB, "
                    "but the official Telegram Bot API keeps this bot at "
                    "49 MB."
                )

            edit_status(
                message.chat.id,
                status_msg.message_id,
                f"{prefix}Could not reduce this file below "
                f"{MAX_UPLOAD_MB} MB."
                + limit_note,
            )
            return False

        total_upload_size = sum(
            part.stat().st_size for part in upload_files
        )

        part_count = len(upload_files)
        for part_index, upload_path in enumerate(
            upload_files,
            start=1,
        ):
            size = upload_path.stat().st_size
            part_title = (
                title
                if part_count == 1
                else f"{title} (Part {part_index}/{part_count})"
            )

            set_job(
                user_id,
                "uploading",
                (
                    f"Part {part_index}/{part_count} • "
                    f"0.0% • {human_bytes(size)}"
                    if part_count > 1
                    else f"0.0% • {human_bytes(size)}"
                ),
            )

            edit_status(
                message.chat.id,
                status_msg.message_id,
                (
                    f"{prefix}⬆️ Uploading part "
                    f"{part_index}/{part_count}\n"
                    f"0.0% • {human_bytes(size)}"
                    if part_count > 1
                    else (
                        f"{prefix}⬆️ Uploading to Telegram\n"
                        f"0.0% • {human_bytes(size)}"
                    )
                ),
            )

            bot.send_chat_action(
                message.chat.id,
                (
                    "upload_video"
                    if media_kind == "video"
                    else "upload_document"
                ),
            )

            upload_media_with_progress(
                chat_id=message.chat.id,
                path=upload_path,
                title=part_title,
                user_id=user_id,
                status_message_id=status_msg.message_id,
                media_kind=media_kind,
                thumbnail_path=thumbnail_path,
                performer=performer,
            )

        set_job(
            user_id,
            "done",
            (
                f"{clean_title(title)} • "
                f"{human_bytes(total_upload_size)} • "
                f"1 {media_kind} file"
            ),
        )

        try:
            bot.delete_message(
                message.chat.id,
                status_msg.message_id,
            )
        except Exception:
            pass

        return True

    except subprocess.TimeoutExpired:
        set_job(user_id, "failed", "Download timed out.")
        edit_status(
            message.chat.id,
            status_msg.message_id,
            prefix + "Download timed out.",
        )
        return False

    except Exception as exc:
        logger.exception(
            "Download failed for user %s",
            user_id,
        )
        error_text = safe_error(exc)
        set_job(user_id, "failed", error_text)
        edit_status(
            message.chat.id,
            status_msg.message_id,
            prefix + "Download failed.\n\n" + error_text,
        )
        return False

    finally:
        cleanup_job(job_dir)


def process_url_batch(
    message,
    urls: list[str],
    batch_label: str = "",
):
    if not message.from_user:
        return

    user_id = message.from_user.id
    lock = user_lock(user_id)

    if not lock.acquire(blocking=False):
        bot.reply_to(
            message,
            "You already have a download running. Use /status.",
        )
        return

    try:
        total = len(urls)
        success = 0

        for index, url in enumerate(urls, start=1):
            sequence = (
                f"{batch_label} ({index}/{total})"
                if batch_label
                else (
                    f"Item {index}/{total}"
                    if total > 1
                    else ""
                )
            )

            if process_one_url(
                message,
                url,
                user_id,
                sequence_text=sequence,
            ):
                success += 1

        if total > 1:
            bot.reply_to(
                message,
                f"Batch complete ✅\n"
                f"Successful: {success}/{total}",
            )
    finally:
        def expire_status():
            time.sleep(60)
            clear_job(user_id)

        threading.Thread(
            target=expire_status,
            daemon=True,
        ).start()

        lock.release()


# ============================================================
# General URL handler
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
            f"Please wait {RATE_LIMIT_SECONDS} seconds before another request.",
        )
        return

    raw_urls = URL_RE.findall(message.text or "")
    urls = []
    seen = set()

    for raw in raw_urls:
        url = raw.rstrip(").,]}>\"'")
        if url in seen:
            continue
        seen.add(url)

        try:
            validate_public_http_url(url)
        except Exception:
            continue

        urls.append(url)

    if not urls:
        bot.reply_to(
            message,
            "Send a valid http/https media URL.",
        )
        return

    # Multiple URLs are accepted only as a bounded batch. This especially
    # preserves the PocketFM multi-episode workflow from the merged bot.
    urls = urls[:20]

    label = (
        "PocketFM episodes"
        if len(urls) > 1
        and all(
            is_pocketfm_url(url)
            and "/episode/" in urlparse(url).path.lower()
            for url in urls
        )
        else ""
    )

    process_url_batch(
        message,
        urls,
        batch_label=label,
    )


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
        requested_max_upload_mb=REQUESTED_MAX_UPLOAD_MB,
        telegram_api_mode=(
            "official" if OFFICIAL_TELEGRAM_API else "local/custom"
        ),
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
