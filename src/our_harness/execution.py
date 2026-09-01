from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
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

# WinBase.h CREATE_SUSPENDED.  ``subprocess`` does not export this creation
# flag, but accepts the documented CreateProcess value in ``creationflags``.
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


_PYTHON_CWD_COMMAND_BOOTSTRAP = (
    "import sys;"
    "sys.path.insert(0,__import__('os').getcwd());"
    "del sys;"
    "exec(compile(__import__('sys').argv.pop(1),'<string>','exec'),globals(),globals())"
)
_PYTHON_CWD_MODULE_BOOTSTRAP = (
    "import os,runpy,sys;"
    "sys.path.insert(0,os.getcwd());"
    "_module=sys.argv.pop(1);"
    "runpy.run_module(_module,run_name='__main__',alter_sys=True)"
)
_PYTHON_CWD_SCRIPT_BOOTSTRAP = (
    "import os,runpy,sys;"
    "_script=sys.argv.pop(1);"
    "_path=os.path.abspath(_script);"
    "sys.path.insert(0,os.path.dirname(_path));"
    "runpy.run_path(_script,run_name='__main__')"
)
_PYTHON_CWD_STDIN_BOOTSTRAP = (
    "import os,sys;"
    "sys.path.insert(0,os.getcwd());"
    "sys.argv[0]='-';"
    "exec(compile(sys.stdin.read(),'<stdin>','exec'),globals(),globals())"
)


def _environment_value(environment: dict[str, str], name: str) -> str:
    wanted = name.casefold()
    return next(
        (value for key, value in environment.items() if key.casefold() == wanted),
        "",
    )


def _resolved_executable(
    command: str, working: Path, environment: dict[str, str],
) -> Path | None:
    """Resolve an argv executable using the same cwd/PATH visible to its child."""

    search = command
    candidate = Path(command)
    if not candidate.is_absolute() and ("/" in command or "\\" in command):
        search = os.fspath(working / candidate)
    elif os.name == "nt" and not candidate.is_absolute():
        # CreateProcess searches the parent application's directory before
        # PATH. In the installed app that is exactly why a plain ``python``
        # command finds Nexus's adjacent private runtime rather than an
        # unrelated system install returned by ``shutil.which``.
        suffixes = [""] if candidate.suffix else ["", ".exe"]
        for directory in (Path(sys.executable).parent, working):
            for suffix in suffixes:
                adjacent = directory / f"{command}{suffix}"
                if adjacent.is_file():
                    try:
                        return adjacent.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
    found = shutil.which(search, path=_environment_value(environment, "PATH") or None)
    if found is None and Path(search).is_file():
        found = search
    if found is None:
        return None
    try:
        return Path(found).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _is_embedded_python(executable: Path | None) -> bool:
    """Whether this is a resolved Python whose adjacent ``._pth`` owns imports."""

    if executable is None or not re.fullmatch(
        r"python(?:w)?(?:[0-9]+(?:\.[0-9]+)*)?(?:_d)?(?:\.exe)?",
        executable.name,
        re.IGNORECASE,
    ):
        return False
    try:
        return any(
            entry.is_file() and re.fullmatch(r"python.*\._pth", entry.name, re.IGNORECASE)
            for entry in executable.parent.iterdir()
        )
    except OSError:
        return False


def _embedded_python_cwd_argv(
    argv: list[str], working: Path, environment: dict[str, str],
) -> list[str]:
    """Restore ordinary Python cwd imports for an embedded ``._pth`` runtime.

    CPython's embeddable distribution intentionally omits the command/script
    directory and ignores ``PYTHONPATH``. That is right for Nexus itself, but a
    user-approved ``python -c``, ``python -m`` or script check must behave like
    the same argv on an ordinary Python installation. Explicit ``-I``/``-P``
    (or ``PYTHONSAFEPATH``) still means "do not expose this directory" and is
    therefore never rewritten.
    """

    if not argv or _environment_value(environment, "PYTHONSAFEPATH"):
        return list(argv)
    executable = _resolved_executable(argv[0], working, environment)
    if not _is_embedded_python(executable):
        return list(argv)

    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            if index + 1 >= len(argv):
                return list(argv)
            return [
                *argv[:index], "-c", _PYTHON_CWD_SCRIPT_BOOTSTRAP,
                argv[index + 1], *argv[index + 2:],
            ]
        if argument == "-":
            return [*argv[:index], "-c", _PYTHON_CWD_STDIN_BOOTSTRAP, *argv[index + 1:]]
        if argument == "-c":
            if index + 1 >= len(argv):
                return list(argv)
            return [
                *argv[:index], "-c", _PYTHON_CWD_COMMAND_BOOTSTRAP,
                argv[index + 1], *argv[index + 2:],
            ]
        if argument.startswith("-c") and len(argument) > 2:
            return [
                *argv[:index], "-c", _PYTHON_CWD_COMMAND_BOOTSTRAP,
                argument[2:], *argv[index + 1:],
            ]
        if argument == "-m":
            if index + 1 >= len(argv):
                return list(argv)
            return [
                *argv[:index], "-c", _PYTHON_CWD_MODULE_BOOTSTRAP,
                argv[index + 1], *argv[index + 2:],
            ]
        if argument.startswith("-m") and len(argument) > 2:
            return [
                *argv[:index], "-c", _PYTHON_CWD_MODULE_BOOTSTRAP,
                argument[2:], *argv[index + 1:],
            ]
        if not argument.startswith("-"):
            return [
                *argv[:index], "-c", _PYTHON_CWD_SCRIPT_BOOTSTRAP,
                argument, *argv[index + 1:],
            ]
        if "I" in argument[1:] or "P" in argument[1:]:
            return list(argv)
        if argument in {"-W", "-X", "--check-hash-based-pycs"}:
            index += 2
        else:
            index += 1
    return list(argv)


def _windows_process_cwd(working: Path) -> Path:
    """Return a CreateProcess-compatible spelling of an existing cwd.

    Windows still limits ``lpCurrentDirectory`` to ``MAX_PATH`` even when the
    executable and Python runtime are long-path aware.  The path has already
    passed project confinement before this helper is called, so using the
    filesystem's own short alias changes only how the same directory is handed
    to CreateProcess; it does not change command authority or reporting.
    """

    if os.name != "nt" or len(os.fspath(working)) < 260:
        return working
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32_768)
        copied = ctypes.windll.kernel32.GetShortPathNameW(
            os.fspath(working), buffer, len(buffer),
        )
        short = Path(buffer.value) if 0 < copied < len(buffer) else None
        if (
            short is None or len(os.fspath(short)) >= 260
            or not short.is_dir() or not working.samefile(short)
        ):
            raise OSError("no usable short path")
        return short
    except (AttributeError, OSError, ValueError) as exc:
        raise HarnessError(
            "Command cwd exceeds the Windows process path limit and this volume "
            "did not provide a usable 8.3 alias for the same directory."
        ) from exc


_POWERSHELL_PROGRAMS = frozenset({"powershell", "pwsh"})
_POWERSHELL_PAYLOAD_SWITCHES = frozenset({
    "command", "commandwithargs", "encodedcommand", "file",
})
_POWERSHELL_VALUE_SWITCHES = frozenset({
    "configurationname", "custompipename", "executionpolicy", "inputformat",
    "outputformat", "psconsolefile", "settingsfile", "version",
    "windowstyle", "workingdirectory",
})
_POWERSHELL_SWITCH_ALIASES = {
    "c": "command",
    "cwa": "commandwithargs",
    "e": "encodedcommand",
    "ec": "encodedcommand",
    "enc": "encodedcommand",
    "f": "file",
}


def _program_leaf(word: str) -> str:
    """Portable executable leaf for the small amount of argv-aware parsing below."""

    leaf = re.split(r"[\\/]", str(word).strip().strip("\"'"))[-1].casefold()
    for ending in (".exe", ".com", ".bat", ".cmd"):
        if leaf.endswith(ending):
            return leaf[: -len(ending)]
    return leaf


def _said_as(word: str, *, one_dash_named: bool = False) -> tuple[str, set[str]]:
    """One word of a command, as either a plain word or the switches it holds.

    The same switch turns up as --force, -Force, /force and /force:yes, and a
    bundle such as -xfd holds three of them. A plain word comes back as itself
    with no switches; a switch comes back as no word with its letters. The
    caller marks the one-dash named-parameter context used by PowerShell.
    """

    plain = word.split(":", 1)[0].split("=", 1)[0].casefold()
    if plain.startswith("--") or plain.startswith("/"):
        return "", {plain.lstrip("-/")}
    if plain.startswith("-") and len(plain) > 1:
        letters = plain.lstrip("-")
        # Outside a command whose own grammar has named one-dash parameters,
        # every alphabetic spelling is conservatively a short-option bundle:
        # git accepts -xfd, -xdf and -nfd as combinations containing -f/-d.
        # Treating arbitrary three-letter spellings as long options lets those
        # destructive forms evade both ``clean -fd`` and ``--force`` policy.
        if one_dash_named or not letters.isalpha():
            return "", {letters}
        return "", set(letters)
    return plain, set()


def _reads_as(words: list[str]) -> tuple[list[str], set[str]]:
    """A whole command as the words it names and the switches it asks for."""

    named: list[str] = []
    switches: set[str] = set()
    arguments = [str(word) for word in words]
    powershell_parameters = bool(
        arguments and _program_leaf(arguments[0]) in _POWERSHELL_PROGRAMS
    )
    powershell_value_pending = False
    for index, argument in enumerate(arguments):
        # argv[0] may itself contain spaces. Later arguments are also searched
        # word-by-word because a shell's -c/-Command payload is commonly one
        # argv element and deny rules must still see commands inside it.
        parts = [argument] if index == 0 else argument.split()
        for part_index, word in enumerate(parts):
            launcher_executable = index == 0 and part_index == 0
            expecting_named_value = powershell_parameters and powershell_value_pending
            plain, found = _said_as(
                word,
                one_dash_named=powershell_parameters and not expecting_named_value,
            )
            if powershell_parameters and not expecting_named_value:
                found = {
                    _POWERSHELL_SWITCH_ALIASES.get(one, one) for one in found
                }
            if plain:
                named.append(plain)
            switches |= found
            if not powershell_parameters or launcher_executable:
                continue
            if expecting_named_value:
                powershell_value_pending = False
                continue
            # -File/-Command belongs to PowerShell, but what follows belongs to
            # a script or command payload and must regain ordinary bundle
            # parsing. This keeps ``pwsh -Command 'git clean -xfd'`` denied.
            if found & _POWERSHELL_PAYLOAD_SWITCHES:
                powershell_parameters = False
            elif (
                found & _POWERSHELL_VALUE_SWITCHES
                and ":" not in word and "=" not in word
            ):
                # Launcher options such as ``-ExecutionPolicy Bypass`` consume
                # one plain value before option parsing resumes.
                powershell_value_pending = True
            elif plain:
                # -Command is PowerShell's default when a command is supplied
                # positionally. Do not let that spelling keep later git flags
                # in named-parameter mode.
                powershell_parameters = False
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
        for sequence in self.config.get("execution.deny_argument_sequences", []):
            wanted = str(sequence).lower()
            if wanted in normalized or _matches_rule(wanted, argv):
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
        environment_overrides: dict[str, str] | None = None,
    ) -> CommandResult:
        self._check(argv)
        working = confined_path(self.root, cwd, allow_missing=False)
        if not working.is_dir():
            raise HarnessError(f"Command cwd is not a directory: {cwd}")
        # ``confined_path`` has already validated this user-supplied relative
        # path.  Keep that spelling rather than deriving it from two resolved
        # Windows paths: GitHub runners can return the same temporary folder as
        # both RUNNER~1 and runneradmin, and pathlib quite correctly refuses to
        # compare those strings even though Windows opens the same directory.
        relative_cwd = Path(*re.split(r"[\\/]", os.fspath(cwd))).as_posix() or "."
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
        process_cwd = _windows_process_cwd(working)
        environment = safe_environment(self.config.get("execution.inherit_environment", []))
        if environment_overrides:
            if not all(
                isinstance(key, str) and key and "=" not in key
                and isinstance(value, str) and "\x00" not in value
                for key, value in environment_overrides.items()
            ):
                raise HarnessError("Command environment overrides must be plain string names and values")
            environment.update(environment_overrides)
        actual = _embedded_python_cwd_argv(actual, process_cwd, environment)
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
            # Start suspended so the process cannot launch a child in the gap
            # between CreateProcess and assignment to our containment job.
            flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
                | _WINDOWS_CREATE_SUSPENDED
            )
        reviewer_process_group = os.name != "nt" and os.environ.get("OUR_HARNESS_REVIEWER_PROCESS_GROUP") == "1"
        started = time.monotonic()
        deadline = started + timeout_seconds
        def start_process(creation_flags: int) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                actual,
                cwd=process_cwd,
                env=environment,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creation_flags,
                start_new_session=os.name != "nt" and not reviewer_process_group,
            )

        if os.name == "nt":
            process, tree = _start_windows_contained_process(
                lambda: start_process(flags | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB),
                lambda: start_process(flags),
                label="Command",
            )
        else:
            process = start_process(flags)
            try:
                tree = _ProcessTree(
                    process, reviewer_process_group=reviewer_process_group
                )
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
        timed_out = not tree.wait_for_root_until(deadline)
        if not timed_out:
            for worker in (*readers, *((writer,) if writer is not None else ())):
                if not tree.join_worker_until(worker, deadline):
                    timed_out = True
                    break
        workers = (*readers, *((writer,) if writer is not None else ()))
        try:
            settled = _settle_process_tree(tree, workers, terminate=timed_out)
        finally:
            unregister_cancel()
            tree.close()
        cancellation.checkpoint()
        if not settled and process.poll() is None:
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
        self._lock = threading.Lock()
        self._termination_lock = threading.Lock()
        self._windows_tree_lock = threading.Lock()
        self._windows_tree_tokens: dict[int, int] = {}
        self._windows_tree_parents: dict[int, int] = {}
        self._windows_tree_handles: dict[int, int] = {}
        self._windows_root_handle: int | None = None
        self._job = _WindowsJob(process) if os.name == "nt" else None
        self._remember_windows_root()

    @property
    def windows_contained(self) -> bool:
        return os.name != "nt" or bool(
            self._job is not None and getattr(self._job, "handle", None)
        )

    def _remember_windows_root(self) -> None:
        if os.name != "nt":
            return
        try:
            handle = int(getattr(self.process, "_handle"))
        except (AttributeError, TypeError, ValueError):
            return
        token = _windows_process_creation_token(handle)
        if token is None:
            return
        with self._windows_tree_lock:
            self._windows_root_handle = handle
            self._windows_tree_tokens[self.process.pid] = token
            self._windows_tree_parents[self.process.pid] = 0

    def remember_windows_process_tree(self) -> None:
        """Retain creation-time identities for brokered descendants.

        Store/venv launchers can broker the real executable outside a Windows
        job.  Polling while the request is active preserves each ancestry hop;
        creation tokens later prevent a reused PID from becoming a kill target.
        """

        if os.name != "nt":
            return
        processes, snapshot_cutoff = _windows_process_snapshot()
        if not processes:
            return
        with self._windows_tree_lock:
            known_tokens = dict(self._windows_tree_tokens)
            known_handles = dict(self._windows_tree_handles)
            if self._windows_root_handle is not None:
                known_handles[self.process.pid] = self._windows_root_handle
        if not known_tokens:
            return

        descendants: set[int] = set()
        changed = True
        while changed:
            changed = False
            ancestry = set(known_tokens) | descendants
            for pid, parent in processes.items():
                if pid not in ancestry and parent in ancestry:
                    descendants.add(pid)
                    changed = True
        if not descendants:
            return

        # Pin each candidate before trusting it.  The Toolhelp row can outlive
        # the process that occupied its numeric PID.  A replacement opened
        # after that snapshot necessarily has a creation time newer than the
        # cutoff captured before CreateToolhelp32Snapshot and is rejected.
        observed: dict[int, tuple[int, int]] = {}
        for pid in descendants:
            handle = _windows_open_process_handle(pid)
            if handle is None:
                continue
            times = _windows_process_times(handle)
            if times is None or times[0] > snapshot_cutoff:
                _windows_close_process_handle(handle)
                continue
            observed[pid] = (handle, times[0])

        # Validate one ancestry edge at a time against handles that pin the
        # actual process identities.  If a remembered parent has exited,
        # Windows may later reuse its PID; a real child must have been created
        # no later than that parent's recorded exit time.  This ordering check
        # prevents a child of a replacement process from joining our tree.
        validated: set[int] = set()
        changed = True
        while changed:
            changed = False
            parents = set(known_tokens) | validated
            for pid, (handle, token) in observed.items():
                if pid in validated:
                    continue
                parent_pid = processes.get(pid, 0)
                if parent_pid not in parents:
                    continue
                parent_handle = (
                    observed[parent_pid][0]
                    if parent_pid in validated
                    else known_handles.get(parent_pid)
                )
                if parent_handle is None:
                    continue
                parent_times = _windows_process_times(parent_handle)
                if parent_times is None or token < parent_times[0]:
                    continue
                parent_exit = _windows_process_exit_time(parent_handle)
                if parent_exit is not None and token > parent_exit:
                    continue
                validated.add(pid)
                changed = True

        with self._windows_tree_lock:
            for pid, (handle, token) in observed.items():
                if pid not in validated:
                    _windows_close_process_handle(handle)
                    continue
                existing = self._windows_tree_tokens.get(pid)
                if existing is None:
                    self._windows_tree_tokens[pid] = token
                    self._windows_tree_parents[pid] = processes.get(pid, 0)
                    self._windows_tree_handles[pid] = handle
                else:
                    # The first retained handle is the immutable identity for
                    # this PID.  Never replace it from a later numeric scan.
                    _windows_close_process_handle(handle)

    def _terminate_remembered_windows_processes(self, deadline_at: float) -> bool:
        if os.name != "nt":
            return True
        previous_signature: tuple[tuple[int, int], ...] | None = None
        stable_empty_scans = 0
        while time.monotonic() < deadline_at:
            self.remember_windows_process_tree()
            with self._windows_tree_lock:
                tokens = dict(self._windows_tree_tokens)
                parents = dict(self._windows_tree_parents)
                handles = dict(self._windows_tree_handles)

            def depth(pid: int) -> int:
                seen: set[int] = set()
                current = pid
                value = 0
                while current in parents and current not in seen:
                    seen.add(current)
                    current = parents[current]
                    value += 1
                return value

            live = [
                pid for pid, handle in handles.items()
                if _windows_process_handle_is_running(handle)
                and _windows_process_creation_token(handle) == tokens.get(pid)
            ]
            signature = tuple(sorted(tokens.items()))
            if not live:
                stable_empty_scans = (
                    stable_empty_scans + 1
                    if signature == previous_signature else 1
                )
                if stable_empty_scans >= 2:
                    return True
                previous_signature = signature
                time.sleep(min(0.005, max(0.0, deadline_at - time.monotonic())))
                continue

            stable_empty_scans = 0
            previous_signature = signature
            for pid in sorted(live, key=depth, reverse=True):
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    return False
                handle = handles[pid]
                # The retained handle, not the numeric PID, is the destructive
                # target.  Revalidate its creation token immediately before
                # TerminateProcess as an additional fail-closed guard.
                if _windows_process_creation_token(handle) != tokens[pid]:
                    continue
                _terminate_windows_process_handle(
                    handle, timeout_seconds=min(0.1, remaining)
                )
            # A terminating launcher may create one last child after the prior
            # snapshot.  Rescan from retained (even exited) intermediaries and
            # require two stable empty observations before declaring quiescence.
        return False

    def kill(self) -> None:
        if os.name == "nt":
            # Normal execution is always assigned to our private job before
            # resume.  If Windows nevertheless refuses job termination, use
            # taskkill's bounded tree fallback while the root PID still names
            # the family, then reap it below.
            with self._termination_lock:
                deadline = time.monotonic() + 1.0
                self._terminate_remembered_windows_processes(deadline)
                with self._lock:
                    job = self._job
                    assigned = bool(job is not None and getattr(job, "handle", None))
                    terminated = job.terminate() if assigned else False
                if not terminated:
                    _terminate_windows_process_tree(
                        self.process,
                        timeout_seconds=max(0.05, deadline - time.monotonic()),
                    )
            return
        with self._termination_lock:
            if self.reviewer_process_group:
                # The command shares the killable reviewer group. A command timeout
                # invalidates that reviewer, so terminate the complete isolation unit.
                os.killpg(os.getpgrp(), signal.SIGKILL)
                return
            # A reaped process no longer reserves its numeric PID/PGID.  Signalling
            # that number can therefore hit an unrelated process group after PID
            # reuse.  Normal command waits use waitid(WNOWAIT) below so the exited
            # leader remains a zombie (and keeps the identity reserved) until this
            # group signal has completed.  Holding the same lock used by the final
            # reap closes the cancellation-vs-reap race around this check and signal.
            if self.process.returncode is not None:
                return
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def kill_descendants_after_exit(self) -> None:
        if self.reviewer_process_group:
            # The panel parent kills this group after collecting the worker's
            # atomic result, which removes any background descendants without
            # terminating the worker before it can report that result.
            return
        # Do not call poll(): it reaps an exited leader and creates a PID-reuse
        # window before killpg.  wait_for_root_until() deliberately observes a
        # normal POSIX exit without reaping when the platform supports WNOWAIT.
        # If another caller already reaped the process, kill() safely declines
        # the now-unverifiable numeric process-group target.
        self.kill()

    def close(self) -> None:
        with self._termination_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                if self._job is not None:
                    self._job.close()
            with self._windows_tree_lock:
                handles = list(self._windows_tree_handles.values())
                self._windows_tree_handles.clear()
            for handle in handles:
                _windows_close_process_handle(handle)

    def wait_until_terminated(self, timeout_seconds: float) -> bool:
        """Wait boundedly for the root and every job-owned descendant.

        The Windows job handle must stay open throughout this wait.  Waiting
        only for ``Popen`` proves that the foreground process exited, not that
        a child released inherited pipes or the command cwd.
        """

        deadline = time.monotonic() + max(0.001, float(timeout_seconds))
        with self._termination_lock:
            tracked_done = self._terminate_remembered_windows_processes(deadline)
            root_done = _wait_for_terminated_process(
                self.process, timeout_seconds=max(0.001, deadline - time.monotonic())
            )
            job_done = True
            if os.name == "nt":
                with self._lock:
                    job = self._job
                    assigned = bool(job is not None and getattr(job, "handle", None))
                if assigned:
                    job_done = job.wait_until_empty(
                        max(0.001, deadline - time.monotonic())
                    )
                tracked_done = self._terminate_remembered_windows_processes(deadline)
            return root_done and job_done and tracked_done

    def wait_for_root_until(self, deadline_at: float) -> bool:
        """Wait for the foreground process while observing brokered children."""

        if os.name != "nt":
            if not self.reviewer_process_group:
                observed = _wait_for_posix_root_without_reaping(
                    self.process, deadline_at
                )
                if observed is not None:
                    return observed
            try:
                self.process.wait(timeout=max(0.001, deadline_at - time.monotonic()))
                return True
            except subprocess.TimeoutExpired:
                return False
        while True:
            self.remember_windows_process_tree()
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                return self.process.poll() is not None
            try:
                self.process.wait(timeout=min(0.01, remaining))
                self.remember_windows_process_tree()
                return True
            except subprocess.TimeoutExpired:
                continue

    def join_worker_until(
        self,
        worker: threading.Thread,
        deadline_at: float,
        *,
        terminate_observed: bool = False,
    ) -> bool:
        """Join one pipe pump while observing processes that retain its pipe."""

        if os.name != "nt":
            worker.join(max(0, deadline_at - time.monotonic()))
            return not worker.is_alive()
        while worker.is_alive():
            self.remember_windows_process_tree()
            if terminate_observed:
                with self._termination_lock:
                    self._terminate_remembered_windows_processes(deadline_at)
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                return False
            worker.join(min(0.01, remaining))
        self.remember_windows_process_tree()
        return True


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _discard_suspended_process(
    process: subprocess.Popen[bytes], tree: _ProcessTree | None,
) -> None:
    """Reap a CREATE_SUSPENDED process before any of its code can run."""

    try:
        if tree is not None and tree.windows_contained:
            tree.kill()
            tree.wait_until_terminated(2.0)
        else:
            _wait_for_terminated_process(process, timeout_seconds=2.0)
    finally:
        if tree is not None:
            tree.close()
        _close_process_pipes(process)


def _start_windows_contained_process(
    start_breakaway: Callable[[], subprocess.Popen[bytes]],
    start_nested: Callable[[], subprocess.Popen[bytes]],
    *,
    label: str,
) -> tuple[subprocess.Popen[bytes], _ProcessTree]:
    """Create outside an inherited job, assign privately, then resume.

    Parent CI jobs can enable SILENT_BREAKAWAY, which can let descendants escape
    a nested private job even though assigning the root appeared to succeed.
    Proactively create the suspended root with CREATE_BREAKAWAY_FROM_JOB so our
    job is its only owner.  If the parent explicitly denies breakaway, retry a
    still-suspended nested launch and require private assignment there.  In
    every path user code starts only after containment succeeds.
    """

    try:
        process = start_breakaway()
    except OSError as breakaway_error:
        if getattr(breakaway_error, "winerror", None) != 5:
            raise HarnessError(f"{label} could not start: {breakaway_error}") from breakaway_error
        try:
            process = start_nested()
        except OSError as nested_error:
            raise HarnessError(f"{label} could not start: {nested_error}") from nested_error
    tree: _ProcessTree | None = None
    try:
        tree = _ProcessTree(process)
    except Exception:
        _discard_suspended_process(process, tree)
        raise
    if not tree.windows_contained:
        _discard_suspended_process(process, tree)
        raise HarnessError(
            f"{label} cannot run safely because Windows refused its private process job"
        )
    if not _resume_windows_process(process):
        _discard_suspended_process(process, tree)
        raise HarnessError(f"{label} could not resume its contained Windows process")
    return process, tree


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

    class _JobBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _WindowsJob:
        _KILL_ON_JOB_CLOSE = 0x00002000
        _BASIC_ACCOUNTING_INFORMATION = 1
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
            kernel32.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            kernel32.QueryInformationJobObject.restype = wintypes.BOOL
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
                # A nested build/CI job can refuse assignment.  The caller has
                # deliberately not resumed this process yet: it may retry a
                # suspended breakaway launch, or fail closed before user code
                # gets a chance to start a descendant outside containment.
                if getattr(error, "winerror", 0) == 5:
                    self.handle = None
                    return
                raise HarnessError(f"Cannot assign command to a Windows process job: {error}")

        def terminate(self) -> bool:
            if not self.handle:
                return False
            return bool(self._kernel32.TerminateJobObject(self.handle, 124))

        def wait_until_empty(self, timeout_seconds: float) -> bool:
            """Wait until Windows reports no active process in this job."""

            if not self.handle:
                return False
            deadline = time.monotonic() + max(0.001, float(timeout_seconds))
            while True:
                information = _JobBasicAccountingInformation()
                queried = self._kernel32.QueryInformationJobObject(
                    self.handle,
                    self._BASIC_ACCOUNTING_INFORMATION,
                    ctypes.byref(information),
                    ctypes.sizeof(information),
                    None,
                )
                if not queried:
                    return False
                if int(information.ActiveProcesses) == 0:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                # QueryInformationJobObject has no event for the general
                # TerminateJobObject case.  A short bounded poll retains the
                # job handle until the kernel releases all process references.
                time.sleep(min(0.01, remaining))

        def close(self) -> None:
            if self.handle:
                self._kernel32.CloseHandle(self.handle)
                self.handle = None

    def _resume_windows_process(process: subprocess.Popen[bytes]) -> bool:
        """Resume every initial thread after the process is contained.

        ``subprocess.Popen`` closes CreateProcess' primary-thread handle before
        returning.  Enumerating the still-suspended process' thread is the
        documented Win32 route left to resume it.  No user code can execute or
        spawn a descendant before this function succeeds.
        """

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == wintypes.HANDLE(-1).value:
            return False
        resumed = False
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            more = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while more:
                if int(entry.th32OwnerProcessID) == int(process.pid):
                    thread = kernel32.OpenThread(
                        0x0002,  # THREAD_SUSPEND_RESUME
                        False,
                        entry.th32ThreadID,
                    )
                    if thread:
                        try:
                            if int(kernel32.ResumeThread(thread)) != 0xFFFFFFFF:
                                resumed = True
                        finally:
                            kernel32.CloseHandle(thread)
                entry.dwSize = ctypes.sizeof(entry)
                more = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)
        return resumed

    def _windows_process_times(handle: int) -> tuple[int, int] | None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        created = _FileTime()
        exited = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not kernel32.GetProcessTimes(
            wintypes.HANDLE(handle),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (
            (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime),
            (int(exited.dwHighDateTime) << 32) | int(exited.dwLowDateTime),
        )

    def _windows_process_creation_token(handle: int) -> int | None:
        times = _windows_process_times(handle)
        return None if times is None else times[0]

    def _windows_open_process_handle(pid: int) -> int | None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            0x0001 | 0x00100000 | 0x1000,
            # PROCESS_TERMINATE | SYNCHRONIZE |
            # PROCESS_QUERY_LIMITED_INFORMATION
            False,
            int(pid),
        )
        return int(handle) if handle else None

    def _windows_close_process_handle(handle: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))

    def _windows_process_handle_is_running(handle: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        return int(kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0)) == 0x102

    def _windows_process_exit_time(handle: int) -> int | None:
        """Return None while live; fail closed with zero for an invalid exit."""

        if _windows_process_handle_is_running(handle):
            return None
        times = _windows_process_times(handle)
        if times is None or times[1] <= 0:
            return 0
        return times[1]

    def _windows_filetime_now() -> int:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        value = _FileTime()
        try:
            precise = kernel32.GetSystemTimePreciseAsFileTime
        except AttributeError:
            precise = kernel32.GetSystemTimeAsFileTime
        precise.argtypes = [ctypes.POINTER(_FileTime)]
        precise.restype = None
        precise(ctypes.byref(value))
        return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

    def _windows_process_snapshot() -> tuple[dict[int, int], int]:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # The cutoff precedes the Toolhelp capture.  If the snapshotted PID
        # exits and is reused before OpenProcess, its replacement is newer than
        # this value and therefore cannot be enrolled from the stale row.
        snapshot_cutoff = _windows_filetime_now()
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            return {}, snapshot_cutoff
        processes: dict[int, int] = {}
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            more = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
            while more:
                processes[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                entry.dwSize = ctypes.sizeof(entry)
                more = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)
        return processes, snapshot_cutoff

    def _terminate_windows_process_handle(
        handle: int, *, timeout_seconds: float,
    ) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        native = wintypes.HANDLE(handle)
        if not kernel32.TerminateProcess(native, 124):
            if int(kernel32.WaitForSingleObject(native, 0)) != 0:
                return False
        wait_ms = max(1, int(max(0.001, timeout_seconds) * 1000))
        return int(kernel32.WaitForSingleObject(native, wait_ms)) == 0

else:
    _WindowsJob = None  # type: ignore[assignment,misc]

    def _resume_windows_process(_process: subprocess.Popen[bytes]) -> bool:
        return True

    def _windows_process_creation_token(_handle: int) -> int | None:
        return None

    def _windows_process_times(_handle: int) -> tuple[int, int] | None:
        return None

    def _windows_open_process_handle(_pid: int) -> int | None:
        return None

    def _windows_close_process_handle(_handle: int) -> None:
        return None

    def _windows_process_handle_is_running(_handle: int) -> bool:
        return False

    def _windows_process_exit_time(_handle: int) -> int | None:
        return 0

    def _windows_filetime_now() -> int:
        return 0

    def _windows_process_snapshot() -> tuple[dict[int, int], int]:
        return {}, 0

    def _terminate_windows_process_handle(
        _handle: int, *, timeout_seconds: float,
    ) -> bool:
        return True



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


def _terminate_windows_process_tree(
    process: subprocess.Popen[bytes], *, timeout_seconds: float,
) -> bool:
    """Use Windows' tree-aware fallback when a private job was unavailable."""

    if os.name != "nt":
        return False
    # Run this while the root PID is still alive.  Killing just the root first
    # can leave an already-started child outside any tree we can identify.
    completed = False
    if process.poll() is None:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.05, float(timeout_seconds)),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            completed = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    return completed


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait()
    except OSError:
        pass


def _wait_for_terminated_process(
    process: subprocess.Popen[bytes], *, timeout_seconds: float = 4.0,
) -> bool:
    """Boundedly kill and reap one process before its workspace is removed."""

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=max(0.001, float(timeout_seconds)))
        return process.poll() is not None
    except (OSError, subprocess.TimeoutExpired):
        return process.poll() is not None


def _wait_for_posix_root_without_reaping(
    process: subprocess.Popen[bytes], deadline_at: float,
) -> bool | None:
    """Observe one POSIX child exit while retaining its PID/PGID identity.

    ``None`` means this platform cannot provide a non-reaping wait and the
    caller must use the ordinary Popen wait.  In that fallback, later process-
    group cleanup intentionally skips killpg once Popen has reaped the leader.
    """

    waitid = getattr(os, "waitid", None)
    p_pid = getattr(os, "P_PID", None)
    wexited = getattr(os, "WEXITED", None)
    wnohang = getattr(os, "WNOHANG", None)
    wnowait = getattr(os, "WNOWAIT", None)
    if (
        not callable(waitid)
        or p_pid is None
        or wexited is None
        or wnohang is None
        or wnowait is None
    ):
        return None
    flags = int(wexited) | int(wnohang) | int(wnowait)
    while True:
        # A Popen method used elsewhere may already have reaped the process.
        # Report completion without pretending its numeric group is still safe.
        if process.returncode is not None:
            return True
        try:
            status = waitid(p_pid, process.pid, flags)
        except (ChildProcessError, OSError):
            return None
        if status is not None:
            return True
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _settle_process_tree(
    tree: _ProcessTree,
    workers: tuple[threading.Thread, ...] | list[threading.Thread],
    *,
    terminate: bool,
    timeout_seconds: float = 5.0,
) -> bool:
    """Terminate a command tree and quiesce every stdio pump, boundedly.

    Keeping the tree (and therefore its Windows job handle) open until after
    the readers and writer finish is intentional.  A foreground process can
    be reaped while a descendant still owns the cwd or inherited pipe handles.
    """

    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    if terminate:
        tree.kill()
    else:
        tree.kill_descendants_after_exit()
    tree_done = tree.wait_until_terminated(
        max(0.001, deadline - time.monotonic())
    )
    workers_done = True
    for worker in workers:
        if not tree.join_worker_until(
            worker, deadline, terminate_observed=True
        ):
            workers_done = False
            break
    return tree_done and workers_done and tree.process.poll() is not None
