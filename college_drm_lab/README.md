# College DRM / Entitlement Cyber Lab Telegram Bot

இந்த project ஒரு **local-only educational cybersecurity lab**. இது PocketFM, Kuku FM, Netflix அல்லது வேறு real paid service-ஐ attack செய்ய வடிவமைக்கப்படவில்லை.

The lab demonstrates the same security ideas with synthetic audio and deliberately vulnerable endpoints:

- broken server-side entitlement validation;
- trusting a client-controlled `client_paid=true` flag;
- predictable playback tokens;
- raw encryption-key exposure;
- decrypting a synthetic AES-GCM protected MP3 after exploiting the lab;
- the patched design: server-side payment/entitlement checks and no key exposure;
- Telegram delivery of both attack-demo and legitimate-demo audio.

## Architecture

```text
Telegram Bot
   |
   +-- /attackdemo
   |      |
   |      +--> POST /vuln/unlock
   |      |       client_paid=true
   |      |
   |      +--> GET /vuln/media/2
   |      +--> GET /vuln/key/2
   |      +--> AES-GCM decrypt
   |      +--> Telegram sendAudio
   |
   +-- /securecheck
   |      |
   |      +--> POST /secure/unlock
   |              attacker has no entitlement
   |              -> 402 payment_required
   |
   +-- /legitdemo
          |
          +--> POST /secure/pay
          +--> POST /secure/unlock
          +--> GET /secure/play/2
          +--> Telegram sendAudio
```

The audio is generated locally with FFmpeg (`sine=660Hz`) and encrypted with a newly generated AES-GCM key. No copyrighted or third-party paid media is included.

## Files

```text
college_drm_lab/
├── crypto_lab.py       # Generates and encrypts synthetic MP3
├── lab_server.py       # Vulnerable + patched Flask APIs
├── telegram_bot.py     # Private Telegram demo bot
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── tests/
    └── test_lab.py
```

CI is in `.github/workflows/college-drm-lab.yml`.

## Requirements

- Python 3.12+
- FFmpeg
- Telegram bot token from BotFather
- Your Telegram numeric user ID

## Run locally with Python

### 1. Install FFmpeg

Ubuntu / Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

### 2. Create a virtual environment

```bash
cd college_drm_lab
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows activation:

```powershell
.venv\Scripts\activate
```

### 3. Start the lab API

```bash
python lab_server.py
```

Check:

```bash
curl http://127.0.0.1:5000/health
```

Expected scope:

```json
{"ok": true, "scope": "local-college-lab"}
```

### 4. Configure Telegram bot

```bash
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=YOUR_BOTFATHER_TOKEN
ALLOWED_USER_IDS=YOUR_TELEGRAM_NUMERIC_ID
LAB_BASE_URL=http://127.0.0.1:5000
LAB_USER=student
```

Never commit the real `.env` file.

Load the variables and run:

Linux/macOS:

```bash
set -a
source .env
set +a
python telegram_bot.py
```

PowerShell:

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^([^#][^=]*)=(.*)$') {
    [Environment]::SetEnvironmentVariable($matches[1], $matches[2])
  }
}
python telegram_bot.py
```

## Run with Docker Compose

Create `.env` first, then:

```bash
docker compose up --build
```

The intentionally vulnerable HTTP service is bound to **127.0.0.1 only** on the host.

## Telegram commands

### `/attackdemo`

Demonstrates the deliberately vulnerable flow:

1. Bot submits `client_paid=true`.
2. Vulnerable server trusts the client instead of checking a transaction.
3. It returns a predictable playback token.
4. The same token can retrieve the encrypted synthetic MP3.
5. The intentionally vulnerable key endpoint returns AES-GCM key material.
6. Bot decrypts the synthetic MP3.
7. Telegram receives the recovered lab audio.

Expected message:

```text
Vulnerable local lab exploited.
Client-paid flag trusted -> entitlement bypassed.
Playback token exposed raw lab key -> sample decrypted.
```

### `/securecheck`

Sends the same fake-unlock concept to the patched API using the zero-credit `attacker` identity.

Expected result:

```text
402 payment_required
```

The secure endpoint ignores the client `client_paid` value and relies on server-side entitlements.

### `/legitdemo`

Demonstrates the correct flow:

```text
server-side lab credit
    -> entitlement
    -> authorized playback
    -> Telegram audio
```

The secure flow never returns the raw AES key to the Telegram client.

## Run tests

```bash
pytest -q
```

The tests validate:

- health endpoint;
- vulnerable fake-payment bypass;
- vulnerable key exposure and successful decryption of the synthetic audio;
- patched API rejecting fake entitlement;
- legitimate server-side payment and playback.

## HOD demo sequence

For a short college presentation:

```text
1. Explain paid-content entitlement.
2. /attackdemo
   -> show broken client-trust design.
3. Explain why raw key exposure is dangerous.
4. /securecheck
   -> show 402 payment_required.
5. /legitdemo
   -> show server-side authorization and audio delivery.
6. Show the tests and CI.
7. Explain mitigations.
```

## Security lessons

The vulnerable design intentionally violates normal security rules. A production service should:

- never trust payment or entitlement state supplied by the client;
- validate ownership/entitlement on the server for every protected playback request;
- use high-entropy, short-lived, audience-scoped playback credentials;
- avoid exposing raw content-encryption keys to ordinary application clients;
- rate-limit and audit entitlement and playback endpoints;
- bind sensitive sessions to appropriate account/device/session context;
- keep secrets out of logs and source control;
- reject replayed/expired tokens;
- use HTTPS in production.

## Safety boundary

`telegram_bot.py` deliberately refuses arbitrary internet targets. `LAB_BASE_URL` must resolve to one of:

- `localhost`
- `127.0.0.1`
- `::1`
- Docker Compose service `lab-server`

There is intentionally **no command accepting a PocketFM/OTT URL, cookie, token, PSSH, license URL, WVD/CDM file, or third-party decryption key**.

This keeps the project suitable for a cybersecurity demonstration while still showing the vulnerable and secure flows end to end.
