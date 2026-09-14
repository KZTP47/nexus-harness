"""Cheap input observations for opt-in timers; never controls agent sessions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import time
from typing import Any

from .models import HarnessError
from .persistent_memory_index import _is_link_or_junction

SCHEMA_VERSION = 1
MAX_FILES = 64
MAX_BYTES = 4 * 1024 * 1024


def paths(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise HarnessError(f"Watch up to {MAX_FILES} project files.")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise HarnessError("Watched files must be project-relative paths.")
        name = item.strip().replace('\\', '/')
        parts = PurePosixPath(name).parts
        if not parts or name.startswith('/') or ':' in name or '..' in parts:
            raise HarnessError("Watched files must stay inside the selected project.")
        if any(part.casefold() in {'.git', '.harness', '.nexus-memory'} for part in parts):
            raise HarnessError("Watch project inputs, not runtime or version-control files.")
        normalized = '/'.join(parts)
        if normalized not in result:
            result.append(normalized)
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def observe(root: Path, names: list[str], contract: dict[str, Any]) -> dict[str, Any] | None:
    """Return a bounded content snapshot, or None to retain normal timer behavior.

    No subprocesses, model calls or goal locks. Unreadable/changing inputs do not
    prevent an otherwise eligible automation from running.
    """
    if not names:
        return None
    root = root.resolve()
    began = time.monotonic()
    total = 0
    entries = []
    try:
        for name in names:
            path = root
            for part in PurePosixPath(name).parts:
                path = path / part
                if _is_link_or_junction(path):
                    return None
            path.resolve().relative_to(root)
            if not path.exists():
                entries.append([name, 'missing'])
                continue
            before = path.stat()
            if not path.is_file() or total + before.st_size > MAX_BYTES:
                return None
            with path.open('rb') as stream:
                body = stream.read(MAX_BYTES - total + 1)
            total += len(body)
            after = path.stat()
            if total > MAX_BYTES or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                return None
            entries.append([name, hashlib.sha256(body).hexdigest()])
            if time.monotonic() - began > 0.5:
                return None
    except (OSError, ValueError):
        return None
    return {
        'schema_version': SCHEMA_VERSION,
        'contract': digest({'root': str(root), 'policy': contract, 'schema': SCHEMA_VERSION}),
        'inputs': digest(entries),
    }
