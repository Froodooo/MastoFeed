# MastoFeed

A tiny, dependency-free service that watches an RSS or Atom feed and posts new entries to Mastodon and, optionally, Bluesky. It can also send outgoing [webmentions](https://www.w3.org/TR/webmention/) to the pages your entries link to.

- Posts `title`, a blank line and the link, so both platforms show a link preview.
- On Bluesky the link is clickable and a link card (title, description and thumbnail from the page's Open Graph tags) is attached.
- Optionally sends webmentions for the links in each new entry.
- Remembers what it has posted in a small SQLite database, so restarts never cause duplicates.
- On the first run it only records the existing entries and posts nothing, so your back catalogue is not flooded onto your timeline.
- Uses the standard library only. No `pip install` needed.

## Quick start with Docker Compose

1. Copy [docker-compose.yml](docker-compose.yml) and [.env.example](.env.example) to your server and rename the latter to `.env`.
2. Fill in `.env` (see below).
3. Start it:

   ```
   docker compose up -d
   ```

The image is published to `ghcr.io/froodooo/mastofeed:latest` on every push to `main`. To update later:

```
docker compose pull && docker compose up -d
```

The database lives in the `mastofeed-data` volume, so it survives updates.

## Configuration

All settings are environment variables, normally set in `.env`.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `FEED_URL` | yes | | RSS or Atom feed to watch. |
| `MASTODON_URL` | yes | | Your instance, for example `https://mastodon.social`. |
| `MASTODON_TOKEN` | yes | | Access token with the `write:statuses` scope. |
| `POLL_INTERVAL` | no | `900` | Seconds between checks. |
| `VISIBILITY` | no | `public` | Mastodon visibility: `public`, `unlisted`, `private` or `direct`. |
| `BLUESKY_HANDLE` | no | | Your handle, for example `you.bsky.social`. |
| `BLUESKY_APP_PASSWORD` | no | | A Bluesky app password (not your main password). |
| `BLUESKY_SERVICE` | no | `https://bsky.social` | Only change this on a self-hosted server. |
| `WEBMENTION` | no | off | Set to `true` to send outgoing webmentions. |
| `WEBMENTION_MAX_LINKS` | no | `20` | Maximum number of links per entry to notify. |
| `WEBMENTION_IGNORE_DOMAINS` | no | | Comma-separated domains to never notify (subdomains included). Links to your own site are always skipped. |
| `USER_AGENT` | no | `mastofeed/1.0` | Sent with every request. Add a contact URL if you like, for example `mastofeed/1.0 (+https://example.com)`. |
| `DB_PATH` | no | `/data/posted.db` | Where the SQLite database is stored. |

Bluesky is switched on only when both `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` are set.

### Getting the credentials

- **Mastodon:** in your instance go to *Preferences → Development → New application*, tick only `write:statuses`, and copy "Your access token".
- **Bluesky:** go to *Settings → Privacy and security → App passwords* (https://bsky.app/settings/app-passwords) and create one. It is shown only once.

## Webmentions

With `WEBMENTION=true`, each new entry's page is fetched and its links are collected from the entry content (`.e-content`, otherwise `<article>`, otherwise `<main>`, otherwise the whole page). For every linked page that advertises a webmention endpoint, a mention is sent. Pages without an endpoint, which is most of them, are skipped.

- This is best-effort. A link that fails is logged and not retried, so a dead site can never hold up your other posts. Only failing to fetch your own entry page causes a retry.
- Links to your own domain, non-HTTP links and domains in `WEBMENTION_IGNORE_DOMAINS` are skipped.
- Endpoints on private or local addresses are refused.
- Like the other targets, enabling it on an existing installation does not send mentions for old entries.

## How it works

Every `POLL_INTERVAL` seconds the feed is fetched, using `ETag` and `Last-Modified` so unchanged feeds cost almost nothing. New entries are posted oldest first to each enabled target.

- Each target is tracked separately. If Mastodon succeeds and Bluesky fails, only Bluesky is retried on the next cycle.
- A failing target never blocks the other.
- Enabling Bluesky on an existing installation does not post old entries. It records the current ones first.
- Mastodon posts carry an idempotency key, so a retried request cannot create a duplicate.

## Running without Docker

Python 3.8 or newer:

```
set -a && . ./.env && set +a
DB_PATH=./posted.db python3 main.py
```

## Testing safely

To try the whole flow without spamming your followers, use a throwaway database:

1. Run once with `DB_PATH=./test.db VISIBILITY=direct`. This records the existing entries and posts nothing.
2. Delete the newest entry for the target you want to test:
   `sqlite3 test.db "delete from deliveries where rowid=(select min(rowid) from deliveries where target='bluesky')"`
   and clear the cached feed state: `sqlite3 test.db "delete from meta where k in ('etag','last_modified')"`.
3. Run again. It posts that one entry.

`VISIBILITY=direct` keeps the Mastodon post visible only to you. Bluesky has no equivalent, so delete the test post afterwards.

## Project layout

| File | Purpose |
| --- | --- |
| [main.py](main.py) | Feed parsing, database and the poll loop. |
| [config.py](config.py) | Settings shared by all targets. |
| [targets/mastodon.py](targets/mastodon.py) | Posting to Mastodon. |
| [targets/bluesky.py](targets/bluesky.py) | Posting to Bluesky, including the link card. |
| [targets/webmention.py](targets/webmention.py) | Sending outgoing webmentions. |
| [web.py](web.py) | HTTP helpers shared by the targets. |

To add another target, create a module in `targets/` that provides `NAME`, `enabled()` and `post(guid, title, link)`, and add it to `enabled_targets()` in `main.py`.
