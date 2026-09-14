"""Bounded best-effort public activity delivery, independent of provider I/O."""
import queue
import threading

_pending = queue.Queue(maxsize=64)
_lock = threading.Lock()
_started = False


def _run():
    while True:
        sink, value = _pending.get()
        try:
            sink(value)
        except Exception:
            pass
        finally:
            _pending.task_done()


def offer(sink, value):
    global _started
    with _lock:
        if not _started:
            for _ in range(4):
                threading.Thread(target=_run, name="nexus-public-activity", daemon=True).start()
            _started = True
    try:
        _pending.put_nowait((sink, value))
        return True
    except queue.Full:
        return False
