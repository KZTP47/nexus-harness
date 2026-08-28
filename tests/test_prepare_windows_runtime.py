from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import prepare_windows_runtime as runtime


class RuntimePreparationTests(unittest.TestCase):
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

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage):
                prepared = runtime.prepare(output)

            self.assertEqual(prepared, output.resolve())
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

            with mock.patch.object(runtime, "DESKTOP", desktop), mock.patch.object(
                runtime, "RUNTIME_LOCK", desktop / ".runtime-build.lock"
            ), mock.patch.object(runtime, "_prepare_staging", side_effect=stage):
                runtime.prepare(output)

            self.assertEqual((output / "identity.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse(any(desktop.glob(".runtime-stage-*")))
            self.assertFalse(any(desktop.glob(".runtime-previous-*")))


if __name__ == "__main__":
    unittest.main()
