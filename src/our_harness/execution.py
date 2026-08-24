from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import cancellation
from .config import LoadedConfig
from .models import CommandResult, HarnessError
from .safety import confined_path, safe_environment


# Names that are never part of writing software, and would cost somebody their
# disk or their session. They are refused whatever the project settings say,
# and they are kept here rather than in the settings so that a project written
# before this list existed still works.
ALWAYS_DENIED = frozenset({
    "mkfs", "fdisk", "diskpart", "format",
    "format-volume", "clear-disk", "initialize-disk",
    "restart-computer", "stop-computer", "shutdown", "reboot",
})


def _said_as(word: str) -> tuple[str, set[str]]:
    """One word of a command, as either a plain word or the switches it holds.

    The same switch turns up as --force, -Force, /force and /force:yes, and a
    bundle such as -fd holds two of them. A plain word comes back as itself
    with no switches; a switch comes back as no word with its letters.
    """

    plain = word.split(":", 1)[0].split("=", 1)[0].casefold()
    if plain.startswith("--") or plain.startswith("/"):
        return "", {plain.lstrip("-/")}
    if plain.startswith("-") and len(plain) > 1:
        letters = plain.lstrip("-")
        # -fd is -f and -d. A long word after one dash, such as -force, is one
        # switch, not five.
        if len(letters) > 1 and not letters.isalpha():
            return "", {letters}
        return "", set(letters) if len(letters) > 1 else {letters}
    return plain, set()


def _reads_as(words: list[str]) -> tuple[list[str], set[str]]:
    """A whole command as the words it names and the switches it asks for."""

    named: list[str] = []
    switches: set[str] = set()
    for word in words:
        plain, found = _said_as(word)
        if plain:
            named.append(plain)
        switches |= found
    return named, switches


def _switch_is_here(wanted: str, switches: set[str]) -> bool:
    """Is this switch here, however it was written?

    A rule naming a long switch also means its short form: --force is -f on
    nearly every tool that has both. A refused command says so plainly and can
    be allowed by changing the policy; a command that was meant to be refused
    and ran instead cannot be undone.
    """

    if wanted in switches:
        return True
    return len(wanted) > 1 and wanted[0] in switches


def _matches_rule(rule: str, argv: list[str]) -> bool:
    """Does this command do what the rule names, however it was typed?"""

    asked_words, asked_switches = _reads_as(rule.split())
    said_words, said_switches = _reads_as(argv)
    if not asked_words and not asked_switches:
        return False
    # Every plain word the rule names, in that order, somewhere in the command.
    at = 0
    for word in said_words:
        if at < len(asked_words) and word == asked_words[at]:
            at += 1
    if at < len(asked_words):
        return False
    return all(_switch_is_here(wanted, said_switches) for wanted in asked_switches)


class CommandRunner:
    def __init__(self, config: LoadedConfig):
        self.config = config
        self.root = config.project_root

    def _check(self, argv: list[str]) -> None:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise HarnessError("Command must be a non-empty argv list")
        denied = {str(item).lower() for item in self.config.get("execution.deny_executables", [])}
        denied |= ALWAYS_DENIED
        first, inside = self._named_programs(argv)
        if first in denied:
            raise HarnessError(f"Executable is denied by policy: {first}")
        for part in inside:
            if part in denied:
                raise HarnessError(f"Executable is denied by policy: {part}")
        normalized = " ".join(part.lower() for part in argv)
        words = [word for part in argv for word in str(part).lower().split()]
        for sequence in self.config.get("execution.deny_argument_sequences", []):
            wanted = str(sequence).lower()
            if wanted in normalized or _matches_rule(wanted, words):
                raise HarnessError(f"Command argument sequence is denied by policy: {sequence}")

    # Programs that run whatever they are handed. A denied name inside one of
    # these is still that program being run, so the whole line is looked at.
    # A shell: every bare word on its line is a command it would run.
    _SHELLS = frozenset({
        "cmd", "command", "powershell", "pwsh", "sh", "bash", "zsh", "dash", "ksh",
        "wsl", "env", "xargs", "start", "runas", "wscript", "cscript",
    })
    # A language: a one-liner in any of these starts programs just as easily,
    # but its words are code, where "format" is an ordinary method name.
    _SCRIPT_RUNNERS = frozenset({
        "python", "python3", "py", "node", "nodejs", "deno", "bun",
        "perl", "ruby", "php", "osascript", "julia", "lua", "tclsh",
    })
    _INTERPRETERS = _SHELLS | _SCRIPT_RUNNERS
    # A few denied names are also ordinary method names. They are only
    # ordinary when they are written as a method call, with a dot in front:
    # "{}".format(x) is code, while subprocess.run(["format", "C:"]) is the
    # disk formatter being started. So the method calls are taken out of the
    # text before it is read, and nothing else is forgiven.
    _METHOD_CALLS = re.compile(r"\.\s*(?:format|start|command)", re.IGNORECASE)
    # Windows runs more than .exe. A name is compared without any of these.
    _PROGRAM_ENDINGS = (".exe", ".com", ".bat", ".cmd", ".ps1", ".msc", ".scr")

    @classmethod
    def _plain_name(cls, part: str) -> str:
        name = Path(part.strip().strip('"')).name.lower()
        for ending in cls._PROGRAM_ENDINGS:
            if name.endswith(ending):
                return name[: -len(ending)]
        return name

    @classmethod
    def _named_programs(cls, argv: list[str]) -> tuple[str, list[str]]:
        """The program being run, and every name inside what it was handed."""

        first = cls._plain_name(argv[0])
        names: list[str] = []
        if first not in cls._INTERPRETERS:
            return first, names
        reading_code = first in cls._SCRIPT_RUNNERS
        for part in argv[1:]:
            # A switch may carry what it is switching on, all in one argument:
            # python -cCODE, cmd "/c whoami". Passing over the whole argument
            # let a denied program be started by packing it next to the letter
            # that asks for it. The letter is dropped and the rest is read.
            if part.startswith(("-", "/")):
                part = re.sub(r"^[-/]+[A-Za-z]?", " ", part, count=1)
                if not part.strip():
                    continue
            # Windows lets a command line hide a letter behind a caret, so
            # dan^ger is danger by the time cmd runs it.
            part = part.replace("^", "")
            if reading_code:
                # Take out method calls such as "{}".format(x) so ordinary code
                # is not mistaken for the program of the same name.
                part = cls._METHOD_CALLS.sub(" ", part)
            # A shell line can hold several commands, one inside another, and
            # joins them with characters a program name never contains. Listing
            # those characters is always one short, so this keeps what a name
            # can hold and treats everything else as a gap.
            plain = "".join(
                character if (character.isalnum() or character in "._-/\\:") else " "
                for character in part
            )
            for word in plain.split():
                names.append(cls._plain_name(word))
        return first, names

    def run(
        self,
        argv: list[str],
        cwd: str | Path = ".",
        timeout: int | float | None = None,
        stdin_text: str | None = None,
        max_output_bytes: int | None = None,
    ) -> CommandResult:
        self._check(argv)
        working = confined_path(self.root, cwd, allow_missing=False)
        if not working.is_dir():
            raise HarnessError(f"Command cwd is not a directory: {cwd}")
        relative_cwd = working.relative_to(self.root).as_posix() or "."
        actual = list(argv)
        if self.config.get("execution.mode") == "docker":
            if not shutil.which("docker"):
                raise HarnessError("Docker execution was selected, but docker is not on PATH")
            mount = f"{self.root}:/workspace"
            actual = [
                "docker", "run", "--rm", "--network", self.config.get("execution.docker_network"),
                "-v", mount, "-w", f"/workspace/{relative_cwd}".rstrip("/"),
                self.config.get("execution.docker_image"), *argv,
            ]
            working = self.root
        environment = safe_environment(self.config.get("execution.inherit_environment", []))
        configured_limit = int(self.config.get("execution.max_output_bytes"))
        limit = configured_limit if max_output_bytes is None else min(configured_limit, max(1, int(max_output_bytes)))
        configured_timeout = float(self.config.get("execution.timeout_seconds"))
        requested_timeout = configured_timeout if timeout is None else float(timeout)
        if requested_timeout <= 0:
            raise HarnessError("Command timeout must be greater than zero")
        timeout_seconds = min(configured_timeout, requested_timeout)
        flags = 0
        if os.name == "nt":
            # Without CREATE_NO_WINDOW every command run from the desktop app,
            # which has no console of its own, pops a black window on screen.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        reviewer_process_group = os.name != "nt" and os.environ.get("OUR_HARNESS_REVIEWER_PROCESS_GROUP") == "1"
        started = time.monotonic()
        deadline = started + timeout_seconds
        process = subprocess.Popen(
            actual,
            cwd=working,
            env=environment,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=flags,
            start_new_session=os.name != "nt" and not reviewer_process_group,
        )
        try:
            tree = _ProcessTree(process, reviewer_process_group=reviewer_process_group)
        except Exception:
            process.kill()
            process.wait()
            raise
        unregister_cancel = cancellation.register(tree.kill)
        capture = _BoundedCapture(limit)
        readers = [
            threading.Thread(target=capture.drain, args=(process.stdout, capture.stdout), daemon=True),
            threading.Thread(target=capture.drain, args=(process.stderr, capture.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        writer = None
        if stdin_text is not None:
            writer = threading.Thread(target=_write_stdin, args=(process.stdin, stdin_text.encode("utf-8")), daemon=True)
            writer.start()
        timed_out = False
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
        if not timed_out:
            for worker in (*readers, *((writer,) if writer is not None else ())):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                worker.join(remaining)
                if worker.is_alive():
                    timed_out = True
                    break
        if timed_out:
            tree.kill()
        else:
            # A command owns every descendant it starts. Closing the Windows
            # job or killing the POSIX group prevents detached background work
            # from surviving after a successful foreground command.
            tree.kill_descendants_after_exit()
        unregister_cancel()
        tree.close()
        cancellation.checkpoint()
        if timed_out:
            _wait_for_terminated_process(process)
        if process.poll() is None:
            threading.Thread(target=_reap_process, args=(process,), daemon=True).start()
        duration = int((time.monotonic() - started) * 1000)
        stdout, stderr, output_truncated = capture.snapshot()
        return CommandResult(
            argv=argv,
            cwd=relative_cwd,
            exit_code=124 if timed_out else int(process.returncode),
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_ms=duration,
            timed_out=timed_out,
            output_truncated=output_truncated,
        )


class _ProcessTree:
    def __init__(self, process: subprocess.Popen[bytes], *, reviewer_process_group: bool = False):
        self.process = process
        self.reviewer_process_group = reviewer_process_group
        self._closed = False
        self._job = _WindowsJob(process) if os.name == "nt" else None

    def kill(self) -> None:
        if os.name == "nt":
            if self._job is not None:
                self._job.terminate()
            return
        if self.reviewer_process_group:
            # The command shares the killable reviewer group. A command timeout
            # invalidates that reviewer, so terminate the complete isolation unit.
            os.killpg(os.getpgrp(), signal.SIGKILL)
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def kill_descendants_after_exit(self) -> None:
        if self.process.poll() is None:
            return
        if self.reviewer_process_group:
            # The panel parent kills this group after collecting the worker's
            # atomic result, which removes any background descendants without
            # terminating the worker before it can report that result.
            return
        self.kill()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._job is not None:
            self._job.close()


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _JobBasicLimitInformation(ctypes.Structure):
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

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _WindowsJob:
        _KILL_ON_JOB_CLOSE = 0x00002000
        _EXTENDED_LIMIT_INFORMATION = 9

        def __init__(self, process: subprocess.Popen[bytes]):
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32 = kernel32
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            self.handle = kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                raise HarnessError(f"Cannot create Windows process job: {ctypes.WinError(ctypes.get_last_error())}")
            information = _JobExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                self.handle,
                self._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise HarnessError(f"Cannot configure Windows process job: {error}")
            if not kernel32.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                # A job is how one command and everything it starts get stopped
                # together. Some machines will not allow it: a build server
                # already puts every step inside a job of its own, and Windows
                # refuses to put a process in a second one. Refusing to run the
                # command at all was the wrong answer to that - it made every
                # command on such a machine fail for a reason nothing to do
                # with the command. So it runs without one, and stopping it
                # stops the command itself rather than its whole tree.
                if getattr(error, "winerror", 0) == 5:
                    self.handle = None
                    return
                raise HarnessError(f"Cannot assign command to a Windows process job: {error}")

        def terminate(self) -> None:
            if self.handle:
                self._kernel32.TerminateJobObject(self.handle, 124)

        def close(self) -> None:
            if self.handle:
                self._kernel32.CloseHandle(self.handle)
                self.handle = None
else:
    _WindowsJob = None  # type: ignore[assignment,misc]


class _BoundedCapture:
    def __init__(self, limit: int):
        self.remaining = max(0, limit)
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def drain(self, pipe: object, destination: bytearray) -> None:
        if pipe is None:
            return
        try:
            read_chunk = getattr(pipe, "read1", pipe.read)
            while True:
                # BufferedReader.read(size) may wait to fill the requested
                # buffer while a descendant still holds the pipe. read1()
                # returns bytes already available from the OS pipe.
                try:
                    chunk = read_chunk(65_536)
                except OSError:
                    break
                if not chunk:
                    break
                with self._lock:
                    accepted = min(self.remaining, len(chunk))
                    if accepted:
                        destination.extend(chunk[:accepted])
                        self.remaining -= accepted
                    if accepted < len(chunk):
                        self.truncated = True
        finally:
            pipe.close()

    def snapshot(self) -> tuple[bytes, bytes, bool]:
        with self._lock:
            return bytes(self.stdout), bytes(self.stderr), self.truncated


def _write_stdin(pipe: object, payload: bytes) -> None:
    if pipe is None:
        return
    try:
        pipe.write(payload)
        pipe.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait()
    except OSError:
        pass


def _wait_for_terminated_process(process: subprocess.Popen[bytes]) -> None:
    """Boundedly release a timed-out process' cwd and pipe handles before returning."""
    try:
        process.wait(timeout=2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
