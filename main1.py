import os
import re
import asyncio
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from DDownloader.modules.downloader import DOWNLOADER


# ----------------------------- Configuration -----------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Keep this conservative for standard Telegram Bot API setups.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# 0 = delete successfully uploaded files, 1 = keep them.
KEEP_FILES = os.getenv("KEEP_FILES", "0").strip() == "1"

# Avoid multiple heavy downloads on a phone at the same time.
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1")))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("DDownloaderTelegramBot")


# ----------------------------- Helpers -----------------------------

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(").,]}>\"'")


def is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def classify_url(url: str) -> str:
    lower = url.lower()

    if "youtube.com/" in lower or "youtu.be/" in lower:
        return "youtube"
    if "iq.com/" in lower:
        return "iq"
    if re.search(r"\.mp4(?:$|[?#])", lower):
        return "mp4"
    if re.search(r"\.m3u8(?:$|[?#])", lower):
        return "m3u8"
    if re.search(r"\.mpd(?:$|[?#])", lower):
        return "mpd"
    if re.search(r"\.ism(?:$|[?#])", lower):
        return "ism"

    return "unknown"


def newest_media_file(folder: Path) -> Path | None:
    candidates = [
        p for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {
            ".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".mov", ".ts"
        }
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def run_ffmpeg_public_stream(url: str, output_file: Path) -> Path:
    """
    Download/remux a public or otherwise authorized, non-DRM manifest.

    No DRM keys, mp4decrypt, cookies, or DRM-bypass options are supplied.
    If the source requires unsupported encryption/authentication, ffmpeg
    will fail and the bot will return an error.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found. In Termux run: pkg install ffmpeg"
        )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", url,
        "-map", "0",
        "-c", "copy",
        str(output_file),
    ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60 * 60 * 6,
    )

    if proc.returncode != 0:
        err = (proc.stderr or "ffmpeg failed").strip()
        raise RuntimeError(err[-1800:])

    if not output_file.exists() or output_file.stat().st_size == 0:
        raise RuntimeError("ffmpeg finished but no output file was created.")

    return output_file


def run_ddownloader(url: str, url_type: str, job_dir: Path) -> Path:
    downloader = DOWNLOADER()

    if url_type == "mp4":
        output_file = job_dir / "video.mp4"
        downloader.normal_downloader(url, str(output_file))

    elif url_type == "youtube":
        # Playlists are intentionally disabled for Telegram/Termux use.
        if "list=" in url.lower():
            raise RuntimeError("YouTube playlists are disabled. Send one video URL.")
        output_file = job_dir / "youtube_video.mp4"
        downloader.youtube_downloader(
            url=url,
            output_file=str(output_file),
            download_type="mp4",
            playlist=False,
        )

    else:
        raise RuntimeError(f"Unsupported DDownloader URL type: {url_type}")

    result = newest_media_file(job_dir)
    if result is None:
        # Some library versions may save relative to the process working dir.
        result = newest_media_file(DOWNLOADS_DIR)

    if result is None:
        raise RuntimeError("Download finished, but the output file was not found.")

    return result


def blocking_download(url: str, url_type: str, job_dir: Path) -> Path:
    if url_type in {"m3u8", "mpd", "ism"}:
        return run_ffmpeg_public_stream(url, job_dir / "stream.mp4")

    if url_type in {"mp4", "youtube"}:
        return run_ddownloader(url, url_type, job_dir)

    if url_type == "iq":
        raise RuntimeError(
            "IQ.com handling is disabled in this bot because protected/DRM "
            "streams are not supported."
        )

    raise RuntimeError(
        "Unsupported URL. Send a direct .mp4, public/authorized .m3u8/.mpd/.ism, "
        "or a single YouTube video URL."
    )


def cleanup_job(job_dir: Path) -> None:
    try:
        if job_dir.exists():
            shutil.rmtree(job_dir)
    except Exception:
        logger.exception("Failed to clean job directory: %s", job_dir)


# ----------------------------- Telegram handlers -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Send me a media URL.\n\n"
        "Supported:\n"
        "• direct .mp4\n"
        "• public/authorized .m3u8\n"
        "• public/authorized .mpd\n"
        "• public/authorized .ism\n"
        "• single YouTube video URL\n\n"
        "DRM keys/decryption and protected IQ.com streams are not supported."
    )
    await update.effective_message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    url = extract_url(message.text or "")
    if not url or not is_http_url(url):
        await message.reply_text("Send a valid http/https media URL.")
        return

    url_type = classify_url(url)
    if url_type == "unknown":
        await message.reply_text(
            "Unsupported URL type. Send .mp4, .m3u8, .mpd, .ism, "
            "or a single YouTube video URL."
        )
        return

    if url_type == "iq":
        await message.reply_text(
            "IQ.com protected/DRM stream downloading is disabled in this bot."
        )
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    job_id = f"{chat_id}_{message.message_id}_{uuid.uuid4().hex[:8]}"
    job_dir = DOWNLOADS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status = await message.reply_text("Downloading…")

    try:
        async with DOWNLOAD_SEMAPHORE:
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
            )

            result_path = await asyncio.to_thread(
                blocking_download,
                url,
                url_type,
                job_dir,
            )

        if not result_path.exists():
            raise RuntimeError("Output file does not exist.")

        size = result_path.stat().st_size

        if size > MAX_UPLOAD_BYTES:
            size_mb = size / (1024 * 1024)
            await status.edit_text(
                f"Download completed ({size_mb:.1f} MB), but it is larger than "
                f"this bot's configured upload limit ({MAX_UPLOAD_MB} MB).\n\n"
                f"Saved in Termux at:\n{result_path}"
            )
            return

        await status.edit_text("Download completed. Uploading to Telegram…")
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_DOCUMENT,
        )

        with result_path.open("rb") as media:
            await message.reply_document(
                document=media,
                filename=result_path.name,
                caption="Done ✅",
                read_timeout=600,
                write_timeout=600,
                connect_timeout=60,
                pool_timeout=60,
            )

        await status.delete()

        if not KEEP_FILES:
            cleanup_job(job_dir)

    except subprocess.TimeoutExpired:
        await status.edit_text("Download timed out.")
        cleanup_job(job_dir)

    except Exception as exc:
        logger.exception("Download failed")
        error_text = str(exc).strip() or exc.__class__.__name__
        if len(error_text) > 1800:
            error_text = error_text[-1800:]

        await status.edit_text(
            "Download failed.\n\n"
            f"{error_text}"
        )
        cleanup_job(job_dir)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "Example:\n"
            "export TELEGRAM_BOT_TOKEN='123456:ABC...'"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
