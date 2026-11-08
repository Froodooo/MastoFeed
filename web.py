"""Small HTTP helpers shared by the posting targets."""
import urllib.request
from functools import lru_cache

from config import USER_AGENT

MAX_PAGE_BYTES = 1_000_000  # how much of a page to read


@lru_cache(maxsize=4)
def fetch_page(url):
    """Return the page's HTML as text.

    Cached for a few URLs, so several targets handling the same entry
    only fetch its page once. Failures raise and are not cached.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read(MAX_PAGE_BYTES).decode(charset, errors="replace")
