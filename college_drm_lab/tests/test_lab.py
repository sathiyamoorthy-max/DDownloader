from __future__ import annotations

import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_server import USERS, app, reset_lab_state


def setup_function():
    reset_lab_state()


def test_health():
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["scope"] == "local-college-lab"


def test_vulnerable_fake_payment_flag_unlocks_paid_episode():
    client = app.test_client()

    response = client.post(
        "/vuln/unlock",
        json={"episode_id": 2, "client_paid": True},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["unlocked"] is True
    assert body["episode"]["paid"] is True
    assert body["media_token"]


def test_vulnerable_flow_exposes_key_and_decrypts_sample():
    client = app.test_client()

    unlock = client.post(
        "/vuln/unlock",
        json={"episode_id": 2, "client_paid": True},
    ).get_json()
    token = unlock["media_token"]

    media = client.get(
        "/vuln/media/2",
        query_string={"token": token},
    )
    key_response = client.get(
        "/vuln/key/2",
        query_string={"token": token},
    )

    assert media.status_code == 200
    assert key_response.status_code == 200

    material = key_response.get_json()
    clear = AESGCM(base64.b64decode(material["key_b64"])).decrypt(
        base64.b64decode(material["nonce_b64"]),
        media.data,
        base64.b64decode(material["aad_b64"]),
    )

    assert len(clear) > 100
    assert clear[:3] == b"ID3" or clear[0] == 0xFF


def test_secure_endpoint_rejects_fake_unlock():
    client = app.test_client()

    response = client.post(
        "/secure/unlock",
        headers={"X-Lab-User": "attacker"},
        json={"episode_id": 2, "client_paid": True},
    )

    assert response.status_code == 402
    assert response.get_json()["error"] == "payment_required"


def test_secure_payment_creates_entitlement_without_exposing_key():
    client = app.test_client()
    headers = {"X-Lab-User": "student"}

    payment = client.post(
        "/secure/pay",
        headers=headers,
        json={"episode_id": 2},
    )
    assert payment.status_code == 200
    assert USERS["student"]["credits"] == 0

    unlock = client.post(
        "/secure/unlock",
        headers=headers,
        json={"episode_id": 2, "client_paid": True},
    )
    assert unlock.status_code == 200
    assert "key" not in str(unlock.get_json()).lower()

    playback = client.get(
        "/secure/play/2",
        headers=headers,
    )
    assert playback.status_code == 200
    assert playback.mimetype == "audio/mpeg"
    assert len(playback.data) > 100
