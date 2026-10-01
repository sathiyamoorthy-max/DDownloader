# Combined encrypted-media project module

This project now has one operator CLI combining two concepts: HLS AES-128
segments with playlist-provided key/IV handling, and CENC MP4 processing using
content keys supplied by the operator. Both paths remux, validate metadata,
fully decode audio/video, and publish output only after validation succeeds.
Existing output files are never overwritten; intermediate files are removed.

## Reproducible college demonstration

Install FFmpeg and ffprobe, then run from this repository:

```sh
python encrypted_media.py demo --output-dir ./demo-output
```

This generates a three-second synthetic tone, encrypts it as HLS AES-128 and
CENC AES-CTR, decrypts each, and produces `hls-demo.m4a` and `cenc-demo.m4a`.
Random demo keys and encrypted intermediates are temporary and deleted. No
PocketFM/Kuku account or external license service is involved. Choose a new
output directory on each run, or remove your earlier demo output yourself.

## Operator inputs

Local HLS media playlist with accessible local segments and key file:

```sh
python encrypted_media.py convert --mode hls --input ./series.m3u8 --output ./series.m4a
```

For an authorized HTTP(S) HLS source, explicitly add `--allow-network`. Remote
sources cannot access the local file protocol. Local manifests must be trusted
operator inputs. HLS processing delegates IV and key rotation to FFmpeg rather
than guessing CBC parameters. Only selected media/key filename extensions are
allowed. Cookies and provider sessions are not automatically forwarded by this
CLI. SAMPLE-AES/FairPlay/Widevine license handling is not implemented.

Local CENC MP4 with a private JSON key file mapping 32-hex-digit KIDs to
32-hex-digit content keys:

```sh
python encrypted_media.py convert --mode cenc --input ./encrypted.mp4 --keys-file /private/keys.json --output ./audio.m4a
```

For audio+video use an `.mp4` output. Audio-only output uses `.m4a`; codecs must
be compatible with the container. A single key uses FFmpeg's CENC input option.
Multiple keys require a separately installed Bento4 `mp4decrypt`. This build
does not bundle that binary. Pass already assembled local MP4 files, not an MPD,
to CENC mode. Separate audio/video inputs and DASH segment acquisition are not
implemented. The multi-key Bento4 path has not been end-to-end tested here.

Keep keys outside the repository. Subprocess error messages are not printed;
they can contain signed URLs or secrets. Content keys are passed to the media
executable as arguments and may be visible to other processes under the same
OS account; run this owner-operated CLI in a trusted environment.

## Telegram integration and limits

`/inspect` now distinguishes HLS AES-128, SAMPLE-AES, Widevine, PlayReady,
FairPlay and CENC indicators. A later `METHOD=NONE` no longer hides an earlier
encrypted section. Indicators do not prove a stream is downloadable or prove
the encryption of an unrelated broken M4A.

The new decryption module is an operator CLI, not a Telegram key-upload feature.
The bot still reports protected streams instead of collecting device credentials
or content keys from chat. No CDM extraction, license acquisition, purchases or
automatic unlocking is included. PocketFM/Kuku paid playback remains unverified;
synthetic demo success is not a provider compatibility test.

## Sources and implementation

- https://github.com/e-ave/DRM-Downloader — inspiration for decrypt/verify/remux sequencing.
- https://www.adityathebe.com/download-drm-protected-video/ — inspiration for HLS key/IV and segmented media handling.
- https://ffmpeg.org/ffmpeg-protocols.html — protocol controls.
- https://www.bento4.com/documentation/mp4decrypt/ — multiple KID/key inputs.

This is independently written code. No upstream source, CDM, account cookies,
content keys, binaries or protected media have been copied into the repository.
