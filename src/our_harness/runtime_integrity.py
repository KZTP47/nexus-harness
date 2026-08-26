"""Keyed integrity material for user-scoped orchestration records.

The key and anchors deliberately live outside every project.  Project files
may contain an append-only readable record, but they never contain enough
material to forge Nexus' integrity decision after that record is rewritten.

This is corruption and blind-rewrite detection, not an OS isolation boundary.
An arbitrary process already running as the same desktop user can read this
user-owned key. Isolating hostile same-user code requires a separate broker or
restricted OS identity and is deliberately not claimed by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any

from .models import HarnessError


_lock = threading.RLock()


def runtime_root() -> Path:
    override = os.environ.get("OUR_HARNESS_SWARM_RUN_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return (base / "OurHarness" / "swarm-runs").resolve()
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state) if state else Path.home() / ".local" / "state"
    return (base / "our-harness" / "swarm-runs").resolve()


def integrity_key() -> bytes:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    where = root / "integrity.key"
    with _lock:
        if where.exists():
            if where.is_symlink() or not where.is_file():
                raise HarnessError("The Swarm integrity key is not a regular file.")
            key = where.read_bytes()
            if len(key) != 32:
                raise HarnessError("The Swarm integrity key is invalid.")
            return key
        key = secrets.token_bytes(32)
        try:
            with where.open("xb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                where.chmod(0o600)
            except OSError:
                pass
            return key
        except FileExistsError:
            loaded = where.read_bytes()
            if len(loaded) != 32:
                raise HarnessError("The Swarm integrity key is invalid.")
            return loaded


def mac(kind: str, value: Any) -> str:
    encoded = json.dumps(
        {"kind": str(kind), "value": value}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(integrity_key(), encoded, hashlib.sha256).hexdigest()


def compare(kind: str, value: Any, claimed: object) -> bool:
    return bool(claimed) and hmac.compare_digest(str(claimed), mac(kind, value))


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    beside = path.with_name(
        f".{path.name}.{os.getpid()}-{threading.get_ident()}.part"
    )
    with beside.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    for delay in (0.01, 0.02, 0.05, 0.1, 0.2, 0.4):
        try:
            os.replace(beside, path)
            break
        except PermissionError:
            time.sleep(delay)
    else:
        os.replace(beside, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def quarantine_marker(name: str, evidence: Path, reason: str) -> Path:
    safe = hashlib.sha256(str(name).encode("utf-8")).hexdigest()
    where = runtime_root() / "quarantine" / f"{safe}.json"
    payload = {
        "schema_version": 1,
        "detected_at_ms": int(time.time() * 1000),
        "evidence_path": str(evidence),
        "reason": str(reason)[:1000],
    }
    atomic_text(where, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return where
