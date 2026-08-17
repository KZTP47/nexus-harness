"""A command still runs on a machine that will not let it have a job of its own.

On Windows the harness puts each command it starts into a "job", which is how
one command and everything it starts get stopped together. Some machines will
not allow that: a build server already puts every step inside a job of its own,
and Windows refuses to put a process into a second one.

Refusing to run the command at all was the wrong answer. It made every command
on such a machine fail for a reason that had nothing to do with the command -
and on the build server it showed up as "this machine has no browser driver",
which was not true and sent anybody reading it to install something that was
already there.
"""

from __future__ import annotations

import copy
import ctypes
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import execution
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class NoJobAllowedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def test_a_command_still_runs_when_a_job_is_refused(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows jobs only exist on Windows")
        runner = execution.CommandRunner(self.config)
        real = execution._WindowsJob.__init__

        def refused(self, process):
            # Exactly what a build server does: access denied, because this
            # process is already inside a job of somebody else's making.
            error = ctypes.WinError(5)
            with mock.patch.object(ctypes, "WinError", return_value=error):
                return real(self, process)

        with mock.patch.object(execution, "_WindowsJob", autospec=False) as job:
            job.side_effect = lambda process: _RefusingJob(process)
            done = runner.run([sys.executable, "-c", "print('it ran')"], cwd=".", timeout=30)
        self.assertTrue(done.passed, done.stderr)
        self.assertIn("it ran", done.stdout)

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


class _RefusingJob:
    """A job that could not be made, standing in for a machine that refuses."""

    handle = None

    def __init__(self, process):
        self.process = process

    def terminate(self) -> None:
        self.process.kill()

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
