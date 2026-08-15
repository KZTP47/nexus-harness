from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from our_harness.changes import FileTransaction, file_sha256
from our_harness.checkpoints import CheckpointManager
from our_harness.config import load_config
from our_harness.execution import CommandRunner
from our_harness.models import ChangePlan, HarnessError
from our_harness.safety import confined_path, confined_walk_files, validate_portable_relative_path


class PathSafetyTests(unittest.TestCase):
    def test_windows_aliases_and_nested_control_paths_are_rejected_portably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rejected = (
                ".git./config",
                ".harness./memory.db",
                "sub/.git/config",
                "sub/.harness/cache",
                "sub/.ＧＩＴ/config",
                "CON",
                "con.txt",
                "CON .txt",
                "ＣＯＮ.txt",
                "aux.log",
                "COM1.json",
                "lpt9.data",
                "file.txt:stream",
                "folder./file.txt",
                "folder /file.txt",
            )
            for relative in rejected:
                with self.subTest(path=relative):
                    with self.assertRaises(HarnessError):
                        validate_portable_relative_path(relative)
                    with self.assertRaises(HarnessError):
                        confined_path(root, relative)

    def test_workspace_walk_fails_closed_on_normalized_control_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alias = root / "sub" / ".ＧＩＴ"
            alias.mkdir(parents=True)
            (alias / "config").write_text("blocked", encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "control paths"):
                list(confined_walk_files(root))

    @unittest.skipUnless(os.name == "nt", "Windows alias behavior")
    def test_windows_ads_and_device_spellings_fail_before_filesystem_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ordinary = root / "ordinary.txt"
            ordinary.write_text("safe", encoding="utf-8")
            for relative in ("ordinary.txt:secret", "NUL.txt", "PRN.log", ".git./config"):
                with self.subTest(path=relative), self.assertRaises(HarnessError):
                    confined_path(root, relative, allow_missing=False)
    def test_rejects_absolute_and_parent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for value in ("../outside", str(root / "absolute")):
                with self.assertRaises(HarnessError):
                    confined_path(root, value)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support is required")
    def test_rejects_link_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            link = root / "linked"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("link creation is not permitted")
            with self.assertRaisesRegex(HarnessError, "Linked path"):
                confined_path(root, "linked/file.txt")


class FileTransactionTests(unittest.TestCase):
    def test_transaction_rejects_portable_duplicate_aliases_before_mutation(self) -> None:
        alias_sets = (
            ("Victim.txt", "victim.txt"),
            ("folder/Value.txt", "folder\\value.txt"),
        )
        for paths in alias_sets:
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                transaction = FileTransaction(root)
                plans = [ChangePlan(path, None, f"content-{index}") for index, path in enumerate(paths)]
                with self.assertRaisesRegex(HarnessError, "duplicate portable path aliases"):
                    transaction.apply(plans, transaction_id="duplicate-alias-audit")
                self.assertFalse((root / "Victim.txt").exists())
                self.assertFalse((root / "victim.txt").exists())
                self.assertFalse((root / "folder").exists())
                self.assertEqual(transaction.reconcile(), [])

    def test_transaction_rejects_portable_alias_and_nested_control_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transaction = FileTransaction(root)
            for relative in (
                ".git./config",
                ".harness./state",
                "sub/.git/config",
                "sub/.harness/state",
                "CON.txt",
                "value.txt:stream",
                "trailing. /value.txt",
            ):
                with self.subTest(path=relative), self.assertRaises(HarnessError):
                    transaction.apply([ChangePlan(relative, None, "blocked")])

    def test_checkpoint_restore_rejects_alias_and_nested_control_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "safe.txt").write_text("safe", encoding="utf-8")
            manager = CheckpointManager(load_config(root))
            checkpoint = manager.create("alias guard")
            for relative in ("sub/.git/config", "sub/.harness/state", "CON.txt", "safe.txt:stream"):
                with self.subTest(path=relative), self.assertRaises(HarnessError):
                    manager.restore_file(checkpoint["id"], relative)
    def test_apply_conflict_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "sample.txt"
            path.write_text("before\n", encoding="utf-8")
            baseline = file_sha256(path)
            tx = FileTransaction(root)
            result = tx.apply([ChangePlan("sample.txt", baseline, "after\n")])
            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")
            with self.assertRaisesRegex(HarnessError, "Baseline conflict"):
                tx.apply([ChangePlan("sample.txt", baseline, "wrong\n")])
            tx.rollback(str(result["transaction_id"]))
            self.assertEqual(path.read_text(encoding="utf-8"), "before\n")

    def test_patch_hash_reserved_paths_and_mode_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tool.sh"
            path.write_text("#!/bin/sh\necho before\n", encoding="utf-8")
            path.chmod(0o751)
            tx = FileTransaction(root)
            result = tx.apply([ChangePlan("tool.sh", file_sha256(path), "#!/bin/sh\necho after\n")])
            self.assertIn("--- a/tool.sh", result["patch"])
            self.assertEqual(hashlib.sha256(result["patch"].encode()).hexdigest(), result["patch_sha256"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(0o751))
            tx.rollback(str(result["transaction_id"]))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(0o751))
            for reserved in (".git/config", ".harness/config.json"):
                with self.assertRaisesRegex(HarnessError, "reserved"):
                    tx.apply([ChangePlan(reserved, None, "blocked")])

    def test_prepared_transaction_is_reported_for_crash_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "sample.txt"
            path.write_text("before", encoding="utf-8")
            tx = FileTransaction(root)
            result = tx.apply([ChangePlan("sample.txt", file_sha256(path), "after")])
            manifest_path = root / ".harness" / "backups" / result["transaction_id"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepared"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(tx.reconcile()[0]["status"], "applied_after_crash")

    def test_interrupted_transactions_can_be_finalized_or_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "sample.txt"
            path.write_text("before", encoding="utf-8")
            tx = FileTransaction(root)

            finalized = tx.apply([ChangePlan("sample.txt", file_sha256(path), "after")])
            finalized_manifest = root / ".harness" / "backups" / finalized["transaction_id"] / "manifest.json"
            data = json.loads(finalized_manifest.read_text(encoding="utf-8"))
            data["state"] = "prepared"
            finalized_manifest.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(tx.recover(str(finalized["transaction_id"]), "finalize")["status"], "applied")
            self.assertEqual(tx.reconcile(), [])

            rolled_back = tx.apply([ChangePlan("sample.txt", file_sha256(path), "newer")])
            rollback_manifest = root / ".harness" / "backups" / rolled_back["transaction_id"] / "manifest.json"
            data = json.loads(rollback_manifest.read_text(encoding="utf-8"))
            data["state"] = "prepared"
            rollback_manifest.write_text(json.dumps(data), encoding="utf-8")
            result = tx.recover(str(rolled_back["transaction_id"]), "rollback")
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            self.assertEqual(tx.reconcile(), [])

    def test_rollback_refuses_later_user_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "sample.txt"
            path.write_text("one", encoding="utf-8")
            tx = FileTransaction(root)
            result = tx.apply([ChangePlan("sample.txt", file_sha256(path), "two")])
            path.write_text("user", encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "Rollback conflict"):
                tx.rollback(str(result["transaction_id"]))
            self.assertEqual(path.read_text(encoding="utf-8"), "user")


class ExecutionTests(unittest.TestCase):
    def _config(self, root: Path, timeout: int = 3, output_limit: int = 250_000):
        (root / ".harness").mkdir(exist_ok=True)
        (root / ".harness" / "config.json").write_text(
            json.dumps({"execution": {"timeout_seconds": timeout, "max_output_bytes": output_limit}}), encoding="utf-8"
        )
        return load_config(root)

    def test_argv_execution_and_denied_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = CommandRunner(self._config(root))
            result = runner.run([sys.executable, "-c", "print('ok')"])
            self.assertTrue(result.passed)
            self.assertEqual(result.stdout.strip(), "ok")
            self.assertEqual(result.cwd, ".")
            with self.assertRaisesRegex(HarnessError, "denied"):
                runner.run(["git", "reset", "--hard"])

    def test_output_is_bounded_while_both_streams_are_drained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child"
            child.mkdir()
            runner = CommandRunner(self._config(root, output_limit=2048))
            result = runner.run(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'o'*200000); sys.stderr.buffer.write(b'e'*200000)"],
                cwd="child",
            )
            self.assertTrue(result.passed)
            self.assertTrue(result.output_truncated)
            self.assertLessEqual(len(result.stdout.encode()) + len(result.stderr.encode()), 2048)
            self.assertEqual(result.cwd, "child")

    def test_timeout_returns_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = CommandRunner(self._config(root, 1))
            result = runner.run([sys.executable, "-c", "import time; time.sleep(10)"])
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)

    def test_timeout_owns_descendants_that_keep_output_pipes_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "escaped-child.txt"
            child_code = (
                "import pathlib,time; "
                "time.sleep(2); "
                f"pathlib.Path({str(sentinel)!r}).write_text('escaped', encoding='utf-8')"
            )
            parent_code = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "print('parent exited', flush=True)"
            )
            runner = CommandRunner(self._config(root, 3))
            started = time.monotonic()
            result = runner.run([sys.executable, "-c", parent_code], timeout=1)
            elapsed = time.monotonic() - started
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)
            self.assertIn("parent exited", result.stdout)
            self.assertLess(elapsed, 1.75)
            time.sleep(1.25)
            self.assertFalse(sentinel.exists(), "a descendant survived the command deadline")


if __name__ == "__main__":
    unittest.main()
