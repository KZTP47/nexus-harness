from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.watcher import Changes, ProjectWatcher, compare, scan


class FakeTime:
    """A clock that only moves when something sleeps, so tests never wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += 1
        self.now += seconds


class WatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.write("value.py", "VALUE = 1\n")
        self.time = FakeTime()
        self.addCleanup(self.temporary.cleanup)

    def config(self, **project: object) -> LoadedConfig:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["project"].update(project)
        return LoadedConfig(data, self.root, [], {})

    def watcher(self, **options: object) -> ProjectWatcher:
        settings = {"interval_seconds": 1.0, "quiet_seconds": 2.0, "clock": self.time.clock, "sleep": self.time.sleep}
        settings.update(options)
        return ProjectWatcher(self.config(), **settings)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def touch(self, name: str, text: str) -> None:
        """Change a file so its recorded size differs, whatever the clock does."""

        self.write(name, text)


class ChangeDetectionTests(WatcherTestCase):
    def test_nothing_changes_when_nothing_moves(self) -> None:
        watcher = self.watcher()
        self.assertFalse(watcher.poll())

    def test_a_new_file_is_noticed(self) -> None:
        watcher = self.watcher()
        self.write("extra.py", "x = 1\n")
        found = watcher.poll()
        self.assertEqual(found.added, ("extra.py",))
        self.assertEqual(found.changed, ())
        self.assertTrue(found)

    def test_an_edited_file_is_noticed(self) -> None:
        watcher = self.watcher()
        self.touch("value.py", "VALUE = 22222\n")
        found = watcher.poll()
        self.assertEqual(found.changed, ("value.py",))

    def test_a_deleted_file_is_noticed(self) -> None:
        watcher = self.watcher()
        (self.root / "value.py").unlink()
        self.assertEqual(watcher.poll().removed, ("value.py",))

    def test_the_same_change_is_only_reported_once(self) -> None:
        watcher = self.watcher()
        self.write("extra.py", "x = 1\n")
        self.assertTrue(watcher.poll())
        self.assertFalse(watcher.poll())

    def test_ignored_folders_are_not_watched(self) -> None:
        watcher = self.watcher()
        self.write("node_modules/big/index.js", "everything\n")
        self.write(".harness/runs/x.json", "{}\n")
        self.assertFalse(watcher.poll())

    def test_a_configured_ignore_name_is_honoured(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["project"]["ignore"] = [*data["project"]["ignore"], "scratch"]
        watcher = ProjectWatcher(
            LoadedConfig(data, self.root, [], {}),
            clock=self.time.clock, sleep=self.time.sleep,
        )
        self.write("scratch/notes.txt", "hello\n")
        self.assertFalse(watcher.poll())

    def test_the_file_count_is_capped(self) -> None:
        for index in range(12):
            self.write(f"file{index}.txt", "x")
        watcher = self.watcher(max_files=5)
        self.assertEqual(len(watcher.snapshot), 5)

    def test_compare_reports_each_kind_separately(self) -> None:
        before = {"a": (1, 1), "b": (1, 1), "c": (1, 1)}
        after = {"b": (2, 1), "c": (1, 1), "d": (1, 1)}
        found = compare(before, after)
        self.assertEqual(found.added, ("d",))
        self.assertEqual(found.changed, ("b",))
        self.assertEqual(found.removed, ("a",))
        self.assertEqual(found.paths, ("a", "b", "d"))

    def test_bad_settings_are_refused(self) -> None:
        for options in ({"interval_seconds": 0}, {"quiet_seconds": -1}, {"max_files": 0}, {"interval_seconds": 7200}):
            with self.subTest(options=options), self.assertRaises(HarnessError):
                self.watcher(**options)


class DescriptionTests(unittest.TestCase):
    def test_a_person_can_read_the_summary(self) -> None:
        found = Changes(added=("a.py",), changed=("b.py", "c.py"), removed=("d.py",))
        text = found.describe()
        self.assertIn("a.py new", text)
        self.assertIn("b.py, c.py changed", text)
        self.assertIn("d.py gone", text)

    def test_a_long_list_is_shortened(self) -> None:
        found = Changes(changed=tuple(f"file{index}.py" for index in range(10)))
        text = found.describe(limit=2)
        self.assertIn("file0.py, file1.py and 8 more changed", text)

    def test_no_change_says_so(self) -> None:
        self.assertEqual(Changes().describe(), "nothing changed")


class WaitingTests(WatcherTestCase):
    def test_a_batch_is_held_back_until_the_project_is_still(self) -> None:
        watcher = self.watcher(quiet_seconds=2.0, interval_seconds=1.0)
        steps = {"count": 0}
        original_poll = watcher.poll

        def poll_with_edits() -> Changes:
            steps["count"] += 1
            if steps["count"] == 1:
                self.write("one.py", "1\n")
            elif steps["count"] == 2:
                self.write("two.py", "2\n")
            return original_poll()

        watcher.poll = poll_with_edits  # type: ignore[method-assign]
        found = watcher.wait_for_changes()
        self.assertEqual(found.added, ("one.py", "two.py"))
        self.assertGreaterEqual(steps["count"], 4)

    def test_waiting_stops_at_the_timeout_with_nothing(self) -> None:
        watcher = self.watcher(interval_seconds=1.0)
        found = watcher.wait_for_changes(timeout_seconds=3.0)
        self.assertFalse(found)
        self.assertGreater(self.time.slept, 0)

    def test_stopping_ends_the_wait(self) -> None:
        watcher = self.watcher()
        original_poll = watcher.poll

        def poll_then_stop() -> Changes:
            watcher.stop()
            return original_poll()

        watcher.poll = poll_then_stop  # type: ignore[method-assign]
        self.assertFalse(watcher.wait_for_changes())

    def test_watch_calls_back_once_for_each_settled_batch(self) -> None:
        watcher = self.watcher(quiet_seconds=1.0, interval_seconds=1.0)
        seen: list[Changes] = []
        steps = {"count": 0}
        original_poll = watcher.poll

        def poll_with_edits() -> Changes:
            steps["count"] += 1
            if steps["count"] == 1:
                self.write("one.py", "1\n")
            elif steps["count"] == 5:
                self.write("two.py", "2\n")
            return original_poll()

        watcher.poll = poll_with_edits  # type: ignore[method-assign]
        batches = watcher.watch(seen.append, max_batches=2)
        self.assertEqual(batches, 2)
        self.assertEqual([item.added for item in seen], [("one.py",), ("two.py",)])

    def test_watch_returns_when_nothing_happens_before_the_timeout(self) -> None:
        watcher = self.watcher()
        seen: list[Changes] = []
        self.assertEqual(watcher.watch(seen.append, timeout_seconds=2.0), 0)
        self.assertEqual(seen, [])


class RepeatTests(WatcherTestCase):
    """The timer is a plain schedule: run again even when nothing moved."""

    def test_the_timer_wakes_the_watch_when_nothing_changed(self) -> None:
        watcher = self.watcher(interval_seconds=1.0, quiet_seconds=1.0)
        wakeup = watcher.wait_for_next(repeat_seconds=3.0)
        self.assertEqual(wakeup.reason, "timer")
        self.assertFalse(wakeup.changes)
        self.assertGreaterEqual(self.time.now, 3.0)

    def test_a_change_still_wins_over_the_timer(self) -> None:
        watcher = self.watcher(interval_seconds=1.0, quiet_seconds=1.0)
        original_poll = watcher.poll
        steps = {"count": 0}

        def poll_with_edit() -> Changes:
            steps["count"] += 1
            if steps["count"] == 1:
                self.write("one.py", "1\n")
            return original_poll()

        watcher.poll = poll_with_edit  # type: ignore[method-assign]
        wakeup = watcher.wait_for_next(repeat_seconds=60.0)
        self.assertEqual(wakeup.reason, "changes")
        self.assertEqual(wakeup.changes.added, ("one.py",))

    def test_watch_keeps_running_on_the_timer(self) -> None:
        watcher = self.watcher(interval_seconds=1.0, quiet_seconds=1.0)
        seen: list[Changes] = []
        batches = watcher.watch(seen.append, max_batches=3, repeat_seconds=2.0)
        self.assertEqual(batches, 3)
        self.assertEqual([bool(item) for item in seen], [False, False, False])

    def test_no_repeat_time_behaves_as_before(self) -> None:
        watcher = self.watcher()
        wakeup = watcher.wait_for_next(timeout_seconds=2.0)
        self.assertEqual(wakeup.reason, "nothing")

    def test_a_repeat_time_of_zero_or_less_is_refused(self) -> None:
        watcher = self.watcher()
        for value in (0, -5):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                watcher.wait_for_next(repeat_seconds=value)

    def test_stopping_ends_a_timed_watch(self) -> None:
        watcher = self.watcher()
        original_poll = watcher.poll

        def poll_then_stop() -> Changes:
            watcher.stop()
            return original_poll()

        watcher.poll = poll_then_stop  # type: ignore[method-assign]
        self.assertEqual(watcher.wait_for_next(repeat_seconds=60.0).reason, "nothing")


class CommandLineTests(WatcherTestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str]:
        from contextlib import redirect_stdout
        from io import StringIO

        from our_harness import cli

        (self.root / ".harness" / "config.json").write_text(
            json.dumps({"schema_version": 1, "memory": {"enabled": False}}), encoding="utf-8"
        )
        (self.root / "README.md").write_text("# Demo\n", encoding="utf-8")
        captured = StringIO()
        with redirect_stdout(captured):
            code = cli.main(["--project", str(self.root), "qa", *arguments])
        return code, captured.getvalue()

    def test_watch_runs_once_then_stops_when_nothing_changes(self) -> None:
        self.run_cli("init")
        code, output = self.run_cli("watch", "--max-runs", "0", "--interval", "0.05", "--quiet", "0.05")
        self.assertEqual(code, 0)
        self.assertIn("Watching", output)
        self.assertIn("will run each time", output)
        self.assertIn("All checks passed", output)
        self.assertIn("Stopped after 0 runs", output)

    def test_watch_can_also_run_on_a_timer(self) -> None:
        self.run_cli("init")
        code, output = self.run_cli(
            "watch", "--every", "0.1", "--max-runs", "1", "--interval", "0.05", "--quiet", "0.05"
        )
        self.assertEqual(code, 0)
        self.assertIn("every 0.1 seconds", output)
        self.assertIn("running on the timer", output)
        self.assertIn("Stopped after 1 run.", output)

    def test_watch_can_wait_before_its_first_run(self) -> None:
        self.run_cli("init")
        code, output = self.run_cli(
            "watch", "--max-runs", "0", "--skip-first", "--interval", "0.05", "--quiet", "0.05"
        )
        self.assertEqual(code, 0)
        self.assertNotIn("All checks passed", output)


if __name__ == "__main__":
    unittest.main()
