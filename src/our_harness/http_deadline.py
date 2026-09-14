"""HTTP send fences for bounded workers whose DNS/TLS may finish late."""
from __future__ import annotations

import http.client
import threading
import urllib.request
from typing import Any, Callable

_DISPATCH_GUARD = threading.local()


def bind_dispatch_guard(check: Callable[[], None]) -> Callable[[], None]:
    """Bind a deadline/cancellation check to this worker; restore on cleanup."""
    previous = getattr(_DISPATCH_GUARD, "check", None)
    _DISPATCH_GUARD.check = check
    def restore() -> None:
        if previous is None:
            del _DISPATCH_GUARD.check
        else:
            _DISPATCH_GUARD.check = previous
    return restore


class _GuardedSend:
    def send(self, data: Any) -> None:
        guard = getattr(_DISPATCH_GUARD, "check", None)
        if guard is not None:
            guard()
        # HTTPConnection.send otherwise connects and sends in one call. DNS,
        # TCP or TLS can finish after cancellation, so fence their return before
        # any HTTP bytes (including the model request) reach the provider.
        if self.sock is None and self.auto_open:
            self.connect()
        if guard is not None:
            guard()
        super().send(data)


class _GuardedHTTPConnection(_GuardedSend, http.client.HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardedSend, http.client.HTTPSConnection):
    pass


class GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_GuardedHTTPConnection, request)


class GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(
            _GuardedHTTPSConnection, request, context=self._context,
        )


