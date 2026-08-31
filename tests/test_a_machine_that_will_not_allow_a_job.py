"""A Windows command is contained before any of its code can run.

On Windows the harness puts each command it starts into a "job", which is how
one command and everything it starts get stopped together. Some machines will
not allow that: a build server already puts every step inside a job of its own,
and Windows refuses to put a process into a second one.

The engine starts it suspended, retries outside the runner's job, and assigns
its own job before resuming. If Windows refuses both safe routes, it fails
before the command can produce a side effect.
"""

from __future__ import annotations

import copy
import ctypes
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from our_harness import execution
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class NoJobAllowedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def test_breakaway_creation_refusal_retries_nested_before_command_runs(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows jobs only exist on Windows")
        runner = execution.CommandRunner(self.config)
        real_popen = execution.subprocess.Popen
        flags: list[int] = []

        def start(*args, **kwargs):
            creation_flags = int(kwargs.get("creationflags", 0))
            flags.append(creation_flags)
            if creation_flags & execution._WINDOWS_CREATE_BREAKAWAY_FROM_JOB:
                raise ctypes.WinError(5)
            return real_popen(*args, **kwargs)

        with mock.patch.object(execution.subprocess, "Popen", side_effect=start):
            done = runner.run([sys.executable, "-c", "print('it ran')"], cwd=".", timeout=30)
        self.assertTrue(done.passed, done.stderr)
        self.assertIn("it ran", done.stdout)
        self.assertEqual(len(flags), 2)
        self.assertTrue(flags[0] & execution._WINDOWS_CREATE_SUSPENDED)
        self.assertTrue(flags[0] & execution._WINDOWS_CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(flags[1] & execution._WINDOWS_CREATE_SUSPENDED)
        self.assertFalse(flags[1] & execution._WINDOWS_CREATE_BREAKAWAY_FROM_JOB)

    def test_double_refusal_fails_closed_before_command_side_effect(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows jobs only exist on Windows")
        runner = execution.CommandRunner(self.config)
        marker = self.root / "must-not-exist.txt"
        command = [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ]

        with mock.patch.object(
            execution, "_WindowsJob", side_effect=lambda process: _RefusingJob(process)
        ):
            with self.assertRaisesRegex(HarnessError, "refused its private process job"):
                runner.run(command, cwd=".", timeout=30)
        self.assertFalse(marker.exists())

    def test_resume_failure_fails_closed_before_command_side_effect(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows jobs only exist on Windows")
        runner = execution.CommandRunner(self.config)
        marker = self.root / "resume-must-not-exist.txt"
        command = [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ]

        with mock.patch.object(execution, "_resume_windows_process", return_value=False):
            with self.assertRaisesRegex(HarnessError, "could not resume"):
                runner.run(command, cwd=".", timeout=30)
        self.assertFalse(marker.exists())

    def test_the_real_one_carries_on_when_windows_says_access_denied(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows jobs only exist on Windows")
        # The real class, with only the one call that a build server refuses
        # made to fail. Everything else is the real thing.
        started = execution.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            stdout=execution.subprocess.PIPE, stderr=execution.subprocess.PIPE,
        )
        self.addCleanup(started.wait)
        self.addCleanup(started.stdout.close)
        self.addCleanup(started.stderr.close)
        real_dll = ctypes.WinDLL

        class Refusing:
            def __init__(self, name, use_last_error=False):
                self._real = real_dll(name, use_last_error=use_last_error)

            def __getattr__(self, name):
                if name == "AssignProcessToJobObject":
                    def no(*_args):
                        ctypes.set_last_error(5)
                        return 0
                    no.argtypes = []
                    no.restype = None
                    return no
                return getattr(self._real, name)

        with mock.patch.object(ctypes, "WinDLL", Refusing):
            job = execution._WindowsJob(started)
        self.assertIsNone(job.handle, "it should carry on without a job")
        # And stopping it must not fall over on a job it never got.
        job.terminate()
        job.close()

    def test_tracker_rejects_pid_created_after_toolhelp_cutoff(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows process identities only exist on Windows")
        root = execution.subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time;time.sleep(10)"],
            stdin=execution.subprocess.DEVNULL,
            stdout=execution.subprocess.DEVNULL,
            stderr=execution.subprocess.DEVNULL,
        )
        candidate = execution.subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time;time.sleep(10)"],
            stdin=execution.subprocess.DEVNULL,
            stdout=execution.subprocess.DEVNULL,
            stderr=execution.subprocess.DEVNULL,
        )
        tree = None
        try:
            with mock.patch.object(execution, "_WindowsJob", _ObservedJob):
                tree = execution._ProcessTree(root)
            # The stale Toolhelp row claims candidate belonged to root.  A zero
            # cutoff proves the process now occupying that PID was created only
            # after the snapshot began, so it must never be retained or killed.
            with mock.patch.object(
                execution,
                "_windows_process_snapshot",
                return_value=({root.pid: 0, candidate.pid: root.pid}, 0),
            ):
                tree.remember_windows_process_tree()
            self.assertNotIn(candidate.pid, tree._windows_tree_handles)
            self.assertIsNone(candidate.poll())
        finally:
            if tree is not None:
                tree.close()
            for process in (root, candidate):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_tracker_rejects_child_created_after_remembered_parent_exit(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows process identities only exist on Windows")
        root = execution.subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time;time.sleep(10)"],
            stdin=execution.subprocess.DEVNULL,
            stdout=execution.subprocess.DEVNULL,
            stderr=execution.subprocess.DEVNULL,
        )
        candidate = execution.subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time;time.sleep(10)"],
            stdin=execution.subprocess.DEVNULL,
            stdout=execution.subprocess.DEVNULL,
            stderr=execution.subprocess.DEVNULL,
        )
        tree = None
        try:
            with mock.patch.object(execution, "_WindowsJob", _ObservedJob):
                tree = execution._ProcessTree(root)
            # Simulate a row whose PPID is the numeric PID of a remembered but
            # already-exited parent.  The candidate's later creation time means
            # it belongs to a PID replacement, not to this command tree.
            with mock.patch.object(
                execution,
                "_windows_process_snapshot",
                return_value=(
                    {root.pid: 0, candidate.pid: root.pid},
                    (1 << 63) - 1,
                ),
            ), mock.patch.object(
                execution, "_windows_process_exit_time", return_value=1
            ):
                tree.remember_windows_process_tree()
            self.assertNotIn(candidate.pid, tree._windows_tree_handles)
            self.assertIsNone(candidate.poll())
        finally:
            if tree is not None:
                tree.close()
            for process in (root, candidate):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_tracker_keeps_first_seen_intermediate_that_exits_after_open(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows broker descendants only exist on Windows")
        ready = self.root / "broker-ready.txt"
        trigger = self.root / "spawn-now.txt"
        child_marker = self.root / "final-child.txt"
        broker_code = (
            "import os,pathlib,subprocess,sys,time; "
            f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
            f"trigger=pathlib.Path({str(trigger)!r}); "
            "list(iter(lambda:(time.sleep(0.005),trigger.exists())[1],True)); "
            "child_code=\"import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "time.sleep(30)\"; "
            f"subprocess.Popen([sys.executable,'-c',child_code,{str(child_marker)!r}]);"
        )
        broker_launcher = execution.subprocess.Popen(
            [
                getattr(sys, "_base_executable", sys.executable),
                "-u", "-c", broker_code,
            ],
            stdin=execution.subprocess.DEVNULL,
            stdout=execution.subprocess.DEVNULL,
            stderr=execution.subprocess.DEVNULL,
        )
        ready_deadline = time.monotonic() + 3.0
        while not ready.exists() and time.monotonic() < ready_deadline:
            time.sleep(0.005)
        self.assertTrue(ready.exists(), "fixture broker did not start")
        broker_pid = int(ready.read_text())
        root_pid = os.getpid()
        root_handle = execution._windows_open_process_handle(root_pid)
        self.assertIsNotNone(root_handle)
        root_token = execution._windows_process_creation_token(root_handle)
        self.assertIsNotNone(root_token)

        tree = execution._ProcessTree.__new__(execution._ProcessTree)
        tree.process = types.SimpleNamespace(pid=root_pid)
        tree.reviewer_process_group = False
        tree._closed = False
        tree._lock = threading.Lock()
        tree._termination_lock = threading.Lock()
        tree._windows_tree_lock = threading.Lock()
        tree._windows_tree_tokens = {root_pid: root_token}
        tree._windows_tree_parents = {root_pid: 0}
        tree._windows_tree_handles = {}
        tree._windows_root_handle = root_handle
        tree._job = _ObservedJob(None)
        real_open = execution._windows_open_process_handle
        state = {"opened_then_exited": False}

        def controlled_snapshot():
            rows = {root_pid: 0, broker_pid: root_pid}
            if child_marker.exists():
                rows[int(child_marker.read_text())] = broker_pid
            return rows, execution._windows_filetime_now()

        def open_then_exit_first_seen(pid):
            handle = real_open(pid)
            if (
                handle is None or pid != broker_pid
                or state["opened_then_exited"]
            ):
                return handle
            # The first snapshot found this intermediate and its handle is now
            # pinned, but it is not enrolled yet.  Spawn the last child and let
            # the intermediate exit before returning the handle to validation.
            trigger.write_text("spawn")
            deadline = time.monotonic() + 3.0
            while not child_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(child_marker.exists(), "fixture final child did not start")
            while (
                execution._windows_process_handle_is_running(handle)
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            self.assertFalse(
                execution._windows_process_handle_is_running(handle),
                "fixture first-seen broker did not exit",
            )
            state["opened_then_exited"] = True
            return handle

        try:
            with mock.patch.object(
                execution, "_windows_process_snapshot", side_effect=controlled_snapshot
            ), mock.patch.object(
                execution,
                "_windows_open_process_handle",
                side_effect=open_then_exit_first_seen,
            ):
                stopped = tree._terminate_remembered_windows_processes(
                    time.monotonic() + 1.5
                )
            self.assertTrue(stopped)
            self.assertTrue(state["opened_then_exited"])
            child_pid = int(child_marker.read_text())
            self.assertIn(child_pid, tree._windows_tree_handles)
            self.assertFalse(
                execution._windows_process_handle_is_running(
                    tree._windows_tree_handles[child_pid]
                )
            )
        finally:
            tree.close()
            execution._windows_close_process_handle(root_handle)
            if broker_launcher.poll() is None:
                broker_launcher.kill()
            broker_launcher.wait(timeout=2)
            if child_marker.exists():
                child_pid = int(child_marker.read_text())
                child_handle = real_open(child_pid)
                if child_handle is not None:
                    execution._terminate_windows_process_handle(
                        child_handle, timeout_seconds=1.0
                    )
                    execution._windows_close_process_handle(child_handle)

    def test_live_parent_ignores_undefined_nonzero_exit_time(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows process times only exist on Windows")
        with mock.patch.object(
            execution, "_windows_process_handle_is_running", return_value=True
        ), mock.patch.object(
            execution, "_windows_process_times", return_value=(10, 999)
        ):
            self.assertIsNone(execution._windows_process_exit_time(123))

    def test_post_reap_cleanup_never_signals_a_reused_numeric_process_group(self) -> None:
        process = types.SimpleNamespace(pid=43210, returncode=0)
        tree = execution._ProcessTree.__new__(execution._ProcessTree)
        tree.process = process
        tree.reviewer_process_group = False
        tree._termination_lock = threading.Lock()

        with mock.patch.object(execution.os, "name", "posix"), \
             mock.patch.object(execution.os, "killpg", create=True) as killpg:
            tree.kill_descendants_after_exit()

        killpg.assert_not_called()

    def test_wnowait_keeps_group_identity_reserved_until_descendants_are_signalled(self) -> None:
        process = types.SimpleNamespace(pid=43210, returncode=None)
        tree = execution._ProcessTree.__new__(execution._ProcessTree)
        tree.process = process
        tree.reviewer_process_group = False
        tree._termination_lock = threading.Lock()

        with mock.patch.object(execution.os, "name", "posix"), \
             mock.patch.object(execution.os, "waitid", return_value=object(), create=True) as waitid, \
             mock.patch.object(execution.os, "P_PID", 1, create=True), \
             mock.patch.object(execution.os, "WEXITED", 2, create=True), \
             mock.patch.object(execution.os, "WNOHANG", 4, create=True), \
             mock.patch.object(execution.os, "WNOWAIT", 8, create=True), \
             mock.patch.object(execution.signal, "SIGKILL", 9, create=True), \
             mock.patch.object(execution.os, "killpg", create=True) as killpg:
            self.assertTrue(tree.wait_for_root_until(time.monotonic() + 1))
            tree.kill_descendants_after_exit()

        waitid.assert_called_once_with(
            1,
            process.pid,
            2 | 4 | 8,
        )
        killpg.assert_called_once_with(process.pid, 9)

    def test_posix_identity_check_and_group_signal_hold_the_reaping_lock(self) -> None:
        tree = execution._ProcessTree.__new__(execution._ProcessTree)
        tree.reviewer_process_group = False
        tree._termination_lock = threading.Lock()

        class Process:
            pid = 43210

            @property
            def returncode(self):
                self.assert_identity_lock()
                return None

            @staticmethod
            def assert_identity_lock():
                if not tree._termination_lock.locked():
                    raise AssertionError("process identity was checked outside the reaping lock")

        tree.process = Process()

        def signal_group(_pid, _signal):
            self.assertTrue(tree._termination_lock.locked())

        with mock.patch.object(execution.os, "name", "posix"), \
             mock.patch.object(execution.signal, "SIGKILL", 9, create=True), \
             mock.patch.object(
                 execution.os, "killpg", side_effect=signal_group, create=True
             ) as killpg:
            tree.kill()

        killpg.assert_called_once_with(tree.process.pid, 9)

    def test_platform_without_wnowait_reaps_but_skips_unsafe_group_signal(self) -> None:
        class Process:
            pid = 43210
            returncode = None

            def wait(self, timeout):
                self.returncode = 0
                return 0

        process = Process()
        tree = execution._ProcessTree.__new__(execution._ProcessTree)
        tree.process = process
        tree.reviewer_process_group = False
        tree._termination_lock = threading.Lock()

        with mock.patch.object(execution.os, "name", "posix"), \
             mock.patch.object(execution.os, "waitid", create=True), \
             mock.patch.object(execution.os, "P_PID", 1, create=True), \
             mock.patch.object(execution.os, "WEXITED", 2, create=True), \
             mock.patch.object(execution.os, "WNOHANG", 4, create=True), \
             mock.patch.object(execution.os, "WNOWAIT", None, create=True), \
             mock.patch.object(execution.os, "killpg", create=True) as killpg:
            self.assertTrue(tree.wait_for_root_until(time.monotonic() + 1))
            tree.kill_descendants_after_exit()

        killpg.assert_not_called()


class _RefusingJob:
    """A job that could not be made, standing in for a machine that refuses."""

    handle = None

    def __init__(self, process):
        self.process = process

    def terminate(self) -> None:
        self.process.kill()

    def close(self) -> None:
        return None


class _ObservedJob:
    """A no-op job used to isolate process-identity tracker unit tests."""

    handle = object()

    def __init__(self, _process):
        pass

    def terminate(self) -> bool:
        return True

    def wait_until_empty(self, _timeout_seconds: float) -> bool:
        return True

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
