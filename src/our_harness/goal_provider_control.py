"""Connect a durable goal's stop/steering boundary to its exact local turn."""
from contextlib import contextmanager
import threading

from . import cancellation


@contextmanager
def watch(store, goal_id, task):
    token = cancellation.Cancellation()
    done = threading.Event()
    lease = task.get("lease_id")

    def observe():
        while not done.wait(0.25):
            try:
                goal = store.get(goal_id)
                current = next(one for one in goal["tasks"] if one["id"] == task["id"])
            except Exception:
                # A transient read failure is not an instruction to kill work.
                continue
            if current.get("lease_id") != lease or goal.get("status") in {
                "paused", "waiting_for_user", "cancelling", "cancelled", "failed", "complete"
            }:
                token.cancel()
                return

    observer = threading.Thread(target=observe, name="nexus-turn-control", daemon=True)
    parent = cancellation.current()
    unregister = parent.register(token.cancel) if parent else lambda: None
    try:
        with cancellation.use(token):
            observer.start()
            yield
    finally:
        done.set()
        unregister()
        if observer.ident is not None:
            observer.join(timeout=0.1)
