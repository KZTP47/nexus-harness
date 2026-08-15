"""Watch a project and say when its files change.

This is the plain version of a trigger: no service, no daemon, no extra
package. It looks at the project every so often, notes what moved, waits for
the changes to stop, then hands the list to whoever asked.

Only the size and modified time of each file are read, never its contents, and
the same ignore rules the rest of the harness uses decide what is looked at.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import LoadedConfig
from .ignore_policy import IgnorePolicy
from .models import HarnessError

DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_QUIET_SECONDS = 0.5
DEFAULT_MAX_FILES = 20_000


@dataclass(frozen=True)
class Changes:
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.added, *self.changed, *self.removed}))

    def describe(self, limit: int = 3) -> str:
        """One short line a person can read, such as 'value.py and 2 more changed'."""

        parts = []
        for label, names in (("new", self.added), ("changed", self.changed), ("gone", self.removed)):
            if not names:
                continue
            shown = ", ".join(names[:limit])
            extra = len(names) - min(limit, len(names))
            parts.append(f"{shown}{f' and {extra} more' if extra else ''} {label}")
        return "; ".join(parts) or "nothing changed"

    def to_dict(self) -> dict[str, Any]:
        return {"added": list(self.added), "changed": list(self.changed), "removed": list(self.removed)}


@dataclass(frozen=True)
class Wakeup:
    """Why the watch stopped waiting."""

    changes: Changes
    # "changes" when files moved, "timer" when the repeat time came round,
    # "nothing" when the wait gave up without either.
    reason: str = "nothing"


Snapshot = dict[str, tuple[int, int]]


def scan(root: Path, ignore: IgnorePolicy, max_files: int = DEFAULT_MAX_FILES) -> Snapshot:
    """Size and modified time for every file the harness would look at."""

    found: Snapshot = {}
    for path in ignore.walk_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        found[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
        if len(found) >= max_files:
            break
    return found


def compare(before: Mapping[str, tuple[int, int]], after: Mapping[str, tuple[int, int]]) -> Changes:
    added = tuple(sorted(set(after) - set(before)))
    removed = tuple(sorted(set(before) - set(after)))
    changed = tuple(sorted(name for name in set(before) & set(after) if before[name] != after[name]))
    return Changes(added=added, changed=changed, removed=removed)


class ProjectWatcher:
    """Reports each settled batch of file changes, one batch at a time."""

    def __init__(
        self,
        config: LoadedConfig,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
        max_files: int = DEFAULT_MAX_FILES,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds <= 0 or quiet_seconds < 0 or max_files < 1:
            raise HarnessError("Watch settings must be positive")
        if interval_seconds > 3600 or quiet_seconds > 3600:
            raise HarnessError("Watch settings must be at most one hour")
        self.config = config
        self.root = config.project_root
        self.ignore = IgnorePolicy(self.root, set(config.get("project.ignore", [])))
        self.interval_seconds = float(interval_seconds)
        self.quiet_seconds = float(quiet_seconds)
        self.max_files = int(max_files)
        self.clock = clock
        self.sleep = sleep
        self.stopped = False
        self.snapshot: Snapshot = scan(self.root, self.ignore, self.max_files)

    def stop(self) -> None:
        self.stopped = True

    def poll(self) -> Changes:
        """One look at the project. Empty when nothing moved."""

        current = scan(self.root, self.ignore, self.max_files)
        found = compare(self.snapshot, current)
        self.snapshot = current
        return found

    def wait_for_changes(self, timeout_seconds: float | None = None) -> Changes:
        """Wait until files change and then stop changing.

        A save often writes several files in a row, so a batch is only handed
        back once the project has been still for the quiet period. Returns an
        empty batch when the timeout passes or the watch is stopped.
        """

        deadline = None if timeout_seconds is None else self.clock() + float(timeout_seconds)
        collected = Changes()
        settled_at: float | None = None
        while not self.stopped:
            found = self.poll()
            if found:
                collected = _merge(collected, found)
                settled_at = self.clock() + self.quiet_seconds
            elif collected and settled_at is not None and self.clock() >= settled_at:
                return collected
            if deadline is not None and self.clock() >= deadline:
                return collected
            self.sleep(self.interval_seconds)
        return collected

    def wait_for_next(
        self,
        *,
        timeout_seconds: float | None = None,
        repeat_seconds: float | None = None,
    ) -> Wakeup:
        """Wait for files to change, or for the repeat time to come round.

        With no repeat time this is just wait_for_changes. With one, the wait
        also ends when that many seconds have passed with nothing happening,
        which is how a plain timed run is asked for.
        """

        if repeat_seconds is None:
            found = self.wait_for_changes(timeout_seconds)
            return Wakeup(found, "changes" if found else "nothing")
        if repeat_seconds <= 0:
            raise HarnessError("The repeat time must be more than zero seconds")
        due_at = self.clock() + float(repeat_seconds)
        while not self.stopped:
            remaining = due_at - self.clock()
            slice_seconds = remaining if timeout_seconds is None else min(remaining, float(timeout_seconds))
            found = self.wait_for_changes(max(0.0, slice_seconds))
            if found:
                return Wakeup(found, "changes")
            if self.clock() >= due_at:
                return Wakeup(Changes(), "timer")
            if timeout_seconds is not None:
                return Wakeup(Changes(), "nothing")
        return Wakeup(Changes(), "nothing")

    def watch(
        self,
        on_change: Callable[[Changes], Any],
        *,
        max_batches: int | None = None,
        timeout_seconds: float | None = None,
        repeat_seconds: float | None = None,
    ) -> int:
        """Call on_change for each settled batch. Returns how many batches ran."""

        batches = 0
        while not self.stopped and (max_batches is None or batches < max_batches):
            wakeup = self.wait_for_next(
                timeout_seconds=timeout_seconds, repeat_seconds=repeat_seconds
            )
            if wakeup.reason == "nothing":
                if timeout_seconds is not None:
                    return batches
                continue
            batches += 1
            on_change(wakeup.changes)
        return batches


def _merge(first: Changes, second: Changes) -> Changes:
    def join(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted({*left, *right}))

    return Changes(
        added=join(first.added, second.added),
        changed=join(first.changed, second.changed),
        removed=join(first.removed, second.removed),
    )
