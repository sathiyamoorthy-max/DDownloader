"""Download preflight checks; never terminate the Telegram process."""
import shutil
import tempfile
from pathlib import Path


def system_status(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    return {'missing': [x for x in ('ffmpeg', 'ffprobe') if not shutil.which(x)],
            'free_mb': shutil.disk_usage(folder).free // (1024 * 1024)}


def check_download_environment(folder, minimum_mb=256):
    status = system_status(folder)
    if status['missing']:
        raise RuntimeError('Missing binaries: ' + ', '.join(status['missing']))
    if status['free_mb'] < minimum_mb:
        raise RuntimeError(f"Insufficient disk space: {status['free_mb']} MB free; {minimum_mb} MB reserve required.")
    try:
        with tempfile.TemporaryFile(dir=folder) as test:
            test.write(b'check')
            test.flush()
    except OSError as exc:
        raise RuntimeError('The download folder is not writable.') from exc
    return status
