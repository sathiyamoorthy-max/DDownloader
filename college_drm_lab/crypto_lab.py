from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENCRYPTED_PATH = DATA_DIR / "episode2.enc"
KEY_PATH = DATA_DIR / "episode2_key.json"
AAD = b"college-drm-lab:episode:2"


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is required to generate the lab audio sample")
    return exe


def ensure_sample() -> None:
    """Create a tiny synthetic MP3 and encrypt it for the local lab."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if ENCRYPTED_PATH.exists() and KEY_PATH.exists():
        return

    clear_path = DATA_DIR / "episode2_clear.mp3"
    subprocess.run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=6",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(clear_path),
        ],
        check=True,
    )

    clear = clear_path.read_bytes()
    key = AESGCM.generate_key(bit_length=128)
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, clear, AAD)

    ENCRYPTED_PATH.write_bytes(encrypted)
    KEY_PATH.write_text(
        json.dumps(
            {
                "key_b64": base64.b64encode(key).decode("ascii"),
                "nonce_b64": base64.b64encode(nonce).decode("ascii"),
                "aad_b64": base64.b64encode(AAD).decode("ascii"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    clear_path.unlink(missing_ok=True)


def load_key_material() -> tuple[bytes, bytes, bytes]:
    ensure_sample()
    raw = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    return (
        base64.b64decode(raw["key_b64"]),
        base64.b64decode(raw["nonce_b64"]),
        base64.b64decode(raw["aad_b64"]),
    )


def key_material_json() -> dict[str, str]:
    ensure_sample()
    return json.loads(KEY_PATH.read_text(encoding="utf-8"))


def decrypt_blob(blob: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, blob, aad)


def decrypt_episode_bytes() -> bytes:
    ensure_sample()
    key, nonce, aad = load_key_material()
    return decrypt_blob(ENCRYPTED_PATH.read_bytes(), key, nonce, aad)
