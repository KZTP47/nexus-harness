"""Remember what an installed CLI said its version was, across app starts.

Asking a CLI its version means starting it, and a cold Node-based CLI on
Windows takes seconds to answer. Nexus asks while it works out which build an
agent will use, so every app start paid for it again before the first chat
list could answer. An answer is only reused while the program is provably the
same program:

- a native executable (``.exe``) is identified by its own file, which an
  update replaces;
- a ``.cmd``/``.bat`` launcher (how npm installs CLIs on Windows) is identified
  together with the scripts it starts, because updating the package leaves the
  launcher itself untouched;
- anything else is never remembered.

Only settled answers are kept, bounded, on this machine's runtime folder.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

SCHEMA = 1
MOST_KEPT = 128
_LAUNCHED = re.compile(r'%~?dp0%?\\?([^"%\r\n]+?\.(?:js|mjs|cjs|exe))', re.IGNORECASE)
_lock = threading.Lock()


def launcher_identity(program: str) -> list[list[Any]] | None:
    """Size and modification time of what ``program`` runs, or None if unknowable."""

    path = Path(program)
    suffix = path.suffix.lower()
    try:
        if suffix == ".exe":
            found = path.stat()
            return [[os.path.normcase(str(path)), int(found.st_size), int(found.st_mtime_ns)]]
        if suffix not in {".cmd", ".bat"}:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return None
    targets = []
    for relative in dict.fromkeys(_LAUNCHED.findall(text)):
        target = (path.parent / relative.replace("/", "\\")).resolve(strict=False)
        try:
            found = target.stat()
        except OSError:
            continue
        targets.append([os.path.normcase(str(target)), int(found.st_size), int(found.st_mtime_ns)])
    return targets or None


def _where(name: str) -> Path:
    from ..runtime_integrity import runtime_root

    return runtime_root() / name


def load(name: str) -> list[tuple[list[Any], Any]]:
    """(key, value) rows still describing the same programs."""

    try:
        held = json.loads(_where(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(held, dict) or held.get("schema_version") != SCHEMA:
        return []
    rows = []
    for row in held.get("rows") or []:
        if not isinstance(row, dict) or not isinstance(row.get("key"), list):
            continue
        program, identity = row.get("program"), row.get("identity")
        if not isinstance(program, str) or not identity or launcher_identity(program) != identity:
            continue
        rows.append((row["key"], row.get("value")))
    return rows


def remember(name: str, key: list[Any], program: str, value: Any) -> None:
    """Keep one settled answer, if the program can be identified."""

    from ..runtime_integrity import atomic_text

    identity = launcher_identity(program)
    if not identity:
        return
    with _lock:
        try:
            held = json.loads(_where(name).read_text(encoding="utf-8"))
            rows = held.get("rows") if isinstance(held, dict) and held.get("schema_version") == SCHEMA else []
        except (OSError, ValueError):
            rows = []
        rows = [one for one in rows or [] if isinstance(one, dict) and one.get("key") != key]
        rows.append({"key": key, "program": program, "identity": identity, "value": value})
        try:
            atomic_text(_where(name), json.dumps(
                {"schema_version": SCHEMA, "rows": rows[-MOST_KEPT:]}, sort_keys=True,
            ) + "\n")
        except OSError:
            return
