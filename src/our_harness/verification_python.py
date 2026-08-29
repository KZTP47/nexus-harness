"""Pinned lightweight Python for contained verification from a source checkout.

The installed desktop carries a complete private runtime.  A Git checkout does
not, and building that complete runtime also downloads Playwright and Chromium.
For Python-only project verification, cache the official embeddable archive and
stage its verified contents in a host-owned engine root outside the writable
AppContainer snapshot.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Iterable, Iterator


PYTHON_VERSION = "3.11.9"
PYTHON_ARCHITECTURE = "amd64"
PYTHON_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-{PYTHON_ARCHITECTURE}.zip"
)
PYTHON_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILES = 256
DOWNLOAD_TIMEOUT_SECONDS = 60.0
LOCK_TIMEOUT_SECONDS = 120.0
_MARKER = ".nexus-source-python.json"
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class VerificationPythonUnavailable(RuntimeError):
    """The pinned source-checkout verification interpreter is unavailable."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def default_cache_root() -> Path:
    """Return the private per-user cache, never a repository directory."""

    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local).expanduser() if local else Path.home() / "AppData" / "Local"
    root = (base / "Nexus Harness" / "verification-python").resolve()
    repository = Path(__file__).resolve().parents[2]
    if root == repository or repository in root.parents:
        raise VerificationPythonUnavailable(
            "LOCALAPPDATA points inside the Nexus source checkout"
        )
    return root


def _cache_archive(cache_root: Path) -> Path:
    return cache_root / (
        f"python-{PYTHON_VERSION}-embed-{PYTHON_ARCHITECTURE}-{PYTHON_SHA256[:16]}.zip"
    )


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).casefold()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize cache/staging publishers in this and other processes."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise VerificationPythonUnavailable(
            "the verification-runtime lock directory could not be prepared"
        ) from error
    local_lock = _thread_lock(path)
    if not local_lock.acquire(timeout=timeout):
        raise VerificationPythonUnavailable("timed out waiting for the verification-runtime lock")
    stream = None
    locked = False
    started = time.monotonic()
    try:
        try:
            stream = path.open("a+b")
        except OSError as error:
            raise VerificationPythonUnavailable(
                "the verification-runtime lock file could not be opened"
            ) from error
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0, os.SEEK_END)
                    if stream.tell() == 0:
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, BlockingIOError) as error:
                if time.monotonic() - started >= timeout:
                    raise VerificationPythonUnavailable(
                        "timed out waiting for the verification-runtime file lock"
                    ) from error
                time.sleep(0.05)
        yield
    finally:
        if stream is not None:
            try:
                if locked:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        local_lock.release()


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_reparse(path: Path) -> bool:
    held = _lstat(path)
    return bool(
        held is not None
        and getattr(held, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _path_exists(path: Path) -> bool:
    """Include broken links and junctions when checking an owned artifact."""

    return _lstat(path) is not None


def _remove_owned_path(path: Path) -> None:
    """Remove one exact engine-owned artifact, regardless of its current type."""

    held = _lstat(path)
    if held is None:
        return
    attributes = getattr(held, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        if attributes & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10):
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    elif stat.S_ISLNK(held.st_mode) or stat.S_ISREG(held.st_mode):
        path.unlink(missing_ok=True)
    elif stat.S_ISDIR(held.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def snapshot_dependency_paths(snapshot: Path) -> tuple[Path, ...]:
    """Return explicit, snapshot-contained pure-Python dependency locations.

    Nexus never invokes a project virtual environment's interpreter and never
    installs packages.  It may import already-prepared project dependencies
    from conventional copied-project layouts.  Native extensions still fail
    the language guard because these paths are intentionally not executable
    roots.
    """

    snapshot = snapshot.resolve()
    candidates = (
        snapshot / ".venv" / "Lib" / "site-packages",
        snapshot / "venv" / "Lib" / "site-packages",
        snapshot / "__pypackages__" / f"{PYTHON_VERSION.rsplit('.', 1)[0]}" / "lib",
        snapshot / "vendor",
        snapshot / "src",
    )
    found: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if (
                _is_reparse(candidate)
                or not candidate.is_dir()
                or resolved == snapshot
                or snapshot not in resolved.parents
            ):
                continue
        except OSError:
            continue
        if resolved not in found:
            found.append(resolved)
    return tuple(found)


def _download_official_archive() -> bytes:
    request = urllib.request.Request(
        PYTHON_URL, headers={"User-Agent": "Nexus-Harness-verification-runtime/1"},
    )
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        final_url = str(response.geturl())
        if not final_url.casefold().startswith("https://"):
            raise VerificationPythonUnavailable("the Python download left HTTPS")
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_ARCHIVE_BYTES:
            raise VerificationPythonUnavailable("the Python archive is unexpectedly large")
        blocks: list[bytes] = []
        total = 0
        while True:
            block = response.read(min(1024 * 1024, MAX_ARCHIVE_BYTES + 1 - total))
            if not block:
                break
            blocks.append(block)
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                raise VerificationPythonUnavailable("the Python archive exceeded its size limit")
    return b"".join(blocks)


def _verified_archive_bytes(cache_root: Path | None = None) -> bytes:
    """Read or atomically cache the exact pinned archive, returning verified bytes."""

    root = (cache_root or default_cache_root()).resolve()
    archive = _cache_archive(root)
    lock = root / ".python-runtime.lock"
    try:
        with _file_lock(lock):
            if archive.is_file():
                try:
                    small_enough = archive.stat().st_size <= MAX_ARCHIVE_BYTES
                except OSError:
                    small_enough = False
                if small_enough:
                    raw = archive.read_bytes()
                    if _sha256(raw) == PYTHON_SHA256:
                        return raw
            try:
                raw = _download_official_archive()
            except VerificationPythonUnavailable:
                raise
            except Exception as error:
                raise VerificationPythonUnavailable(
                    "the pinned Python archive could not be downloaded: " + str(error)
                ) from error
            if _sha256(raw) != PYTHON_SHA256:
                raise VerificationPythonUnavailable(
                    "the downloaded Python archive checksum did not match the pinned release"
                )
            temporary = root / f".{archive.name}.{uuid.uuid4().hex}.part"
            try:
                with temporary.open("xb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, archive)
            finally:
                temporary.unlink(missing_ok=True)
            return raw
    except VerificationPythonUnavailable:
        raise
    except Exception as error:
        raise VerificationPythonUnavailable(
            "the pinned Python cache could not be prepared: " + str(error)
        ) from error


def _archive_entries(raw: bytes) -> dict[str, bytes]:
    """Validate and expand the flat official embeddable archive in memory."""

    entries: dict[str, bytes] = {}
    names: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as packed:
            files = packed.infolist()
            if len(files) > MAX_ARCHIVE_FILES:
                raise VerificationPythonUnavailable("the Python archive has too many files")
            for item in files:
                name = item.filename
                mode = item.external_attr >> 16
                if (
                    not name
                    or item.is_dir()
                    or "/" in name
                    or "\\" in name
                    or ":" in name
                    or name in {".", ".."}
                    or stat.S_ISLNK(mode)
                ):
                    raise VerificationPythonUnavailable(
                        "the Python archive contains an unsafe member: " + name[:120]
                    )
                folded = name.casefold()
                if folded in names:
                    raise VerificationPythonUnavailable(
                        "the Python archive contains duplicate file names"
                    )
                names.add(folded)
                total += int(item.file_size)
                if total > MAX_UNPACKED_BYTES:
                    raise VerificationPythonUnavailable(
                        "the unpacked Python runtime exceeded its size limit"
                    )
                payload = packed.read(item)
                if len(payload) != item.file_size:
                    raise VerificationPythonUnavailable(
                        "a Python archive member did not match its declared size"
                    )
                entries[name] = payload
    except VerificationPythonUnavailable:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise VerificationPythonUnavailable(
            "the pinned Python archive is not a valid embeddable runtime"
        ) from error
    required = {
        "python.exe", "python3.dll", "python311.dll", "python311.zip",
        "python311._pth", "vcruntime140.dll", "vcruntime140_1.dll",
    }
    missing = sorted(required - set(entries))
    if missing:
        raise VerificationPythonUnavailable(
            "the pinned Python archive is incomplete: " + ", ".join(missing)
        )
    return entries


def packaged_runtime_if_usable(root: Path) -> Path | None:
    """Prefer a complete built runtime, but never mistake a partial build for one."""

    root = root.resolve()
    required = (
        "python.exe", "python3.dll", "python311.dll", "python311.zip",
        "python311._pth", "vcruntime140.dll", "vcruntime140_1.dll",
        "NEXUS_RUNTIME.json",
    )
    if not all((root / name).is_file() for name in required):
        return None
    try:
        manifest = json.loads((root / "NEXUS_RUNTIME.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("python") != PYTHON_VERSION
        or manifest.get("python_sha256") != PYTHON_SHA256
    ):
        return None
    return root


def _staged_runtime_matches(
    destination: Path, entries: dict[str, bytes], pth: bytes,
) -> bool:
    if not destination.is_dir():
        return False
    try:
        marker = json.loads((destination / _MARKER).read_text(encoding="utf-8"))
        if marker != {
            "schema_version": 1,
            "python": PYTHON_VERSION,
            "archive_sha256": PYTHON_SHA256,
        }:
            return False
        children = list(destination.iterdir())
        if any(one.is_symlink() or not one.is_file() for one in children):
            return False
        actual = {one.name for one in children if one.name != _MARKER}
        if actual != set(entries) or sum(one.name == _MARKER for one in children) != 1:
            return False
        for name, expected in entries.items():
            wanted = pth if name == "python311._pth" else expected
            if (destination / name).read_bytes() != wanted:
                return False
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return True


def stage_source_runtime(
    destination: Path,
    *,
    snapshot: Path,
    python_guard_parent: Path,
    cache_root: Path | None = None,
    dependency_paths: Iterable[Path] = (),
) -> Path:
    """Atomically stage verified Python in one external host-owned engine root."""

    snapshot = snapshot.resolve()
    lexical_guard_parent = Path(os.path.abspath(python_guard_parent))
    guard_parent = lexical_guard_parent.resolve()
    lexical_destination = Path(os.path.abspath(destination))
    destination = lexical_destination.resolve()
    if (
        not guard_parent.is_dir()
        or _is_reparse(lexical_guard_parent)
        or guard_parent == snapshot
        or snapshot in guard_parent.parents
        or guard_parent in snapshot.parents
    ):
        raise VerificationPythonUnavailable(
            "the Python guard directory is not a direct external host-owned engine root"
        )
    if destination != guard_parent / "runtime":
        raise VerificationPythonUnavailable(
            "the verification runtime destination is not the exact external engine runtime"
        )
    dependencies: list[Path] = []
    for dependency in dependency_paths:
        resolved = Path(dependency).resolve()
        if (
            resolved == snapshot
            or snapshot not in resolved.parents
            or not resolved.is_dir()
            or _is_reparse(Path(dependency))
        ):
            raise VerificationPythonUnavailable(
                "a Python dependency path is outside the exact disposable snapshot"
            )
        if resolved not in dependencies:
            dependencies.append(resolved)
    raw = _verified_archive_bytes(cache_root)
    # Recheck after loading the cache, and only parse bytes held in this
    # process. A cache replacement cannot change what will be executed.
    if _sha256(raw) != PYTHON_SHA256:
        raise VerificationPythonUnavailable("the cached Python archive changed during validation")
    entries = _archive_entries(raw)
    pth = (
        "python311.zip\n.\n"
        + str(guard_parent) + "\n"
        + "".join(str(one) + "\n" for one in dependencies)
        + str(snapshot) + "\nimport site\n"
    ).encode("utf-8")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock = destination.parent / ".source-python-stage.lock"
        with _file_lock(lock):
            if _staged_runtime_matches(destination, entries, pth):
                return destination
            for pattern in (
                f".{destination.name}-stage-*", f".{destination.name}-previous-*",
            ):
                for abandoned in destination.parent.glob(pattern):
                    _remove_owned_path(abandoned)
            staging = destination.parent / f".{destination.name}-stage-{uuid.uuid4().hex}"
            previous = destination.parent / f".{destination.name}-previous-{uuid.uuid4().hex}"
            try:
                staging.mkdir()
                for name, payload in entries.items():
                    with (staging / name).open("xb") as output:
                        output.write(pth if name == "python311._pth" else payload)
                (staging / _MARKER).write_text(json.dumps({
                    "schema_version": 1,
                    "python": PYTHON_VERSION,
                    "archive_sha256": PYTHON_SHA256,
                }, sort_keys=True), encoding="utf-8")
                if not _staged_runtime_matches(staging, entries, pth):
                    raise VerificationPythonUnavailable(
                        "the staged Python runtime failed its integrity check"
                    )
                had_previous = _path_exists(destination)
                if had_previous:
                    destination.replace(previous)
                try:
                    staging.replace(destination)
                except BaseException:
                    if had_previous and _path_exists(previous) and not _path_exists(destination):
                        previous.replace(destination)
                    raise
                if _path_exists(previous):
                    _remove_owned_path(previous)
            finally:
                if _path_exists(staging):
                    _remove_owned_path(staging)
                if _path_exists(previous) and _path_exists(destination):
                    _remove_owned_path(previous)
    except VerificationPythonUnavailable:
        raise
    except OSError as error:
        raise VerificationPythonUnavailable(
            "the contained Python runtime could not be staged safely: " + str(error)
        ) from error
    return destination
