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


## PocketFM merged workflow

The Render bot now keeps the general DDownloader URL workflow and also includes
the safe/public parts of the PocketFM bot workflow:

- Public PocketFM episode page metadata and openly exposed media-link extraction
- Multiple PocketFM episode URLs in one Telegram message
- Public show-page episode discovery with single/range selection
- Title/thumbnail handling
- Audio compression when the result is above the configured Telegram upload limit
- Existing live download/upload progress and `/status`
- Existing `/inspect <url>` manifest/DRM diagnostics

The original `sathiyamoorthy-max/Pocket-` repository is not modified by this
merge. DRM key extraction, decryption, and protection bypass are not included.

A rollback point was created before the merge on branch
`pre-pocket-merge-20260927`.


## Larger single-file Telegram uploads

The bot supports a configurable Telegram Bot API endpoint through:

- `TELEGRAM_API_BASE_URL`
- `MAX_UPLOAD_MB`

With the default hosted endpoint (`https://api.telegram.org`), the bot automatically
clamps its effective upload limit to 49 MB. With a Local Telegram Bot API Server,
set `TELEGRAM_API_BASE_URL` to that server and `MAX_UPLOAD_MB` to the desired
limit (for example `100`). Audio remains a single file; compression is only used
when the file exceeds the active configured limit.

Do not put Bot API server credentials or Telegram API credentials in the repository.
Keep them in Render Environment/Secrets.
