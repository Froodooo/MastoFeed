"""Poll an RSS/Atom feed and post new entries to Mastodon."""
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

# Required (set in .env, no defaults)
FEED_URL = os.environ["FEED_URL"]
MASTODON_URL = os.environ["MASTODON_URL"].rstrip("/")
MASTODON_TOKEN = os.environ["MASTODON_TOKEN"]

# Optional (defaults)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))
VISIBILITY = os.environ.get("VISIBILITY", "public")
DB_PATH = os.environ.get("DB_PATH", "/data/posted.db")
USER_AGENT = "mastofeed/1.0 (+https://yordi.me)"

ATOM = "{http://www.w3.org/2005/Atom}"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("mastofeed")


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS posted (guid TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    db.commit()
    return db


def get_meta(db, key):
    row = db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))
    db.commit()


def fetch_feed(db):
    """Return feed bytes, or None if unchanged (HTTP 304)."""
    headers = {"User-Agent": USER_AGENT}
    if etag := get_meta(db, "etag"):
        headers["If-None-Match"] = etag
    if modified := get_meta(db, "last_modified"):
        headers["If-Modified-Since"] = modified
    req = urllib.request.Request(FEED_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            set_meta(db, "etag", resp.headers.get("ETag") or "")
            set_meta(db, "last_modified", resp.headers.get("Last-Modified") or "")
            return body
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


def post_status(guid, title, link):
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


def poll(db):
    body = fetch_feed(db)
    if body is None:
        log.info("Feed unchanged")
        return
    entries = parse_entries(body)
    seen = {r[0] for r in db.execute("SELECT guid FROM posted")}

    # First run: record the existing back catalogue without posting it.
    if get_meta(db, "seeded") != "1":
        db.executemany(
            "INSERT OR IGNORE INTO posted (guid) VALUES (?)",
            [(g,) for g, _, _ in entries],
        )
        set_meta(db, "seeded", "1")
        log.info("First run: marked %d existing entries as posted", len(entries))
        return

    new = [e for e in entries if e[0] not in seen]
    for guid, title, link in reversed(new):  # oldest first
        url = post_status(guid, title, link)
        db.execute("INSERT OR IGNORE INTO posted (guid) VALUES (?)", (guid,))
        db.commit()
        log.info("Posted %r -> %s", title, url)
    if not new:
        log.info("No new entries")


def main():
    db = open_db()
    log.info("Watching %s every %ds", FEED_URL, POLL_INTERVAL)
    while True:
        try:
            poll(db)
        except Exception:
            log.exception("Poll failed; will retry next cycle")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
