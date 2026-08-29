from __future__ import annotations

import contextlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_windows_desktop as builder


class DesktopBuildLeaseTests(unittest.TestCase):
    def test_one_lease_spans_prepare_smoke_and_electron_builder(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            selected = desktop / "candidate"
            selected.mkdir(parents=True)
            (selected / "sentinel.txt").write_text("verified", encoding="utf-8")

            @contextlib.contextmanager
            def lease():
                events.append("lock-enter")
                try:
                    yield
                finally:
                    events.append("lock-exit")

            def prepare(_output: Path) -> Path:
                events.append("prepare")
                return selected

            def run(command, **_kwargs):
                if "smoke_bundled_playwright.py" in str(command):
                    events.append("smoke")
                else:
                    events.append("builder")
                    packaged = desktop / "build-output" / "win-unpacked" / "resources" / "runtime"
                    shutil.copytree(selected, packaged)
                return mock.Mock(returncode=0)

            with mock.patch.object(builder.runtime, "runtime_build_lock", side_effect=lease), \
                    mock.patch.object(builder.runtime, "_prepare_locked", side_effect=prepare), \
                    mock.patch.object(builder, "DESKTOP", desktop), \
                    mock.patch.object(builder.shutil, "which", return_value="node"), \
                    mock.patch.object(builder.subprocess, "run", side_effect=run):
                self.assertEqual(builder.build(), selected)

        self.assertEqual(events, ["lock-enter", "prepare", "smoke", "builder", "lock-exit"])

    def test_builder_failure_releases_lease_without_mutating_selected_candidate(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "candidate"
            selected.mkdir()
            sentinel = selected / "sentinel.txt"
            sentinel.write_text("verified", encoding="utf-8")

            @contextlib.contextmanager
            def lease():
                events.append("lock-enter")
                try:
                    yield
                finally:
                    events.append("lock-exit")

            answers = [mock.Mock(returncode=0), mock.Mock(returncode=7)]
            with mock.patch.object(builder.runtime, "runtime_build_lock", side_effect=lease), \
                    mock.patch.object(builder.runtime, "_prepare_locked", return_value=selected), \
                    mock.patch.object(builder.shutil, "which", return_value="node"), \
                    mock.patch.object(builder.subprocess, "run", side_effect=answers):
                with self.assertRaisesRegex(RuntimeError, r"packaging failed \(7\)"):
                    builder.build()

            self.assertEqual(events, ["lock-enter", "lock-exit"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "verified")

    def test_packaged_runtime_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            selected = desktop / "candidate"
            selected.mkdir(parents=True)
            (selected / "sentinel.txt").write_text("fresh", encoding="utf-8")
            packaged = desktop / "build-output" / "win-unpacked" / "resources" / "runtime"

            def run(command, **_kwargs):
                if "smoke_bundled_playwright.py" not in str(command):
                    packaged.mkdir(parents=True)
                    (packaged / "sentinel.txt").write_text("stale", encoding="utf-8")
                return mock.Mock(returncode=0)

            with mock.patch.object(builder.runtime, "runtime_build_lock", contextlib.nullcontext), \
                    mock.patch.object(builder.runtime, "_prepare_locked", return_value=selected), \
                    mock.patch.object(builder, "DESKTOP", desktop), \
                    mock.patch.object(builder.shutil, "which", return_value="node"), \
                    mock.patch.object(builder.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "does not exactly match"):
                    builder.build()

    def test_matching_inflight_source_and_package_tampering_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            selected = desktop / "candidate"
            selected.mkdir(parents=True)
            sentinel = selected / "sentinel.txt"
            sentinel.write_text("fresh", encoding="utf-8")
            packaged = desktop / "build-output" / "win-unpacked" / "resources" / "runtime"

            def run(command, **_kwargs):
                if "smoke_bundled_playwright.py" not in str(command):
                    sentinel.write_text("same tampered bytes", encoding="utf-8")
                    shutil.copytree(selected, packaged)
                return mock.Mock(returncode=0)

            with mock.patch.object(builder.runtime, "runtime_build_lock", contextlib.nullcontext), \
                    mock.patch.object(builder.runtime, "_prepare_locked", return_value=selected), \
                    mock.patch.object(builder, "DESKTOP", desktop), \
                    mock.patch.object(builder.shutil, "which", return_value="node"), \
                    mock.patch.object(builder.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "does not exactly match"):
                    builder.build()

    def test_runtime_builder_configuration_cannot_be_overridden(self) -> None:
        for arguments in (
            ["--config", "attacker.cjs"], ["--config.foo=bar"], ["-c"], ["-c=attacker.cjs"],
            ["--project", "elsewhere"], ["--projectDir=elsewhere"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(RuntimeError, "cannot be overridden"):
                    builder.build(arguments)


if __name__ == "__main__":
    unittest.main()
