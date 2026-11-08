"""Poll an RSS/Atom feed and post new entries to Mastodon (and Bluesky)."""
import datetime
import hashlib
import html
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin

# Required (set in .env, no defaults)
FEED_URL = os.environ["FEED_URL"]
MASTODON_URL = os.environ["MASTODON_URL"].rstrip("/")
MASTODON_TOKEN = os.environ["MASTODON_TOKEN"]

# Optional (defaults)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))
VISIBILITY = os.environ.get("VISIBILITY", "public")
DB_PATH = os.environ.get("DB_PATH", "/data/posted.db")
USER_AGENT = os.environ.get("USER_AGENT", "mastofeed/1.0")

# Optional: Bluesky is enabled when both handle and app password are set
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "")
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
BLUESKY_SERVICE = os.environ.get("BLUESKY_SERVICE", "https://bsky.social").rstrip("/")
BLUESKY_MAX_CHARS = 300
BLUESKY_MAX_THUMB_BYTES = 1_000_000  # Bluesky's blob limit for thumbnails
MAX_PAGE_BYTES = 1_000_000  # how much of an entry's page to scan for og: tags

ATOM = "{http://www.w3.org/2005/Atom}"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("mastofeed")


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS deliveries ("
        "guid TEXT, target TEXT, PRIMARY KEY (guid, target))"
    )
    # Migrate the old single-target schema (Mastodon only).
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posted'"
    ).fetchone():
        db.execute(
            "INSERT OR IGNORE INTO deliveries (guid, target) "
            "SELECT guid, 'mastodon' FROM posted"
        )
        db.execute("DROP TABLE posted")
        if get_meta(db, "seeded") == "1":
            set_meta(db, "seeded_mastodon", "1")
    db.commit()
    return db


def get_meta(db, key):
    row = db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))
    db.commit()


def fetch_feed(db):
    """Return (body, etag, last_modified), or None if unchanged (HTTP 304)."""
    headers = {"User-Agent": USER_AGENT}
    if etag := get_meta(db, "etag"):
        headers["If-None-Match"] = etag
    if modified := get_meta(db, "last_modified"):
        headers["If-Modified-Since"] = modified
    req = urllib.request.Request(FEED_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (
                resp.read(),
                resp.headers.get("ETag") or "",
                resp.headers.get("Last-Modified") or "",
            )
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise


def parse_entries(body):
    """Return [(guid, title, link)] in feed order (newest first)."""
    root = ET.fromstring(body)
    entries = []
    for item in root.iter("item"):  # RSS
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        title = html.unescape((item.findtext("title") or "").strip())
        if guid and link:
            entries.append((guid, title, link))
    for entry in root.iter(ATOM + "entry"):  # Atom
        link_el = entry.find(ATOM + "link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find(ATOM + "link")
        link = (link_el.get("href") if link_el is not None else "").strip()
        guid = (entry.findtext(ATOM + "id") or link).strip()
        title = html.unescape((entry.findtext(ATOM + "title") or "").strip())
        if guid and link:
            entries.append((guid, title, link))
    return entries


def post_mastodon(guid, title, link):
    text = f"{title}\n\n{link}"
    req = urllib.request.Request(
        f"{MASTODON_URL}/api/v1/statuses",
        data=json.dumps({"status": text, "visibility": VISIBILITY}).encode(),
        headers={
            "Authorization": f"Bearer {MASTODON_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            # Makes retries safe: Mastodon ignores duplicates with the same key.
            "Idempotency-Key": hashlib.sha256(guid.encode()).hexdigest(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("url")


def bluesky_request(path, payload, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BLUESKY_SERVICE}/xrpc/{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def build_bluesky_post(title, link):
    """Return (text, facets): "title\\n\\nlink" with a clickable link facet.

    The title is truncated so the text fits Bluesky's limit. Counting code
    points never undercounts graphemes, so this is conservative.
    """
    sep = "\n\n" if title else ""
    room = BLUESKY_MAX_CHARS - len(sep) - len(link)
    if len(title) > room:
        title = title[: max(room - 1, 0)].rstrip() + "…" if room > 0 else ""
        sep = "\n\n" if title else ""
    prefix = title + sep
    start = len(prefix.encode("utf-8"))  # facet indexes are UTF-8 byte offsets
    facets = [
        {
            "index": {"byteStart": start, "byteEnd": start + len(link.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}],
        }
    ]
    return prefix + link, facets


class _OGParser(HTMLParser):
    """Collect Open Graph meta tags (og:image, og:description, ...)."""

    def __init__(self):
        super().__init__()
        self.og = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = dict(attrs)
        key = a.get("property") or a.get("name") or ""
        if key.startswith("og:") and a.get("content"):
            self.og.setdefault(key, a["content"].strip())


def fetch_og(link):
    """Return the page's Open Graph tags (empty dict on any failure)."""
    try:
        req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            page = resp.read(MAX_PAGE_BYTES).decode(charset, errors="replace")
        parser = _OGParser()
        parser.feed(page)
        return parser.og
    except Exception:
        log.warning("Could not read Open Graph tags from %s", link, exc_info=True)
        return {}


def upload_bluesky_thumb(image_url, token):
    """Upload an image as a Bluesky blob; return the blob ref or None."""
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            mime = resp.headers.get_content_type()
            data = resp.read(BLUESKY_MAX_THUMB_BYTES + 1)
        if not mime.startswith("image/") or len(data) > BLUESKY_MAX_THUMB_BYTES:
            log.warning("Skipping thumbnail %s (type %s, too large or not an image)", image_url, mime)
            return None
        up = urllib.request.Request(
            f"{BLUESKY_SERVICE}/xrpc/com.atproto.repo.uploadBlob",
            data=data,
            headers={
                "Content-Type": mime,
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(up, timeout=30) as resp:
            return json.load(resp)["blob"]
    except Exception:
        log.warning("Could not upload thumbnail %s", image_url, exc_info=True)
        return None


def post_bluesky(guid, title, link):
    session = bluesky_request(
        "com.atproto.server.createSession",
        {"identifier": BLUESKY_HANDLE, "password": BLUESKY_APP_PASSWORD},
    )
    text, facets = build_bluesky_post(title, link)
    og = fetch_og(link)
    external = {
        "uri": link,
        "title": og.get("og:title") or title,
        "description": og.get("og:description", ""),
    }
    if image := og.get("og:image"):
        blob = upload_bluesky_thumb(urljoin(link, image), session["accessJwt"])
        if blob:
            external["thumb"] = blob
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "facets": facets,
        # Link card shown under the post.
        "embed": {"$type": "app.bsky.embed.external", "external": external},
        "createdAt": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    result = bluesky_request(
        "com.atproto.repo.createRecord",
        {
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
        token=session["accessJwt"],
    )
    # Turn at://did/app.bsky.feed.post/rkey into a web URL.
    rkey = result["uri"].rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{session['did']}/post/{rkey}"


def enabled_targets():
    targets = {"mastodon": post_mastodon}
    if BLUESKY_HANDLE and BLUESKY_APP_PASSWORD:
        targets["bluesky"] = post_bluesky
    return targets


def poll(db, targets):
    fetched = fetch_feed(db)
    if fetched is None:
        log.info("Feed unchanged")
        return
    body, etag, modified = fetched
    entries = parse_entries(body)
    all_ok = True

    for name, post in targets.items():
        done = {
            r[0]
            for r in db.execute("SELECT guid FROM deliveries WHERE target = ?", (name,))
        }

        # First run for this target: record the existing back catalogue
        # without posting it.
        if get_meta(db, f"seeded_{name}") != "1":
            db.executemany(
                "INSERT OR IGNORE INTO deliveries (guid, target) VALUES (?, ?)",
                [(g, name) for g, _, _ in entries],
            )
            set_meta(db, f"seeded_{name}", "1")
            log.info(
                "%s: first run, marked %d existing entries as posted",
                name,
                len(entries),
            )
            continue

        new = [e for e in entries if e[0] not in done]
        for guid, title, link in reversed(new):  # oldest first
            try:
                url = post(guid, title, link)
            except Exception:
                log.exception("%s: posting %r failed; will retry next cycle", name, title)
                all_ok = False
                break  # keep order: don't post newer entries before this one
            db.execute(
                "INSERT OR IGNORE INTO deliveries (guid, target) VALUES (?, ?)",
                (guid, name),
            )
            db.commit()
            log.info("%s: posted %r -> %s", name, title, url)
        if not new:
            log.info("%s: no new entries", name)

    # Only remember the feed's ETag once every target succeeded; otherwise the
    # next poll would get a 304 and never retry the failed post.
    if all_ok:
        set_meta(db, "etag", etag)
        set_meta(db, "last_modified", modified)


def main():
    db = open_db()
    targets = enabled_targets()
    log.info(
        "Watching %s every %ds -> %s",
        FEED_URL,
        POLL_INTERVAL,
        ", ".join(targets),
    )
    while True:
        try:
            poll(db, targets)
        except Exception:
            log.exception("Poll failed; will retry next cycle")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
