from __future__ import annotations

import os
import re
import stat
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import HarnessError


CONTROL_COMPONENTS = {".git", ".harness"}
_DOS_DEVICES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
# The console, by its other names. Writing to one of these throws the words
# away and the folder stays empty; reading from one waits for somebody to type
# something, for ever.
_DOS_DEVICES |= {"conin$", "conout$"}
# Windows gives every folder a second, shorter name: .git is also GIT~1, and
# .harness is also HARNES~1. Both open the same folder, so a rule that only
# knows the long spelling is a rule with a door beside it. Nothing the harness
# writes ever needs one of these, so the shape itself is refused.
_SHORT_NAME = re.compile(r"^[^\\/]{1,6}~\d{1,3}(\.[^\\/.]{0,3})?$")


def portable_component_key(component: str) -> str:
    """Return the comparison key Windows-style aliases resolve toward."""
    return unicodedata.normalize("NFKC", component).rstrip(" .").casefold()


def filesystem_case_key(value: str) -> str:
    """Use the host filesystem's ordinary case semantics for policy matching."""
    return value.casefold() if os.name == "nt" else value


def portable_relative_path_key(relative: str | Path, *, allow_control: bool = False) -> str:
    """Return one separator- and Windows-alias-stable key for a relative path."""
    validate_portable_relative_path(relative, allow_control=allow_control)
    components = re.split(r"[\\/]", os.fspath(relative))
    return "/".join(portable_component_key(component) for component in components if component != ".")


def validate_portable_relative_path(relative: str | Path, *, allow_control: bool = False) -> None:
    """Reject path spellings that alias on Windows or cross harness control state."""
    text = os.fspath(relative)
    if not isinstance(text, str) or not text or "\0" in text:
        raise HarnessError(f"Path must be a non-empty project-relative string: {relative}")
    if text == ".":
        return
    components = re.split(r"[\\/]", text)
    if any(component == "" for component in components):
        raise HarnessError(f"Path contains an empty component: {relative}")
    for component in components:
        if component in {".", ".."}:
            if component == "..":
                raise HarnessError(f"Path escapes the project: {relative}")
            continue
        if component.endswith((" ", ".")):
            raise HarnessError(f"Path components must not end with a space or dot: {relative}")
        if ":" in component:
            raise HarnessError(f"Path components must not contain a colon or alternate data stream: {relative}")
        key = portable_component_key(component)
        device_stem = key.split(".", 1)[0].rstrip(" .")
        if device_stem in _DOS_DEVICES or key in _DOS_DEVICES:
            raise HarnessError(f"Reserved Windows device path is not accepted: {relative}")
        if _SHORT_NAME.match(key):
            raise HarnessError(
                f"Windows short names such as GIT~1 are not accepted, because they open the "
                f"same folder under another spelling: {relative}"
            )
        if not allow_control and key in CONTROL_COMPONENTS:
            raise HarnessError(f"Git and harness control paths are not accepted: {relative}")


class ProjectTransactionLock:
    """A re-entrant, cross-process lock for mutations within one project."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._stream = None

    @contextmanager
    def held(self, timeout_seconds: float | None = None) -> Iterator[None]:
        with self._thread_lock:
            if self._depth == 0:
                self._acquire_file_lock(timeout_seconds)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._release_file_lock()

    def _acquire_file_lock(self, timeout_seconds: float | None) -> None:
        harness_root = confined_path(self.root, ".harness", allow_control=True)
        harness_root.mkdir(parents=True, exist_ok=True)
        lock_path = confined_path(self.root, Path(".harness") / "transaction.lock", allow_control=True)
        stream = lock_path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            started = time.monotonic()
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._stream = stream
                    return
                except OSError as exc:
                    if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                        raise HarnessError("Another harness process holds the project transaction lock") from exc
                    time.sleep(0.05)
        except Exception:
            stream.close()
            raise

    def _release_file_lock(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _is_reparse(path: Path) -> bool:
    try:
        attrs = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def confined_path(
    root: Path,
    relative: str | Path,
    *,
    allow_missing: bool = True,
    allow_control: bool = False,
) -> Path:
    root = root.resolve()
    validate_portable_relative_path(relative, allow_control=allow_control)
    raw = Path(*re.split(r"[\\/]", os.fspath(relative)))
    if raw == Path("."):
        if not allow_missing and not root.exists():
            raise HarnessError(f"Path does not exist: {relative}")
        return root
    if raw.is_absolute() or raw.drive or str(raw).startswith(("\\\\", "//")):
        raise HarnessError(f"Path must be project-relative: {relative}")
    if any(part in {"..", ""} for part in raw.parts):
        raise HarnessError(f"Path escapes the project: {relative}")
    candidate = root.joinpath(raw)
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink() or _is_reparse(cursor):
                raise HarnessError(f"Linked path components are not accepted: {relative}")
    resolved_parent = candidate.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise HarnessError(f"Path escapes the project: {relative}") from exc
    if not allow_missing and not candidate.exists():
        raise HarnessError(f"Path does not exist: {relative}")
    return candidate


def confined_walk_files(root: Path, ignored_names: set[str] | None = None) -> Iterator[Path]:
    """Yield regular files beneath root without entering linked or reparse paths."""
    root = root.resolve(strict=True)
    ignored = ignored_names or set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError as exc:
            raise HarnessError(f"Cannot inspect workspace directory: {directory.relative_to(root).as_posix() or '.'}: {exc}") from exc
        for entry in entries:
            if entry.name in ignored:
                continue
            path = Path(entry.path)
            relative = path.relative_to(root)
            validate_portable_relative_path(relative)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise HarnessError(f"Cannot inspect workspace path: {relative.as_posix()}: {exc}") from exc
            attributes = getattr(metadata, "st_file_attributes", 0)
            if entry.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                yield path


def safe_environment(names: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            result[name] = value
    return result


def take_the_file_away(path: Path, *, missing_ok: bool = False) -> None:
    """Delete a file that somebody else may have open for a moment.

    Windows will not delete a file while anything has it open, even only to
    read it. That somebody is usually the panel refreshing, and it lets go in a
    moment - so this waits rather than handing back a page of machine detail
    for a delete that would have worked a tenth of a second later.
    """

    for wait in (0.02, 0.05, 0.1, 0.2, 0.4, 0.8):
        try:
            path.unlink(missing_ok=missing_ok)
            return
        except PermissionError:
            time.sleep(wait)
    try:
        path.unlink(missing_ok=missing_ok)
    except PermissionError as exc:
        raise HarnessError(
            f"{path.name} is held open by something else, so it could not be "
            "removed. Close whatever has it open and try again."
        ) from exc


def put_this_file_in_place(path: Path, written: str) -> None:
    """Write beside, then move into place, so no reader ever sees half of one.

    The moving is what needs the patience. Windows will not move a file over
    one that anything has open, even only to read it, and on a busy panel
    something usually does for a moment. Without the waiting, a settings file
    written while two checks were reading it handed back a page of machine
    detail for a write that would have worked a tenth of a second later.

    The file beside it carries this process and this thread in its name, so two
    writes at once cannot land on the same one and take each other's half.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    beside = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    beside.write_text(written, encoding="utf-8")
    # Six and a bit seconds all told. A reader really does hold the move off on
    # Windows, and four checks running side by side hold it longer than one
    # does, so the waiting is longer than it looks like it needs to be.
    for wait in (0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2):
        try:
            os.replace(beside, path)
            return
        except PermissionError:
            time.sleep(wait)
    try:
        os.replace(beside, path)
    except PermissionError as exc:
        beside.unlink(missing_ok=True)
        raise HarnessError(
            f"{path.name} is held open by something else, so it could not be "
            "written. Close whatever has it open and try again."
        ) from exc


def read_this_file_patiently(path: Path) -> str:
    """Read a file something else may be moving into place this moment.

    The other half of put_this_file_in_place. Windows will not let a file be
    opened while it is being moved over, so a reader can lose the race just as
    a writer can - and the settings file is read by nearly everything while the
    panel writes it. Waiting a moment beats handing somebody a page of machine
    detail for a read that works on the next try.
    """

    last: OSError | None = None
    for wait in (0.02, 0.05, 0.1, 0.2, 0.4, 0.8):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError as exc:
            last = exc
            time.sleep(wait)
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError as exc:
        raise HarnessError(
            f"{path.name} could not be read: something else is writing it this "
            "moment, or this account is not allowed to read it. Try again in a "
            "moment, and if it says the same thing, it is the permissions."
        ) from (last or exc)


def redact(value: str, secrets: list[str]) -> str:
    output = value
    for secret in sorted({item for item in secrets if len(item) >= 6}, key=len, reverse=True):
        output = output.replace(secret, "[REDACTED]")
    return output
