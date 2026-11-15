"""Post entries to Bluesky, with a clickable link and a link card."""
import datetime
import json
import logging
import os
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin

from config import USER_AGENT

NAME = "bluesky"
log = logging.getLogger("mastofeed")

# Optional: enabled when both handle and app password are set
HANDLE = os.environ.get("BLUESKY_HANDLE", "")
APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
SERVICE = os.environ.get("BLUESKY_SERVICE", "https://bsky.social").rstrip("/")

MAX_CHARS = 300
MAX_THUMB_BYTES = 1_000_000  # Bluesky's blob limit for thumbnails
MAX_PAGE_BYTES = 1_000_000  # how much of an entry's page to scan for og: tags
RECENT_POSTS = 20  # how many recent posts to check for an existing copy


def enabled():
    return bool(HANDLE and APP_PASSWORD)


def _request(path, payload, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{SERVICE}/xrpc/{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _web_url(did, uri):
    """Turn at://did/app.bsky.feed.post/rkey into a web URL."""
    return f"https://bsky.app/profile/{did}/post/{uri.rsplit('/', 1)[-1]}"


def find_existing(did, link, token):
    """Return the web URL of a recent post that already links to `link`, or None.

    Makes retries safe: Bluesky has no idempotency key, so a post that was
    created but not recorded locally would otherwise be duplicated.
    """
    query = urlencode(
        {"repo": did, "collection": "app.bsky.feed.post", "limit": RECENT_POSTS}
    )
    req = urllib.request.Request(
        f"{SERVICE}/xrpc/com.atproto.repo.listRecords?{query}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        records = json.load(resp).get("records", [])
    for rec in records:
        value = rec.get("value", {})
        embed_uri = value.get("embed", {}).get("external", {}).get("uri")
        if embed_uri == link or link in value.get("text", ""):
            return _web_url(did, rec["uri"])
    return None


def build_post(title, link):
    """Return (text, facets): "title\\n\\nlink" with a clickable link facet.

    The title is truncated so the text fits Bluesky's limit. Counting code
    points never undercounts graphemes, so this is conservative.
    """
    sep = "\n\n" if title else ""
    room = MAX_CHARS - len(sep) - len(link)
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


def upload_thumb(image_url, token):
    """Upload an image as a Bluesky blob; return the blob ref or None."""
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            mime = resp.headers.get_content_type()
            data = resp.read(MAX_THUMB_BYTES + 1)
        if not mime.startswith("image/") or len(data) > MAX_THUMB_BYTES:
            log.warning(
                "Skipping thumbnail %s (type %s, too large or not an image)",
                image_url,
                mime,
            )
            return None
        up = urllib.request.Request(
            f"{SERVICE}/xrpc/com.atproto.repo.uploadBlob",
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


def post(guid, title, link):
    session = _request(
        "com.atproto.server.createSession",
        {"identifier": HANDLE, "password": APP_PASSWORD},
    )
    # If the lookup fails, the exception propagates and the post is retried
    # next cycle; that is safer than risking a duplicate.
    if existing := find_existing(session["did"], link, session["accessJwt"]):
        log.info("bluesky: %s is already posted, skipping", link)
        return existing
    text, facets = build_post(title, link)
    og = fetch_og(link)
    external = {
        "uri": link,
        "title": og.get("og:title") or title,
        "description": og.get("og:description", ""),
    }
    if image := og.get("og:image"):
        blob = upload_thumb(urljoin(link, image), session["accessJwt"])
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
    result = _request(
        "com.atproto.repo.createRecord",
        {
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
        token=session["accessJwt"],
    )
    return _web_url(session["did"], result["uri"])
