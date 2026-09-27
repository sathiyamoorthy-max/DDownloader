# DDownloader Render Advanced Telegram Bot

A Render-oriented Telegram media downloader build.

This package intentionally does not import the original `DDownloader` Python
package. That keeps the Render bot isolated from package-import issues and from
platform-specific DRM/decryption binaries.

## Features

- Telegram webhook
- Render `/health` endpoint
- Docker image with ffmpeg
- yt-dlp for public/downloadable media URLs
- public/non-DRM HLS/DASH/ISM fallback through ffmpeg
- `/start`, `/help`, `/status`, `/whoami`
- optional Telegram user-ID allowlist
- global download concurrency limit
- one active download per Telegram user
- rate limiting
- private/local-network URL blocking
- temporary-file cleanup
- automatic Telegram webhook registration on each deploy

## Render setup

1. Render Dashboard -> New -> Blueprint.
2. Select this repository.
3. Enter `TELEGRAM_BOT_TOKEN` when prompted.
4. `ALLOWED_USER_IDS` can initially be left blank.
5. Deploy.
6. Send `/whoami` to the bot.
7. Put the returned numeric ID in Render as `ALLOWED_USER_IDS`.
8. Deploy again.

## DRM

This Render build does not accept DRM keys and does not implement DRM
decryption/circumvention. It is for public or otherwise authorized media.

## Security

Never commit the BotFather token to GitHub. Keep it in Render Environment.
If an old token was exposed publicly, revoke it in BotFather and use a new one.
