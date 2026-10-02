import hashlib
import copy
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
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from flask import Flask, jsonify, request, send_file
import requests
import telebot
from telebot import apihelper
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from kuku_catalog import show_slug as kuku_show_slug, get_catalog as kuku_get_catalog, refresh_episode as kuku_refresh_episode
from runtime_checks import system_status, check_download_environment
from batch_state import BatchStore, pending_indices, failure_report, failure_category
from provider_cookies import cookie_header
from encrypted_media import encryption_markers, export_telegram_format
from miniapp_bridge import parse_action
from pocketfm_api import api_url as pocket_api_url, normalize as pocket_normalize
from kuku_catalog import api_url as kuku_api_url, normalize_page as kuku_normalize
from pocketfm_api import get_catalog as pocket_api_catalog, refresh_episode as pocket_api_refresh, API_HOST as POCKET_API_HOST, API_PATH as POCKET_API_PATH
from pocketfm_catalog import (
    PageParser, page_values, catalog_from_values, episode_action_id,
    action_catalog, select_entries, episode_metadata, public_episode_candidates,
    episode_access, access_summary, episode_list_page,
)


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
KUKU_COOKIE = os.getenv("KUKU_COOKIE", "").strip()
POCKETFM_ACCESS_TOKEN = os.getenv("POCKETFM_ACCESS_TOKEN", "").strip()
POCKETFM_COOKIE = os.getenv("POCKETFM_COOKIE", "").strip()
MIN_FREE_DISK_MB = max(0, int(os.getenv("MIN_FREE_DISK_MB", "256")))

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
BATCH_STORE = BatchStore(os.getenv('BATCH_STATE_PATH', str(BASE_DIR / 'state' / 'batches.sqlite3')))

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
_pocket_action_cache = {}
_batch_cancel_events = {}

# Per-user basic rate limit.
_rate_guard = threading.Lock()
_last_request_at = {}

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


# ============================================================
# Utility helpers
# ============================================================

_output_formats = {}


def get_main_menu():
    markup = ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    markup.row(
        KeyboardButton("📥 Media URL"),
        KeyboardButton("🔍 Series"),
    )
    markup.row(
        KeyboardButton("📊 Status"),
        KeyboardButton("ℹ️ Help"),
    )
    markup.row(KeyboardButton("🎵 MP3"), KeyboardButton("🎬 MP4"))
    markup.row(KeyboardButton("📱 Mini App"))
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
    if host in {"pocketfm.com", "www.pocketfm.com"} and POCKETFM_COOKIE:
        if urlparse(url).scheme != "https":
            return {}
        if not ALLOWED_USER_IDS:
            raise RuntimeError("Set ALLOWED_USER_IDS before using a PocketFM cookie.")
        return {"Cookie": cookie_header(POCKETFM_COOKIE, url, 'pocketfm.com')}
    if host in {"kukufm.com", "www.kukufm.com"} and KUKU_COOKIE:
        return {"Cookie": cookie_header(KUKU_COOKIE, url, 'kukufm.com')} if urlparse(url).scheme == "https" else {}
    if (host == POCKET_API_HOST and urlparse(url).scheme == "https"
            and urlparse(url).path == POCKET_API_PATH):
        return {"access-token": POCKETFM_ACCESS_TOKEN} if POCKETFM_ACCESS_TOKEN else {}
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
    extra_headers: dict | None = None,
):
    """
    GET with per-hop auth scoping. Auth headers are recalculated after every
    redirect, so secrets are never forwarded to a host that is not explicitly
    allowlisted in AUTH_DOMAINS.
    """
    current = url
    for _ in range(max_redirects + 1):
        validate_public_http_url(current)
        headers = request_headers_for_url(current, accept)
        if extra_headers:
            headers.update(extra_headers)
            # Auth values always win over convenience/default headers.
            headers.update(auth_headers_for_url(current))

        response = requests.get(
            current,
            headers=headers,
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
    return encryption_markers(text)


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


def validate_audio_decodes(path: Path) -> None:
    """A readable container and successful stream copy do not prove playback."""
    try:
        completed = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-xerror",
             "-err_detect", "explode", "-i", str(path), "-map", "0:a:0",
             "-vn", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Audio playback validation timed out. File was not uploaded.") from exc
    if completed.returncode != 0 or completed.stderr.strip():
        raise RuntimeError(
            "Downloaded audio could not be decoded and was not uploaded. "
            "The source may be damaged or protected. Check playback in your "
            "signed-in provider account and retry with a fresh session. "
            "Renaming or copying the file cannot repair its audio data."
        )


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
        ""
        if media_kind == "audio"
        else (
            f"✅ Done\n"
            f"{caption_icon} {clean_title(title, path.stem)}\n"
            f"📦 {human_bytes(size)}"
        )
    )

    api_url = (
        f"{TELEGRAM_API_BASE_URL}/bot{BOT_TOKEN}/{method}"
    )
    last = {"time": 0.0}

    with ExitStack() as stack:
        media = stack.enter_context(path.open("rb"))
        fields = {
            "chat_id": str(chat_id),
            field_name: (filename, media, mime),
        }
        if caption:
            fields["caption"] = caption[:1024]

        if media_kind == "audio":
            fields["title"] = clean_title(title, path.stem)[:128]
            if performer:
                fields["performer"] = clean_title(
                    performer,
                    "PocketFM",
                )[:64]
            duration = media_duration_seconds(path)
            if duration:
                fields["duration"] = str(max(1, int(round(duration))))
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


def is_pocketfm_onelink(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "pocketfm.onelink.me" or host.endswith(".pocketfm.onelink.me")


def _pocketfm_episode_url_from_text(value: str | None) -> str | None:
    if not value:
        return None

    text = str(value)
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded

    match = re.search(
        r'https?://(?:www\.)?pocketfm\.com/episode/'
        r'[A-Za-z0-9_-]+(?:\?[^\s"\'<>]*)?',
        text,
        flags=re.I,
    )
    if match:
        return match.group(0).rstrip(").,]}>")

    # Some OneLink campaigns can carry an app deep link instead of a web URL.
    match = re.search(
        r'pocketfm://(?:[^\s"\'<>]*/)?episode/'
        r'([A-Za-z0-9_-]+)',
        text,
        flags=re.I,
    )
    if match:
        return "https://www.pocketfm.com/episode/" + match.group(1)

    return None


def _extract_pocketfm_episode_from_payload(value: str | None) -> str | None:
    if not value:
        return None

    episode = _pocketfm_episode_url_from_text(value)
    if episode:
        return episode

    text = str(value)
    for _ in range(4):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
        episode = _pocketfm_episode_url_from_text(text)
        if episode:
            return episode

    # AppsFlyer/OneLink payloads can store destination values in nested
    # query parameters such as af_dp/deep_link_value/af_web_dp.
    try:
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        for key, values in query.items():
            if key.lower() in {
                "af_dp",
                "af_web_dp",
                "af_android_url",
                "af_ios_url",
                "deep_link_value",
                "deep_link_sub1",
                "deep_link_sub2",
                "deep_link_sub3",
                "deep_link_sub4",
                "deep_link_sub5",
            }:
                for item in values:
                    episode = _pocketfm_episode_url_from_text(item)
                    if episode:
                        return episode
    except Exception:
        pass

    # Also inspect JSON/HTML text around common OneLink field names.
    for match in re.finditer(
        r"(?i)(?:af_dp|af_web_dp|af_android_url|af_ios_url|"
        r"deep_link_value|deep_link_sub[1-5])[\\\"']?\\s*[:=]\\s*"
        r"[\\\"']([^\\\"']+)[\\\"']",
        text,
    ):
        episode = _pocketfm_episode_url_from_text(match.group(1))
        if episode:
            return episode

    return None


def resolve_pocketfm_onelink(url: str) -> tuple[str | None, str]:
    """
    Try multiple normal mobile/browser OneLink resolution paths and extract an
    episode URL only when the public redirect/query/HTML payload actually
    exposes one. This does not use a private PocketFM API.
    """
    validate_public_http_url(url)

    user_agents = [
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
            "AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
    ]

    last_url = url

    for user_agent in user_agents:
        current = url
        seen = set()

        for _ in range(10):
            if current in seen:
                break
            seen.add(current)
            last_url = current

            episode = _extract_pocketfm_episode_from_payload(current)
            if episode:
                return episode, current

            validate_public_http_url(current)
            response = requests.get(
                current,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                },
                timeout=30,
                allow_redirects=False,
            )

            location = response.headers.get("Location")
            if location:
                next_url = urljoin(current, location)
                episode = _extract_pocketfm_episode_from_payload(next_url)
                if episode:
                    response.close()
                    return episode, next_url

                if 300 <= response.status_code < 400:
                    response.close()
                    # App/store deep links are not HTTP requests. Try the next
                    # browser profile instead of raising a misleading URL error.
                    if urlparse(next_url).scheme not in {"http", "https"}:
                        last_url = next_url
                        break
                    current = next_url
                    continue

            body = ""
            content_type = (response.headers.get("Content-Type") or "").lower()
            if (
                "text" in content_type
                or "html" in content_type
                or "json" in content_type
                or not content_type
            ):
                try:
                    body = response.text[:2_000_000]
                except Exception:
                    body = ""
            response.close()

            episode = _extract_pocketfm_episode_from_payload(body)
            if episode:
                return episode, current

            break

    return None, last_url


def is_pocketfm_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "pocketfm.com" or host.endswith(".pocketfm.com")


def pocketfm_public_page_info(url: str) -> dict:
    """
    Extract metadata and openly exposed media links from the public PocketFM
    webpage. No private API, key extraction, or decryption is used here.
    """
    validate_public_http_url(url)

    response = scoped_get(
        url,
        accept="text/html,application/xhtml+xml",
        timeout=30,
        stream=False,
        max_redirects=5,
        extra_headers={
            "Referer": "https://www.pocketfm.com/",
        },
    )
    response.raise_for_status()

    html = response.text
    response.close()
    episode_id = urlparse(url).path.rstrip("/").split("/")[-1]
    story = episode_metadata(html, episode_id)
    if story:
        # Do not fall through to unrelated/recommended episode media when this
        # episode needs unlocking or exposes no playable media.
        if story.get("is_locked") is True or episode_access(story) == "locked":
            raise RuntimeError("This PocketFM episode requires unlocking in your account.")
        return {
            "title": clean_title(story["story_title"], "Pocket FM"),
            "thumbnail": story.get("image_url"),
            "series": story.get("show_title"),
            "candidates": public_episode_candidates(story),
            "final_url": response.url,
        }
    raise RuntimeError(
        "PocketFM did not expose identifiable metadata for the requested episode."
    )


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
            result["performer"] = page.get("series")
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


def pocketfm_public_show_catalog(url: str, progress=None) -> dict:
    validate_public_http_url(url)
    response = scoped_get(url, accept="text/html", timeout=30)
    try:
        response.raise_for_status()
        html, page_url = response.text, response.url
    finally:
        response.close()
    if not is_pocketfm_url(page_url):
        raise RuntimeError("The show URL did not resolve to PocketFM.")
    match = re.search(r"/show/([A-Za-z0-9_-]+)", urlparse(page_url).path)
    if not match:
        raise RuntimeError("Send a PocketFM /show/ URL.")
    show_id = match.group(1)
    catalog = catalog_from_values(page_values(html), show_id)
    parser = PageParser()
    parser.feed(html)
    catalog["title"] = catalog["title"] or parser.title or "Pocket FM Series"
    catalog["warning"] = ""
    # Sending credentials does not prove login succeeded. Describe the source
    # as a session request, never claim the user is authenticated.
    catalog["session_request"] = any(
        name in auth_headers_for_url(page_url) for name in ("Cookie", "Authorization")
    )
    # Preserve compatibility with older pages which expose ordinary links.
    if not catalog["entries"]:
        matches = re.findall(r'/episode/([A-Za-z0-9_-]+)', html.replace("\\/", "/"))
        catalog["entries"] = [
            {"id": sid, "number": i, "title": f"Episode {i}",
             "url": "https://pocketfm.com/episode/" + sid, "access": "unknown"}
            for i, sid in enumerate(dict.fromkeys(matches), 1)
        ]
        catalog["total"] = 0
        catalog["warning"] = "The full catalogue size could not be verified."
        return catalog

    entries = {e["id"]: e for e in catalog["entries"]}
    cursor = catalog["next_ptr"]
    total = catalog["total"]
    if total and len(entries) >= total:
        return catalog
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    try:
        action = None
        sources = list(dict.fromkeys(urljoin(page_url, src) for src in parser.sources))
        for src in reversed(sources[-60:]):
            if urlparse(src).netloc != urlparse(page_url).netloc:
                continue
            if not urlparse(src).path.startswith("/_next/static/"):
                continue
            cached = _pocket_action_cache.get(src)
            if cached:
                action = cached
                break
            script = scoped_get(src, timeout=10)
            try:
                script.raise_for_status()
                action = episode_action_id(script.text)
            finally:
                script.close()
            if action:
                _pocket_action_cache.clear()
                _pocket_action_cache[src] = action
                break
        if not action:
            raise RuntimeError("The website's Load more action could not be found.")
        seen_cursors = set()
        for _ in range(1000):
            if cursor is None or cursor == -1 or (total and len(entries) >= total):
                break
            if not isinstance(cursor, int) or cursor in seen_cursors:
                raise RuntimeError("PocketFM repeated its episode pagination cursor.")
            seen_cursors.add(cursor)
            validate_public_http_url(page_url)
            headers = request_headers_for_url(page_url, "text/x-component")
            headers.update({"Next-Action": action, "Origin": origin,
                            "Content-Type": "text/plain;charset=UTF-8"})
            # This is the public site's read-only Load more action. Never
            # follow POST redirects or execute JavaScript from the website.
            result = requests.post(
                page_url, headers=headers,
                data=json.dumps([{"showId": show_id, "currPtr": cursor, "pageSize": 20}]),
                timeout=30, allow_redirects=False,
            )
            try:
                result.raise_for_status()
                page = action_catalog(result.text, show_id)
            finally:
                result.close()
            before = len(entries)
            entries.update({e["id"]: e for e in page["entries"]})
            if len(entries) == before:
                raise RuntimeError("PocketFM returned no new episodes.")
            total = max(total, page["total"])
            cursor = page["next_ptr"]
            if progress:
                progress(len(entries), total)
        if not total or len(entries) < total:
            raise RuntimeError("Only part of the episode catalogue was returned.")
    except Exception as exc:
        logger.warning("PocketFM catalogue is incomplete: %s", exc)
        catalog["warning"] = (
            "The full list could not be loaded. ALL selects only the episodes listed here. "
            "Try the show link again later."
        )
    catalog["entries"] = sorted(entries.values(), key=lambda e: (e["number"], e["id"]))
    catalog["total"] = total
    return catalog


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

def pocket_api_fetch_json(url):
    if POCKETFM_ACCESS_TOKEN and not ALLOWED_USER_IDS:
        raise RuntimeError("Set ALLOWED_USER_IDS before using a PocketFM account token.")
    response = scoped_get(url, accept="application/json", timeout=30,
                          extra_headers={"app-client": "consumer-web", "platform": "web",
                                         "auth-token": "web-auth"})
    try:
        if response.status_code in {401, 403}:
            raise RuntimeError("PocketFM API refused access. Check POCKETFM_ACCESS_TOKEN and account access.")
        response.raise_for_status()
        return response.json()
    except ValueError as exc:
        raise RuntimeError("PocketFM API returned an unsupported response.") from exc
    finally:
        response.close()


def pocket_catalog_with_api(url, progress=None):
    try:
        return pocket_api_catalog(url, pocket_api_fetch_json, progress,
                                  session=bool(POCKETFM_ACCESS_TOKEN))
    except Exception:
        if POCKETFM_ACCESS_TOKEN:
            # Do not silently substitute guest access for an invalid account.
            raise
        catalog = pocketfm_public_show_catalog(url, progress)
        catalog["provider"] = "pocketfm"
        source = "configured website session" if catalog.get("session_request") else "public website"
        catalog["warning"] = (f"Guest API unavailable; showing the {source} catalogue. "
                              + catalog.get("warning", ""))
        return catalog


def pocket_api_download_entry(entry, job_dir, progress_hook=None):
    current, page = pocket_api_refresh(entry, pocket_api_fetch_json)
    for candidate in current["media_candidates"]:
        try:
            validate_public_http_url(candidate)
            result = download_public_candidate(candidate, job_dir, progress_hook=progress_hook)
            result.update(title=current["title"], thumbnail=page.get("thumbnail"),
                          performer=page.get("title") or "PocketFM")
            return result
        except Exception:
            continue
    raise RuntimeError("The account API returned media, but no supported candidate could be downloaded.")


def kuku_fetch_json(url):
    if (KUKU_COOKIE or any(k in auth_headers_for_url(url) for k in ("Cookie", "Authorization"))) and not ALLOWED_USER_IDS:
        raise RuntimeError("Set ALLOWED_USER_IDS before using a private Kuku FM session.")
    response = scoped_get(url, accept="application/json", timeout=30)
    try:
        if response.status_code in {401, 403}:
            raise RuntimeError("Kuku FM refused access. Check the KUKU_COOKIE session and account access.")
        response.raise_for_status()
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Kuku FM returned an unsupported response instead of episode data.") from exc
    finally:
        response.close()


def kuku_download_entry(entry, job_dir, progress_hook=None):
    current, page = kuku_refresh_episode(entry, kuku_fetch_json)
    validate_public_http_url(current["media_url"])
    result = download_public_candidate(current["media_url"], job_dir, progress_hook=progress_hook)
    result.update(title=current["title"], thumbnail=page.get("thumbnail"),
                  performer=page["title"])
    return result


def download_media(
    url: str,
    job_dir: Path,
    progress_hook=None,
) -> dict:
    validate_public_http_url(url)

    if is_pocketfm_onelink(url):
        resolved, final_url = resolve_pocketfm_onelink(url)
        if not resolved:
            final_host = urlparse(final_url).hostname or "unknown"
            raise RuntimeError(
                "This PocketFM OneLink was checked with Android, iPhone and "
                "browser redirect/deep-link parsing, but it did not expose an "
                "episode URL or episode ID. "
                f"Final destination host: {final_host}. "
                "A store/campaign link by itself is not enough to identify "
                "which episode to download."
            )
        url = resolved
        validate_public_http_url(url)

    first_error = None

    if is_pocketfm_url(url) and "/episode/" in urlparse(url).path.lower():
        return pocketfm_public_download(url, job_dir, progress_hook=progress_hook)

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

@bot.message_handler(commands=['mp3', 'mp4'])
@bot.message_handler(func=lambda m: m.text in {'🎵 MP3', '🎬 MP4'})
def cmd_output_format(message):
    if not allowed_user(message) or not message.from_user:
        return
    fmt = 'mp4' if 'mp4' in (message.text or '').lower() else 'mp3'
    with _pocket_states_guard:
        _output_formats[(message.from_user.id, message.chat.id)] = fmt
    bot.reply_to(message, 'Output: ' + fmt.upper() +
                 (' — audio with cover video.' if fmt == 'mp4' else ' — audio file.') +
                 '\nNow send an episode/show link or select episodes. Choice resets on restart.',
                 reply_markup=get_main_menu())


def miniapp_url():
    base = os.getenv('MINIAPP_BASE_URL', os.getenv('RENDER_EXTERNAL_URL', '')).strip().rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return None
    return base + '/miniapp'


@bot.message_handler(commands=['app'])
@bot.message_handler(func=lambda m: m.text == '📱 Mini App')
def cmd_miniapp(message):
    if not allowed_user(message):
        return
    if message.chat.type != 'private':
        bot.reply_to(message, 'Open /app in a private chat with this bot.')
        return
    url = miniapp_url()
    if not url:
        bot.reply_to(message, 'Mini App URL is not configured. Set MINIAPP_BASE_URL to the HTTPS bot service origin in Render.')
        return
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton('Open Mini App', web_app=WebAppInfo(url)))
    bot.reply_to(message, 'Open Story Studio. Results appear in this chat.', reply_markup=markup)


@bot.message_handler(content_types=['web_app_data'])
def handle_miniapp_data(message):
    if not allowed_user(message) or not message.from_user or message.chat.type != 'private':
        return
    try:
        action, value, fmt = parse_action(message.web_app_data.data)
    except ValueError as exc:
        bot.reply_to(message, str(exc))
        return
    command = copy.copy(message)
    with _pocket_states_guard:
        if action in {'show', 'download', 'select'}:
            _output_formats[(message.from_user.id, message.chat.id)] = fmt
    if action == 'show':
        command.text = value
        if not extract_series_url(value):
            bot.reply_to(message, 'Use a supported PocketFM or Kuku FM show URL.')
            return
        handle_pocket_show(command)
    elif action == 'download':
        command.text = value
        handle_url(command)
    elif action == 'select':
        command.text = value
        process_pocket_range(command)
    else:
        command.text = '/' + action + (' ' + value if value else '')
        handlers = {'episodes': cmd_episodes, 'status': cmd_status, 'resume': cmd_continue_batch,
                    'retry': cmd_continue_batch, 'cancel': cmd_cancel, 'failures': cmd_failures,
                    'authstatus': cmd_authstatus, 'accountcheck': cmd_accountcheck, 'system': cmd_system}
        handlers[action](command)


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if not allowed_user(message):
        bot.reply_to(message, "This bot is private.")
        return

    text = (
        "DDownloader • PocketFM + Kuku FM ✅\n\n"
        "Send a public/authorized media URL. PocketFM episode links are "
        "checked against the requested episode metadata. Send a show link "
        "and then ALL to download every listed episode in one batch.\n\n"
        "Commands:\n"
        "/app - open Story Studio Mini App\n"
        "/mp3 or /mp4 - choose audio or cover-video output\n"
        "/status - live download/upload progress\n"
        "/cancel - stop a batch after the current episode\n"
        "/episodes 1 - episode list with Public/Locked status\n"
        "/pocketfm <show_id> - open a PocketFM series by ID\n"
        "/kuku <show_slug> - open a Kuku FM series by slug\n"
        "/system - binaries and free disk space\n"
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
        "Audio-only sources are sent as native Telegram audio cards with "
        "episode title, series/artist, duration and cover art when available. "
        "Video sources are sent as video with audio. "
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
            f"Kuku FM cookie configured: {'yes' if KUKU_COOKIE else 'no'}\n"
            f"PocketFM API token configured: {'yes' if POCKETFM_ACCESS_TOKEN else 'no'}\n"
            f"PocketFM website cookie configured: {'yes' if POCKETFM_COOKIE else 'no'}\n"
            "Configured does not mean login has been verified.\n"
            "Secrets are never shown. DRM/license/key bypass is not supported."
        ),
    )


@bot.message_handler(commands=["system"])
def cmd_system(message):
    if not allowed_user(message):
        return
    try:
        status = system_status(DOWNLOAD_ROOT)
        text = ("Binaries: " + (", ".join(status["missing"]) + " missing" if status["missing"] else "ffmpeg / ffprobe OK")
                + f"\nFree disk: {status['free_mb']} MB\nRequired reserve: {MIN_FREE_DISK_MB} MB")
    except OSError:
        text = "Could not access the download folder."
    bot.reply_to(message, text)


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
        "on separate lines. PocketFM OneLink share URLs are resolved when "
        "they contain an actual episode deep link.",
    )


@bot.message_handler(func=lambda message: message.text in {"🔍 PocketFM Series", "🔍 Series"})
def button_pocket_series(message):
    bot.reply_to(
        message,
        "Send a PocketFM or Kuku FM show link. Then send ALL for one batch, "
        "an episode number, or a range such as 1-15, *10, 25*, or 10*20 (inclusive).",
    )


@bot.message_handler(func=lambda message: message.text == "📊 Status")
def button_status(message):
    cmd_status(message)


@bot.message_handler(func=lambda message: message.text == "ℹ️ Help")
def button_help(message):
    cmd_start(message)


def extract_pocketfm_show_url(text):
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(").,]}>\"'")
        if is_pocketfm_url(url) and re.match(
            r"^/(?:[a-z]{2}-[a-z]{2}/)?show/[^/]+", urlparse(url).path, re.I
        ):
            return url
    return None


def extract_series_url(text):
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(").,]}>\"'")
        if extract_pocketfm_show_url(url) or kuku_show_slug(url):
            return url
    return None


def series_command_url(text):
    parts = (text or "").strip().split()
    if len(parts) != 2:
        raise ValueError("Use /pocketfm <show_id> or /kuku <show_slug>. These identify a series, not an account.")
    command = parts[0].lower().split("@", 1)[0]
    identifier = parts[1]
    if command == "/pocketfm" and re.fullmatch(r"[a-fA-F0-9]{24,64}", identifier):
        return "https://pocketfm.com/show/" + identifier
    if command == "/kuku" and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
        return "https://kukufm.com/show/" + identifier
    raise ValueError("Invalid series ID. Copy the part after /show/ in the series URL. Do not enter a mobile number or account token.")


@bot.message_handler(commands=["pocketfm", "kuku"])
def cmd_series_id(message):
    if not allowed_user(message) or not message.from_user:
        return
    try:
        url = series_command_url(message.text)
    except ValueError as exc:
        bot.reply_to(message, str(exc))
        return
    handle_pocket_show(message, series_url=url)


@bot.message_handler(func=lambda message: bool(extract_series_url(message.text)))
def handle_pocket_show(message, series_url=None):
    if not allowed_user(message) or not message.from_user:
        return

    url = series_url or extract_series_url(message.text or "")
    if not url:
        bot.reply_to(message, "Send a full PocketFM or Kuku FM show URL.")
        return

    with _pocket_states_guard:
        _pocket_states.pop(message.from_user.id, None)
    status_message = bot.reply_to(
        message,
        "🔍 Reading series episodes and access status…",
    )

    try:
        last_update = [0.0]
        def progress(count, total):
            if time.monotonic() - last_update[0] >= PROGRESS_UPDATE_SECONDS:
                edit_status(message.chat.id, status_message.message_id,
                            f"Reading episode list: {count}/{total or '?'}…")
                last_update[0] = time.monotonic()
        provider = "kuku" if kuku_show_slug(url) else "pocketfm"
        if provider == "kuku":
            catalog = kuku_get_catalog(url, kuku_fetch_json, progress=progress,
                                       session=any(k in auth_headers_for_url("https://kukufm.com") for k in ("Cookie", "Authorization")))
        else:
            catalog = pocket_catalog_with_api(url, progress=progress)
            provider = catalog.get("provider", "pocketfm")
        entries = catalog["entries"]
        title = catalog["title"]
        if not entries:
            edit_status(
                message.chat.id,
                status_message.message_id,
                "The episode catalogue could not be read from this show page. "
                "Try again later or send an individual episode URL.",
            )
            return

        with _pocket_states_guard:
            _pocket_states[message.from_user.id] = {
                "title": title,
                "show_url": url,
                "provider": provider,
                "entries": entries,
                "session_request": catalog.get("session_request", False),
                "chat_id": message.chat.id,
                "created": time.time(),
            }

        edit_status(
            message.chat.id,
            status_message.message_id,
            f"🎧 {title}\n"
            f"Episodes listed: {len(entries)}/{catalog['total'] or '?'}\n"
            + access_summary(entries, catalog.get("session_request", False)) + "\n"
            + ("Source: configured session request (login not verified).\n"
               if catalog.get("session_request") else "Source: public website, not your app account.\n")
            + "Access labels are page metadata, not a download guarantee.\n"
            + (catalog["warning"] + "\n\n" if catalog["warning"] else "\n")
            + "Send ALL for every listed episode in one batch.\n"
            "Or send 7, 1-15, *10 (1–10), 25* (25–last listed), or 10*20 (10–20).\n"
            "Each episode is sent as a separate audio file.\n"
            "Locked/unavailable episodes are reported as failed.\n"
            "Use /cancel to stop after the current episode.",
        )
        bot.reply_to(message, episode_list_page(
            entries, session=catalog.get("session_request", False)
        ), reply_markup=series_controls())
    except Exception as exc:
        edit_status(
            message.chat.id,
            status_message.message_id,
            "Series read failed.\n\n" + safe_error(exc),
        )


@bot.message_handler(commands=["episodes"])
def cmd_episodes(message):
    if not allowed_user(message) or not message.from_user:
        return
    with _pocket_states_guard:
        state = dict(_pocket_states.get(message.from_user.id, {}))
    if (not state.get("entries") or state.get("chat_id") != message.chat.id
            or time.time() - state.get("created", 0) > 3600):
        bot.reply_to(message, "Send the show link again to load the episode list.")
        return
    try:
        parts = (message.text or "").split()
        if len(parts) > 2:
            raise ValueError("Use /episodes 1")
        page = int(parts[1]) if len(parts) == 2 else 1
        text = episode_list_page(state["entries"], page, state.get("session_request", False))
    except ValueError:
        bot.reply_to(message, "Use /episodes followed by a valid page number, e.g. /episodes 1.")
        return
    bot.reply_to(message, text)


def series_controls():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton('📋 Episodes'), KeyboardButton('⬇️ Available'))
    markup.row(KeyboardButton('▶️ Resume'), KeyboardButton('🔄 Retry'), KeyboardButton('⛔ Cancel'))
    markup.row(KeyboardButton('🔐 Account check'), KeyboardButton('📄 Failures'))
    markup.row(KeyboardButton('🔍 Series'), KeyboardButton('📊 Status'))
    markup.row(KeyboardButton("🎵 MP3"), KeyboardButton("🎬 MP4"))
    markup.row(KeyboardButton("📱 Mini App"))
    return markup


@bot.message_handler(commands=['available'])
def cmd_available(message):
    selection = copy.copy(message)
    selection.text = 'AVAILABLE'
    process_pocket_range(selection)


@bot.message_handler(commands=['resume', 'retry'])
def cmd_continue_batch(message):
    if not allowed_user(message) or not message.from_user:
        return
    mode = message.text.split()[0].split('@')[0].lstrip('/')
    process_url_batch(message, [], saved_mode=mode)


@bot.message_handler(commands=['failures'])
def cmd_failures(message):
    if not allowed_user(message) or not message.from_user:
        return
    batch = BATCH_STORE.load(message.from_user.id, message.chat.id)
    bot.reply_to(message, failure_report(batch) if batch else 'No saved batch in this chat.')


@bot.message_handler(commands=['accountcheck'])
def cmd_accountcheck(message):
    if not allowed_user(message) or not message.from_user:
        return
    with _pocket_states_guard:
        state = dict(_pocket_states.get(message.from_user.id, {}))
    url = extract_series_url(message.text or '')
    if not url and state.get('chat_id') == message.chat.id:
        url = state.get('show_url')
    if not url:
        bot.reply_to(message, 'Use /accountcheck followed by a PocketFM or Kuku FM show URL, or load a series first.')
        return
    try:
        slug = kuku_show_slug(url)
        if slug:
            if not auth_headers_for_url(url).get('Cookie'):
                bot.reply_to(message, 'Kuku FM: no cookie configured. Set KUKU_COOKIE privately in Render.')
                return
            page = kuku_normalize(kuku_fetch_json(kuku_api_url(slug, 1)), slug, 1)
            label = 'Kuku FM API'
        elif POCKETFM_ACCESS_TOKEN:
            show_id = re.search(r'/show/([A-Za-z0-9_-]+)', urlparse(url).path).group(1)
            page = pocket_normalize(pocket_api_fetch_json(pocket_api_url(show_id, 0)), show_id, 0)
            label = 'PocketFM API'
        elif auth_headers_for_url(url).get('Cookie'):
            response = scoped_get(url, timeout=30)
            try:
                response.raise_for_status()
                bot.reply_to(message, 'PocketFM website responded to the cookie request. Login and paid access are NOT verified. Set POCKETFM_ACCESS_TOKEN for an API check.')
            finally:
                response.close()
            return
        else:
            bot.reply_to(message, 'PocketFM: no token/cookie configured. Set POCKETFM_ACCESS_TOKEN or POCKETFM_COOKIE privately in Render.')
            return
        bot.reply_to(message, label + ': request with credentials returned a valid catalogue page.\n'
                     + access_summary(page['entries'], session=True)
                     + '\nFirst page only. This is not proof of account identity, paid entitlement or playable audio. Try one purchased episode to verify playback.')
    except Exception as exc:
        bot.reply_to(message, 'Account check failed: ' + failure_category(exc)
                     + '\nA 401/403 may mean expired credentials or denied access; it does not prove expiry. Check your session privately in Render.')


@bot.message_handler(func=lambda m: m.text in {
    '📋 Episodes', '⬇️ Available', '▶️ Resume', '🔄 Retry', '⛔ Cancel',
    '🔐 Account check', '📄 Failures'})
def button_series_action(message):
    if not allowed_user(message) or not message.from_user:
        return
    actions = {
        '📋 Episodes': ('/episodes', cmd_episodes), '⬇️ Available': ('/available', cmd_available),
        '▶️ Resume': ('/resume', cmd_continue_batch), '🔄 Retry': ('/retry', cmd_continue_batch),
        '⛔ Cancel': ('/cancel', cmd_cancel), '🔐 Account check': ('/accountcheck', cmd_accountcheck),
        '📄 Failures': ('/failures', cmd_failures),
    }
    text, action = actions[message.text]
    command = copy.copy(message)
    command.text = text
    action(command)


def process_pocket_range(message):
    if not allowed_user(message) or not message.from_user:
        return
    user_id = message.from_user.id
    with _pocket_states_guard:
        state = dict(_pocket_states.get(user_id, {}))
    entries = state.get("entries") or []
    if (not entries or state.get("chat_id") != message.chat.id
            or time.time() - state.get("created", 0) > 3600):
        bot.reply_to(message, "Series selection expired. Send the /show/ link again.")
        return
    try:
        selected = select_entries(message.text or "", entries)
    except ValueError as exc:
        bot.reply_to(message, str(exc))
        return
    if not selected:
        bot.reply_to(message, "No episodes are explicitly Available in this catalogue. Reload the show after updating your session.")
        return
    process_url_batch(
        message, [e["url"] for e in selected],
        batch_label="Kuku FM batch" if state.get("provider") == "kuku" else "PocketFM batch",
        performer_override=state.get("title"),
        episode_numbers=[e["number"] for e in selected],
        media_entries=selected,
        journal_entries=selected,
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if not allowed_user(message) or not message.from_user:
        return
    with _pocket_states_guard:
        event = _batch_cancel_events.get(message.from_user.id)
        _pocket_states.pop(message.from_user.id, None)
        if event:
            event.set()
    bot.reply_to(message, "Batch will stop after the current episode." if event
                 else "Series selection cleared. No active batch.")


def process_one_url(
    message,
    url: str,
    user_id: int,
    sequence_text: str = "",
    performer_override: str | None = None,
    media_entry: dict | None = None,
) -> bool:
    with _pocket_states_guard:
        output_format = _output_formats.get((user_id, message.chat.id))
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
            check_download_environment(DOWNLOAD_ROOT, MIN_FREE_DISK_MB)
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

            if media_entry and media_entry.get("provider") == "kuku":
                media_info = kuku_download_entry(media_entry, job_dir, progress_hook)
            elif media_entry and media_entry.get("provider") == "pocketfm_api":
                media_info = pocket_api_download_entry(media_entry, job_dir, progress_hook)
            else:
                media_info = download_media(url, job_dir, progress_hook=progress_hook)
            result = media_info["path"]

        title = media_info.get("title") or result.stem
        thumbnail = media_info.get("thumbnail")
        performer = (
            media_info.get("performer")
            or performer_override
            or (
                "PocketFM"
                if is_pocketfm_url(url) or is_pocketfm_onelink(url)
                else None
            )
        )

        probe = probe_media_streams(result)

        # PocketFM episodes are audio-first content. Some public manifests can
        # include a poster/video track inside an MP4 container, which made the
        # bot incorrectly send them as Telegram videos. If a PocketFM source
        # contains audio, always extract/send the audio stream as sendAudio.
        pocketfm_audio_source = (
            is_pocketfm_url(url)
            or is_pocketfm_onelink(url)
            or bool(media_entry and media_entry.get("provider") == "kuku")
        )

        if pocketfm_audio_source and probe.get("has_audio"):
            media_kind = "audio"
        elif probe.get("has_video"):
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
                "Preparing series audio card…"
                if media_kind == "audio" and pocketfm_audio_source
                else (
                    "Preparing audio title, cover and file size…"
                    if media_kind == "audio"
                    else "Preparing video + audio for Telegram…"
                )
            ),
        )

        thumbnail_path = prepare_telegram_thumbnail(
            thumbnail,
            job_dir,
        )

        if output_format and probe.get("has_audio"):
            result = export_telegram_format(result, job_dir, output_format, thumbnail_path)
            media_kind = "video" if output_format == "mp4" else "audio"

        if media_kind == "audio" and output_format != "mp3":
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
            for audio_path in upload_files:
                validate_audio_decodes(audio_path)

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
        stage = (get_job(user_id) or {}).get('state', '')
        category = failure_category(exc, stage)
        error_text = '[' + category + '] ' + safe_error(exc)
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
    performer_override: str | None = None,
    episode_numbers: list[int] | None = None,
    media_entries: list[dict] | None = None,
    journal_entries: list[dict] | None = None,
    saved_mode: str | None = None,
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

    cancel_event = threading.Event()
    with _pocket_states_guard:
        _batch_cancel_events[user_id] = cancel_event
    try:
        saved = None
        indices = []
        if saved_mode:
            saved = BATCH_STORE.load(user_id, message.chat.id)
            if not saved:
                bot.reply_to(message, "No saved series batch in this chat. Send a show link first.")
                return
            indices = pending_indices(saved, retry=saved_mode == 'retry')
            if not indices:
                bot.reply_to(message, "No failed episodes to retry." if saved_mode == 'retry' else "No pending episodes. Use /retry for failures.")
                return
            media_entries = [saved['items'][i]['entry'] for i in indices]
            urls = [e['url'] for e in media_entries]
            episode_numbers = [e['number'] for e in media_entries]
            performer_override = saved['title']
            batch_label = saved_mode.capitalize()
        elif journal_entries:
            saved = BATCH_STORE.create(user_id, message.chat.id, journal_entries, performer_override)
            indices = list(range(len(journal_entries)))
        total = len(urls)
        success = 0
        failed = []
        attempted = 0
        for index, url in enumerate(urls, start=1):
            if cancel_event.is_set():
                break
            attempted += 1
            if saved:
                saved['items'][indices[index - 1]]['status'] = 'running'
                BATCH_STORE.save(user_id, message.chat.id, saved)
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
                performer_override=performer_override,
                media_entry=media_entries[index - 1] if media_entries else None,
            ):
                success += 1
                outcome = 'done'
            else:
                failed.append(episode_numbers[index - 1] if episode_numbers else index)
                outcome = 'failed'
            if saved:
                item = saved['items'][indices[index - 1]]
                item['status'] = outcome
                detail = str(get_job(user_id) or '')
                item['reason'] = failure_category(detail) if outcome == 'failed' else ''
                BATCH_STORE.save(user_id, message.chat.id, saved)

        if total > 1:
            bot.reply_to(
                message,
                f"Batch {'stopped' if attempted < total else 'complete'}\n"
                f"Successful: {success}/{total}\n"
                f"Failed: {len(failed)}\n"
                f"Not attempted: {total - attempted}"
                + ("\nFailed episode numbers: " + ", ".join(map(str, failed[:100]))
                   + (" …" if len(failed) > 100 else "") if failed else ""),
            )
        if saved:
            bot.reply_to(message, failure_report(saved) + "\n/resume: unattempted/interrupted episodes\n/retry: failed episodes",
                         reply_markup=series_controls())
    finally:
        with _pocket_states_guard:
            _batch_cancel_events.pop(user_id, None)
        completed_job = get_job(user_id)
        def expire_status():
            time.sleep(60)
            # A completed batch must not clear a newer batch's status.
            with _jobs_guard:
                if _jobs.get(user_id) == completed_job:
                    _jobs.pop(user_id, None)

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

    text = (message.text or "").strip()
    if not URL_RE.search(text) and (
        text.lower() in {"all", "available", "அனைத்தும்"} or re.fullmatch(r"[0-9\s–*\-]+", text)
    ):
        process_pocket_range(message)
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


@app.get('/miniapp')
def miniapp_page():
    response = send_file(Path(__file__).parent / 'web' / 'miniapp.html')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


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
