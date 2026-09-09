"""Bounded public-web GETs with no credentials, cookies, or private-network access."""
from __future__ import annotations

import http.client
from html.parser import HTMLParser
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from .models import HarnessError

MAX_WEB_BYTES = 8 * 1024 * 1024


def public_url(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise HarnessError("Use a bounded public HTTP or HTTPS URL without whitespace")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise HarnessError("Invalid public URL") from exc
    if parsed.scheme not in {"https", "http"} or not host or parsed.username is not None or parsed.password is not None:
        raise HarnessError("Only public HTTP(S) URLs without embedded credentials are supported")
    if port not in {80, 443}:
        raise HarnessError("Public web tools support ports 80 and 443")
    return parsed.scheme, host, port


def _public_addresses(host: str, port: int) -> list[str]:
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast
                            or ipaddress.ip_address(address).is_reserved for address in addresses):
        raise HarnessError("Public web tools cannot connect to private, loopback, or special-use addresses")
    return addresses


def fetch_public(url: str, *, timeout: float = 20, max_bytes: int = MAX_WEB_BYTES) -> dict:
    """Resolve once per hop and connect to that verified IP, retaining TLS SNI."""
    expires = time.monotonic() + max(0.1, min(timeout, 30))
    try:
        for _hop in range(6):
            scheme, host, port = public_url(url)
            addresses = _public_addresses(host, port)
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise HarnessError("Public web request timed out")
            connection = http.client.HTTPConnection(host, port, timeout=remaining)
            try:
                connection.sock = socket.create_connection((addresses[0], port), timeout=remaining)
                if scheme == "https":
                    connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
                parsed = urlsplit(url)
                target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request("GET", target, headers={
                    "User-Agent": "Nexus-Harness/1.0 public-research", "Accept": "*/*",
                    "Accept-Encoding": "identity",
                })
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise HarnessError("Web redirect has no destination")
                    url = urljoin(url, location)
                    continue
                if response.status >= 400:
                    hint = " Public service rate limit or access restriction; retry later or use a direct public source URL." if response.status in {403, 429} else ""
                    raise HarnessError(f"Public source returned HTTP {response.status}.{hint}")
                if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise HarnessError("Public source returned an unsupported compressed response")
                chunks, size = [], 0
                while True:
                    remaining = expires - time.monotonic()
                    if remaining <= 0:
                        raise HarnessError("Public web request timed out")
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(65536, max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise HarnessError("Public source exceeds the bounded download size")
                return {"url": url, "content_type": response.getheader("Content-Type", ""), "data": b"".join(chunks)}
            finally:
                connection.close()
    except HarnessError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise HarnessError(f"Public web request failed: {type(exc).__name__}") from exc
    raise HarnessError("Public source redirected too many times")


class _ReadableHTML(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url, self.skip = url, 0
        self.parts: list[str] = []
        self.characters = 0

    def append(self, text):
        self.characters += len(text)
        if self.characters > 2_000_000:
            raise HarnessError("Readable web page exceeds the text size limit")
        self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if self.skip:
            return
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "pre"}:
            self.append("\n")
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.append(" [" + urljoin(self.url, href) + "] ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.append(data)


def web_text(response: dict) -> bytes:
    raw = response["data"]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise HarnessError("This source is not UTF-8 text. For ZIP downloads use list_archive/read_archive/extract_archive with the URL.") from exc
    if "html" in response["content_type"].lower():
        parser = _ReadableHTML(response["url"])
        parser.feed(text)
        text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    return text.encode("utf-8")
