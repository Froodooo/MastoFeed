"""Poll an RSS/Atom feed and post new entries to Mastodon (and Bluesky)."""
import html
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from config import USER_AGENT
from targets import bluesky, mastodon

# Required (set in .env, no defaults)
FEED_URL = os.environ["FEED_URL"]

# Optional (defaults)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))
DB_PATH = os.environ.get("DB_PATH", "/data/posted.db")

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


def enabled_targets():
    return [t for t in (mastodon, bluesky) if t.enabled()]


def poll(db, targets):
    fetched = fetch_feed(db)
    if fetched is None:
        log.info("Feed unchanged")
        return
    body, etag, modified = fetched
    entries = parse_entries(body)
    all_ok = True

    for target in targets:
        name = target.NAME
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
                url = target.post(guid, title, link)
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
        ", ".join(t.NAME for t in targets),
    )
    while True:
        try:
            poll(db, targets)
        except Exception:
            log.exception("Poll failed; will retry next cycle")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
