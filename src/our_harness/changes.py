from __future__ import annotations

import hashlib
import difflib
import json
import os
import stat
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .models import ChangePlan, HarnessError
from .safety import ProjectTransactionLock, confined_path, portable_component_key, portable_relative_path_key


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str | None:
    return _read_snapshot(path).sha256


CONTROL_ROOTS = {".git", ".harness"}


def _content_bytes(entry: ChangePlan) -> bytes | None:
    if entry.delete:
        return None
    if isinstance(entry.content, bytes):
        return entry.content
    return (entry.content or "").encode("utf-8")


def atomic_write(path: Path, content: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The file is written under a temporary name first and then moved into
    # place. Building that name out of the real one made it longer than the
    # system allows, so a file the person could create by hand could not be
    # written by the harness at all. A short fixed stem is used instead, and
    # the first few letters are kept only to make it recognisable.
    stem = "".join(letter for letter in path.name[:24] if letter.isalnum() or letter in "._-")
    handle, name = tempfile.mkstemp(prefix=f".{stem}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if mode is not None:
            path.chmod(mode)
    finally:
            temporary.unlink(missing_ok=True)


def _canonical_file_patch(path: str, before: bytes | None, after: bytes | None) -> str:
    before_hash = sha256_bytes(before) if before is not None else None
    after_hash = sha256_bytes(after) if after is not None else None
    try:
        before_text = before.decode("utf-8") if before is not None else ""
        after_text = after.decode("utf-8") if after is not None else ""
    except UnicodeDecodeError:
        return (
            f"Binary change {path}\n"
            f"before_sha256={before_hash or 'null'}\n"
            f"after_sha256={after_hash or 'null'}\n"
        )
    old_name = f"a/{path}" if before is not None else "/dev/null"
    new_name = f"b/{path}" if after is not None else "/dev/null"
    return "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=old_name,
            tofile=new_name,
            lineterm="\n",
        )
    )


@dataclass(frozen=True)
class _FileSnapshot:
    content: bytes | None
    mode: int | None
    device: int | None
    inode: int | None
    size: int | None
    modified_ns: int | None

    @property
    def sha256(self) -> str | None:
        return sha256_bytes(self.content) if self.content is not None else None


def _read_snapshot(path: Path) -> _FileSnapshot:
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise HarnessError(f"Change target is not a regular file: {path}")
            content = stream.read()
            after = os.fstat(stream.fileno())
    except FileNotFoundError:
        return _FileSnapshot(None, None, None, None, None, None)
    except (IsADirectoryError, PermissionError) as exc:
        if path.exists() and not path.is_file():
            raise HarnessError(f"Change target is not a regular file: {path}") from exc
        raise HarnessError(f"Cannot read change target: {path}") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(content) != after.st_size:
        raise HarnessError(f"File changed while it was being read: {path}")
    return _FileSnapshot(
        content,
        stat.S_IMODE(after.st_mode),
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def _same_snapshot(left: _FileSnapshot, right: _FileSnapshot) -> bool:
    return (
        left.sha256 == right.sha256
        and left.mode == right.mode
        and left.device == right.device
        and left.inode == right.inode
        and left.size == right.size
        and left.modified_ns == right.modified_ns
    )


class _ExclusiveTarget:
    """An exclusive, identity-stable target lease for the mutation window."""

    def __init__(self, path: Path, expected: _FileSnapshot) -> None:
        self.path = path
        self.expected = expected
        self.created = False
        self.handle: int | None = None
        self.committed = False

    def __enter__(self) -> "_ExclusiveTarget":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            import fcntl
            flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if self.expected.sha256 is None else 0)
            try:
                self.handle = os.open(self.path, flags, 0o600)
                self.created = self.expected.sha256 is None
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise HarnessError(f"Concurrent edit conflict before replacement: {self.path.name}") from exc
        else:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateFileW.restype = wintypes.HANDLE
            access = 0x80000000 | 0x40000000 | 0x00010000  # read, write, delete
            share = 0x00000001  # existing readers only; no writer/delete sharing
            creation = 1 if self.expected.sha256 is None else 3  # CREATE_NEW / OPEN_EXISTING
            handle = kernel32.CreateFileW(
                wintypes.LPCWSTR(str(self.path)), access, share, None, creation,
                0x00000080, None,
            )
            if handle == wintypes.HANDLE(-1).value:
                raise HarnessError(
                    f"Concurrent edit conflict before replacement: {self.path.name}"
                )
            self.handle = int(handle)
            self.created = self.expected.sha256 is None
        if os.name == "nt":
            content, inode = self._windows_content_identity()
            current_hash = sha256_bytes(content)
        else:
            current = _read_snapshot(self.path)
            current_hash = current.sha256
            inode = current.inode
        if self.created:
            if current_hash != sha256_bytes(b""):
                self.close(remove_created=True)
                raise HarnessError(f"Concurrent create conflict before replacement: {self.path.name}")
        else:
            if current_hash != self.expected.sha256 or (
                self.expected.inode is not None and inode is not None
                and int(inode) != int(self.expected.inode)
            ):
                self.close()
                raise HarnessError(f"Baseline conflict before exclusive replacement: {self.path.name}")
        return self

    def _windows_content_identity(self) -> tuple[bytes, int | None]:
        import ctypes
        from ctypes import wintypes
        if self.handle is None:
            raise HarnessError("Exclusive target lease is closed")
        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD),
            ]
        info = BY_HANDLE_FILE_INFORMATION()
        if not ctypes.windll.kernel32.GetFileInformationByHandle(self.handle, ctypes.byref(info)):
            raise HarnessError(f"Could not identify exclusive target: {self.path.name}")
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        if not ctypes.windll.kernel32.SetFilePointerEx(
            self.handle, ctypes.c_longlong(0), None, 0,
        ):
            raise HarnessError(f"Could not read exclusive target: {self.path.name}")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            amount = min(remaining, 1024 * 1024)
            buffer = ctypes.create_string_buffer(amount)
            read = wintypes.DWORD(0)
            if not ctypes.windll.kernel32.ReadFile(
                self.handle, buffer, amount, ctypes.byref(read), None,
            ):
                raise HarnessError(f"Could not read exclusive target: {self.path.name}")
            if read.value == 0:
                break
            chunks.append(buffer.raw[:read.value])
            remaining -= read.value
        inode = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        return b"".join(chunks), inode

    def write(self, content: bytes) -> None:
        if self.handle is None:
            raise HarnessError("Exclusive target lease is closed")
        if os.name != "nt":
            os.lseek(self.handle, 0, os.SEEK_SET)
            os.ftruncate(self.handle, 0)
            view = memoryview(content)
            while view:
                written = os.write(self.handle, view)
                view = view[written:]
            os.fsync(self.handle)
            return
        import ctypes
        from ctypes import wintypes
        position = ctypes.c_longlong(0)
        if not ctypes.windll.kernel32.SetFilePointerEx(self.handle, position, None, 0):
            raise HarnessError(f"Could not position exclusive target: {self.path.name}")
        if not ctypes.windll.kernel32.SetEndOfFile(self.handle):
            raise HarnessError(f"Could not truncate exclusive target: {self.path.name}")
        if content:
            buffer = ctypes.create_string_buffer(content)
            written = wintypes.DWORD(0)
            if not ctypes.windll.kernel32.WriteFile(
                self.handle, buffer, len(content), ctypes.byref(written), None,
            ) or written.value != len(content):
                raise HarnessError(f"Could not write exclusive target: {self.path.name}")
        if not ctypes.windll.kernel32.FlushFileBuffers(self.handle):
            raise HarnessError(f"Could not flush exclusive target: {self.path.name}")

    def delete(self) -> None:
        if self.handle is None:
            raise HarnessError("Exclusive target lease is closed")
        if os.name != "nt":
            self.path.unlink()
            return
        import ctypes
        class FILE_DISPOSITION_INFO(ctypes.Structure):
            _fields_ = [("DeleteFile", ctypes.c_ubyte)]
        info = FILE_DISPOSITION_INFO(1)
        if not ctypes.windll.kernel32.SetFileInformationByHandle(
            self.handle, 4, ctypes.byref(info), ctypes.sizeof(info),
        ):
            raise HarnessError(f"Could not delete exclusive target: {self.path.name}")

    def close(self, *, remove_created: bool = False) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            if os.name == "nt":
                import ctypes
                ctypes.windll.kernel32.CloseHandle(handle)
            else:
                os.close(handle)
        if remove_created and self.path.exists():
            self.path.unlink(missing_ok=True)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        remove_created = self.created and not self.committed
        self.close(remove_created=remove_created)


class FileTransaction:
    def __init__(self, root: Path, max_files: int = 24, max_bytes: int = 32_000_000):
        self.root = root.resolve()
        self.max_files = max_files
        self.max_bytes = max_bytes
        self._project_lock = ProjectTransactionLock(self.root)

    @contextmanager
    def locked(self, timeout_seconds: float | None = None) -> Iterator[None]:
        with self._project_lock.held(timeout_seconds):
            yield

    def _assert_unchanged(self, relative: str, expected: _FileSnapshot, operation: str) -> _FileSnapshot:
        path = confined_path(self.root, relative)
        current = _read_snapshot(path)
        if not _same_snapshot(current, expected):
            raise HarnessError(f"Baseline conflict before {operation}: {relative}; reread the file and make a new plan")
        return current

    @staticmethod
    def _verified_backup(backup_root: Path, record: dict[str, object]) -> bytes:
        backup = confined_path(backup_root, Path("files") / str(record["path"]), allow_missing=False)
        try:
            content = backup.read_bytes()
        except OSError as exc:
            raise HarnessError(f"Rollback backup is unavailable: {record['path']}") from exc
        expected_hash = record.get("backup_sha256", record.get("before_sha256"))
        expected_size = record.get("backup_bytes")
        if (
            expected_hash != record.get("before_sha256")
            or sha256_bytes(content) != expected_hash
            or (expected_size is not None and len(content) != expected_size)
        ):
            raise HarnessError(f"Rollback backup failed integrity verification: {record['path']}")
        return content

    @staticmethod
    def new_transaction_id() -> str:
        return f"{int(time.time())}-{uuid.uuid4().hex[:10]}"

    def load_manifest(self, transaction_id: str) -> dict[str, object]:
        backup_root = confined_path(
            self.root,
            Path(".harness") / "backups" / transaction_id,
            allow_missing=False,
            allow_control=True,
        )
        try:
            value = json.loads((backup_root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(f"Cannot load transaction {transaction_id}: {exc}") from exc
        if not isinstance(value, dict) or value.get("transaction_id") != transaction_id:
            raise HarnessError(f"Transaction manifest identity mismatch: {transaction_id}")
        return value

    def _prepare_locked(
        self,
        entries: list[ChangePlan],
        transaction_id: str,
    ) -> tuple[list[tuple[ChangePlan, Path, _FileSnapshot]], Path, dict[str, object]]:
        if not entries:
            raise HarnessError("A prepared transaction needs at least one change")
        if len(entries) > self.max_files:
            raise HarnessError(f"Change plan has {len(entries)} files; limit is {self.max_files}")
        target_keys = [portable_relative_path_key(entry.path, allow_control=True) for entry in entries]
        if len(set(target_keys)) != len(entries):
            raise HarnessError("Change plan contains duplicate portable path aliases")
        total = sum(len(_content_bytes(entry) or b"") for entry in entries)
        if total > self.max_bytes:
            raise HarnessError(f"Change plan has {total} bytes; limit is {self.max_bytes}")
        prepared: list[tuple[ChangePlan, Path, _FileSnapshot]] = []
        for entry in entries:
            if entry.mode is not None and (
                isinstance(entry.mode, bool) or not isinstance(entry.mode, int) or not 0 <= entry.mode <= 0o7777
            ):
                raise HarnessError(f"Change mode is invalid: {entry.path}")
            first = Path(entry.path).parts[0] if Path(entry.path).parts else ""
            if portable_component_key(first) in CONTROL_ROOTS:
                raise HarnessError(f"Change target is reserved harness or Git control state: {entry.path}")
            path = confined_path(self.root, entry.path)
            before = _read_snapshot(path)
            if entry.baseline_sha256 != before.sha256:
                raise HarnessError(f"Baseline conflict: {entry.path}; reread the file and make a new plan")
            prepared.append((entry, path, before))

        backup_root = confined_path(
            self.root, Path(".harness") / "backups" / transaction_id, allow_control=True
        )
        manifest_path = backup_root / "manifest.json"
        if backup_root.exists():
            manifest = self.load_manifest(transaction_id)
            records = manifest.get("changes")
            if manifest.get("state") != "prepared" or not isinstance(records, list) or len(records) != len(entries):
                raise HarnessError(f"Prepared transaction cannot be reused: {transaction_id}")
            for (entry, _, before), record in zip(prepared, records):
                after = _content_bytes(entry)
                expected = {
                    "path": entry.path,
                    "before_sha256": before.sha256,
                    "after_sha256": sha256_bytes(after) if after is not None else None,
                    "delete": entry.delete,
                }
                if not isinstance(record, dict) or any(record.get(key) != value for key, value in expected.items()):
                    raise HarnessError(f"Prepared transaction does not match the change plan: {transaction_id}")
                if before.content is not None:
                    self._verified_backup(backup_root, record)
            return prepared, backup_root, manifest

        backup_root.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, object] = {
            "schema_version": 3,
            "transaction_id": transaction_id,
            "created_at": int(time.time()),
            "state": "prepared",
            "changes": [],
        }
        for entry, _, before in prepared:
            self._assert_unchanged(entry.path, before, "backup")
            after = _content_bytes(entry)
            record: dict[str, object] = {
                "path": entry.path,
                "before_sha256": before.sha256,
                "after_sha256": sha256_bytes(after) if after is not None else None,
                "before_mode": before.mode,
                "after_mode": entry.mode if entry.mode is not None else before.mode,
                "delete": entry.delete,
                "reason": entry.reason,
            }
            if before.content is not None:
                backup = backup_root / "files" / entry.path
                atomic_write(backup, before.content, before.mode)
                record["backup_sha256"] = before.sha256
                record["backup_bytes"] = len(before.content)
                self._verified_backup(backup_root, record)
            manifest["changes"].append(record)
        patch = "".join(
            _canonical_file_patch(entry.path, before.content, _content_bytes(entry))
            for entry, _, before in prepared
        )
        manifest["patch"] = patch
        manifest["patch_sha256"] = sha256_bytes(patch.encode("utf-8"))
        atomic_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        return prepared, backup_root, manifest

    def prepare(self, plans: Iterable[ChangePlan], transaction_id: str | None = None) -> dict[str, object]:
        """Write immutable intent and verified backups before any project-file mutation."""
        with self.locked():
            entries = list(plans)
            if not entries:
                return {"transaction_id": None, "changes": []}
            _, _, manifest = self._prepare_locked(entries, transaction_id or self.new_transaction_id())
            return manifest

    def apply(
        self,
        plans: Iterable[ChangePlan],
        transaction_id: str | None = None,
        *,
        allowed_exact_capabilities: dict[str, set[str]] | None = None,
        allowed_write_roots: list[str] | None = None,
        protected_paths: list[str] | None = None,
    ) -> dict[str, object]:
        with self.locked():
            entries = list(plans)
            if not entries:
                return {"transaction_id": None, "changes": []}
            for entry in entries:
                relative = entry.path.replace("\\", "/").strip("/")
                folded = relative.casefold()
                under = lambda roots: any(
                    folded == str(root).replace("\\", "/").strip("/").casefold()
                    or folded.startswith(str(root).replace("\\", "/").strip("/").casefold() + "/")
                    for root in (roots or [])
                )
                if protected_paths and under(protected_paths):
                    raise HarnessError(f"Transaction path is protected by the compiled goal: {relative}")
                if allowed_write_roots is not None and not under(allowed_write_roots):
                    raise HarnessError(f"Transaction path is outside the explicit write destinations: {relative}")
                # Root grants and exact operation grants compose.  Exact-only
                # goals pass no root grant and therefore remain exact.
                if allowed_exact_capabilities is not None:
                    capability = "DELETE" if entry.delete else (
                        "MODIFY" if confined_path(self.root, relative).exists() else "CREATE"
                    )
                    allowed = {
                        str(one).upper()
                        for one in allowed_exact_capabilities.get(folded, set())
                    }
                    allowed_by_root = bool(allowed_write_roots and under(allowed_write_roots))
                    if allowed and capability not in allowed and "CREATE_OR_MODIFY" not in allowed:
                        raise HarnessError(
                            f"Transaction {capability.lower()} is not an exact compiled goal grant: {relative}"
                        )
                    if not allowed and not allowed_by_root:
                        raise HarnessError(
                            f"Transaction {capability.lower()} is not an exact compiled goal grant: {relative}"
                        )
            txid = transaction_id or self.new_transaction_id()
            prepared, backup_root, manifest = self._prepare_locked(entries, txid)
            attempted: set[str] = set()
            try:
                for entry, _path, before in prepared:
                    self._assert_unchanged(entry.path, before, "replacement")
                with ExitStack() as stack:
                    leases = [
                        stack.enter_context(_ExclusiveTarget(path, before))
                        for _entry, path, before in prepared
                    ]
                    for (entry, path, before), lease in zip(prepared, leases):
                        attempted.add(entry.path)
                        if entry.delete:
                            lease.delete()
                        else:
                            lease.write(_content_bytes(entry) or b"")
                            if entry.mode is not None:
                                path.chmod(entry.mode)
                        record = next(item for item in manifest["changes"] if item["path"] == entry.path)
                        if not entry.delete:
                            applied_hash = (
                                sha256_bytes(lease._windows_content_identity()[0])
                                if os.name == "nt" else file_sha256(path)
                            )
                            if applied_hash != record["after_sha256"]:
                                raise HarnessError(f"Applied file failed verification: {entry.path}")
                    for lease in leases:
                        lease.committed = True
                for entry, path, _before in prepared:
                    record = next(item for item in manifest["changes"] if item["path"] == entry.path)
                    if file_sha256(path) != record["after_sha256"]:
                        raise HarnessError(f"Applied file failed verification: {entry.path}")
                manifest["state"] = "applied"
                atomic_write(backup_root / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
                return manifest
            except Exception as exc:
                if not attempted:
                    manifest["state"] = "aborted"
                    manifest["aborted_at"] = int(time.time())
                    manifest["failure"] = str(exc)
                    self._write_manifest(backup_root / "manifest.json", manifest)
                    raise
                manifest["state"] = "rolling_back"
                manifest["rollback_reason"] = "apply_failed"
                manifest["failure"] = str(exc)
                try:
                    self._write_manifest(backup_root / "manifest.json", manifest)
                    self.rollback(txid)
                except Exception as rollback_error:
                    raise HarnessError(f"Transaction failed and automatic rollback was refused: {rollback_error}") from exc
                raise

    def rollback(self, transaction_id: str) -> dict[str, object]:
        with self.locked():
            backup_root = confined_path(
                self.root,
                Path(".harness") / "backups" / transaction_id,
                allow_missing=False,
                allow_control=True,
            )
            manifest_path = backup_root / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessError(f"Cannot load rollback transaction {transaction_id}: {exc}") from exc
            changes = manifest.get("changes", [])
            if not isinstance(changes, list):
                raise HarnessError(f"Cannot load rollback transaction {transaction_id}: changes must be an array")
            if manifest.get("state") in {"aborted", "rolled_back"}:
                return {"transaction_id": transaction_id, "rolled_back": []}
            if manifest.get("state") == "prepared":
                states = []
                for record in changes:
                    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                        raise HarnessError(f"Cannot load rollback transaction {transaction_id}: invalid change record")
                    current = file_sha256(confined_path(self.root, record["path"]))
                    states.append(
                        "before"
                        if current == record.get("before_sha256")
                        else "after"
                        if current == record.get("after_sha256")
                        else "other"
                    )
                if states and all(value == "before" for value in states):
                    manifest["state"] = "aborted"
                    manifest["aborted_at"] = int(time.time())
                    atomic_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
                    return {"transaction_id": transaction_id, "rolled_back": []}
                if not states or not all(value == "after" for value in states):
                    raise HarnessError(f"Rollback conflict: interrupted transaction {transaction_id} is in doubt")
            restore: list[tuple[dict[str, object], Path, bytes | None, str]] = []
            for record in changes:
                if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                    raise HarnessError(f"Cannot load rollback transaction {transaction_id}: invalid change record")
                path = confined_path(self.root, record["path"])
                before = None if record.get("before_sha256") is None else self._verified_backup(backup_root, record)
                state = self._rollback_record_state(path, record)
                if state == "other":
                    raise HarnessError(f"Rollback conflict: {record['path']} matches neither transaction boundary")
                restore.append((record, path, before, state))
            intent = {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "changes": [
                    {
                        key: record.get(key)
                        for key in (
                            "path",
                            "before_sha256",
                            "after_sha256",
                            "before_mode",
                            "after_mode",
                            "backup_sha256",
                            "backup_bytes",
                        )
                    }
                    for record, _, _, _ in restore
                ],
            }
            intent_sha256 = sha256_bytes(
                json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            )
            retained_intent = manifest.get("rollback_intent_sha256")
            if retained_intent is not None and retained_intent != intent_sha256:
                raise HarnessError(f"Rollback intent failed integrity validation: {transaction_id}")
            manifest["state"] = "rolling_back"
            manifest.setdefault("rollback_started_at", int(time.time()))
            manifest["rollback_intent_sha256"] = intent_sha256
            manifest["rollback_completed"] = sorted(
                str(record["path"]) for record, _, _, state in restore if state == "before"
            )
            self._write_manifest(manifest_path, manifest)
            completed = set(manifest["rollback_completed"])
            for record, path, before, _ in reversed(restore):
                current = self._rollback_record_state(path, record)
                if current == "other":
                    raise HarnessError(f"Rollback conflict: {record['path']} changed during rollback")
                if current == "after":
                    self._restore_rollback_record(path, before, record)
                    if self._rollback_record_state(path, record) != "before":
                        raise HarnessError(f"Rollback result failed verification: {record['path']}")
                completed.add(str(record["path"]))
                manifest["rollback_completed"] = sorted(completed)
                manifest["rollback_updated_at"] = int(time.time())
                self._write_manifest(manifest_path, manifest)
            manifest["state"] = "rolled_back"
            manifest["rolled_back_at"] = int(time.time())
            self._write_manifest(manifest_path, manifest)
            return {"transaction_id": transaction_id, "rolled_back": [item["path"] for item in changes]}

    @staticmethod
    def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
        atomic_write(path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())

    @staticmethod
    def _restore_rollback_record(path: Path, before: bytes | None, record: dict[str, object]) -> None:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, before, record.get("before_mode"))

    @staticmethod
    def _rollback_record_state(path: Path, record: dict[str, object]) -> str:
        snapshot = _read_snapshot(path)

        def matches(boundary: str) -> bool:
            expected_hash = record.get(f"{boundary}_sha256")
            if snapshot.sha256 != expected_hash:
                return False
            expected_mode = record.get(f"{boundary}_mode")
            return snapshot.content is None or expected_mode is None or snapshot.mode == expected_mode

        return "before" if matches("before") else "after" if matches("after") else "other"

    def verify_applied(self, manifest: dict[str, object]) -> None:
        with self.locked():
            for record in manifest.get("changes", []):
                path = confined_path(self.root, record["path"])
                if file_sha256(path) != record["after_sha256"]:
                    raise HarnessError(f"Applied file changed after verification packet creation: {record['path']}")

    def combine_applied(self, manifests: list[dict[str, object]]) -> dict[str, object]:
        with self.locked():
            first_records: dict[str, tuple[dict[str, object], str]] = {}
            last_records: dict[str, dict[str, object]] = {}
            for manifest in manifests:
                transaction_id = str(manifest.get("transaction_id"))
                for record in manifest.get("changes", []):
                    path = str(record["path"])
                    first_records.setdefault(path, (record, transaction_id))
                    last_records[path] = record
            changes: list[dict[str, object]] = []
            patches: list[str] = []
            for path in sorted(first_records):
                first, transaction_id = first_records[path]
                before = None
                if first.get("before_sha256") is not None:
                    backup_root = confined_path(
                        self.root,
                        Path(".harness") / "backups" / transaction_id,
                        allow_missing=False,
                        allow_control=True,
                    )
                    before = self._verified_backup(backup_root, first)
                current_path = confined_path(self.root, path)
                after_snapshot = _read_snapshot(current_path)
                before_hash = sha256_bytes(before) if before is not None else None
                after_hash = after_snapshot.sha256
                if after_hash != last_records[path].get("after_sha256"):
                    raise HarnessError(f"Applied file changed before verification packet creation: {path}")
                if before_hash == after_hash:
                    continue
                changes.append(
                    {
                        "path": path,
                        "before_sha256": before_hash,
                        "after_sha256": after_hash,
                        "before_mode": first.get("before_mode"),
                        "delete": after_snapshot.content is None,
                        "reason": last_records[path].get("reason", ""),
                    }
                )
                patches.append(_canonical_file_patch(path, before, after_snapshot.content))
            patch = "".join(patches)
            return {
                "schema_version": 2,
                "transaction_ids": [manifest.get("transaction_id") for manifest in manifests],
                "state": "applied",
                "changes": changes,
                "patch": patch,
                "patch_sha256": sha256_bytes(patch.encode("utf-8")),
            }

    def reconcile(self) -> list[dict[str, object]]:
        with self.locked():
            return self._reconcile_locked()

    def _reconcile_locked(self) -> list[dict[str, object]]:
        root = confined_path(self.root, ".harness/backups", allow_control=True)
        if not root.exists():
            return []
        results: list[dict[str, object]] = []
        for folder in sorted(root.iterdir()):
            manifest_path = folder / "manifest.json"
            if not folder.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                results.append({"transaction_id": folder.name, "status": "invalid_manifest"})
                continue
            manifest_state = manifest.get("state")
            if manifest_state not in {"prepared", "rolling_back"}:
                continue
            states = []
            for record in manifest.get("changes", []):
                states.append(
                    self._rollback_record_state(confined_path(self.root, record["path"]), record)
                )
            if manifest_state == "rolling_back":
                status = "rollback_in_progress" if states and all(value in {"before", "after"} for value in states) else "in_doubt"
            else:
                status = "not_applied" if states and all(value == "before" for value in states) else "applied_after_crash" if states and all(value == "after" for value in states) else "in_doubt"
            results.append({"transaction_id": manifest.get("transaction_id", folder.name), "status": status, "files": len(states)})
        return results

    def recover(self, transaction_id: str, action: str) -> dict[str, object]:
        with self.locked():
            return self._recover_locked(transaction_id, action)

    def _recover_locked(self, transaction_id: str, action: str) -> dict[str, object]:
        if action not in {"rollback", "finalize"}:
            raise HarnessError("Recovery action must be rollback or finalize")
        states = {str(item["transaction_id"]): item for item in self._reconcile_locked()}
        state = states.get(transaction_id)
        if state is None:
            raise HarnessError(f"Transaction does not need recovery: {transaction_id}")
        status = str(state["status"])
        if status in {"invalid_manifest", "in_doubt"}:
            raise HarnessError(f"Transaction {transaction_id} cannot be recovered automatically: {status}")
        backup_root = confined_path(
            self.root,
            Path(".harness") / "backups" / transaction_id,
            allow_missing=False,
            allow_control=True,
        )
        manifest_path = backup_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if action == "finalize":
            if status != "applied_after_crash":
                raise HarnessError(f"Only a fully applied interrupted transaction can be finalized: {transaction_id}")
            self.verify_applied(manifest)
            manifest["state"] = "applied"
            manifest["finalized_at"] = int(time.time())
            atomic_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
            return {"transaction_id": transaction_id, "action": "finalize", "status": "applied"}
        if status == "rollback_in_progress":
            result = self.rollback(transaction_id)
            return {**result, "action": "rollback", "status": "rolled_back"}
        if status == "applied_after_crash":
            result = self.rollback(transaction_id)
            return {**result, "action": "rollback", "status": "rolled_back"}
        manifest["state"] = "aborted"
        manifest["aborted_at"] = int(time.time())
        atomic_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        return {"transaction_id": transaction_id, "action": "rollback", "status": "aborted", "rolled_back": []}
