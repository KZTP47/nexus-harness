"""Publish immutable launch copies, separate from Electron Builder's output.

Never launch the developer desktop shortcut from win-unpacked: rebuilding that
directory otherwise locks or terminates the user's running application. Only
packaged files enter this cache; project settings, chats and vaults never do.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

CONTRACT = "nexus-desktop-launch-copy/v1"
MANIFEST = "nexus-launch-build.json"
_NAME = re.compile(r"build-[0-9a-f]{64}-[0-9a-f]{12}")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _linked(path: Path) -> bool:
    # Path.is_junction was added in 3.12; the product also supports 3.11.
    return path.is_symlink() or getattr(path.lstat(), "st_reparse_tag", 0) == 0xA0000003


def inventory(folder: Path) -> dict:
    files = {}
    def unreadable(error):
        raise error
    for directory, subdirs, names in os.walk(folder, onerror=unreadable):
        base = Path(directory)
        subdirs[:] = sorted(one for one in subdirs if one != "__pycache__")
        for one in subdirs:
            if _linked(base / one):
                raise RuntimeError("A desktop launch copy cannot contain linked directories")
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(folder).as_posix()
            if relative == MANIFEST or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise RuntimeError("A desktop launch copy cannot contain linked files")
            files[relative] = {"size": path.stat().st_size, "sha256": _file_digest(path)}
    return files


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _current(cache: Path, owner: str) -> Path | None:
    held = _read(cache / "current.json")
    name = str(held.get("directory") or "")
    if held.get("contract") != CONTRACT or held.get("schema_version") != 1 \
            or held.get("owner_sha256") != owner or not _NAME.fullmatch(name):
        return None
    target = cache / name
    if target.is_symlink() or target.resolve().parent != cache.resolve():
        return None
    return target if target.is_dir() else None


def publish_build(root: Path) -> Path:
    """Return a verified copy of this checkout's packaged executable.

    A failed copy leaves the prior selection and every running build untouched.
    Unchanged files may link to an older immutable launch copy, never to mutable
    build output. Old copies are retained because they may still be running.
    """
    root = root.resolve(strict=True)
    source = root / "desktop" / "build-output" / "win-unpacked"
    executable = "Nexus Harness.exe"
    if not (source / executable).is_file() or not (source / "resources").is_dir():
        raise RuntimeError("Build the complete desktop application before publishing its launcher")
    if source.resolve() != source or _linked(source):
        raise RuntimeError("The packaged desktop application must stay inside this checkout")
    cache = root / ".harness" / "runtime" / "desktop-apps"
    if not cache.resolve().is_relative_to(root) or (cache.exists() and _linked(cache)):
        raise RuntimeError("Desktop launch copies must stay inside this checkout")
    cache.mkdir(parents=True, exist_ok=True)
    owner = _digest({"root": os.path.normcase(str(root)), "contract": CONTRACT})
    files = inventory(source)
    content = _digest(files)
    manifest = {"schema_version": 1, "contract": CONTRACT, "owner_sha256": owner,
                "content_sha256": content, "files": files}
    previous = _current(cache, owner)
    prior = _read(previous / MANIFEST) if previous else {}
    if previous and prior == manifest and inventory(previous) == files:
        return previous / executable
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=cache))
    try:
        for relative, identity in files.items():
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            former = previous / relative if previous else None
            # Only reuse a verified file from a previously published copy.
            # Linking win-unpacked would let a rebuild alter the live app.
            if former and prior.get("owner_sha256") == owner \
                    and prior.get("contract") == CONTRACT \
                    and isinstance(prior.get("files"), dict) \
                    and prior["files"].get(relative) == identity \
                    and former.is_file() and not former.is_symlink() \
                    and former.resolve().is_relative_to(previous.resolve()) \
                    and _file_digest(former) == identity["sha256"]:
                try:
                    os.link(former, destination)
                    continue
                except OSError:
                    pass
            shutil.copy2(source / relative, destination)
        if inventory(stage) != files or inventory(source) != files:
            raise RuntimeError("The packaged app changed while publishing; the previous launcher was kept")
        (stage / MANIFEST).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        name = f"build-{content}-{uuid.uuid4().hex[:12]}"
        destination = cache / name
        stage.rename(destination)
        receipt = {key: manifest[key] for key in ("schema_version", "contract", "owner_sha256", "content_sha256")}
        receipt["directory"] = name
        pending = cache / f".current-{uuid.uuid4().hex}.json"
        try:
            pending.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
            os.replace(pending, cache / "current.json")
        finally:
            pending.unlink(missing_ok=True)
        return destination / executable
    finally:
        # This is our own unpublished temporary directory, never a live copy.
        if stage.exists() and stage.resolve().parent == cache.resolve() and stage.name.startswith(".stage-"):
            shutil.rmtree(stage)
