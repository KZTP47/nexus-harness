"""A fast, shared answer to "is anything listening at this local address?"

Local model servers (Ollama and similar) normally live on a loopback address.
When one is not running, Windows takes one to two seconds to refuse every
connection attempt, and several readiness checks used to ask the same question
one after another, so opening the app sat through seconds of nothing.

A loopback listener accepts in well under a millisecond, so a short connect
bound tells "not running" apart without slowing the real request. The answer
is remembered for a few seconds so one screen asks once. Only a refusal is
ever decided here; a listening service still gets the caller's full request.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.parse
from typing import Callable

LOOPBACK_CONNECT_SECONDS = 0.5
REMEMBER_SECONDS = 5.0

_lock = threading.Lock()
_seen: dict[tuple[str, int], tuple[float, bool]] = {}


def _loopback_target(url: str) -> tuple[str, int] | None:
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if host.casefold() == "localhost":
        return host, port
    try:
        return (host, port) if ipaddress.ip_address(host).is_loopback else None
    except ValueError:
        return None


def _listening(host: str, port: int) -> bool:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, kind, protocol, _name, address in addresses:
        with socket.socket(family, kind, protocol) as probe:
            probe.settimeout(LOOPBACK_CONNECT_SECONDS)
            try:
                probe.connect(address)
                return True
            except OSError:
                continue
    return False


def loopback_refuses(url: str, *, clock: Callable[[], float] = time.monotonic) -> bool:
    """True only when ``url`` is a loopback address with nothing listening.

    Remote addresses always answer False: they are for the caller's own
    request and timeout to judge.
    """

    target = _loopback_target(url)
    if target is None:
        return False
    now = clock()
    with _lock:
        found = _seen.get(target)
    if found is not None and now - found[0] < REMEMBER_SECONDS:
        return not found[1]
    listening = _listening(*target)
    with _lock:
        _seen[target] = (now, listening)
        if len(_seen) > 64:
            _seen.pop(next(iter(_seen)))
    return not listening


def forget() -> None:
    """Drop remembered answers (tests, or an explicit fresh check)."""

    with _lock:
        _seen.clear()
