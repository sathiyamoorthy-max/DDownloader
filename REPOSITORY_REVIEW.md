# PocketFM repository review

Reviewed the user-supplied repositories on 2026-09-30 through GitHub metadata,
READMEs and file trees, with source inspection for the relevant downloader,
Telegram services and story API client. Repository names alone do not establish
an integration with the Pocket FM service.

| Repository | Observed purpose | Decision for DDownloader |
| --- | --- | --- |
| [ayakix/PocketFM](https://github.com/ayakix/PocketFM) | Android RTL-SDR FM radio receiver | Unrelated to the audiobook service |
| [adarshvinayak/PocketFM](https://github.com/adarshvinayak/PocketFM) | StoryPulse player, investigation portal and writer tools | No downloader integration identified |
| [MikasaAckerman10002/pocketfm](https://github.com/MikasaAckerman10002/pocketfm) | AI character chat and detective game | No catalogue/account API integration identified |
| [shannu1247/PocketFM](https://github.com/shannu1247/PocketFM) | AI contributor for Go repositories | Unrelated to episode downloading |
| [Nadeeem111/Pocketfm](https://github.com/Nadeeem111/Pocketfm) | Default-branch contents returned an empty list; README unavailable | No inspectable implementation to integrate |
| [sajid-da/pocketFM](https://github.com/sajid-da/pocketFM) | EchoVerse AI audio-drama generation, described in README | No downloader integration identified; recursive tree unavailable |
| [battulahemanth/pocketFM](https://github.com/battulahemanth/pocketFM) | React story player; inspected API client targets localhost:5000 | Does not supply the Pocket FM service API |
| [sathiyamoorthy-max/TelegramStoryBot](https://github.com/sathiyamoorthy-max/TelegramStoryBot) | Java/Spring Telegram audio catalogue, stored file IDs and episode ranges | Useful catalogue concepts; existing Python range/pagination covers this need. No service-account unlock API identified in inspected services |
| [Ravikiran-Sunkad/pocketfm-slack-clone](https://github.com/Ravikiran-Sunkad/pocketfm-slack-clone) | Slack-like workspace API | Unrelated |
| [iampawan/pocketfm_downloader](https://github.com/iampawan/pocketfm_downloader) | Account-token downloader with episode selection patterns | Token API approach already considered; added independently implemented star selection to both providers |
| [sethiudit/PocketFM_Splitwise](https://github.com/sethiudit/PocketFM_Splitwise) | Go expense/balance splitting application | Unrelated |
| [nihar7das-gif/PocketFM_Review_Analysis](https://github.com/nihar7das-gif/PocketFM_Review_Analysis) | Play Store review sentiment analysis | No downloader integration |
| [Phazzie/pocketfm-contest-forge](https://github.com/Phazzie/pocketfm-contest-forge) | SvelteKit contest-writing lab | No downloader integration |
| [apriya-gif/hack-backend-pocketfm](https://github.com/apriya-gif/hack-backend-pocketfm) | Minimal FastAPI health-check service | No episode API |

## Implemented improvement

The selection vocabulary documented by `iampawan/pocketfm_downloader` is useful
for continuing a long series. The shared selector now accepts `*`, `*10`, `25*`
and `10*20`, with inclusive endpoints. This is a fresh implementation based on
actual catalogue episode numbers. It rejects malformed patterns, missing
numbers and reversed ranges instead of falling back to ALL. Open-ended ranges
stop at the last loaded episode, so a partial catalogue is still partial.
Existing access flags, authentication scoping and per-episode refresh are
preserved. No upstream source files, binaries or credentials were copied.

The reviewed upstream downloader's old API host and unbounded retry behavior
were not imported. Paid-account API access, actual media transfers and Telegram
uploads remain unverified with the owner's account. These changes add no
phone-number login, purchases or automatic unlocking.
