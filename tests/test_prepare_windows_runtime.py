from __future__ import annotations

import base64
import hashlib
import io
import inspect
import json
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import prepare_windows_runtime as runtime


def write_runtime_manifest(destination: Path) -> None:
    (destination / "NEXUS_RUNTIME.json").write_text(json.dumps({
        "python": runtime.PYTHON_VERSION,
        "python_sha256": runtime.PYTHON_SHA256,
        "requirements_sha256": runtime.digest(runtime.LOCK),
        "playwright": {"lock_sha256": runtime.digest(runtime.PLAYWRIGHT_LOCK)},
    }), encoding="utf-8")


class RuntimePreparationTests(unittest.TestCase):
    @staticmethod
    def _make_directory_link(link: Path, target: Path) -> None:
        if os.name == "nt":
            made = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if made.returncode:
                raise unittest.SkipTest(
                    "This Windows host cannot create a directory reparse point: "
                    + made.stderr.decode(errors="replace")
                )
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_owned_windows_loader_lock_is_retried(self) -> None:
        attempts = 0

        def briefly_locked() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                error = PermissionError("access denied")
                error.winerror = 5
                raise error
            return "published"

        with mock.patch.object(runtime.os, "name", "nt"), mock.patch.object(runtime.time, "sleep"):
            self.assertEqual(
                runtime.retry_owned_windows_operation(briefly_locked, "publish", timeout_seconds=1),
                "published",
            )
        self.assertEqual(attempts, 3)

    def test_playwright_runtime_lock_is_exact_and_checksum_bound(self) -> None:
        locked = json.loads(runtime.PLAYWRIGHT_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(locked["schema_version"], 1)
        self.assertRegex(locked["node"]["version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(locked["node"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(locked["chromium"]["name"], "chromium-headless-shell")
        self.assertRegex(locked["chromium"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            {one["name"] for one in locked["packages"]},
            {"playwright", "playwright-core", "@playwright/test"},
        )
        self.assertEqual({one["version"] for one in locked["packages"]}, {"1.62.1"})
        for package in locked["packages"]:
            self.assertRegex(package["integrity"], r"^sha512-[A-Za-z0-9+/]+={0,2}$")

    def test_python_runtime_lock_and_validator_include_pinned_pytest_graph(self) -> None:
        locked = [
            line.strip() for line in runtime.LOCK.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        by_name = {
            line.split("==", 1)[0].casefold(): line
            for line in locked if "==" in line
        }
        for package in ("pytest", "iniconfig", "packaging", "pluggy", "pygments"):
            with self.subTest(package=package):
                self.assertIn(package, by_name)
                self.assertRegex(
                    by_name[package],
                    re.compile(rf"^{package}==[^=<>!~]+$", re.IGNORECASE),
                )
        validator = inspect.getsource(runtime.validate_runtime)
        self.assertIn("import pytest", validator)
        self.assertIn("dist.requires or []", validator)
        self.assertIn("required.specifier", validator)

    def test_npm_integrity_is_validated_from_archive_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "package.tgz"
            archive.write_bytes(b"immutable package bytes")
            encoded = base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode("ascii")
            self.assertTrue(runtime.sri_digest(archive, "sha512-" + encoded))
            archive.write_bytes(b"tampered")
            self.assertFalse(runtime.sri_digest(archive, "sha512-" + encoded))

    def test_npm_extractor_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "package.tgz"
            with tarfile.open(archive, "w:gz") as packed:
                content = b"escape"
                item = tarfile.TarInfo("package/../../escape.txt")
                item.size = len(content)
                packed.addfile(item, io.BytesIO(content))
            with self.assertRaisesRegex(RuntimeError, "escapes"):
                runtime._safe_npm_extract(archive, Path(temporary) / "module")

    def test_validated_staging_replaces_runtime_without_mutating_live_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")

            def stage(destination: Path) -> None:
                self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "old")
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("new", encoding="utf-8")
                write_runtime_manifest(destination)

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage):
                prepared = runtime.prepare(output)

            self.assertEqual(prepared, Path(os.path.abspath(output)))
            self.assertTrue(prepared.samefile(output))
            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse(any(desktop.glob(".runtime-stage-*")))
            self.assertFalse(any(desktop.glob(".runtime-previous-*")))

    def test_failed_staging_preserves_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")

            def fail(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "partial.txt").write_text("partial", encoding="utf-8")
                raise RuntimeError("validation failed")

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, "validation failed"):
                    runtime.prepare(output)

            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "old")
            self.assertFalse(any(desktop.glob(".runtime-stage-*")))

    def test_atomic_runtime_publication_waits_for_active_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")
            timeouts: dict[str, float] = {}

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("new", encoding="utf-8")
                write_runtime_manifest(destination)

            real_retry = runtime.retry_owned_windows_operation

            def record_timeout(operation, description: str, timeout_seconds: float = 30.0):
                timeouts[description] = timeout_seconds
                return real_retry(operation, description, timeout_seconds)

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=record_timeout
            ):
                runtime.prepare(output)

            self.assertEqual(
                timeouts["preserve previous private runtime"],
                runtime.RUNTIME_CANONICAL_RENAME_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                timeouts["publish validated private runtime"],
                runtime.RUNTIME_PUBLISH_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                timeouts["remove previous private runtime"],
                runtime.RUNTIME_CLEANUP_TIMEOUT_SECONDS,
            )
            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "new")

    def test_failed_publication_uses_full_timeout_for_restore_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")
            timeouts: dict[str, float] = {}

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("new", encoding="utf-8")
                write_runtime_manifest(destination)

            def fail_publish(operation, description: str, timeout_seconds: float = 30.0):
                timeouts[description] = timeout_seconds
                if description == "publish validated private runtime":
                    raise PermissionError("injected publish failure")
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=fail_publish
            ):
                with self.assertRaisesRegex(PermissionError, "injected publish failure"):
                    runtime.prepare(output)

            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "old")
            for operation in ("publish validated private runtime", "restore previous private runtime"):
                self.assertEqual(
                    timeouts[operation], runtime.RUNTIME_PUBLISH_TIMEOUT_SECONDS, operation
                )
            self.assertEqual(
                timeouts["preserve previous private runtime"],
                runtime.RUNTIME_CANONICAL_RENAME_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                timeouts["remove private-runtime staging tree"],
                runtime.RUNTIME_CLEANUP_TIMEOUT_SECONDS,
            )

    def test_locked_prepare_removes_abandoned_stage_and_rollback_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")
            abandoned_stage = desktop / ".runtime-stage-abandoned"
            abandoned_previous = desktop / ".runtime-previous-abandoned"
            for abandoned in (abandoned_stage, abandoned_previous):
                abandoned.mkdir()
                (abandoned / "partial.txt").write_text("partial", encoding="utf-8")

            def stage(destination: Path) -> None:
                self.assertFalse(abandoned_stage.exists())
                self.assertFalse(abandoned_previous.exists())
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("new", encoding="utf-8")
                write_runtime_manifest(destination)

            timeouts: list[float] = []
            real_retry = runtime.retry_owned_windows_operation

            def record_timeout(operation, description: str, timeout_seconds: float = 30.0):
                if description == "remove abandoned private-runtime tree":
                    timeouts.append(timeout_seconds)
                return real_retry(operation, description, timeout_seconds)

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=record_timeout
            ):
                runtime.prepare(output)

            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse(any(desktop.glob(".runtime-stage-*")))
            self.assertFalse(any(desktop.glob(".runtime-previous-*")))
            self.assertEqual(
                timeouts,
                [runtime.RUNTIME_CLEANUP_TIMEOUT_SECONDS, runtime.RUNTIME_CLEANUP_TIMEOUT_SECONDS],
            )

    def test_locked_previous_runtime_selects_one_immutable_verified_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            old = b"old-runtime-must-stay-byte-identical"
            (output / "identity.txt").write_bytes(old)

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("fresh", encoding="utf-8")
                write_runtime_manifest(destination)

            def locked_previous(operation, description: str, timeout_seconds: float = 30.0):
                if description == "preserve previous private runtime":
                    error = PermissionError("watched directory")
                    error.winerror = 5
                    raise error
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=locked_previous
            ), mock.patch.object(runtime.os, "name", "nt"):
                selected = runtime.prepare(output)
                self.assertEqual(runtime.selected_runtime(), selected)

            expected = desktop / ".runtime-published" / runtime._runtime_input_identity()
            self.assertEqual(selected, expected)
            self.assertEqual((output / "identity.txt").read_bytes(), old)
            self.assertEqual((selected / "identity.txt").read_text(encoding="utf-8"), "fresh")
            self.assertFalse(any(desktop.glob(".runtime-stage-*")))
            self.assertFalse(any(desktop.glob(".runtime-previous-*")))
            self.assertEqual(
                len([one for one in (desktop / ".runtime-published").iterdir() if one.is_dir()]), 1
            )

    def test_runtime_selection_preserves_a_safe_ancestor_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real_parent = base / "real-parent"
            real_desktop = real_parent / "desktop"
            real_desktop.mkdir(parents=True)
            alias_parent = base / "ancestor-alias"
            self._make_directory_link(alias_parent, real_parent)
            desktop = alias_parent / "desktop"
            output = desktop / "runtime"

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("verified", encoding="utf-8")
                write_runtime_manifest(destination)

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage):
                prepared = runtime.prepare(output)
                selected = runtime.selected_runtime()

            self.assertEqual(prepared, Path(os.path.abspath(output)))
            self.assertEqual(selected, prepared)
            self.assertTrue(prepared.samefile(real_desktop / "runtime"))

    def test_candidate_collision_fails_without_mutating_old_or_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")
            published = desktop / ".runtime-published" / runtime._runtime_input_identity()
            published.mkdir(parents=True)
            (published / "identity.txt").write_text("unrelated", encoding="utf-8")

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("fresh", encoding="utf-8")
                write_runtime_manifest(destination)

            def locked_previous(operation, description: str, timeout_seconds: float = 30.0):
                if description == "preserve previous private runtime":
                    error = PermissionError("watched directory")
                    error.winerror = 5
                    raise error
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=locked_previous
            ), mock.patch.object(runtime.os, "name", "nt"):
                with self.assertRaisesRegex(RuntimeError, "failed complete"):
                    runtime.prepare(output)

            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((published / "identity.txt").read_text(encoding="utf-8"), "unrelated")
            self.assertFalse((desktop / ".runtime-selection.json").exists())

    def test_repeat_build_reuses_verified_candidate_despite_nondeterministic_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("locked old", encoding="utf-8")
            assemblies = iter(("first-wheel-metadata", "second-wheel-metadata"))

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "functional.txt").write_text("same behavior", encoding="utf-8")
                (destination / "installer-metadata.txt").write_text(next(assemblies), encoding="utf-8")
                write_runtime_manifest(destination)

            def locked_previous(operation, description: str, timeout_seconds: float = 30.0):
                if description == "preserve previous private runtime":
                    error = PermissionError("watched directory")
                    error.winerror = 32
                    raise error
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=locked_previous
            ), mock.patch.object(runtime.os, "name", "nt"):
                first = runtime.prepare(output)
                first_digest = runtime.runtime_tree_digest(first)
                second_staging = desktop / ".runtime-stage-manual-second-assembly"
                stage(second_staging)
                second = runtime._publish_immutable_candidate(second_staging)
                self.assertEqual(runtime.selected_runtime(), first)

            self.assertEqual(first, second)
            self.assertEqual(runtime.runtime_tree_digest(first), first_digest)
            self.assertEqual(
                (first / "installer-metadata.txt").read_text(encoding="utf-8"),
                "first-wheel-metadata",
            )
            self.assertFalse(any(desktop.glob(".runtime-stage-*")))
            self.assertEqual(
                len([one for one in (desktop / ".runtime-published").iterdir() if one.is_dir()]), 1
            )

    def test_repeat_prepare_reuses_legacy_candidate_before_offline_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("locked old", encoding="utf-8")
            stage_calls = 0

            def stage(destination: Path) -> None:
                nonlocal stage_calls
                stage_calls += 1
                destination.mkdir(parents=True)
                (destination / "payload.txt").write_text("verified", encoding="utf-8")
                write_runtime_manifest(destination)

            def locked_previous(operation, description: str, timeout_seconds: float = 30.0):
                if description == "preserve previous private runtime":
                    error = PermissionError("watched directory")
                    error.winerror = 32
                    raise error
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=locked_previous
            ), mock.patch.object(runtime.os, "name", "nt"):
                first = runtime.prepare(output)
                receipt = runtime._candidate_receipt_path(runtime._runtime_input_identity())
                receipt.unlink()
                legacy = json.loads(runtime._runtime_selection_path().read_text(encoding="utf-8"))
                legacy.pop("input_identity")
                runtime._runtime_selection_path().write_text(json.dumps(legacy), encoding="utf-8")
                with mock.patch.object(
                    runtime, "_prepare_staging", side_effect=RuntimeError("offline")
                ) as offline_stage:
                    second = runtime.prepare(output)
                self.assertTrue(receipt.is_file())
                self.assertEqual(runtime.selected_runtime(), first)

            self.assertEqual(second, first)
            self.assertEqual(stage_calls, 1)
            offline_stage.assert_not_called()

    def test_repeat_build_rejects_tampered_existing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("locked old", encoding="utf-8")
            turn = 0

            def stage(destination: Path) -> None:
                nonlocal turn
                turn += 1
                destination.mkdir(parents=True)
                (destination / "payload.txt").write_text(f"assembly {turn}", encoding="utf-8")
                write_runtime_manifest(destination)

            def locked_previous(operation, description: str, timeout_seconds: float = 30.0):
                if description == "preserve previous private runtime":
                    error = PermissionError("watched directory")
                    error.winerror = 32
                    raise error
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=locked_previous
            ), mock.patch.object(runtime.os, "name", "nt"):
                selected = runtime.prepare(output)
                (selected / "payload.txt").write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "failed complete"):
                    runtime.prepare(output)

            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "locked old")
            self.assertEqual((selected / "payload.txt").read_text(encoding="utf-8"), "tampered")

    def test_runtime_selection_rejects_traversal_and_tree_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            desktop.mkdir()
            selector = desktop / ".runtime-selection.json"
            selector.write_text(json.dumps({
                "schema_version": 1,
                "runtime_path": "../outside",
                "manifest_sha256": "0" * 64,
                "tree_sha256": "0" * 64,
            }), encoding="utf-8")
            with mock.patch.object(runtime, "DESKTOP", desktop):
                with self.assertRaisesRegex(RuntimeError, "outside"):
                    runtime.selected_runtime()

                selected = desktop / "runtime"
                selected.mkdir()
                write_runtime_manifest(selected)
                (selected / "one.txt").write_text("one", encoding="utf-8")
                runtime._write_runtime_selection(selected)
                (selected / "one.txt").write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "tree does not match"):
                    runtime.selected_runtime()

    def test_nonretryable_preserve_failure_keeps_old_runtime_and_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                write_runtime_manifest(destination)

            def denied(operation, description: str, timeout_seconds: float = 30.0):
                if description == "preserve previous private runtime":
                    raise OSError("nonretryable")
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=denied
            ):
                with self.assertRaisesRegex(OSError, "nonretryable"):
                    runtime.prepare(output)
            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "old")
            self.assertFalse((desktop / ".runtime-selection.json").exists())

    def test_retryable_previous_cleanup_denial_does_not_fail_valid_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("fresh", encoding="utf-8")
                write_runtime_manifest(destination)

            def cleanup_denied(operation, description: str, timeout_seconds: float = 30.0):
                if description in {
                    "remove previous private runtime", "remove private-runtime rollback tree",
                }:
                    error = PermissionError("still watched")
                    error.winerror = 32
                    raise error
                return operation()

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                runtime, "retry_owned_windows_operation", side_effect=cleanup_denied
            ), mock.patch.object(runtime.os, "name", "nt"):
                self.assertEqual(runtime.prepare(output), output)

            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "fresh")
            with mock.patch.object(runtime, "DESKTOP", desktop):
                self.assertEqual(runtime.selected_runtime(), output)
            previous = list(desktop.glob(".runtime-previous-*"))
            self.assertEqual(len(previous), 1)
            self.assertEqual((previous[0] / "identity.txt").read_text(encoding="utf-8"), "old")

    def test_abandoned_runtime_link_is_rejected_without_deleting_victim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            desktop.mkdir()
            victim = desktop / "victim"
            victim.mkdir()
            sentinel = victim / "sentinel.txt"
            sentinel.write_text("must survive", encoding="utf-8")
            linked = desktop / ".runtime-stage-attacker"
            try:
                linked.symlink_to(victim, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory link unavailable: {error}")
            with mock.patch.object(runtime, "DESKTOP", desktop):
                with self.assertRaisesRegex(RuntimeError, "unsafe abandoned"):
                    runtime._remove_abandoned_runtime_trees()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive")

    def test_runtime_digest_allows_safe_ancestor_alias_but_rejects_nested_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real = base / "real-parent"
            runtime_root = real / "desktop" / "candidate"
            runtime_root.mkdir(parents=True)
            (runtime_root / "one.txt").write_text("one", encoding="utf-8")
            alias = base / "runner-alias"
            self._make_directory_link(alias, real)
            aliased_runtime = alias / "desktop" / "candidate"
            self.assertEqual(
                runtime.runtime_tree_digest(aliased_runtime),
                runtime.runtime_tree_digest(runtime_root),
            )

            victim = base / "victim"
            victim.mkdir()
            (victim / "sentinel.txt").write_text("outside", encoding="utf-8")
            self._make_directory_link(runtime_root / "nested-link", victim)
            with self.assertRaisesRegex(RuntimeError, "link or reparse|escapes"):
                runtime.runtime_tree_digest(aliased_runtime)
            self.assertEqual((victim / "sentinel.txt").read_text(encoding="utf-8"), "outside")

    def test_runtime_selection_rejects_a_reparse_publication_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            desktop.mkdir()
            real_published = desktop / ".published-real"
            identity = runtime._runtime_input_identity()
            candidate = real_published / identity
            candidate.mkdir(parents=True)
            write_runtime_manifest(candidate)
            (candidate / "identity.txt").write_text("owned elsewhere", encoding="utf-8")
            self._make_directory_link(desktop / ".runtime-published", real_published)
            payload = {
                "schema_version": 1,
                "runtime_path": f".runtime-published/{identity}",
                "manifest_sha256": runtime.digest(candidate / "NEXUS_RUNTIME.json"),
                "tree_sha256": runtime.runtime_tree_digest(candidate),
                "python": runtime.PYTHON_VERSION,
                "python_sha256": runtime.PYTHON_SHA256,
                "requirements_sha256": runtime.digest(runtime.LOCK),
                "playwright_lock_sha256": runtime.digest(runtime.PLAYWRIGHT_LOCK),
                "input_identity": identity,
            }

            with mock.patch.object(runtime, "DESKTOP", desktop):
                with self.assertRaisesRegex(RuntimeError, "publication root.*reparse"):
                    runtime._selected_runtime_from_payload(payload)

    def test_prepare_and_selection_allow_a_safe_ancestor_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real = base / "real-parent"
            desktop = real / "desktop"
            desktop.mkdir(parents=True)
            alias = base / "runner-alias"
            self._make_directory_link(alias, real)
            aliased_desktop = alias / "desktop"
            output = aliased_desktop / "runtime"

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("fresh", encoding="utf-8")
                write_runtime_manifest(destination)

            with mock.patch.object(runtime, "DESKTOP", aliased_desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", aliased_desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage):
                prepared = runtime.prepare(output)
                payload = runtime._selection_payload(prepared)

            self.assertEqual(prepared, output)
            self.assertEqual(payload["runtime_path"], "runtime")
            self.assertEqual((desktop / "runtime" / "identity.txt").read_text(), "fresh")

    def test_runtime_output_link_is_rejected_without_mutating_victim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            desktop.mkdir()
            victim = desktop / "victim"
            victim.mkdir()
            sentinel = victim / "sentinel.txt"
            sentinel.write_text("must survive", encoding="utf-8")
            output = desktop / "runtime"
            try:
                output.symlink_to(victim, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory link unavailable: {error}")
            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ):
                with self.assertRaisesRegex(RuntimeError, "link or reparse"):
                    runtime.prepare(output)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive")

    @unittest.skipUnless(os.name == "nt", "real Windows directory sharing regression")
    def test_real_no_share_delete_handle_uses_immutable_candidate(self) -> None:
        import ctypes
        from ctypes import wintypes

        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "desktop"
            output = desktop / "runtime"
            output.mkdir(parents=True)
            (output / "identity.txt").write_text("old", encoding="utf-8")
            create = ctypes.windll.kernel32.CreateFileW
            create.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            ]
            create.restype = wintypes.HANDLE
            handle = create(str(output), 1, 3, None, 3, 0x02000000, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)

            def stage(destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / "identity.txt").write_text("fresh", encoding="utf-8")
                write_runtime_manifest(destination)

            try:
                with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                    runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
                ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage), mock.patch.object(
                    runtime, "RUNTIME_CANONICAL_RENAME_TIMEOUT_SECONDS", 0
                ):
                    selected = runtime.prepare(output)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            self.assertNotEqual(selected, output)
            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((selected / "identity.txt").read_text(encoding="utf-8"), "fresh")


if __name__ == "__main__":
    unittest.main()
