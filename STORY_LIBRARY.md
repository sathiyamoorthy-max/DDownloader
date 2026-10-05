# Saved stories and clean show links

Open a PocketFM or Kuku FM show in the bot, then send `/save`.

| Command | Behavior |
| --- | --- |
| `/gen <PocketFM show ID or provider show URL>` | Returns a canonical HTTPS show link without query/fragment credentials. Does not create an app deep link or unlock anything. |
| `/save` | Bookmarks the current series from this chat. |
| `/saved [page]` | Lists up to 10 bookmarks per page. |
| `/search <title>` | Searches this user's saved stories in this chat. This is not provider-wide search. |
| `/open <saved ID>` | Reloads the provider catalogue and current access information. Does not start downloading. |
| `/forget <saved ID>` | Removes one bookmark belonging to the caller in this chat. |

Bookmarks share the SQLite file configured with `BATCH_STATE_PATH`. They survive
process restarts on the same disk; Render's ephemeral filesystem does not
preserve them across redeploys. Use a persistent disk and point that variable
inside its mount to retain them across redeploys. Each user/chat has a limit of
200 saved stories. Titles and canonical show URLs are stored, not media URLs,
cookies, Telegram init data, access labels or tokens. Commands follow the bot's
existing allowlist. In group chats, replies are visible to group members.

The existing PocketFM and Kuku adapters, download ranges, media metadata, and
retry/resume workflow are reused. The reference project
https://github.com/iampawan/pocketfm_downloader was reviewed for overlap; this
update does not vendor its CLI or claim compatibility with another bot's private
backend. Automatic unlock, bonus claims, scheduled subscriptions, custom audio
metadata and album delivery are not added by this update.

Validation: `python -m unittest discover -s tests` includes persistence,
credential stripping, owner/chat isolation, pagination, limits and bot command
dispatch without contacting Telegram or a paid provider account.
