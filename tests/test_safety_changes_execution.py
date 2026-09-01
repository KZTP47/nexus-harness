from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import execution
from our_harness.changes import FileTransaction, file_sha256
from our_harness.checkpoints import CheckpointManager
from our_harness.config import DEFAULT_CONFIG, LoadedConfig, load_config
from our_harness.execution import CommandRunner
from our_harness.models import ChangePlan, HarnessError
from our_harness.safety import (
    ProjectTransactionLock,
    confined_path,
    confined_walk_files,
    validate_portable_relative_path,
)


class CommandPolicyArgumentParsingTests(unittest.TestCase):
    def test_git_clean_bundles_expose_every_short_switch_to_policy(self) -> None:
        for switch in ("-xfd", "-xdf", "-nfd"):
            with self.subTest(switch=switch):
                argv = ["git", "clean", switch]
                self.assertTrue(execution._matches_rule("clean -fd", argv))
                self.assertTrue(execution._matches_rule("--force", argv))

    def test_safe_git_clean_bundles_do_not_invent_force(self) -> None:
        for switch in ("-n", "-nd", "-nxd"):
            with self.subTest(switch=switch):
                argv = ["git", "clean", switch]
                self.assertFalse(execution._matches_rule("clean -fd", argv))
                self.assertFalse(execution._matches_rule("--force", argv))

    def test_powershell_named_parameters_end_at_the_payload_boundary(self) -> None:
        launcher = [
            r"C:\Program Files\PowerShell\7\pwsh.exe",
            "-NoProfile", "-InputFormat", "Text", "-File", "script.ps1",
        ]
        self.assertFalse(execution._matches_rule("--force", launcher))
        self.assertTrue(execution._matches_rule(
            "--force", ["not-powershell", "-NoProfile"],
        ))
        self.assertFalse(execution._matches_rule(
            "--force",
            ["powershell.exe", "-ExecutionPolicy:Bypass", "-File", "script.ps1"],
        ))
        self.assertTrue(execution._matches_rule(
            "clean -fd",
            ["powershell.exe", "-NoProfile", "-Command", "git clean -nfd"],
        ))
        self.assertTrue(execution._matches_rule(
            "clean -fd",
            ["powershell.exe", "-NoProfile", "git clean -xfd"],
        ))


class ProjectTransactionLockTests(unittest.TestCase):
    def test_cross_instance_thread_wait_honors_the_whole_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            holder = ProjectTransactionLock(root)
            waiter = ProjectTransactionLock(root)
            result: list[str] = []

            def wait_briefly() -> None:
                started = time.monotonic()
                try:
                    with waiter.held(0.1):
                        result.append("acquired")
                except HarnessError as error:
                    result.append(str(error))
                finally:
                    result.append(f"elapsed:{time.monotonic() - started:.3f}")

            with holder.held():
                thread = threading.Thread(target=wait_briefly)
                thread.start()
                thread.join(timeout=0.5)
                self.assertFalse(thread.is_alive(), "the in-process lock ignored its timeout")
            thread.join(timeout=1)

            self.assertIn("Another harness process holds the project transaction lock", result)
            elapsed = float(next(one.split(":", 1)[1] for one in result if one.startswith("elapsed:")))
            self.assertGreaterEqual(elapsed, 0.08)
            self.assertLess(elapsed, 0.5)

    def test_one_lock_instance_remains_reentrant_on_one_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transaction = ProjectTransactionLock(root)
            with transaction.held(0.2):
                with transaction.held(0.2):
                    self.assertTrue((root / ".harness" / "transaction.lock").is_file())

    def test_distinct_lock_instances_on_one_thread_are_not_treated_as_reentrant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ProjectTransactionLock(root).held(0.2):
                with self.assertRaisesRegex(HarnessError, "project transaction lock"):
                    with ProjectTransactionLock(root).held(0.2):
                        self.fail("a separate application lock entered the transaction")


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

    def test_embedded_python_restores_cwd_imports_and_user_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "local_probe.py").write_text("VALUE = 'cwd-visible'\n", encoding="utf-8")
            runner = CommandRunner(self._config(root))
            command = [
                sys.executable,
                "-c",
                "from __future__ import annotations\n"
                "import local_probe, sys\n"
                "print(local_probe.VALUE, sys.argv)",
                "one",
                "two",
            ]
            result = runner.run(command)
            self.assertTrue(result.passed, result.stderr)
            self.assertIn("cwd-visible ['-c', 'one', 'two']", result.stdout)
            self.assertEqual(result.argv, command)

    def test_embedded_python_restores_module_and_script_import_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "local_probe.py").write_text("VALUE = 'cwd-visible'\n", encoding="utf-8")
            (root / "runnable_probe.py").write_text(
                "import local_probe, sys\n"
                "print('module', local_probe.VALUE, sys.argv[1:])\n",
                encoding="utf-8",
            )
            (root / "probe_script.py").write_text(
                "from pathlib import Path\n"
                "import local_probe, sys\n"
                "print('script', local_probe.VALUE, Path(sys.argv[0]).name, sys.argv[1:])\n",
                encoding="utf-8",
            )
            runner = CommandRunner(self._config(root))
            module = runner.run([sys.executable, "-m", "runnable_probe", "one"])
            script = runner.run([sys.executable, "probe_script.py", "two"])
            self.assertTrue(module.passed, module.stderr)
            self.assertEqual(module.stdout.strip(), "module cwd-visible ['one']")
            self.assertTrue(script.passed, script.stderr)
            self.assertEqual(script.stdout.strip(), "script cwd-visible probe_script.py ['two']")

    def test_explicit_python_safe_path_modes_are_never_rewritten(self) -> None:
        working = Path.cwd()
        environment = {"PATH": os.environ.get("PATH", "")}
        for command in (
            [sys.executable, "-I", "-c", "print('isolated')"],
            [sys.executable, "-P", "-m", "module"],
        ):
            with self.subTest(command=command), mock.patch.object(
                execution, "_is_embedded_python", return_value=True,
            ):
                self.assertEqual(
                    execution._embedded_python_cwd_argv(command, working, environment),
                    command,
                )
        with mock.patch.object(execution, "_is_embedded_python", return_value=True):
            command = [sys.executable, "-c", "print('safe')"]
            self.assertEqual(
                execution._embedded_python_cwd_argv(
                    command, working, {**environment, "PYTHONSAFEPATH": "1"},
                ),
                command,
            )

    def test_explicit_python_safe_path_still_hides_project_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "private_probe.py").write_text("VISIBLE = True\n", encoding="utf-8")
            runner = CommandRunner(self._config(root))
            for flag in ("-I", "-P"):
                with self.subTest(flag=flag):
                    result = runner.run([
                        sys.executable, flag, "-c", "import private_probe",
                    ])
                    self.assertFalse(result.passed)
                    self.assertIn("No module named 'private_probe'", result.stderr)
            safe_environment = runner.run(
                [sys.executable, "-c", "import private_probe"],
                environment_overrides={"PYTHONSAFEPATH": "1"},
            )
            self.assertFalse(safe_environment.passed)
            self.assertIn("No module named 'private_probe'", safe_environment.stderr)

    def test_non_python_commands_are_not_rewritten_by_cwd_compatibility(self) -> None:
        command = ["node", "-e", "console.log('ok')"]
        with mock.patch.object(execution, "_resolved_executable", return_value=Path("node.exe")):
            self.assertEqual(
                execution._embedded_python_cwd_argv(command, Path.cwd(), {}),
                command,
            )

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 path aliases")
    def test_short_root_alias_and_long_resolved_root_are_the_same_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Nexus Alias Test ") as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / "child folder").mkdir()
            buffer = ctypes.create_unicode_buffer(32_768)
            copied = ctypes.windll.kernel32.GetShortPathNameW(
                str(root), buffer, len(buffer),
            )
            self.assertGreater(copied, 0, "the Windows test volume has no 8.3 root alias")
            short_root = Path(buffer.value)
            self.assertTrue(root.samefile(short_root))
            runner = CommandRunner(LoadedConfig(
                copy.deepcopy(DEFAULT_CONFIG), short_root, [], {},
            ))

            result = runner.run(
                [sys.executable, "-c", "import os; print(os.getcwd())"],
                cwd="child folder",
            )

            self.assertTrue(result.passed, result)
            self.assertEqual(result.cwd, "child folder")
            self.assertTrue((Path(result.stdout.strip())).samefile(root / "child folder"))

    @unittest.skipUnless(os.name == "nt", "Windows MAX_PATH cwd behavior")
    def test_existing_cwd_over_max_path_uses_verified_short_alias(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Nexus MaxPath Test ") as temporary:
            root = Path(temporary)
            runner = CommandRunner(self._config(root))
            working = root
            relative_parts: list[str] = []
            index = 0
            while len(str(working)) < 285:
                component = f"long-directory-component-{index:02d}"
                relative_parts.append(component)
                working = working / component
                working.mkdir()
                index += 1
            relative = "/".join(relative_parts)
            self.assertGreaterEqual(len(str(working)), 260)

            result = runner.run(
                [sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('ok')"],
                cwd=relative,
            )

            self.assertTrue(result.passed, result)
            self.assertEqual(result.cwd, relative)
            self.assertEqual((working / "ran.txt").read_text(encoding="utf-8"), "ok")

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


class ChildEnvironmentTests(unittest.TestCase):
    """What a child process must be able to see.

    A Windows program that asks the system where the shared program folder is
    gets the answer "%SystemDrive%\ProgramData" and expands it itself. With no
    SystemDrive to expand, the name stays as it is and the program creates a
    folder literally called "%SystemDrive%" next to whatever it was working in.
    We once found one of those sitting in this project. So these names are not
    a nicety; leaving them out puts rubbish in the user's folders.
    """

    NEEDED = ("PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "TMP", "TEMP")

    def test_the_default_list_keeps_the_windows_basics(self) -> None:
        from our_harness.config import DEFAULT_CONFIG

        allowed = {name.upper() for name in DEFAULT_CONFIG["execution"]["inherit_environment"]}
        for name in self.NEEDED:
            with self.subTest(name=name):
                self.assertIn(name, allowed)

    @unittest.skipUnless(os.name == "nt", "these names only exist on Windows")
    def test_a_child_on_windows_really_gets_them(self) -> None:
        from our_harness.config import DEFAULT_CONFIG
        from our_harness.safety import safe_environment

        passed = safe_environment(DEFAULT_CONFIG["execution"]["inherit_environment"])
        upper = {name.upper(): value for name, value in passed.items()}
        for name in ("SYSTEMDRIVE", "SYSTEMROOT"):
            with self.subTest(name=name):
                self.assertTrue(upper.get(name, "").strip(), f"a child would run without {name}")
