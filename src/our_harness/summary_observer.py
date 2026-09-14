"""Best-effort optional summaries: bounded work, no provider wait or error path."""

from __future__ import annotations

import queue
import threading


class SummaryObserver:
    def __init__(self, capacity: int = 32):
        self.pending = queue.Queue(maxsize=capacity)
        self.worker = threading.Thread(target=self._run, name="nexus-public-summaries", daemon=True)
        self.worker.start()

    def offer(self, sink, value: dict) -> bool:
        try:
            self.pending.put_nowait((sink, value))
            return True
        except queue.Full:
            return False

    def _run(self):
        while True:
            sink, value = self.pending.get()
            try:
                sink(value)
            except Exception:
                # Summaries are optional observations, never dispatch authority.
                # Stale effects and unavailable storage must not fail agent work.
                pass
            finally:
                self.pending.task_done()


_observer = None
_lock = threading.Lock()


def offer(sink, value: dict) -> bool:
    global _observer
    with _lock:
        if _observer is None:
            _observer = SummaryObserver()
        observer = _observer
    return observer.offer(sink, value)
