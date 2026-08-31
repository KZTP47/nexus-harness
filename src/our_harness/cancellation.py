"""Per-request cancellation shared by chat orchestration and provider boundaries."""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from typing import Any

from .models import HarnessError


STOPPED_MESSAGE = "Stopped by you."


class ChatCancelled(HarnessError):
    """A user deliberately stopped one chat request."""


class Cancellation:
    def __init__(self) -> None:
        self._event = threading.Event()
        # Transcript/file commits and cancellation use one short linearization
        # boundary. If a commit owns it first, Stop takes effect immediately
        # after that commit; if Stop owns it first, the commit never starts.
        self._mutation_lock = threading.Lock()
        self._lock = threading.Lock()
        self._callbacks: set[Callable[[], None]] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        with self._mutation_lock:
            with self._lock:
                if self._event.is_set():
                    return False
                self._event.set()
                callbacks = tuple(self._callbacks)
                self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Cancellation remains successful even if a best-effort transport
                # close races with a process or socket that has already exited.
                pass
        return True

    @contextmanager
    def mutation_boundary(self) -> Iterator[None]:
        """Linearize one local post-provider mutation against cancellation."""

        with self._mutation_lock:
            self.checkpoint()
            yield

    def checkpoint(self) -> None:
        if self.cancelled:
            raise ChatCancelled(STOPPED_MESSAGE)

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        call_now = False
        with self._lock:
            if self._event.is_set():
                call_now = True
            else:
                self._callbacks.add(callback)
        if call_now:
            callback()

        def unregister() -> None:
            with self._lock:
                self._callbacks.discard(callback)

        return unregister


_CURRENT: contextvars.ContextVar[Cancellation | None] = contextvars.ContextVar(
    "nexus_chat_cancellation", default=None
)


@contextmanager
def use(token: Cancellation) -> Iterator[Cancellation]:
    held = _CURRENT.set(token)
    try:
        token.checkpoint()
        yield token
    finally:
        _CURRENT.reset(held)


def current() -> Cancellation | None:
    return _CURRENT.get()


def checkpoint() -> None:
    token = current()
    if token is not None:
        token.checkpoint()


@contextmanager
def mutation_boundary() -> Iterator[None]:
    """Fence a post-provider mutation against the current local Stop token."""

    token = current()
    if token is None:
        yield
        return
    with token.mutation_boundary():
        yield


def register(callback: Callable[[], None]) -> Callable[[], None]:
    token = current()
    return token.register(callback) if token is not None else (lambda: None)


def submit(executor: Executor, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
    """Submit work with the caller's cancellation context copied into its worker."""

    context = contextvars.copy_context()
    return executor.submit(context.run, function, *args, **kwargs)


class ChatCancellationRegistry:
    """Tracks one active request per exact chat without globally stopping peers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, tuple[str, Cancellation]] = {}

    def begin(self, chat_key: str, activity_id: str = "") -> Cancellation:
        token = Cancellation()
        with self._lock:
            previous = self._active.get(chat_key)
            if previous is not None:
                raise HarnessError("That chat is already waiting for an answer.")
            self._active[chat_key] = (activity_id, token)
        return token

    def finish(self, chat_key: str, token: Cancellation) -> None:
        with self._lock:
            current_entry = self._active.get(chat_key)
            if current_entry is not None and current_entry[1] is token:
                self._active.pop(chat_key, None)

    def is_active(self, chat_key: str) -> bool:
        with self._lock:
            return chat_key in self._active

    def active_activity(self, chat_key: str) -> str:
        with self._lock:
            current_entry = self._active.get(chat_key)
            return str(current_entry[0]) if current_entry is not None else ""

    def stop(self, chat_key: str, activity_id: str = "") -> tuple[bool, str]:
        with self._lock:
            current_entry = self._active.get(chat_key)
        if current_entry is None:
            return False, ""
        active_activity, token = current_entry
        if activity_id and active_activity and activity_id != active_activity:
            return False, active_activity
        return token.cancel(), active_activity
