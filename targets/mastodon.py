"""Post entries to Mastodon."""
import hashlib
import json
import os
import urllib.request

from config import USER_AGENT

NAME = "mastodon"

# Required (set in .env, no defaults)
MASTODON_URL = os.environ["MASTODON_URL"].rstrip("/")
MASTODON_TOKEN = os.environ["MASTODON_TOKEN"]

# Optional (defaults)
VISIBILITY = os.environ.get("VISIBILITY", "public")


def enabled():
    return True


def post(guid, title, link):
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
