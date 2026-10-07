"""Web pages linked in a captured message (2026-10-07): fetch one page and turn it into text.

The routine that files a capture runs in a sandbox with no network access, so a bare link in a message reaches the
vault as a URL and nothing else: the page is filed by its address, unread. With `[capture] web_links = "text"` the
relay, which does have network access, fetches each linked page when the message is filed and stores its text in
`raw/attachments/`, like the text of an attachment or of a linked Drive file.

The relay runs on the owner's server, so a fetch is a request FROM that server: a link (or a redirect behind a
short link) that points at the machine itself, at its private network or at a cloud metadata address must never be
followed. `check_url` therefore refuses anything but http(s) on the default ports to a public address, and `fetch`
follows redirects itself so that every hop gets the same check. The name is resolved once for the check and again
by the HTTP client: a name that changes its answer between the two (DNS rebinding) is not covered. Anyone who can
post in the capture channel can make the relay fetch a public page, so keep that channel private.
"""
import ipaddress
import re
import socket
import urllib.parse

import requests
from bs4 import BeautifulSoup

__all__ = ["Refused", "check_url", "fetch", "page_text", "why", "WEB_MAX_BYTES", "MAX_HOPS", "USER_AGENT"]

WEB_MAX_BYTES = 10 * 1024 * 1024   # a page (or a linked PDF) over this is not read; the capture keeps the link
MAX_HOPS = 5                       # redirects followed, each one checked
TIMEOUT = (10, 20)                 # connect, read (seconds)
USER_AGENT = "Mozilla/5.0 (compatible; flux-brain link reader; +https://github.com/flux-brain/server)"
_HTML = ("text/html", "application/xhtml+xml")
_NOISE = ("script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form", "iframe")


class Refused(Exception):
    """A URL the relay will not request (scheme, port or address), or a response it will not read (size)."""


def check_url(url, resolve=socket.getaddrinfo):
    """Raise Refused unless `url` is http(s), on its default port, and its host resolves ONLY to public addresses."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Refused("not an http(s) address")
    try:
        port = parts.port
    except ValueError:
        raise Refused("malformed port") from None
    if port not in (None, 80, 443):
        raise Refused("non-standard port")
    host = parts.hostname
    if not host or parts.username or parts.password:
        raise Refused("no host, or credentials in the address")
    try:
        infos = resolve(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise Refused("host does not resolve") from None
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise Refused("host does not resolve")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise Refused("address is not public")


def fetch(url, max_bytes=WEB_MAX_BYTES, get=requests.get, resolve=socket.getaddrinfo):
    """(final url, content type, bytes) of one page. Redirects are followed here, one checked hop at a time. Raises
    Refused, or the requests error of a failed request (an HTTP error status included)."""
    for _ in range(MAX_HOPS + 1):
        check_url(url, resolve)
        resp = get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"},
                   timeout=TIMEOUT, allow_redirects=False, stream=True)
        try:
            location = resp.headers.get("Location")
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                continue
            resp.raise_for_status()
            if int(resp.headers.get("Content-Length") or 0) > max_bytes:
                raise Refused(f"over {max_bytes // (1024 * 1024)} MB")
            data = b""
            for chunk in resp.iter_content(65536):
                data += chunk
                if len(data) > max_bytes:
                    raise Refused(f"over {max_bytes // (1024 * 1024)} MB")
            return url, (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower(), data
        finally:
            resp.close()
    raise Refused(f"more than {MAX_HOPS} redirects")


def page_text(data, content_type, url, extract):
    """(title, text, method) for a fetched page; method is None when the type is not converted.

    HTML is reduced to its readable text (scripts, styles, navigation and forms dropped). Anything else goes to
    `extract(data, mime, name)`, the attachment converter, which reads a linked PDF or plain text file."""
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1]) or urllib.parse.urlsplit(url).hostname or "page"
    if content_type in _HTML or (not content_type and data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))):
        soup = BeautifulSoup(data, "html.parser")
        title = " ".join((soup.title.get_text() if soup.title else "").split())
        for tag in soup(_NOISE):
            tag.decompose()
        lines = [" ".join(line.split()) for line in soup.get_text("\n").splitlines()]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return title or name, text, "web page text"
    if content_type == "application/pdf" and not name.lower().endswith(".pdf"):
        name += ".pdf"
    text, method = extract(data, content_type, name)
    return name, text, method


def why(exc):
    """A short reason for the `## Links` line: the refusal, the HTTP status, or the kind of failure."""
    if isinstance(exc, Refused):
        return str(exc)
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code:
        return f"HTTP {code}"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    return exc.__class__.__name__
