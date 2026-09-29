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


## Authorized non-DRM downloads

The bot can use credentials stored only in Render Environment for explicitly
allowlisted domains:

- `AUTH_DOMAINS`
- `AUTH_COOKIE`
- `AUTHORIZATION_HEADER`
- `AUTH_REFERER`

Authenticated requests are scoped per redirect hop: authorization values are
recalculated for the destination host and are not forwarded to hosts that are
not listed in `AUTH_DOMAINS`.

This mode is for media the user is already authorized to access and that does
not require DRM decryption. It can authenticate normal webpage/direct-media
requests and download openly exposed non-DRM media URLs. It does not obtain
licenses or keys, bypass entitlements/paywalls, or decrypt protected streams.

Use `/authstatus` in Telegram to verify that the configuration is present
without printing any secret values.


## PocketFM full-catalogue batch

Send a /show/ URL, wait for the catalogue count, then send:

- `ALL`: every listed episode in one sequential batch, one audio file per episode.
- `7`: one episode by its actual number.
- `1-15` or `1 15`: an inclusive range.
- `/cancel`: stop after the current episode finishes.

The parser reads the public webpage's embedded episode data and follows the
website's read-only Load more action. It reports the number listed versus the
show total and warns if pagination fails. ALL never silently means only the
first 20 or 500 episodes. A failed episode does not stop the remaining batch;
the final message reports successful, failed and unattempted counts.

Catalogue visibility does not guarantee media access. Locked episodes and
unavailable/protected media fail individually. Episode extraction selects only
the requested story, preventing accidental downloads of recommended episodes.
OneLink store/app redirects are handled without attempting a non-HTTP request;
a share link that contains no episode destination still needs an episode URL.

Selection expires after one hour. Selection and active batches are in memory:
a Render restart/deploy interrupts them. Large batches can take many hours.
This is a download batch, not a backup of PocketFM account state or a merged
single audio file.

Run offline regression tests with `python -m unittest discover -s tests -v`.



## Episode availability labels

A show URL now displays Public / Locked / Unknown totals and the first page of
per-episode titles and labels. Use `/episodes 2`, `/episodes 3`, etc. to browse
20 entries per page. Standard, www and language-prefixed show URLs are accepted.

Labels come from explicit episode access metadata, not from the presence of a
media URL. Missing or conflicting metadata is Unknown. If the show request uses
configured Cookie or Authorization headers, the label is Available (session),
not Public, and the UI says that login has not been verified. Without credentials,
the catalogue is the public website view and does not reflect app purchases.
An explicit unlocked flag takes precedence over a nonzero coin price.

This bot does not implement phone/OTP login. Existing domain-scoped environment
credentials can be used for authorized webpage requests, but PocketFM account
login, app/web entitlement synchronization, and paid media downloads have not
been verified. Never post OTPs or session cookies to Telegram, GitHub or a chat.

## Combined PocketFM + Kuku FM bot

The Series button accepts either provider's full show URL. Both use the same
`ALL`, episode/range, `/episodes N` and `/cancel` flow. Kuku FM episode URLs are
refreshed from their catalogue page before each download, so expired signed media
links and changed account access are checked again. Existing Telegram audio cover,
title, compression and upload handling are reused.

Kuku FM uses `KUKU_COOKIE`, a raw HTTP Cookie header stored only in the service's
secret environment settings (not a Netscape cookies.txt file). Set
`ALLOWED_USER_IDS` to the owner's Telegram user ID before using this private
session. No cookie is needed to attempt publicly returned catalogue metadata.
A PocketFM purchase does not grant Kuku FM access; use your own Kuku FM account.
`/authstatus` reports configuration presence, not verified account login.

KUKU_COOKIE is attached only to HTTPS requests for kukufm.com / www.kukufm.com.
It is not forwarded to PocketFM or media CDNs. The adapter supports media URLs
returned to that session through the existing non-decrypting downloader. It does
not support DRM decryption, app-only short links, buying/unlocking episodes,
or media requiring extra CDN authentication. Missing access flags remain Unknown.

`/system` reports ffmpeg/ffprobe availability and free disk space. Before every
download, the bot checks both binaries, folder writability and the free-space
reserve set by `MIN_FREE_DISK_MB` (default 256). This reserve is a preflight check,
not a guarantee that an arbitrarily large episode will fit. Failures are reported
in Telegram without exiting the bot process.

The Kuku adapter was independently implemented against the API shape described
in https://github.com/bunnykek/kuku-dl and verified against a public catalogue
response. Upstream cookies, executables and source files are not bundled.
Paid account playback and end-to-end Telegram uploads still require testing
with the owner's privately configured session.

## Direct API selection for both services

Kuku FM uses its `/api/v2.3/channels/{show}/episodes/` endpoint.
PocketFM show links now try `https://api.pocketfm.com/v2/content_api/show.get_details`
first. Set `POCKETFM_ACCESS_TOKEN` privately in the service environment to request
your account's catalogue; the bot sends it as the `access-token` header only to
that exact HTTPS host and endpoint. `ALLOWED_USER_IDS` is required for account
access. Do not paste token values into chat, GitHub or the repository.

Without a PocketFM token, API failure falls back to the public website catalogue
with an explicit guest-source notice. With a token, an API failure is reported
rather than silently substituting guest results. API-selected episode metadata
and URLs are refreshed before download, preserving lock checks and episode IDs.
`/authstatus` reports whether both providers' credentials are configured; this is
not a validation of login or entitlement. Individual PocketFM episode links still
use the existing webpage flow; the direct API flow is for show selections.

PocketFM's account API integration has offline fixture coverage but still needs
validation with a real owner-provided token. The older web.pocketfm.com host
returned HTTP 502 in this environment. No account credentials were supplied or
configured during development, and no deployment was performed.
