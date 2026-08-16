"""Watch mode says when it can only watch part of a project.

Watch mode re-runs your checks when a file changes. It reads at most a set
number of files, and it stops if the project moves underneath it while it is
reading. Both are reasonable. Doing either quietly is not: the tool then sits
there saying nothing while the work changes, which looks exactly like a project
where nothing is happening.
"""

from __future__ import annotations

import copy
import tempfile
import time
import unittest
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.ignore_policy import IgnorePolicy
from our_harness.watcher import ProjectWatcher, compare, scan


class ProjectTooBigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for number in range(12):
            (self.root / f"file-{number:03d}.txt").write_text("first", encoding="utf-8")
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.ignore = IgnorePolicy(self.root, set(self.config.get("project.ignore", [])))

    def test_it_says_when_there_are_more_files_than_it_follows(self) -> None:
        said: list[str] = []
        scan(self.root, self.ignore, max_files=5, on_partial=said.append)
        self.assertEqual(len(said), 1, said)
        self.assertIn("more files than watch mode follows", said[0])
        self.assertIn("will not be noticed", said[0])
        self.assertIn("project.ignore", said[0])

    def test_it_says_nothing_when_it_can_see_the_whole_project(self) -> None:
        said: list[str] = []
        scan(self.root, self.ignore, max_files=100, on_partial=said.append)
        self.assertEqual(said, [])

    def test_a_change_it_cannot_see_is_still_a_change_it_cannot_see(self) -> None:
        # The limit is real: this test is here so the promise made by the
        # warning is the true one. It does not pretend the files are watched.
        before = scan(self.root, self.ignore, max_files=5)
        unwatched = sorted(
            {f"file-{number:03d}.txt" for number in range(12)} - set(before)
        )
        self.assertTrue(unwatched)
        time.sleep(0.02)
        (self.root / unwatched[0]).write_text("changed", encoding="utf-8")
        found = compare(before, scan(self.root, self.ignore, max_files=5))
        self.assertFalse(found, "the warning is what tells the person, not the watch")

    def test_the_watcher_passes_the_warning_on(self) -> None:
        said: list[str] = []
        watcher = ProjectWatcher(
            self.config,
            max_files=5,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
            on_partial=said.append,
        )
        self.assertEqual(len(said), 1, said)
        self.assertIn("more files than watch mode follows", said[0])
        self.assertTrue(watcher.partial_note)

    def test_it_is_said_once_and_not_on_every_look(self) -> None:
        # A warning repeated every second is a warning nobody reads.
        said: list[str] = []
        watcher = ProjectWatcher(
            self.config,
            max_files=5,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
            on_partial=said.append,
        )
        for _ in range(5):
            watcher.poll()
        self.assertEqual(len(said), 1, said)

    def test_a_watcher_with_nobody_listening_still_works(self) -> None:
        watcher = ProjectWatcher(
            self.config, max_files=5, clock=lambda: 0.0, sleep=lambda _seconds: None
        )
        self.assertTrue(watcher.partial_note)
        self.assertIsInstance(watcher.poll().paths, tuple)


class ProjectMovedWhileReadingTests(unittest.TestCase):
    def test_it_says_when_it_stopped_part_way_through(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "one.txt").write_text("x", encoding="utf-8")
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})

            class WalkThatBreaks(IgnorePolicy):
                def walk_files(self):  # type: ignore[override]
                    yield root / "one.txt"
                    raise OSError("the folder went away")

            said: list[str] = []
            found = scan(root, WalkThatBreaks(root, set()), on_partial=said.append)
            self.assertEqual(len(said), 1, said)
            self.assertIn("Stopped part way through", said[0])
            self.assertIn("the folder went away", said[0])
            # What it did read is still handed back, so the watch carries on.
            self.assertEqual(sorted(found), ["one.txt"])


if __name__ == "__main__":
    unittest.main()
