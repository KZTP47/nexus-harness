from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import time
import threading
import atexit
import uuid
import tempfile
import struct
from ctypes import wintypes
from pathlib import Path
from typing import Any


_PERSISTENT_RX_GRANTS: set[tuple[str, str]] = set()
_PERSISTENT_GRANT_LOCK = threading.Lock()
_PROCESS_RUNTIME_PROFILE = "NexusHarness.Verify." + uuid.uuid4().hex[:20]
_ACL_COMMAND_TIMEOUT_SECONDS = 15.0
_REPARSE_SCAN_TIMEOUT_SECONDS = 10.0
_REPARSE_SCAN_MAX_ENTRIES = 100_000
VERIFICATION_RUNTIME_CONTRACT = "appcontainer-explicit-application-short-runtime-alias-native-handles/v3"


def _create_runtime_junction(alias: Path, target: Path) -> None:
    """Create a directory junction without interpreting paths as shell code."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    native = str(target.resolve())
    substitute = ("\\??\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\??\\" + native).encode("utf-16-le")
    display = native.encode("utf-16-le")
    data = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(display)) + substitute + b"\0\0" + display + b"\0\0"
    raw = struct.pack("<IHH", 0xA0000003, len(data), 0) + data
    alias.mkdir()
    handle = kernel.CreateFileW(str(alias), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        error = _last_error("Could not open immutable runtime alias")
        alias.rmdir()
        raise error
    try:
        buffer = ctypes.create_string_buffer(raw)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(raw), None, 0, ctypes.byref(returned), None):
            raise _last_error("Could not create immutable runtime alias")
    except BaseException:
        kernel.CloseHandle(handle)
        alias.rmdir()
        raise
    else:
        kernel.CloseHandle(handle)


def _application_name(argv: list[str]) -> str:
    """Supply the module separately: NULL lpApplicationName imposes MAX_PATH.

    Keep argv[0] unchanged for the child, including spaces and Unicode. The
    extended Win32 spelling changes name resolution, not the token or its ACLs.
    """
    if not argv or not argv[0] or any("\0" in value for value in argv):
        raise ValueError("Contained command requires an executable and NUL-free arguments")
    command = subprocess.list2cmdline(argv)
    if len(command.encode("utf-16-le")) // 2 >= 32767:
        raise ValueError("Contained command exceeds the Windows 32767 UTF-16 command-line limit")
    executable = argv[0]
    if not os.path.isabs(executable):
        raise ValueError("Contained executable must be an absolute engine-resolved path")
    executable = os.path.abspath(executable)
    if executable.startswith("\\\\?\\"):
        return executable
    if executable.startswith("\\\\"):
        return "\\\\?\\UNC\\" + executable[2:]
    return "\\\\?\\" + executable


def _bounded_command(command: list[str], *, text: bool = False) -> subprocess.CompletedProcess:
    """Run one ACL/drive command with a hard wall-clock bound."""

    return subprocess.run(
        command, capture_output=True, text=text, check=False,
        errors="replace" if text else None,
        timeout=_ACL_COMMAND_TIMEOUT_SECONDS,
    )


def _checked_bounded_command(
    command: list[str], *, label: str, text: bool = False,
) -> subprocess.CompletedProcess:
    """Run a bounded authority command and make non-zero cleanup fail closed."""

    result = _bounded_command(command, text=text)
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "unknown command failure"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise OSError(f"{label}: {str(detail).strip()}")
    return result


def _owned_entry_disappeared(
    root: Path,
    entry: Path,
    error: BaseException,
    root_identity: tuple[int, int],
) -> bool:
    """Confirm a raced-away disposable child instead of masking cleanup errors.

    Chromium can finish deleting transient profile files after ``scandir`` has
    returned their names.  Treat that specific ENOENT race as successful only
    for a lexical child of the owned tree and only when a fresh no-follow stat
    confirms that there is no object left at the name.  Any other error, a
    missing or replaced root, or a name that still resolves remains a
    fail-closed cleanup failure.
    """

    if not isinstance(error, FileNotFoundError) or entry == root:
        return False
    try:
        entry.relative_to(root)
    except ValueError:
        return False
    try:
        os.stat(entry, follow_symlinks=False)
    except FileNotFoundError:
        try:
            root_metadata = os.stat(root, follow_symlinks=False)
        except OSError:
            return False
        root_attributes = getattr(root_metadata, "st_file_attributes", 0)
        return bool(
            stat.S_ISDIR(root_metadata.st_mode)
            and (root_metadata.st_dev, root_metadata.st_ino) == root_identity
            and not root_attributes
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    return False


def _remove_snapshot_reparse_entries(snapshot: Path) -> list[str]:
    """Unlink unsafe aliases lexically without ever walking their targets.

    Reparse nodes can redirect a recursive privileged ACL operation outside
    the disposable tree.  A non-directory hard link is equally unsafe: its
    lexical snapshot name refers to the same file object (and DACL) as an
    external authorized runtime file.  Both must disappear before icacls /T.
    """

    root = Path(os.path.abspath(snapshot))
    root_metadata = root.stat(follow_symlinks=False)
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    if (
        root.is_symlink()
        or getattr(root_metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise OSError("The disposable snapshot root became a reparse point")
    deadline = time.monotonic() + _REPARSE_SCAN_TIMEOUT_SECONDS
    visited = 0
    removed: list[str] = []
    pending = [root]
    while pending:
        if time.monotonic() > deadline or visited > _REPARSE_SCAN_MAX_ENTRIES:
            raise TimeoutError("Timed out sanitizing disposable snapshot reparse entries")
        folder = pending.pop()
        try:
            entries = os.scandir(folder)
        except OSError as error:
            if _owned_entry_disappeared(
                root, folder, error, root_identity,
            ):
                continue
            raise
        with entries:
            while True:
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                except OSError as error:
                    if _owned_entry_disappeared(
                        root, folder, error, root_identity,
                    ):
                        break
                    raise
                visited += 1
                if (
                    time.monotonic() > deadline
                    or visited > _REPARSE_SCAN_MAX_ENTRIES
                ):
                    raise TimeoutError(
                        "Timed out sanitizing disposable snapshot reparse entries"
                    )
                lexical = Path(entry.path)
                # CPython's Windows DirEntry.stat() currently reports
                # st_nlink=0 even for a real hard link.  A direct no-follow
                # stat on the lexical entry preserves the actual link count.
                try:
                    metadata = os.stat(lexical, follow_symlinks=False)
                except OSError as error:
                    if _owned_entry_disappeared(
                        root, lexical, error, root_identity,
                    ):
                        continue
                    raise
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse = bool(
                    attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ) or stat.S_ISLNK(metadata.st_mode)
                external_file_alias = (
                    not stat.S_ISDIR(metadata.st_mode)
                    and getattr(metadata, "st_nlink", 1) > 1
                )
                if reparse or external_file_alias:
                    relative = os.path.relpath(lexical, root).replace("\\", "/")
                    try:
                        if (
                            reparse
                            and attributes
                            & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
                        ):
                            os.rmdir(lexical)
                        else:
                            os.unlink(lexical)
                    except OSError as error:
                        if _owned_entry_disappeared(
                            root, lexical, error, root_identity,
                        ):
                            continue
                        raise
                    removed.append(relative)
                elif stat.S_ISDIR(metadata.st_mode):
                    pending.append(lexical)
    return sorted(removed)


def verification_runtime_profile() -> str:
    return _PROCESS_RUNTIME_PROFILE


def _delete_process_runtime_profile() -> None:
    if os.name != "nt":
        return
    try:
        for sid_value, read_root in list(_PERSISTENT_RX_GRANTS):
            _bounded_command(
                ["icacls", read_root, "/remove:g", f"*{sid_value}", "/T", "/C", "/Q"],
            )
        ctypes.WinDLL("userenv", use_last_error=True).DeleteAppContainerProfile(
            _PROCESS_RUNTIME_PROFILE
        )
    except Exception:
        pass


atexit.register(_delete_process_runtime_profile)


def _last_error(message: str) -> OSError:
    return OSError(ctypes.get_last_error(), message)


def appcontainer_available() -> bool:
    return os.name == "nt" and hasattr(ctypes.windll.userenv, "CreateAppContainerProfile")


def _map_roots_to_private_drives(roots: tuple[Path, ...]) -> tuple[dict[str, str], int]:
    """Map authorized roots without granting metadata access to their parents."""

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    mutex = kernel32.CreateMutexW(None, False, "Local\\NexusHarnessVerificationDriveMap")
    if not mutex or kernel32.WaitForSingleObject(mutex, 60_000) not in {0, 0x80}:
        if mutex:
            kernel32.CloseHandle(mutex)
        raise OSError("Could not acquire the contained drive-map lease")
    mappings: dict[str, str] = {}
    try:
        mask = int(kernel32.GetLogicalDrives())
        letters = [
            chr(code) for code in range(ord("Z"), ord("P") - 1, -1)
            if not (mask & (1 << (code - ord("A"))))
        ]
        unique = list(dict.fromkeys(str(one.resolve()) for one in roots))
        if len(letters) < len(unique):
            raise OSError("No private drive letters are available for contained verification")
        for root, letter in zip(unique, letters):
            result = _bounded_command(["subst", letter + ":", root], text=True)
            if result.returncode != 0:
                raise OSError("Could not map a contained verification root: " + result.stderr)
            mappings[root] = letter + ":\\"
        return mappings, int(mutex)
    except BaseException as error:
        cleanup_errors: list[str] = []
        try:
            for drive in reversed(list(mappings.values())):
                try:
                    _checked_bounded_command(
                        ["subst", drive[:2], "/D"],
                        label="Could not roll back a contained drive mapping",
                    )
                except Exception as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
        finally:
            kernel32.ReleaseMutex(mutex)
            kernel32.CloseHandle(mutex)
        if cleanup_errors:
            raise OSError(
                "Drive-map setup failed and rollback was incomplete: "
                + " | ".join(cleanup_errors)
            ) from error
        raise


def _unmap_private_drives(mappings: dict[str, str], mutex: int) -> None:
    for name in ("ReleaseMutex", "CloseHandle"):
        function = getattr(ctypes.windll.kernel32, name)
        function.argtypes = [wintypes.HANDLE]
        function.restype = wintypes.BOOL
    cleanup_errors: list[str] = []
    try:
        for drive in reversed(list(mappings.values())):
            try:
                _checked_bounded_command(
                    ["subst", drive[:2], "/D"],
                    label="Could not remove a contained drive mapping",
                )
            except Exception as error:
                cleanup_errors.append(str(error))
    finally:
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)
    if cleanup_errors:
        raise OSError(
            "One or more private drive mappings could not be removed: "
            + " | ".join(cleanup_errors)
        )


class _AppContainerAuthorityLease:
    """Own every ACL/drive mutation and roll back partial acquisition."""

    def __init__(
        self,
        snapshot: Path,
        sid_value: str,
        *,
        read_execute_roots: tuple[Path, ...],
        transient_read_execute_roots: tuple[Path, ...],
        grant_traverse_ancestors: bool,
        map_authorized_roots: bool,
    ) -> None:
        self.snapshot = snapshot
        self.sid_value = sid_value
        self.read_execute_roots = read_execute_roots
        self.transient_read_execute_roots = transient_read_execute_roots
        self.grant_traverse_ancestors = grant_traverse_ancestors
        self.map_authorized_roots = map_authorized_roots
        self.snapshot_granted = False
        self.traversed_ancestors: list[Path] = []
        self.new_read_grants: list[tuple[tuple[str, str], Path]] = []
        self.drive_mappings: dict[str, str] = {}
        self.drive_mutex = 0
        self.removed_reparse_entries: list[str] = []
        self.cleanup_errors: list[str] = []
        self.closed = False

    def prepare(self) -> "_AppContainerAuthorityLease":
        try:
            snapshot_grants = (
                ["icacls", str(self.snapshot), "/grant:r", f"*{self.sid_value}:(RX,W,D,DC)", "/C", "/Q"],
                ["icacls", str(self.snapshot), "/grant", f"*{self.sid_value}:(OI)(IO)(R,W,D)", "/C", "/Q"],
                ["icacls", str(self.snapshot), "/grant", f"*{self.sid_value}:(CI)(IO)(RX,W,D,DC)", "/C", "/Q"],
            )
            for index, grant_command in enumerate(snapshot_grants):
                if index == 0:
                    self.snapshot_granted = True
                grant = _bounded_command(grant_command, text=True)
                if grant.returncode != 0:
                    raise OSError(
                        "Could not grant the disposable AppContainer access to its snapshot: "
                        + grant.stderr
                    )
            if self.grant_traverse_ancestors:
                candidates: list[Path] = []
                for authorized_root in (self.snapshot, *self.read_execute_roots):
                    cursor = authorized_root.resolve().parent
                    while cursor != cursor.parent:
                        candidates.append(cursor)
                        cursor = cursor.parent
                    candidates.append(cursor)
                for ancestor in dict.fromkeys(candidates):
                    self.traversed_ancestors.append(ancestor)
                    traverse_grant = _bounded_command([
                        "icacls", str(ancestor), "/grant",
                        f"*{self.sid_value}:(S,RA,X)", "/C", "/Q",
                    ], text=True)
                    if traverse_grant.returncode != 0:
                        raise OSError(
                            "Could not grant metadata/traverse-only ancestor access: "
                            + traverse_grant.stderr
                        )
            with _PERSISTENT_GRANT_LOCK:
                for read_root in self.read_execute_roots:
                    resolved_read = read_root.resolve()
                    grant_key = (self.sid_value, str(resolved_read).casefold())
                    if grant_key in _PERSISTENT_RX_GRANTS:
                        continue
                    self.new_read_grants.append((grant_key, resolved_read))
                    read_grant = _bounded_command([
                        "icacls", str(resolved_read), "/grant",
                        f"*{self.sid_value}:(OI)(CI)(RX)", "/T", "/C", "/Q",
                    ], text=True)
                    if read_grant.returncode != 0:
                        raise OSError(
                            "Could not grant the contained runtime read/execute access: "
                            + read_grant.stderr
                        )
                    _PERSISTENT_RX_GRANTS.add(grant_key)
            if self.map_authorized_roots:
                self.drive_mappings, self.drive_mutex = _map_roots_to_private_drives(
                    (self.snapshot, *self.read_execute_roots)
                )
            return self
        except BaseException as error:
            self.cleanup(process_started=False)
            if self.cleanup_errors:
                raise OSError(
                    "AppContainer authority setup failed and rollback was incomplete: "
                    + " | ".join(self.cleanup_errors)
                ) from error
            raise

    def cleanup(self, *, process_started: bool) -> None:
        if self.closed:
            return
        self.closed = True
        if self.drive_mutex:
            try:
                _unmap_private_drives(self.drive_mappings, self.drive_mutex)
            except Exception as error:
                self.cleanup_errors.append("drive cleanup: " + str(error))
            self.drive_mappings = {}
            self.drive_mutex = 0
        for ancestor in reversed(self.traversed_ancestors):
            try:
                _checked_bounded_command(
                    [
                        "icacls", str(ancestor), "/remove:g",
                        f"*{self.sid_value}", "/C", "/Q",
                    ],
                    label="Could not remove an ancestor traversal grant",
                )
            except Exception as error:
                self.cleanup_errors.append("ancestor ACL cleanup: " + str(error))
        transient = {
            str(one.resolve()).casefold() for one in self.transient_read_execute_roots
        }
        for grant_key, read_root in reversed(self.new_read_grants):
            if process_started and str(read_root).casefold() not in transient:
                continue
            removed = False
            try:
                _checked_bounded_command(
                    [
                        "icacls", str(read_root), "/remove:g", f"*{self.sid_value}",
                        "/T", "/C", "/Q",
                    ],
                    label="Could not remove a runtime read/execute grant",
                )
                removed = True
            except Exception as error:
                self.cleanup_errors.append("runtime ACL cleanup: " + str(error))
            if removed:
                with _PERSISTENT_GRANT_LOCK:
                    _PERSISTENT_RX_GRANTS.discard(grant_key)
        if self.snapshot_granted:
            try:
                self.removed_reparse_entries = _remove_snapshot_reparse_entries(
                    self.snapshot
                )
            except Exception as error:
                self.cleanup_errors.append("snapshot reparse cleanup: " + str(error))
                return
            for command, label in (
                (["icacls", str(self.snapshot), "/remove:g", f"*{self.sid_value}", "/T", "/C", "/Q"], "snapshot ACL cleanup"),
                (["icacls", str(self.snapshot), "/reset", "/T", "/C", "/Q"], "snapshot ACL reset"),
            ):
                try:
                    _checked_bounded_command(command, label=label)
                except Exception as error:
                    self.cleanup_errors.append(label + ": " + str(error))


def run_appcontainer(
    snapshot: Path,
    argv: list[str],
    environment: dict[str, str],
    timeout: float,
    *,
    reparse_probe: tuple[Path, Path] | None = None,
    persistent_profile: str | None = None,
    read_execute_roots: tuple[Path, ...] = (),
    transient_read_execute_roots: tuple[Path, ...] = (),
    capability_sids: tuple[str, ...] = (),
    grant_traverse_ancestors: bool = False,
    map_authorized_roots: bool = False,
    nested_mapped_cwd: bool = False,
) -> dict[str, Any]:
    """Run one process in a unique zero-capability AppContainer and Job.

    The unique package SID is granted access only to the disposable snapshot.
    The original project and its siblings have no package-SID ACE, so native
    CreateFile and child processes are denied by the Windows access check.
    """

    if not appcontainer_available():
        raise OSError("Windows AppContainer APIs are unavailable")
    _application_name(argv)
    if len(argv[0].encode("utf-16-le")) // 2 >= 240:
        executable = Path(argv[0]).resolve(strict=True)
        if not any(executable.is_relative_to(root.resolve()) for root in read_execute_roots):
            raise ValueError("Long executable aliases require immutable runtime authority")
        # Chromium launches its own children with MAX_PATH-limited APIs. A
        # junction provides a short module name without copying the runtime,
        # depending on 8.3 names, or holding the global drive-map mutex while
        # its Node/CDP peer starts. The target retains its immutable RX ACL.
        with tempfile.TemporaryDirectory(prefix="nv-") as temporary:
            alias = Path(temporary) / "runtime"
            short_executable = alias / executable.name
            if len(str(short_executable).encode("utf-16-le")) // 2 >= 240:
                raise OSError("Windows temporary directory is too long for a native runtime alias")
            _create_runtime_junction(alias, executable.parent)
            try:
                if getattr(alias.lstat(), "st_reparse_tag", 0) != 0xA0000003 or not short_executable.samefile(executable):
                    raise OSError("Immutable runtime alias identity check failed")
                result = run_appcontainer(
                    snapshot, [str(short_executable), *argv[1:]], environment, timeout,
                    reparse_probe=reparse_probe, persistent_profile=persistent_profile,
                    read_execute_roots=read_execute_roots,
                    transient_read_execute_roots=transient_read_execute_roots,
                    capability_sids=capability_sids,
                    grant_traverse_ancestors=grant_traverse_ancestors,
                    map_authorized_roots=map_authorized_roots,
                    nested_mapped_cwd=nested_mapped_cwd,
                )
                result["argv"] = list(argv)
                result["runtime_launch_alias"] = True
                return result
            finally:
                # Remove only the junction itself, never recursively walk its
                # immutable target. The contained Job has already closed.
                os.rmdir(alias)
    stdout_path = snapshot / ".nexus-verification" / "contained-stdout.txt"
    stderr_path = snapshot / ".nexus-verification" / "contained-stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    name = persistent_profile or ("NexusVerification." + uuid.uuid4().hex)
    sid = wintypes.LPVOID()
    create_profile = userenv.CreateAppContainerProfile
    create_profile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
    ]
    create_profile.restype = ctypes.c_long
    result = create_profile(name, name, "Nexus disposable verification", None, 0, ctypes.byref(sid))
    created_profile = result == 0
    if result != 0:
        # A stable zero-capability runtime profile lets the immutable bundled
        # runtime receive RX once per process instead of recursively restaging
        # it for every verification command.
        if persistent_profile and (int(result) & 0xffffffff) == 0x800700B7:
            derive = userenv.DeriveAppContainerSidFromAppContainerName
            derive.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
            derive.restype = ctypes.c_long
            derived = derive(name, ctypes.byref(sid))
            if derived != 0:
                raise OSError(f"DeriveAppContainerSid failed: 0x{int(derived) & 0xffffffff:08x}")
        else:
            raise OSError(f"CreateAppContainerProfile failed: 0x{int(result) & 0xffffffff:08x}")
    sid_text = wintypes.LPWSTR()
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
        if created_profile:
            userenv.DeleteAppContainerProfile(name)
        advapi32.FreeSid(sid)
        raise _last_error("ConvertSidToStringSidW failed")
    sid_value = sid_text.value
    identity_released = False

    def release_identity(*, rollback_profile: bool) -> None:
        nonlocal identity_released
        if identity_released:
            return
        identity_released = True
        if not persistent_profile or (rollback_profile and created_profile):
            userenv.DeleteAppContainerProfile(name)
        kernel32.LocalFree(sid_text)
        advapi32.FreeSid(sid)

    authority = _AppContainerAuthorityLease(
        snapshot, sid_value,
        read_execute_roots=read_execute_roots,
        transient_read_execute_roots=transient_read_execute_roots,
        grant_traverse_ancestors=grant_traverse_ancestors,
        map_authorized_roots=map_authorized_roots,
    )
    try:
        authority.prepare()
    except BaseException:
        release_identity(rollback_profile=True)
        raise
    drive_mappings = authority.drive_mappings
    drive_mutex = authority.drive_mutex

    def prepare_effective_launch() -> tuple[list[str], dict[str, str], str]:
        def remap(value: str) -> str:
            output = str(value)
            for root, drive in sorted(
                drive_mappings.items(), key=lambda one: len(one[0]), reverse=True,
            ):
                def replacement(match: re.Match[str], mapped: str = drive) -> str:
                    return mapped if match.end() == len(output) else mapped.rstrip("\\")
                output = re.sub(
                    re.escape(root), replacement,
                    output, flags=re.I,
                )
            return output

        executable = str(argv[0]) if argv else ""
        executable_in_immutable_root = any(
            executable.casefold() == str(root.resolve()).casefold()
            or executable.casefold().startswith(
                str(root.resolve()).casefold().rstrip("\\/") + os.sep
            )
            for root in read_execute_roots
        )
        effective_argv = [
            executable if executable_in_immutable_root else remap(executable),
            *[remap(one) for one in argv[1:]],
        ] if argv else []
        effective_environment = {
            key: remap(value) for key, value in environment.items()
        }
        effective_cwd = drive_mappings.get(str(snapshot.resolve()), str(snapshot))
        if nested_mapped_cwd and drive_mappings:
            cwd_junction = snapshot / ".nexus-verification" / "node-workspace"
            if not cwd_junction.is_dir() or cwd_junction.is_symlink():
                raise OSError("Contained Node workspace was not prepared")
            effective_cwd = (
                drive_mappings[str(snapshot.resolve())]
                + ".nexus-verification\\node-workspace"
            )
        return effective_argv, effective_environment, effective_cwd

    try:
        effective_argv, effective_environment, effective_cwd = prepare_effective_launch()
    except BaseException:
        authority.cleanup(process_started=False)
        release_identity(rollback_profile=True)
        raise
    reparse_created = False
    reparse_path: Path | None = None

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID), ("bInheritHandle", wintypes.BOOL)]

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEX(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", wintypes.LPVOID)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
        ]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", wintypes.LPVOID), ("Capabilities", wintypes.LPVOID),
            ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD),
        ]

    # ctypes otherwise assumes int returns and arguments, truncating HANDLEs
    # on 64-bit Windows. Declare the launch/lifecycle boundary explicitly.
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    for function, arguments, result_type in (
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
        ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ("GetExitCodeProcess", [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ("TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ("LocalFree", [wintypes.LPVOID], wintypes.LPVOID),
    ):
        native = getattr(kernel32, function)
        native.argtypes, native.restype = arguments, result_type

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    process = PROCESS_INFORMATION()
    job = wintypes.HANDLE()
    handles: list[Any] = []
    capability_allocations: list[wintypes.LPVOID] = []
    attr_buffer = None
    attr_initialized = False
    started = time.monotonic()
    timed_out = False
    try:
        if reparse_probe is not None:
            reparse_path, reparse_target = reparse_probe
            try:
                os.symlink(reparse_target, reparse_path, target_is_directory=True)
                reparse_created = True
            except OSError:
                made = _bounded_command([
                    os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
                    str(reparse_path), str(reparse_target),
                ])
                reparse_created = made.returncode == 0 and reparse_path.is_dir()
        stdout_file = open(stdout_path, "wb")
        stderr_file = open(stderr_path, "wb")
        stdin_file = open(os.devnull, "rb")
        handles.extend([stdout_file, stderr_file, stdin_file])
        import msvcrt
        for stream in handles:
            os.set_handle_inheritable(msvcrt.get_osfhandle(stream.fileno()), True)
        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attr_buffer = ctypes.create_string_buffer(size.value)
        attr_list = ctypes.cast(attr_buffer, wintypes.LPVOID)
        if not kernel32.InitializeProcThreadAttributeList(attr_list, 1, 0, ctypes.byref(size)):
            raise _last_error("InitializeProcThreadAttributeList failed")
        attr_initialized = True
        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]
        capability_array = None
        if capability_sids:
            capability_array = (SID_AND_ATTRIBUTES * len(capability_sids))()
            advapi32.ConvertStringSidToSidW.argtypes = [
                wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID),
            ]
            advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
            for index, capability_text in enumerate(capability_sids):
                allocation = wintypes.LPVOID()
                if not advapi32.ConvertStringSidToSidW(
                    capability_text, ctypes.byref(allocation),
                ):
                    raise _last_error("ConvertStringSidToSidW capability failed")
                capability_allocations.append(allocation)
                capability_array[index] = SID_AND_ATTRIBUTES(allocation, 0x00000004)
        capabilities = SECURITY_CAPABILITIES(
            sid,
            ctypes.cast(capability_array, wintypes.LPVOID) if capability_array is not None else None,
            len(capability_sids), 0,
        )
        if not kernel32.UpdateProcThreadAttribute(
            attr_list, 0, 0x00020009, ctypes.byref(capabilities),
            ctypes.sizeof(capabilities), None, None,
        ):
            raise _last_error("UpdateProcThreadAttribute failed")
        startup = STARTUPINFOEX()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x00000100
        startup.StartupInfo.hStdInput = msvcrt.get_osfhandle(stdin_file.fileno())
        startup.StartupInfo.hStdOutput = msvcrt.get_osfhandle(stdout_file.fileno())
        startup.StartupInfo.hStdError = msvcrt.get_osfhandle(stderr_file.fileno())
        startup.lpAttributeList = attr_list
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(effective_argv))
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(effective_environment.items(), key=lambda one: one[0].casefold())) + "\0\0"
        )
        flags = 0x00080000 | 0x00000400 | 0x08000000 | 0x00000004
        if not kernel32.CreateProcessW(
            _application_name(effective_argv), command_line, None, None, True, flags,
            environment_block, effective_cwd, ctypes.byref(startup.StartupInfo), ctypes.byref(process),
        ):
            raise _last_error("CreateProcessW AppContainer launch failed")
        job = kernel32.CreateJobObjectW(None, None)
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # Children inherit the AppContainer token and cannot request breakaway;
        # KILL_ON_JOB_CLOSE also guarantees that a crashed verifier cannot
        # leave a server/browser/worker alive after the engine releases the
        # checkout-owned job handle.
        limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000400
        configured = bool(job) and bool(kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ))
        if not configured or not kernel32.AssignProcessToJobObject(job, process.hProcess):
            kernel32.TerminateProcess(process.hProcess, 255)
            raise _last_error("Could not configure and assign contained process to its no-breakaway Job")
        if kernel32.ResumeThread(process.hThread) == 0xffffffff:
            raise _last_error("Could not resume contained process")
        wait = kernel32.WaitForSingleObject(process.hProcess, max(1, int(timeout * 1000)))
        if wait == 0xffffffff:
            raise _last_error("Could not wait for contained process")
        if wait == 0x00000102:
            timed_out = True
            kernel32.TerminateJobObject(job, 124)
            kernel32.WaitForSingleObject(process.hProcess, 5000)
        exit_code = wintypes.DWORD(255)
        if not kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
            raise _last_error("Could not read contained process exit code")
    finally:
        for stream in handles:
            try:
                stream.close()
            except OSError:
                pass
        for handle in (process.hThread, process.hProcess, job):
            if handle:
                kernel32.CloseHandle(handle)
        if attr_initialized:
            try:
                kernel32.DeleteProcThreadAttributeList(ctypes.cast(attr_buffer, wintypes.LPVOID))
            except Exception:
                pass
        for allocation in capability_allocations:
            kernel32.LocalFree(allocation)
        process_started = bool(process.hProcess)
        authority.cleanup(process_started=process_started)
        release_identity(rollback_profile=not process_started)
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    cleanup_error = " | ".join(authority.cleanup_errors)
    if cleanup_error:
        stderr += (
            "\nVerification containment cleanup failed closed: " + cleanup_error
        )
    return {
        "argv": argv, "cwd": ".",
        "exit_code": -2 if cleanup_error else int(exit_code.value),
        "effective_argv": effective_argv,
        "stdout": stdout, "stderr": stderr,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out, "output_truncated": False,
        "containment_profile": "windows-appcontainer-job-v1",
        "containment_sid": sid_value,
        "reparse_created": reparse_created,
        "persistent_runtime_profile": bool(persistent_profile),
        "capability_sids": list(capability_sids),
        "job_policy": "kill-on-close-no-breakaway",
        "ancestor_authority": "synchronize-read_attributes-traverse-only" if grant_traverse_ancestors else "none",
        "private_drive_roots": sorted(drive_mappings.values()),
        "snapshot_file_execute_denied": True,
        "cleanup_reparse_entries_removed": authority.removed_reparse_entries,
        "containment_cleanup_error": cleanup_error,
        "containment_unavailable": bool(cleanup_error),
    }
