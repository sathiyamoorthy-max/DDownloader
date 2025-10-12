import os, re, logging, coloredlogs, time, json
from pathlib import Path
from colorama import Fore, Style
from DDownloader.modules.helper import download_binaries, detect_platform, get_media_info
from DDownloader.modules.args_parser import parse_arguments
from DDownloader.modules.banners import clear_and_print, display_help
from DDownloader.modules.downloader import DOWNLOADER

logger = logging.getLogger("+ MAIN + ")
coloredlogs.install(level='DEBUG', logger=logger)

# =========================================================================================================== #

def validate_directories():
    downloads_dir = 'downloads'
    if not os.path.exists(downloads_dir):
        os.makedirs(downloads_dir)
        logger.debug(f"Created '{downloads_dir}' directory.")
    return downloads_dir

# =========================================================================================================== #

def process_media_info(directory="downloads", log_dir="logs"):
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
        logger.info(f"Created logs directory: {log_dir}")

    if not os.path.exists(directory):
        logger.error(f"Directory '{directory}' does not exist. Please create it and add media files.")
        return

    mp4_files = [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".mp4")]

    if not mp4_files:
        logger.info(f"No .mp4 files found in directory: {directory}")
        return

    logger.info(f"Found {len(mp4_files)} .mp4 file(s) in '{directory}'. Processing...")

    for file_path in mp4_files:
        try:
            media_info = get_media_info(file_path)
            if media_info:
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                log_file_path = os.path.join(log_dir, f"{base_name}.log")
                with open(log_file_path, "w", encoding="utf-8") as log_file:
                    json.dump(media_info, log_file, indent=4)
                logger.info(f"Saved media information to: {log_file_path}")
                print(Fore.RED + "═" * 100 + Fore.RESET + "\n")

        except Exception as e:
            logger.error(f"Failed to process {file_path}: {e}")

# =========================================================================================================== #

def main():
    clear_and_print()
    platform_name = detect_platform()
    logger.info("Please be patient...")
    print(Fore.RED + "═" * 100 + Fore.RESET)
    time.sleep(1)
    bin_dir = Path(__file__).resolve().parent / "bin"
    download_binaries(bin_dir, platform_name)
    clear_and_print()

    downloads_dir = validate_directories()
    try:
        args = parse_arguments()
    except SystemExit:
        display_help()
        exit(1)

    downloader = DOWNLOADER()
    downloader.auto_select = args.auto_select

    if args.url:
        if re.search(r"\.mpd\b", args.url, re.IGNORECASE):
            logger.info("DASH stream detected. Initializing DASH downloader...")
        elif re.search(r"\.m3u8\b", args.url, re.IGNORECASE):
            logger.info("HLS stream detected. Initializing HLS downloader...")
        elif re.search(r"\.ism\b", args.url, re.IGNORECASE):
            logger.info("ISM (Smooth Streaming) detected. Initializing ISM downloader...")
        elif re.search(r"\.mp4\b", args.url, re.IGNORECASE):
            logger.info("MP4 file detected. Processing media information...")
            print(Fore.RED + "═" * 100 + Fore.RESET)
            downloader.normal_downloader(args.url, os.path.join(downloads_dir, args.output))
            exit(1)
        elif re.search(r"(youtube\.com|youtu\.be)", args.url, re.IGNORECASE):
            logger.info("YouTube URL detected. Initializing YouTube downloader...")
            print(Fore.RED + "═" * 100 + Fore.RESET)
            is_playlist = "list=" in args.url
            downloader.cookies = args.cookies
            downloader.youtube_downloader(
                url=args.url,
                output_file=os.path.join(downloads_dir, args.output),
                download_type="mp4",
                playlist=is_playlist
            )
            exit(0)
        elif re.search(r"iq\.com", args.url, re.IGNORECASE):
            logger.info("IQ.com URL detected. Initializing IQ.com downloader...")
            print(Fore.RED + "═" * 100 + Fore.RESET)
            # Set default output name if not provided
            output_name = args.output if args.output else "iq_video"
            downloader.cookies = args.cookies
            downloader.iq_downloader(
                url=args.url,
                output_file=os.path.join(downloads_dir, output_name),
                download_type="mp4"
            )
            exit(0)
        else:
            logger.error("Unsupported URL format. Please provide a valid DASH (.mpd), HLS (.m3u8), ISM (.ism), YouTube, or IQ.com URL.")
            exit(1)

        downloader.manifest_url = args.url
        downloader.output_name = args.output
        downloader.decryption_keys = args.key or []
        downloader.headers = args.header or []
        downloader.proxy = args.proxy

        if downloader.proxy:
            if not downloader.proxy.startswith("http://"):
                downloader.proxy = f"http://{downloader.proxy}"
            logger.info(f"Proxy: {downloader.proxy}")
            print(Fore.RED + "═" * 100 + Fore.RESET + "\n")

        if downloader.headers:
            logger.info("Headers:")
            for header in downloader.headers:
                logger.info(f"  - {header}")
            print(Fore.RED + "═" * 100 + Fore.RESET + "\n")

        if downloader.decryption_keys:
            logger.info("Decryption keys:")
            for key in downloader.decryption_keys:
                logger.info(f"  - {key}")
            print(Fore.RED + "═" * 100 + Fore.RESET + "\n")
            
        if downloader.auto_select:
            logger.info("Auto-select enabled - will choose best quality automatically")
            print(Fore.RED + "═" * 100 + Fore.RESET + "\n")

        try:
            downloader.drm_downloader()
        except Exception as e:
            logger.error(f"An error occurred during the download process: {e}")
            exit(1)

        process_media_info(downloads_dir)

    if args.input and args.quality:
        logger.info(f"Starting re-encode process for {args.input} to {args.quality.upper()} quality...")
        output_file = downloader.re_encode_content(
            input_file=args.input,
            quality=args.quality,
            codec="libx265",
            crf=20,
            preset="medium"
        )

        if output_file:
            logger.info(f"Re-encoding completed successfully! Output saved to: {output_file}")
        else:
            logger.error("Re-encoding failed.")
            exit(1)

# =========================================================================================================== #

if __name__ == "__main__":
    main()
