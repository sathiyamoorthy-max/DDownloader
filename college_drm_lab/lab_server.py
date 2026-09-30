from __future__ import annotations

import os
from io import BytesIO

from flask import Flask, jsonify, request, send_file

from crypto_lab import (
    ENCRYPTED_PATH,
    decrypt_episode_bytes,
    ensure_sample,
    key_material_json,
)

app = Flask(__name__)

EPISODES = {
    1: {"id": 1, "title": "Free Lab Episode", "paid": False},
    2: {"id": 2, "title": "Paid Lab Episode", "paid": True},
}

USERS = {
    "student": {
        "credits": 1,
        "entitlements": set(),
    },
    "attacker": {
        "credits": 0,
        "entitlements": set(),
    },
}


def reset_lab_state() -> None:
    USERS["student"]["credits"] = 1
    USERS["student"]["entitlements"].clear()
    USERS["attacker"]["credits"] = 0
    USERS["attacker"]["entitlements"].clear()


def _episode_or_404(episode_id: int):
    episode = EPISODES.get(episode_id)
    if not episode:
        return None, (jsonify({"error": "episode_not_found"}), 404)
    return episode, None


def _vuln_token(episode_id: int) -> str:
    # Intentionally predictable for the local educational lab.
    return f"vuln-{episode_id}-demo-token"


def _valid_vuln_token(episode_id: int) -> bool:
    return request.args.get("token") == _vuln_token(episode_id)


def _lab_user():
    name = request.headers.get("X-Lab-User", "")
    return name, USERS.get(name)


@app.get("/health")
def health():
    return jsonify({"ok": True, "scope": "local-college-lab"})


@app.get("/api/catalog")
def catalog():
    return jsonify({"episodes": list(EPISODES.values())})


@app.post("/vuln/unlock")
def vulnerable_unlock():
    """
    INTENTIONAL VULNERABILITY:
    The server trusts a client-controlled payment flag.
    This exists only to demonstrate broken entitlement validation.
    """
    payload = request.get_json(silent=True) or {}
    episode_id = int(payload.get("episode_id", 0))
    episode, error = _episode_or_404(episode_id)
    if error:
        return error

    client_says_paid = bool(payload.get("client_paid", False))
    if episode["paid"] and not client_says_paid:
        return jsonify({"error": "payment_required"}), 402

    return jsonify(
        {
            "unlocked": True,
            "episode": episode,
            "media_token": _vuln_token(episode_id),
            "warning": "intentionally vulnerable local lab endpoint",
        }
    )


@app.get("/vuln/media/<int:episode_id>")
def vulnerable_media(episode_id: int):
    episode, error = _episode_or_404(episode_id)
    if error:
        return error
    if episode_id != 2 or not _valid_vuln_token(episode_id):
        return jsonify({"error": "invalid_token"}), 403

    ensure_sample()
    return send_file(
        ENCRYPTED_PATH,
        mimetype="application/octet-stream",
        download_name="paid_episode.enc",
    )


@app.get("/vuln/key/<int:episode_id>")
def vulnerable_key(episode_id: int):
    """
    INTENTIONAL VULNERABILITY:
    A playback token is incorrectly sufficient to retrieve raw key material.
    """
    if episode_id != 2 or not _valid_vuln_token(episode_id):
        return jsonify({"error": "invalid_token"}), 403

    return jsonify(
        {
            **key_material_json(),
            "warning": "raw key exposure is intentional in this local lab",
        }
    )


@app.post("/secure/unlock")
def secure_unlock():
    payload = request.get_json(silent=True) or {}
    episode_id = int(payload.get("episode_id", 0))
    episode, error = _episode_or_404(episode_id)
    if error:
        return error

    user_name, user = _lab_user()
    if not user:
        return jsonify({"error": "unknown_lab_user"}), 401

    if episode["paid"] and episode_id not in user["entitlements"]:
        return jsonify({"error": "payment_required"}), 402

    return jsonify(
        {
            "unlocked": True,
            "episode": episode,
            "playback_path": f"/secure/play/{episode_id}",
            "user": user_name,
        }
    )


@app.post("/secure/pay")
def secure_pay():
    payload = request.get_json(silent=True) or {}
    episode_id = int(payload.get("episode_id", 0))
    episode, error = _episode_or_404(episode_id)
    if error:
        return error

    user_name, user = _lab_user()
    if not user:
        return jsonify({"error": "unknown_lab_user"}), 401

    if episode_id in user["entitlements"]:
        return jsonify({"paid": True, "already_entitled": True, "credits": user["credits"]})

    if episode["paid"]:
        if user["credits"] < 1:
            return jsonify({"error": "insufficient_credits"}), 402
        user["credits"] -= 1
        user["entitlements"].add(episode_id)

    return jsonify(
        {
            "paid": True,
            "episode_id": episode_id,
            "credits": user["credits"],
            "user": user_name,
        }
    )


@app.get("/secure/play/<int:episode_id>")
def secure_play(episode_id: int):
    episode, error = _episode_or_404(episode_id)
    if error:
        return error

    _user_name, user = _lab_user()
    if not user:
        return jsonify({"error": "unknown_lab_user"}), 401

    if episode["paid"] and episode_id not in user["entitlements"]:
        return jsonify({"error": "payment_required"}), 402

    if episode_id != 2:
        return jsonify({"error": "sample_only_available_for_episode_2"}), 404

    # The patched path never exposes the raw encryption key to the client.
    clear = decrypt_episode_bytes()
    return send_file(
        BytesIO(clear),
        mimetype="audio/mpeg",
        download_name="paid_episode.mp3",
    )


if __name__ == "__main__":
    ensure_sample()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
