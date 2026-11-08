"""Send outgoing webmentions for links found in a new entry.

See https://www.w3.org/TR/webmention/. The entry's page is fetched, the
links in its content are collected, and every linked page that advertises
a webmention endpoint is notified. This is best-effort: problems with an
individual link are logged and never block the other targets or later
entries.
"""
import ipaddress
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import web
from config import USER_AGENT

NAME = "webmention"
log = logging.getLogger("mastofeed")

# Optional
MAX_LINKS = int(os.environ.get("WEBMENTION_MAX_LINKS", "20"))
IGNORE_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("WEBMENTION_IGNORE_DOMAINS", "").split(",")
    if d.strip()
]

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
}


def enabled():
    return os.environ.get("WEBMENTION", "").lower() in ("1", "true", "yes", "on")


# --- Finding the links in an entry -----------------------------------------

class _LinkParser(HTMLParser):
    """Collect hrefs, grouped by region: e-content, article, main, whole page."""

    def __init__(self):
        super().__init__()
        self.stack = []  # open (non-void) tags
        self.regions = {"e-content": [], "article": [], "main": [], "page": []}
        self.found = set()  # regions that exist in the page
        self.open_regions = {}  # region -> stack depth where it started

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in VOID_TAGS:
            self.stack.append(tag)
            classes = (a.get("class") or "").split()
            if "e-content" in classes:
                self._open("e-content")
            if tag in ("article", "main"):
                self._open(tag)
        if tag == "a" and a.get("href"):
            self.regions["page"].append(a["href"])
            for region in self.open_regions:
                self.regions[region].append(a["href"])

    def _open(self, region):
        self.found.add(region)
        self.open_regions.setdefault(region, len(self.stack))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS or tag not in self.stack:
            return
        # Close back to the matching tag, which also closes any unclosed <p>/<li>.
        del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]
        for region, depth in list(self.open_regions.items()):
            if depth > len(self.stack):
                del self.open_regions[region]


def extract_links(page, source):
    """Return the http(s) links to notify, in page order, without duplicates.

    Uses the entry's e-content if it has one, otherwise its <article>, then
    <main>, and finally the whole page. Links to the entry's own site and to ignored
    domains are dropped.
    """
    parser = _LinkParser()
    parser.feed(page)
    region = next((r for r in ("e-content", "article", "main") if r in parser.found), "page")
    source_host = (urlsplit(source).hostname or "").lower()

    links = []
    for href in parser.regions[region]:
        url, _ = urllib.parse.urldefrag(urljoin(source, href.strip()))
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            continue
        if host == source_host or _ignored(host) or url in links:
            continue
        links.append(url)
    return links[:MAX_LINKS]


def _ignored(host):
    return any(host == d or host.endswith("." + d) for d in IGNORE_DOMAINS)


# --- Safe fetching ----------------------------------------------------------

def check_public(url):
    """Raise ValueError unless url is http(s) and resolves to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported URL {url}")
    for info in socket.getaddrinfo(parts.hostname, parts.port or 443):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"{url} resolves to non-public address {ip}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def _open(req):
    check_public(req.full_url)
    return _opener.open(req, timeout=15)


# --- Endpoint discovery -----------------------------------------------------

def _rel_has_webmention(rel):
    return "webmention" in (rel or "").lower().split()


class _EndpointParser(HTMLParser):
    """Find the first <link>/<a> with rel="webmention", in document order."""

    def __init__(self):
        super().__init__()
        self.href = None

    def handle_starttag(self, tag, attrs):
        if self.href is None and tag in ("link", "a"):
            a = dict(attrs)
            if _rel_has_webmention(a.get("rel")) and a.get("href") is not None:
                self.href = a["href"]


def _endpoint_from_headers(headers, base):
    for value in headers.get_all("Link") or []:
        # <url>; rel="a b"; ..., <url2>; rel=...
        for part in value.split(","):
            part = part.strip()
            if not part.startswith("<") or ">" not in part:
                continue
            url, _, params = part[1:].partition(">")
            for param in params.split(";"):
                key, _, val = param.strip().partition("=")
                if key.lower() == "rel" and _rel_has_webmention(val.strip('"')):
                    return urljoin(base, url)
    return None


def discover_endpoint(target):
    """Return the target's webmention endpoint URL, or None."""
    with _open(urllib.request.Request(target, headers={"User-Agent": USER_AGENT})) as resp:
        base = resp.geturl()
        if endpoint := _endpoint_from_headers(resp.headers, base):
            return endpoint
        if "html" not in resp.headers.get_content_type():
            return None
        charset = resp.headers.get_content_charset() or "utf-8"
        page = resp.read(web.MAX_PAGE_BYTES).decode(charset, errors="replace")
    parser = _EndpointParser()
    parser.feed(page)
    return urljoin(base, parser.href) if parser.href is not None else None


# --- Sending ----------------------------------------------------------------

def send(endpoint, source, target):
    data = urllib.parse.urlencode({"source": source, "target": target}).encode()
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with _open(req) as resp:
        return resp.status


def post(guid, title, link):
    """Notify every page linked from the entry. Returns a short summary."""
    links = extract_links(web.fetch_page(link), link)  # raises -> retried later
    sent = no_endpoint = failed = 0
    for target in links:
        try:
            endpoint = discover_endpoint(target)
            if not endpoint:
                no_endpoint += 1
                continue
            status = send(endpoint, link, target)
            sent += 1
            log.info("webmention: sent %s -> %s (HTTP %s)", link, target, status)
        except Exception as e:
            failed += 1
            log.warning("webmention: %s failed: %s", target, e)
    return f"{len(links)} links: {sent} sent, {no_endpoint} without endpoint, {failed} failed"
