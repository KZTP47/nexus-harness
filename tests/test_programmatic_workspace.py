from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.changes import FileTransaction
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.execution import CommandRunner
from our_harness.models import HarnessError
from our_harness.programmatic_workspace import (
    ApplyPatch,
    FinalizeCandidate,
    InspectFile,
    PersistentProgrammaticWorkspace,
    ReplaceFile,
    RunVerification,
)
from our_harness.staged_coding import TextReplacement, VerificationAction


def config_for(root: Path, **limits: int) -> LoadedConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["execution"]["mode"] = "docker"
    for dotted, value in limits.items():
        owner, name = dotted.split("__", 1)
        data[owner][name] = value
    return LoadedConfig(data, root.resolve(), [], {})


def attach_test_host_runner(workspace: PersistentProgrammaticWorkspace) -> None:
    """Exercise staged command behavior without requiring Docker in unit tests."""
    data = copy.deepcopy(workspace.config.data)
    data["execution"]["mode"] = "process"
    stage_config = LoadedConfig(data, workspace.stage_root, [], {})
    workspace._stage()._runner = CommandRunner(stage_config)


def addition_check(*, timeout: float | None = None) -> VerificationAction:
    return VerificationAction(
        "unit",
        (
            sys.executable,
            "-c",
            "scope={}; exec(open('calc.py', encoding='utf-8').read(), scope); "
            "raise SystemExit(0 if scope['add'](2, 3) == 5 else 1)",
        ),
        timeout_seconds=timeout,
    )


def registry_lease_for_stage(stage: Path) -> Path:
    registry = Path(tempfile.gettempdir()).resolve() / "our-harness-stage-registry-v1"
    matches: list[Path] = []
    for candidate in registry.glob("*.lease"):
        try:
            payload = json.loads(candidate.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if payload.get("stage_path") == str(stage):
            matches.append(candidate)
    if len(matches) != 1:
        raise AssertionError(f"expected one registry lease for {stage}, found {matches}")
    return matches[0]


class PersistentProgrammaticWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key_directory = tempfile.TemporaryDirectory()
        key_path = Path(self.key_directory.name) / "checkpoint.key"
        self.key_environment = patch.dict(
            os.environ, {"OUR_HARNESS_CHECKPOINT_KEY_FILE": str(key_path)}, clear=False,
        )
        self.key_environment.start()

    def tearDown(self) -> None:
        self.key_environment.stop()
        self.key_directory.cleanup()

    def test_process_crash_keeps_project_unchanged_and_restores_into_fresh_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "calc.py"
            original = "def add(left, right):\n    return left - right\n"
            source.write_text(original, encoding="utf-8")
            stage_marker = root / "crashed-stage.txt"
            package_root = Path(__file__).resolve().parents[1] / "src"
            script = """
import copy
import json
import os
import sys
from pathlib import Path
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.programmatic_workspace import InspectFile, PersistentProgrammaticWorkspace, ReplaceFile
from our_harness.staged_coding import VerificationAction

root = Path(sys.argv[1]).resolve()
config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
config.data["execution"]["mode"] = "docker"
check = VerificationAction(
    "unit",
    (sys.executable, "-c", "scope={}; exec(open('calc.py', encoding='utf-8').read(), scope); raise SystemExit(0 if scope['add'](2, 3) == 5 else 1)"),
)
workspace = PersistentProgrammaticWorkspace(config, "crashed", ["calc.py"], [check])
state = workspace.execute(InspectFile("calc.py"))
workspace.execute(ReplaceFile("write-before-crash", "calc.py", state["sha256"], "def add(left, right):\\n    return left + right\\n"))
Path(sys.argv[2]).write_text(
    json.dumps({"stage": str(workspace.stage_root), "lease": str(workspace._stage_lease.path)}),
    encoding="utf-8",
)
os._exit(23)
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(package_root)
            crashed = subprocess.run(
                [sys.executable, "-c", script, str(root), str(stage_marker)],
                cwd=root,
                env=environment,
                check=False,
                timeout=10,
            )
            self.assertEqual(crashed.returncode, 23)
            abandoned = json.loads(stage_marker.read_text(encoding="utf-8"))
            abandoned_stage = Path(abandoned["stage"])
            abandoned_lease = Path(abandoned["lease"])
            self.assertTrue(abandoned_stage.is_dir())
            self.assertTrue(abandoned_lease.is_file())
            abandoned_lease_payload = abandoned_lease.read_bytes()
            abandoned_nonce = json.loads(abandoned_lease_payload)["nonce"]
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            try:
                restored = PersistentProgrammaticWorkspace.open(
                    config_for(root), "crashed", ["calc.py"], [addition_check()]
                )
                self.assertFalse(abandoned_stage.exists())
                self.assertTrue(abandoned_lease.exists())
                self.assertNotEqual(restored._stage_record["nonce"], abandoned_nonce)
                self.assertNotEqual(restored.stage_root, abandoned_stage)
                attach_test_host_runner(restored)
                result = restored.execute(RunVerification("verify-after-crash", "unit"))
                self.assertTrue(result.result.passed)
                restored.execute(FinalizeCandidate())
                self.assertEqual(source.read_text(encoding="utf-8"), original)
                restored.discard()
                self.assertFalse(abandoned_lease.exists())
            finally:
                shutil.rmtree(abandoned_stage, ignore_errors=True)

    def test_authenticated_checkpoint_is_bound_to_its_canonical_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original = base / "original"
            relocated = base / "relocated"
            original.mkdir()
            relocated.mkdir()
            first = PersistentProgrammaticWorkspace(
                config_for(original), "project-bound", ["new.py"],
                [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))],
            )
            first.execute(ReplaceFile("create", "new.py", None, "value = 1\n"))
            source_checkpoint = first.checkpoint_path
            first.close()
            copied_checkpoint = (
                relocated / ".harness" / "checkpoints" / "programmatic" / "project-bound.json"
            )
            copied_checkpoint.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_checkpoint, copied_checkpoint)
            with self.assertRaisesRegex(HarnessError, "different project"):
                PersistentProgrammaticWorkspace.open(
                    config_for(relocated), "project-bound", ["new.py"],
                    [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))],
                )
            self.assertFalse((relocated / "new.py").exists())

    def test_initial_pre_checkpoint_crash_stage_is_scavenged_by_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            marker = Path(self.key_directory.name) / "initial-crash.json"
            package_root = Path(__file__).resolve().parents[1] / "src"
            script = """
import copy, json, os, sys
from pathlib import Path
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.programmatic_workspace import PersistentProgrammaticWorkspace
from our_harness.staged_coding import VerificationAction
root, marker = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
config.data["execution"]["mode"] = "docker"
def crash(self, *args, **kwargs):
    marker.write_text(json.dumps({"stage": str(self.stage_root), "lease": str(self._stage_lease.path)}), encoding="utf-8")
    os._exit(31)
PersistentProgrammaticWorkspace._persist = crash
PersistentProgrammaticWorkspace(config, "initial-window", ["calc.py"], [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))])
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(package_root)
            crashed = subprocess.run(
                [sys.executable, "-c", script, str(root), str(marker)],
                cwd=root, env=environment, check=False, timeout=10,
            )
            self.assertEqual(crashed.returncode, 31)
            abandoned = json.loads(marker.read_text(encoding="utf-8"))
            stage, lease = Path(abandoned["stage"]), Path(abandoned["lease"])
            self.assertTrue(stage.is_dir())
            self.assertTrue(lease.is_file())
            old_payload = lease.read_bytes()
            old_nonce = json.loads(old_payload)["nonce"]
            recovered = PersistentProgrammaticWorkspace(
                config_for(root), "initial-window", ["calc.py"],
                [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))],
            )
            self.assertFalse(stage.exists())
            self.assertTrue(lease.exists())
            self.assertNotEqual(recovered._stage_record["nonce"], old_nonce)
            recovered.discard()
            self.assertFalse(lease.exists())

    def test_initial_pre_copy_crash_is_registered_and_scavenged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("secret_source = 1\n", encoding="utf-8")
            marker = Path(self.key_directory.name) / "initial-pre-copy-stage.txt"
            package_root = Path(__file__).resolve().parents[1] / "src"
            script = """
import copy, os, sys
from pathlib import Path
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.programmatic_workspace import PersistentProgrammaticWorkspace
from our_harness.staged_coding import StagedCodingWorkspace, VerificationAction
root, marker = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
config.data["execution"]["mode"] = "docker"
def crash_before_copy(self):
    marker.write_text(str(self.stage_root), encoding="utf-8")
    os._exit(41)
StagedCodingWorkspace._populate_stage = crash_before_copy
PersistentProgrammaticWorkspace(config, "initial-pre-copy", ["calc.py"], [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))])
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(package_root)
            crashed = subprocess.run(
                [sys.executable, "-c", script, str(root), str(marker)],
                cwd=root, env=environment, check=False, timeout=10,
            )
            self.assertEqual(crashed.returncode, 41)
            abandoned_stage = Path(marker.read_text(encoding="utf-8"))
            self.assertEqual(list(abandoned_stage.iterdir()), [])
            abandoned_lease = registry_lease_for_stage(abandoned_stage)
            recovered = PersistentProgrammaticWorkspace(
                config_for(root), "initial-pre-copy", ["calc.py"],
                [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))],
            )
            self.assertFalse(abandoned_stage.exists())
            self.assertEqual(recovered._stage_lease.path, abandoned_lease)
            self.assertNotEqual(recovered.stage_root, abandoned_stage)
            recovered.discard()
            self.assertFalse(abandoned_lease.exists())

    def test_restore_pre_checkpoint_crash_replacement_stage_is_scavenged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            action = VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))
            original = PersistentProgrammaticWorkspace(
                config_for(root), "restore-window", ["calc.py"], [action],
            )
            original.close()
            marker = Path(self.key_directory.name) / "restore-crash.json"
            package_root = Path(__file__).resolve().parents[1] / "src"
            script = """
import copy, json, os, sys
from pathlib import Path
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.programmatic_workspace import PersistentProgrammaticWorkspace
from our_harness.staged_coding import VerificationAction
root, marker = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
config.data["execution"]["mode"] = "docker"
def crash(self, *args, **kwargs):
    marker.write_text(json.dumps({"stage": str(self.stage_root), "lease": str(self._stage_lease.path)}), encoding="utf-8")
    os._exit(32)
PersistentProgrammaticWorkspace._persist = crash
PersistentProgrammaticWorkspace.open(config, "restore-window", ["calc.py"], [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))])
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(package_root)
            crashed = subprocess.run(
                [sys.executable, "-c", script, str(root), str(marker)],
                cwd=root, env=environment, check=False, timeout=10,
            )
            self.assertEqual(crashed.returncode, 32)
            abandoned = json.loads(marker.read_text(encoding="utf-8"))
            stage, lease = Path(abandoned["stage"]), Path(abandoned["lease"])
            self.assertTrue(stage.is_dir())
            self.assertTrue(lease.is_file())
            old_payload = lease.read_bytes()
            old_nonce = json.loads(old_payload)["nonce"]
            recovered = PersistentProgrammaticWorkspace.open(
                config_for(root), "restore-window", ["calc.py"], [action],
            )
            self.assertFalse(stage.exists())
            self.assertTrue(lease.exists())
            self.assertNotEqual(recovered._stage_record["nonce"], old_nonce)
            recovered.discard()
            self.assertFalse(lease.exists())

    def test_restore_pre_copy_crash_is_registered_and_scavenged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("secret_source = 1\n", encoding="utf-8")
            action = VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))
            original = PersistentProgrammaticWorkspace(
                config_for(root), "restore-pre-copy", ["calc.py"], [action],
            )
            original.close()
            marker = Path(self.key_directory.name) / "restore-pre-copy-stage.txt"
            package_root = Path(__file__).resolve().parents[1] / "src"
            script = """
import copy, os, sys
from pathlib import Path
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.programmatic_workspace import PersistentProgrammaticWorkspace
from our_harness.staged_coding import StagedCodingWorkspace, VerificationAction
root, marker = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
config.data["execution"]["mode"] = "docker"
def crash_before_copy(self):
    marker.write_text(str(self.stage_root), encoding="utf-8")
    os._exit(42)
StagedCodingWorkspace._populate_stage = crash_before_copy
PersistentProgrammaticWorkspace.open(config, "restore-pre-copy", ["calc.py"], [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))])
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(package_root)
            crashed = subprocess.run(
                [sys.executable, "-c", script, str(root), str(marker)],
                cwd=root, env=environment, check=False, timeout=10,
            )
            self.assertEqual(crashed.returncode, 42)
            abandoned_stage = Path(marker.read_text(encoding="utf-8"))
            self.assertEqual(list(abandoned_stage.iterdir()), [])
            abandoned_lease = registry_lease_for_stage(abandoned_stage)
            recovered = PersistentProgrammaticWorkspace.open(
                config_for(root), "restore-pre-copy", ["calc.py"], [action],
            )
            self.assertFalse(abandoned_stage.exists())
            self.assertEqual(recovered._stage_lease.path, abandoned_lease)
            self.assertNotEqual(recovered.stage_root, abandoned_stage)
            recovered.discard()
            self.assertFalse(abandoned_lease.exists())

    def test_stage_scavenger_preserves_active_and_malicious_registry_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            action = VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))
            active = PersistentProgrammaticWorkspace(
                config_for(root), "active-stage", ["calc.py"], [action],
            )
            active_stage = active.stage_root
            with self.assertRaisesRegex(HarnessError, "stage is still active"):
                PersistentProgrammaticWorkspace.open(
                    config_for(root), "active-stage", ["calc.py"], [action],
                )
            self.assertTrue(active_stage.is_dir())
            active.discard()

            probe = PersistentProgrammaticWorkspace(
                config_for(root), "malicious-stage", ["calc.py"], [action],
            )
            expected_lease = probe._stage_lease.path
            registry = expected_lease.parent
            probe.discard()
            expected_lease.write_text("not an authenticated registry", encoding="utf-8")
            fake_stage = Path(tempfile.gettempdir()).resolve() / "our-harness-stage-malicious-sibling"
            fake_stage.mkdir(exist_ok=True)
            sentinel = fake_stage / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            fake_lease = Path(str(fake_stage) + ".lease")
            fake_lease.write_text("not ours", encoding="utf-8")
            victim: Path | None = None
            try:
                with self.assertRaises(HarnessError):
                    PersistentProgrammaticWorkspace(
                        config_for(root), "malicious-stage", ["calc.py"], [action],
                    )
                self.assertEqual(expected_lease.read_text(encoding="utf-8"), "not an authenticated registry")
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
                self.assertEqual(fake_lease.read_text(encoding="utf-8"), "not ours")
                expected_lease.unlink()
                victim = registry / "victim.txt"
                victim.write_text("keep", encoding="utf-8")
                try:
                    expected_lease.symlink_to(victim)
                except OSError:
                    pass
                else:
                    with self.assertRaises(HarnessError):
                        PersistentProgrammaticWorkspace(
                            config_for(root), "malicious-stage", ["calc.py"], [action],
                        )
                    self.assertTrue(expected_lease.is_symlink())
                    self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            finally:
                if expected_lease.is_symlink() or expected_lease.is_file():
                    expected_lease.unlink()
                if victim is not None:
                    victim.unlink(missing_ok=True)
                fake_lease.unlink(missing_ok=True)
                shutil.rmtree(fake_stage, ignore_errors=True)

    def test_host_process_verification_is_refused_before_project_code_can_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            outside = root / "unapproved.py"
            outside.write_text("safe = True\n", encoding="utf-8")
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["execution"]["mode"] = "process"
            unsafe = VerificationAction(
                "unsafe",
                (
                    sys.executable,
                    "-c",
                    f"open({str(outside)!r}, 'w', encoding='utf-8').write('safe = False\\n')",
                ),
            )
            workspace = PersistentProgrammaticWorkspace(
                LoadedConfig(data, root.resolve(), [], {}), "host-refused", ["calc.py"], [unsafe],
            )
            with self.assertRaisesRegex(HarnessError, "requires execution.mode=docker"):
                workspace.execute(RunVerification("unsafe-1", "unsafe"))
            self.assertEqual(outside.read_text(encoding="utf-8"), "safe = True\n")
            workspace.discard()

    def test_project_identity_guard_rejects_a_misconfigured_backend_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            outside = root / "unapproved.py"
            outside.write_text("safe = True\n", encoding="utf-8")
            action = VerificationAction(
                "escape",
                (
                    sys.executable,
                    "-c",
                    f"open({str(outside)!r}, 'w', encoding='utf-8').write('safe = False\\n')",
                ),
            )
            workspace = PersistentProgrammaticWorkspace(
                config_for(root), "guard", ["calc.py"], [action],
            )
            # Unit-only host runner simulates a broken isolation adapter.
            attach_test_host_runner(workspace)
            with self.assertRaisesRegex(HarnessError, "changed the source project"):
                workspace.execute(RunVerification("escape-1", "escape"))
            workspace.close()
            with self.assertRaisesRegex(HarnessError, "tainted"):
                PersistentProgrammaticWorkspace.open(
                    config_for(root), "guard", ["calc.py"], [action],
                )

    def test_crash_after_verification_effect_leaves_uncertain_intent_and_never_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("value = 1\n", encoding="utf-8")
            effect = Path(self.key_directory.name) / "effect.txt"
            action = VerificationAction(
                "effect",
                (
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                        "old=int(p.read_text()) if p.exists() else 0; p.write_text(str(old+1))"
                    ),
                    str(effect),
                ),
            )
            workspace = PersistentProgrammaticWorkspace(
                config_for(root), "verify-crash", ["calc.py"], [action],
            )
            attach_test_host_runner(workspace)
            with patch.object(workspace, "_complete_action", side_effect=SystemExit(91)):
                with self.assertRaises(SystemExit):
                    workspace.execute(RunVerification("effect-1", "effect"))
            self.assertEqual(effect.read_text(encoding="utf-8"), "1")
            workspace.close()
            with self.assertRaisesRegex(HarnessError, "uncertain action"):
                PersistentProgrammaticWorkspace.open(
                    config_for(root), "verify-crash", ["calc.py"], [action],
                )
            self.assertEqual(effect.read_text(encoding="utf-8"), "1")

    def test_hard_restart_reconstructs_candidate_and_requires_fresh_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "calc.py"
            original = "def add(left, right):\n    return left - right\n"
            source.write_text(original, encoding="utf-8")
            config = config_for(root)
            first = PersistentProgrammaticWorkspace(
                config, "repair-1", ["calc.py"], [addition_check()]
            )
            stage_root = first.stage_root
            state = first.execute(InspectFile("calc.py"))
            first.execute(
                ApplyPatch(
                    "patch-1",
                    "calc.py",
                    str(state["sha256"]),
                    (TextReplacement("left - right", "left + right"),),
                    "fix addition",
                )
            )
            checkpoint = first.checkpoint_path
            self.assertTrue(checkpoint.is_file())
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertNotIn("left + right", checkpoint.read_text(encoding="utf-8"))
            first.close()
            self.assertFalse(stage_root.exists())

            resumed = PersistentProgrammaticWorkspace.open(
                config, "repair-1", ["calc.py"], [addition_check()]
            )
            with self.assertRaisesRegex(HarnessError, "checks that did not pass"):
                resumed.execute(FinalizeCandidate())
            attach_test_host_runner(resumed)
            verification = resumed.execute(RunVerification("unit-after-restart", "unit"))
            self.assertTrue(verification.result.passed)
            candidate = resumed.execute(FinalizeCandidate())
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            resumed.discard()
            self.assertFalse(checkpoint.exists())

            FileTransaction(root).apply(candidate.changes)
            self.assertIn("left + right", source.read_text(encoding="utf-8"))

    def test_restart_rejects_stale_source_baseline_and_changed_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "calc.py"
            source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            config = config_for(root)
            workspace = PersistentProgrammaticWorkspace(
                config, "stale", ["calc.py"], [addition_check()]
            )
            state = workspace.execute(InspectFile("calc.py"))
            workspace.execute(
                ReplaceFile(
                    "replace",
                    "calc.py",
                    str(state["sha256"]),
                    "def add(a, b):\n    return a + b\n",
                    "fix",
                )
            )
            workspace.close()
            with self.assertRaisesRegex(HarnessError, "specification does not match"):
                PersistentProgrammaticWorkspace.open(
                    config,
                    "stale",
                    ["calc.py"],
                    [VerificationAction("different", (sys.executable, "-c", "raise SystemExit(0)"))],
                )
            source.write_text("def add(a, b):\n    return 99\n", encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "source baseline changed"):
                PersistentProgrammaticWorkspace.open(
                    config, "stale", ["calc.py"], [addition_check()]
                )
            self.assertEqual(source.read_text(encoding="utf-8"), "def add(a, b):\n    return 99\n")

    def test_checkpoint_integrity_replay_ids_and_concurrent_writers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            config = config_for(root)
            owner = PersistentProgrammaticWorkspace(config, "integrity", ["calc.py"], [addition_check()])
            with self.assertRaisesRegex(HarnessError, "stage is still active"):
                PersistentProgrammaticWorkspace.open(
                    config, "integrity", ["calc.py"], [addition_check()]
                )
            state = owner.execute(InspectFile("calc.py"))
            owner.execute(ReplaceFile("write-1", "calc.py", str(state["sha256"]), "def add(a, b):\n    return a + b\n"))
            owner.close()

            resumed = PersistentProgrammaticWorkspace.open(config, "integrity", ["calc.py"], [addition_check()])
            with self.assertRaisesRegex(HarnessError, "already used"):
                resumed.execute(ReplaceFile("write-1", "calc.py", resumed.execute(InspectFile("calc.py"))["sha256"], "x=1\n"))
            resumed.close()

            checkpoint = root / ".harness" / "checkpoints" / "programmatic" / "integrity.json"
            document = json.loads(checkpoint.read_text(encoding="utf-8"))
            document["generation"] += 1
            material = dict(document)
            material.pop("checkpoint_hmac_sha256")
            document["checkpoint_hmac_sha256"] = hashlib.sha256(
                json.dumps(
                    material, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            checkpoint.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "authentication"):
                PersistentProgrammaticWorkspace.open(config, "integrity", ["calc.py"], [addition_check()])

    def test_restart_binds_source_mode_type_and_filesystem_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "calc.py"
            source.write_text("value = 1\n", encoding="utf-8")
            config = config_for(root)
            mode_workspace = PersistentProgrammaticWorkspace(
                config, "mode-bound", ["calc.py"], [addition_check()]
            )
            mode_workspace.close()
            original_mode = stat.S_IMODE(source.stat().st_mode)
            changed_mode = 0o444 if original_mode != 0o444 else 0o644
            source.chmod(changed_mode)
            try:
                with self.assertRaisesRegex(HarnessError, "source baseline changed"):
                    PersistentProgrammaticWorkspace.open(
                        config, "mode-bound", ["calc.py"], [addition_check()]
                    )
            finally:
                source.chmod(original_mode)

            type_workspace = PersistentProgrammaticWorkspace(
                config, "type-bound", ["calc.py"], [addition_check()]
            )
            type_workspace.close()
            source.unlink()
            source.mkdir()
            with self.assertRaisesRegex(
                HarnessError, "not a regular file|source baseline changed|Cannot read staged file",
            ):
                PersistentProgrammaticWorkspace.open(
                    config, "type-bound", ["calc.py"], [addition_check()]
                )

    def test_bounded_verification_and_tainted_worker_do_not_change_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "calc.py"
            source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            loud = VerificationAction(
                "loud",
                (sys.executable, "-c", "import sys; sys.stdout.write('x'*10000)"),
            )
            config = config_for(
                root,
                execution__max_output_bytes=1024,
                workflow__max_tool_output_bytes=1024,
                workflow__max_tool_total_bytes=2048,
            )
            workspace = PersistentProgrammaticWorkspace(config, "bounded", ["calc.py"], [loud])
            attach_test_host_runner(workspace)
            result = workspace.execute(RunVerification("loud-1", "loud"))
            self.assertTrue(result.result.output_truncated)
            self.assertLessEqual(len(result.result.stdout.encode("utf-8")), 1024)
            workspace.close()

            slow = VerificationAction(
                "slow",
                (sys.executable, "-c", "import time; time.sleep(5)"),
                timeout_seconds=0.1,
            )
            timed = PersistentProgrammaticWorkspace(config, "timed", ["calc.py"], [slow])
            attach_test_host_runner(timed)
            timed_result = timed.execute(RunVerification("slow-1", "slow"))
            self.assertTrue(timed_result.result.timed_out)
            self.assertFalse(timed_result.result.passed)
            timed.close()

            mutator = VerificationAction(
                "mutate",
                (sys.executable, "-c", "open('calc.py','w').write('bad=1\\n')"),
            )
            tainted = PersistentProgrammaticWorkspace(config, "tainted", ["calc.py"], [mutator])
            attach_test_host_runner(tainted)
            with self.assertRaisesRegex(HarnessError, "changed .*approved staged file"):
                tainted.execute(RunVerification("mutate-1", "mutate"))
            self.assertEqual(source.read_text(encoding="utf-8"), "def add(a, b):\n    return a - b\n")
            tainted.close()
            with self.assertRaisesRegex(HarnessError, "tainted"):
                PersistentProgrammaticWorkspace.open(config, "tainted", ["calc.py"], [mutator])

    def test_credential_material_is_rejected_before_stage_or_checkpoint_change(self) -> None:
        secret = "sk-programmatic-secret-1234567890"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"HARNESS_API_KEY": secret}, clear=False
        ):
            root = Path(temporary)
            source = root / "calc.py"
            source.write_text("value = 1\n", encoding="utf-8")
            workspace = PersistentProgrammaticWorkspace(
                config_for(root), "secret", ["calc.py"],
                [VerificationAction("ok", (sys.executable, "-c", "raise SystemExit(0)"))],
            )
            before = workspace.checkpoint_path.read_bytes()
            state = workspace.execute(InspectFile("calc.py"))
            with self.assertRaisesRegex(HarnessError, "credential-like material"):
                workspace.execute(
                    ReplaceFile("secret-write", "calc.py", str(state["sha256"]), f'value = "{secret}"\n')
                )
            self.assertEqual(workspace.checkpoint_path.read_bytes(), before)
            self.assertNotIn(secret.encode(), workspace.checkpoint_path.read_bytes())
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
            workspace.close()


if __name__ == "__main__":
    unittest.main()
