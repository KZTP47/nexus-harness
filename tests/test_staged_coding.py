from __future__ import annotations

import copy
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from our_harness.changes import FileTransaction
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.staged_coding import (
    StagedCodingWorkspace,
    TextReplacement,
    VerificationAction,
)


class FixtureDeadline:
    def __init__(self, seconds: float = 30):
        self.end = time.monotonic() + seconds

    def check(self, operation: str) -> None:
        if time.monotonic() >= self.end:
            raise HarnessError(f"Workflow deadline exceeded while trying to {operation}")

    def remaining_seconds(self, operation: str, cap: float | None = None) -> float:
        self.check(operation)
        remaining = self.end - time.monotonic()
        return remaining if cap is None else min(remaining, cap)


def config_for(root: Path, **limits: int) -> LoadedConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    for dotted, value in limits.items():
        owner, name = dotted.split("__", 1)
        data[owner][name] = value
    return LoadedConfig(data, root.resolve(), [], {})


def python_check(name: str, source: str, *, timeout: float | None = None) -> VerificationAction:
    return VerificationAction(name, (sys.executable, "-c", source), timeout_seconds=timeout)


class StagedCodingWorkspaceTests(unittest.TestCase):
    def test_rejects_escape_control_alias_and_link_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "safe.py").write_text("value = 1\n", encoding="utf-8")
            action = python_check("check", "raise SystemExit(0)")
            invalid = ["../outside.py", ".git/config", ".harness/state.json", "CON.txt"]
            if os.name == "nt":
                invalid.append("C:\\outside.py")
            else:
                invalid.append("/outside.py")
            for path in invalid:
                with self.subTest(path=path), self.assertRaises(HarnessError):
                    StagedCodingWorkspace(config_for(root), [path], [action])
            with self.assertRaisesRegex(HarnessError, "portable path alias"):
                StagedCodingWorkspace(config_for(root), ["safe.py", "SAFE.PY"], [action])

            link = root / "linked.py"
            try:
                link.symlink_to(root / "safe.py")
            except OSError:
                return
            with self.assertRaisesRegex(HarnessError, "Linked path"):
                StagedCodingWorkspace(config_for(root), ["linked.py"], [action])

    def test_stale_patch_and_source_baselines_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "module.py"
            source.write_text("value = 1\n", encoding="utf-8")
            action = python_check("check", "raise SystemExit(0)")
            with StagedCodingWorkspace(config_for(root), ["module.py"], [action]) as staged:
                with self.assertRaisesRegex(HarnessError, "baseline conflict"):
                    staged.apply_patch(
                        "bad-baseline",
                        "module.py",
                        "0" * 64,
                        [TextReplacement("1", "2")],
                    )
                current = staged.file_state("module.py")
                staged.apply_patch(
                    "valid-patch",
                    "module.py",
                    str(current["sha256"]),
                    [TextReplacement("1", "2")],
                )
                self.assertTrue(staged.run_verification("check-1", "check").result.passed)
                source.write_text("value = 3\n", encoding="utf-8")
                with self.assertRaisesRegex(HarnessError, "Source baseline changed"):
                    staged.finalize()

    def test_repeated_action_id_is_rejected_without_a_second_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            action = python_check("check", "raise SystemExit(0)")
            with StagedCodingWorkspace(config_for(root), ["module.py"], [action]) as staged:
                baseline = staged.file_state("module.py")["sha256"]
                state = staged.replace_file("replace-1", "module.py", baseline, "value = 2\n")
                with self.assertRaisesRegex(HarnessError, "already used"):
                    staged.replace_file("replace-1", "module.py", state["sha256"], "value = 3\n")
                staged_path = staged.stage_root / "module.py"
                self.assertEqual(staged_path.read_text(encoding="utf-8"), "value = 2\n")
                self.assertEqual((root / "module.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_verification_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            action = python_check("slow", "import time; time.sleep(2)", timeout=0.15)
            started = time.monotonic()
            with StagedCodingWorkspace(config_for(root), ["module.py"], [action], deadline=FixtureDeadline()) as staged:
                result = staged.run_verification("slow-1", "slow").result
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_verification_output_is_capped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            action = python_check("loud", "import sys; sys.stdout.write('x' * 10000); sys.stderr.write('y' * 10000)")
            config = config_for(
                root,
                execution__max_output_bytes=1024,
                workflow__max_tool_output_bytes=1024,
                workflow__max_tool_total_bytes=2048,
            )
            with StagedCodingWorkspace(config, ["module.py"], [action]) as staged:
                result = staged.run_verification("loud-1", "loud").result
            captured = len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
            self.assertLessEqual(captured, 1024)
            self.assertTrue(result.output_truncated)
            self.assertTrue(result.passed)
            self.assertFalse(result.complete_success)

    def test_finalize_rejects_real_exit_zero_check_truncated_from_10k_to_1k(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            action = python_check("loud", "import sys; sys.stdout.write('x' * 10000)")
            config = config_for(
                root,
                execution__max_output_bytes=1024,
                workflow__max_tool_output_bytes=1024,
                workflow__max_tool_total_bytes=2048,
            )
            with StagedCodingWorkspace(config, ["module.py"], [action]) as staged:
                baseline = staged.file_state("module.py")["sha256"]
                staged.replace_file("edit", "module.py", baseline, "value = 2\n")
                verification = staged.run_verification("loud-1", "loud").result
                self.assertEqual(verification.exit_code, 0)
                self.assertTrue(verification.output_truncated)
                self.assertEqual(len(verification.stdout.encode("utf-8")), 1024)
                with self.assertRaisesRegex(HarnessError, "checks that did not pass: loud"):
                    staged.finalize()

    def test_temp_snapshot_is_removed_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            action = python_check("check", "raise SystemExit(0)")
            staged = StagedCodingWorkspace(config_for(root), ["module.py"], [action])
            location = staged.stage_root
            self.assertTrue(location.is_dir())
            staged.close()
            self.assertFalse(location.exists())
            staged.close()

    def test_preallocated_stage_requires_an_empty_private_regular_temp_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            action = python_check("check", "raise SystemExit(0)")

            reserved = Path(tempfile.mkdtemp(prefix="our-harness-stage-"))
            with StagedCodingWorkspace(
                config_for(root), ["module.py"], [action], preallocated_stage_root=reserved,
            ) as staged:
                self.assertEqual(staged.stage_root, reserved.resolve())
                self.assertEqual((reserved / "module.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertFalse(reserved.exists())

            occupied = Path(tempfile.mkdtemp(prefix="our-harness-stage-"))
            sentinel = occupied / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            try:
                with self.assertRaisesRegex(HarnessError, "must be empty"):
                    StagedCodingWorkspace(
                        config_for(root), ["module.py"], [action],
                        preallocated_stage_root=occupied,
                    )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            finally:
                sentinel.unlink(missing_ok=True)
                occupied.rmdir()

            link = Path(tempfile.gettempdir()).resolve() / (
                f"our-harness-stage-link-{os.getpid()}-{time.time_ns()}"
            )
            try:
                link.symlink_to(root, target_is_directory=True)
            except OSError:
                return
            try:
                with self.assertRaisesRegex(HarnessError, "private regular directory"):
                    StagedCodingWorkspace(
                        config_for(root), ["module.py"], [action], preallocated_stage_root=link,
                    )
                self.assertEqual((root / "module.py").read_text(encoding="utf-8"), "value = 1\n")
            finally:
                link.unlink(missing_ok=True)

    def test_support_files_enable_project_tests_but_remain_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            (root / "calc.py").write_text(
                "from helper import offset\n\ndef add(left, right):\n    return left - right + offset\n",
                encoding="utf-8",
            )
            (root / "helper.py").write_text("offset = 0\n", encoding="utf-8")
            (root / "tests" / "test_calc.py").write_text(
                "import unittest\nimport calc\n\n"
                "class CalcTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n",
                encoding="utf-8",
            )
            check = VerificationAction(
                "unit",
                (
                    sys.executable,
                    "-c",
                    "import sys, unittest; "
                    "sys.path.insert(0, '.'); "
                    "unittest.main(module=None)",
                    "discover",
                    "-s",
                    "tests",
                ),
            )
            with StagedCodingWorkspace(
                config_for(root),
                ["calc.py"],
                [check],
                support_files=["helper.py", "tests/test_calc.py"],
                generated_output_ignores=["**/__pycache__/**", "__pycache__/**", "*.pyc"],
            ) as staged:
                state = staged.file_state("calc.py")
                staged.apply_patch(
                    "fix",
                    "calc.py",
                    str(state["sha256"]),
                    [TextReplacement("left - right", "left + right")],
                )
                self.assertTrue(staged.run_verification("unit-1", "unit").result.passed)
                candidate = staged.finalize()
                self.assertEqual([change.path for change in candidate.changes], ["calc.py"])

            mutator = python_check("mutate", "open('helper.py', 'w').write('offset = 9\\n')")
            with StagedCodingWorkspace(
                config_for(root), ["calc.py"], [mutator], support_files=["helper.py"]
            ) as staged:
                with self.assertRaisesRegex(HarnessError, "read-only support"):
                    staged.run_verification("mutate-1", "mutate")

    def test_unexpected_verification_outputs_are_rejected_or_explicitly_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            creator = python_check("generate", "open('result.tmp', 'w').write('ok')")
            with StagedCodingWorkspace(config_for(root), ["module.py"], [creator]) as staged:
                with self.assertRaisesRegex(HarnessError, "unexpected staged file"):
                    staged.run_verification("generate-1", "generate")
            with StagedCodingWorkspace(
                config_for(root),
                ["module.py"],
                [creator],
                generated_output_ignores=["*.tmp"],
            ) as staged:
                self.assertTrue(staged.run_verification("generate-2", "generate").result.passed)

    def test_new_file_replace_delete_and_posix_mode_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            existing = root / "old.py"
            existing.write_text("old = True\n", encoding="utf-8")
            if os.name != "nt":
                existing.chmod(0o755)
            check = python_check(
                "shape",
                "from pathlib import Path; raise SystemExit(0 if "
                "Path('new.py').read_text() == 'new = True\\n' and not Path('old.py').exists() else 1)",
            )
            with StagedCodingWorkspace(config_for(root), ["old.py", "new.py"], [check]) as staged:
                old_state = staged.file_state("old.py")
                new_state = staged.file_state("new.py")
                self.assertIsNone(new_state["sha256"])
                staged.replace_file("create", "new.py", None, "new = True\n", reason="add module")
                staged.delete_file("delete", "old.py", str(old_state["sha256"]), reason="remove old module")
                self.assertTrue(staged.run_verification("shape-1", "shape").result.passed)
                candidate = staged.finalize()
            by_path = {change.path: change for change in candidate.changes}
            self.assertIsNone(by_path["new.py"].baseline_sha256)
            self.assertFalse(by_path["new.py"].delete)
            self.assertTrue(by_path["old.py"].delete)
            self.assertIsNone(by_path["old.py"].content)

            if os.name != "nt":
                preserve = python_check("check", "raise SystemExit(0)")
                with StagedCodingWorkspace(config_for(root), ["old.py"], [preserve]) as staged:
                    state = staged.file_state("old.py")
                    staged.replace_file("replace", "old.py", state["sha256"], "old = False\n")
                    self.assertEqual(stat.S_IMODE((staged.stage_root / "old.py").stat().st_mode), 0o755)
                    staged.run_verification("check-mode", "check")
                    mode_candidate = staged.finalize()
                self.assertIsNone(mode_candidate.changes[0].mode)

    def test_change_byte_and_tool_budgets_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("x\n", encoding="utf-8")
            action = python_check("check", "raise SystemExit(0)")
            config = config_for(root, execution__max_changed_bytes=4, workflow__max_tool_calls=1)
            with StagedCodingWorkspace(config, ["module.py"], [action]) as staged:
                baseline = staged.file_state("module.py")["sha256"]
                with self.assertRaisesRegex(HarnessError, "bytes; limit"):
                    staged.replace_file("too-large", "module.py", baseline, "12345")
                with self.assertRaisesRegex(HarnessError, "tool-call budget"):
                    staged.run_verification("second-call", "check")

    def test_two_round_fail_fix_loop_keeps_source_untouched_until_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "calc.py"
            original = "def add(left, right):\n    return left - right\n"
            fixed = "def add(left, right):\n    return left + right\n"
            source.write_text(original, encoding="utf-8")
            expected_candidate = source.read_bytes().replace(b"return left - right", b"return left + right")
            check = python_check(
                "unit",
                "scope = {}; exec(open('calc.py', encoding='utf-8').read(), scope); "
                "raise SystemExit(0 if scope['add'](2, 3) == 5 else 1)",
            )
            with StagedCodingWorkspace(
                config_for(root), ["calc.py"], [check], deadline=FixtureDeadline()
            ) as staged:
                first = staged.run_verification("unit-round-1", "unit").result
                self.assertFalse(first.passed)
                state = staged.file_state("calc.py")
                staged.apply_patch(
                    "repair-round-2",
                    "calc.py",
                    str(state["sha256"]),
                    [TextReplacement("return left - right", "return left + right")],
                    reason="make addition correct",
                )
                second = staged.run_verification("unit-round-2", "unit").result
                self.assertTrue(second.passed)
                candidate = staged.finalize()
                self.assertEqual(source.read_text(encoding="utf-8"), original)
                self.assertEqual(len(candidate.changes), 1)
                self.assertEqual(candidate.changes[0].content, expected_candidate)

            FileTransaction(root).apply(candidate.changes)
            self.assertEqual(source.read_text(encoding="utf-8"), fixed)


if __name__ == "__main__":
    unittest.main()
