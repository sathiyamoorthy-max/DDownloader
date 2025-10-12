import os
import requests
from tqdm import tqdm
from colorama import Fore, Style, init
import logging
import coloredlogs
import platform
from pymediainfo import MediaInfo

init(autoreset=True)

logger = logging.getLogger(Fore.GREEN + "+ HELPER + ")
coloredlogs.install(level='DEBUG', logger=logger)

# =========================================================================================================== #

binaries = {
    "Windows": [
        "https://github.com/ThatNotEasy/DDownloader/raw/refs/heads/main/DDownloader/bin/N_m3u8DL-RE.exe",
        "https://github.com/ThatNotEasy/DDownloader/raw/refs/heads/main/DDownloader/bin/ffmpeg.exe",
        "https://github.com/ThatNotEasy/DDownloader/raw/refs/heads/main/DDownloader/bin/aria2c.exe",
        "https://github.com/ThatNotEasy/DDownloader/raw/refs/heads/main/DDownloader/bin/mp4decrypt.exe",
        "https://github.com/ThatNotEasy/DDownloader/raw/refs/heads/main/DDownloader/bin/shaka-packager.exe",
        "https://github.com/ThatNotEasy/DDownloader/raw/refs/heads/main/DDownloader/bin/mkvmerge.exe",
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    ],
    "Linux": [
        "https://github.com/ThatNotEasy/DDownloader/raw/refs/heads/main/DDownloader/bin/N_m3u8DL-RE",
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
    ]
}

# =========================================================================================================== #

def download_binaries(bin_dir, platform_name):
    os.makedirs(bin_dir, exist_ok=True)
    logger.info(f"Platform detected: {platform_name}")
    logger.info(f"Using binary directory: {bin_dir}")
    
    platform_binaries = binaries.get(platform_name, [])
    
    if not platform_binaries:
        logger.error(f"No binaries available for platform: {platform_name}")
        return

    for binary_url in platform_binaries:
        try:
            filename = binary_url.split("/")[-1]
            filepath = os.path.join(bin_dir, filename)

            if os.path.exists(filepath):
                logger.info(f"{Style.BRIGHT}{Fore.YELLOW}Skipping {filename} (already exists).")
                continue

            logger.info(f"{Fore.GREEN}Downloading {Fore.WHITE}{filename}...{Fore.RESET}")
            response = requests.get(binary_url, stream=True, timeout=30)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            with open(filepath, "wb") as file, tqdm(
                total=total_size,
                unit='B',
                unit_scale=True,
                desc=f"{Fore.CYAN}{filename}{Fore.RESET}",
                dynamic_ncols=True,
                bar_format="{l_bar}{bar} | {n_fmt}/{total_fmt} [{rate_fmt}]"
            ) as progress_bar:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
                    progress_bar.update(len(chunk))

            if platform_name == "Linux":
                os.chmod(filepath, 0o755)
        except requests.exceptions.RequestException as e:
            logger.error(f"{Fore.RED}Failed to download {binary_url}: {e}{Fore.RESET}")
        except Exception as e:
            logger.error(f"{Fore.RED}Unexpected error for {binary_url}: {e}{Fore.RESET}")

# =========================================================================================================== #

def detect_platform():
    system_platform = platform.system().lower()
    if system_platform == 'windows':
        return 'Windows'
    elif system_platform == 'linux':
        return 'Linux'
    elif system_platform == 'darwin':
        return 'MacOS'
    else:
        return 'Unknown'

# =========================================================================================================== #

def get_media_info(file_path):
    try:
        logger.info(f"📂 Parsing media file: {file_path}")
        media_info = MediaInfo.parse(file_path)

        result = {
            "file_path": file_path,
            "tracks": [],
            "container": None,
            "file_size": None,
            "duration": None,
            "bit_rate": None,
        }

        for track in media_info.tracks:
            track_info = {"track_type": track.track_type}

            if track.track_type == "General":
                result.update({
                    "container": getattr(track, "format", None),
                    "file_size": getattr(track, "file_size", None),
                    "duration": getattr(track, "duration", None),
                    "bit_rate": getattr(track, "overall_bit_rate", None),
                    "title": getattr(track, "title", None),
                    "encoded_application": getattr(track, "encoded_application", None),
                    "encoded_library": getattr(track, "encoded_library", None),
                    "writing_library": getattr(track, "writing_library", None),
                    "file_creation_date": getattr(track, "file_created_date", None),
                })

            elif track.track_type == "Video":
                track_info.update({
                    "codec": getattr(track, "codec_id", getattr(track, "format", None)),
                    "codec_profile": getattr(track, "format_profile", None),
                    "width": getattr(track, "width", None),
                    "height": getattr(track, "height", None),
                    "frame_rate": getattr(track, "frame_rate", None),
                    "bit_rate": getattr(track, "bit_rate", None),
                    "duration": getattr(track, "duration", None),
                    "aspect_ratio": getattr(track, "display_aspect_ratio", None),
                    "hdr_format": getattr(track, "hdr_format", None),
                    "bit_depth": getattr(track, "bit_depth", None),
                    "color_space": getattr(track, "colour_primaries", None),
                    "color_range": getattr(track, "colour_range", None),
                    "color_transfer": getattr(track, "transfer_characteristics", None),
                    "chroma_subsampling": getattr(track, "chroma_subsampling", None),
                })

            elif track.track_type == "Audio":
                track_info.update({
                    "codec": getattr(track, "codec_id", getattr(track, "format", None)),
                    "codec_profile": getattr(track, "format_profile", None),
                    "channels": getattr(track, "channel_s", None),
                    "sample_rate": getattr(track, "sampling_rate", None),
                    "bit_rate": getattr(track, "bit_rate", None),
                    "duration": getattr(track, "duration", None),
                    "language": getattr(track, "language", "Unknown"),
                    "compression_mode": getattr(track, "compression_mode", None),
                    "bit_depth": getattr(track, "bit_depth", None),
                })

            elif track.track_type == "Text":
                track_info.update({
                    "format": getattr(track, "format", None),
                    "language": getattr(track, "language", "Unknown"),
                    "default": getattr(track, "default", None),
                    "forced": getattr(track, "forced", None),
                    "format_profile": getattr(track, "format_profile", None),
                })

            elif track.track_type == "Chapters":
                track_info.update({
                    "title": getattr(track, "title", None),
                    "chapter_count": getattr(track, "part_count", None),
                })

            if any(value is not None for value in track_info.values()):  # Avoid empty entries
                result["tracks"].append(track_info)

        logger.info(f"✅ Successfully extracted media info for: {file_path}")
        return result

    except Exception as e:
        logger.error(f"❌ Error parsing media file '{file_path}': {e}")
        return None

    
# =========================================================================================================== #
