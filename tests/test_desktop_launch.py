from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import desktop_launch
from scripts import put_it_on_your_desktop as shortcut


class DesktopLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Arbitrary user's checkout Å"
        self.source = self.root / "desktop/build-output/win-unpacked"
        self.source.mkdir(parents=True)
        (self.source / "Nexus Harness.exe").write_bytes(b"executable-v1")
        (self.source / "resources").mkdir()
        (self.source / "resources/app.js").write_text("version one", encoding="utf-8")
        self.cache = self.root / ".harness/runtime/desktop-apps"

    def publish(self):
        return desktop_launch.publish_build(self.root)

    def test_rebuild_cannot_replace_running_copy_and_new_launch_selects_new_bytes(self):
        first = self.publish()
        self.assertNotEqual(first.parent, self.source)
        self.assertFalse(os.path.samefile(first, self.source / first.name))
        first_bytes = first.read_bytes()
        (self.source / first.name).write_bytes(b"executable-v2")
        (self.source / "resources/app.js").write_text("version two", encoding="utf-8")
        second = self.publish()
        self.assertNotEqual(second, first)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual((first.parent / "resources/app.js").read_text(), "version one")
        self.assertEqual(second.read_bytes(), b"executable-v2")
        held = json.loads((self.cache / "current.json").read_text())
        self.assertEqual(held["directory"], second.parent.name)
        self.assertEqual(held["schema_version"], 1)
        self.assertEqual(held["contract"], desktop_launch.CONTRACT)

    def test_restart_reuses_verified_copy_and_ignores_generated_python_cache(self):
        first = self.publish()
        generated = first.parent / "resources/__pycache__"
        generated.mkdir()
        (generated / "runtime.pyc").write_bytes(b"runtime-generated cache")
        self.assertEqual(self.publish(), first)

    def test_failed_or_unstable_copy_keeps_previous_selection_and_files(self):
        first = self.publish()
        receipt = (self.cache / "current.json").read_bytes()
        (self.source / "Nexus Harness.exe").write_bytes(b"new executable")
        with mock.patch.object(desktop_launch.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual((self.cache / "current.json").read_bytes(), receipt)
        self.assertEqual(first.read_bytes(), b"executable-v1")
        copy = desktop_launch.shutil.copy2
        def changing(source, target):
            result = copy(source, target)
            Path(source).write_bytes(b"another build replaced this file")
            return result
        with mock.patch.object(desktop_launch.shutil, "copy2", side_effect=changing):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                self.publish()
        self.assertEqual((self.cache / "current.json").read_bytes(), receipt)
        self.assertEqual(list(self.cache.glob(".stage-*")), [])

    def test_corrupt_or_other_checkout_selection_never_overwrites_held_copy(self):
        first = self.publish()
        first.write_bytes(b"damaged")
        second = self.publish()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"damaged")
        self.assertEqual(second.read_bytes(), b"executable-v1")
        receipt = json.loads((self.cache / "current.json").read_text())
        receipt["owner_sha256"] = "other-checkout"
        (self.cache / "current.json").write_text(json.dumps(receipt))
        self.assertNotEqual(self.publish(), second)

    def test_shared_immutable_files_are_never_links_to_mutable_build_output(self):
        first = self.publish()
        (self.source / "resources/app.js").write_text("changed")
        second = self.publish()
        self.assertFalse(os.path.samefile(first, self.source / first.name))
        self.assertFalse(os.path.samefile(second, self.source / second.name))
        (self.source / first.name).write_bytes(b"overwritten while old app runs")
        self.assertEqual(first.read_bytes(), b"executable-v1")
        self.assertEqual(second.read_bytes(), b"executable-v1")

    def test_publication_only_contains_packaged_files_not_project_state(self):
        (self.root / "private-vault").mkdir()
        (self.root / "private-vault/private.md").write_text("private memory")
        (self.root / ".harness").mkdir()
        (self.root / ".harness/config.local.json").write_text("private settings")
        launched = self.publish()
        self.assertFalse((launched.parent / ".harness").exists())
        self.assertFalse((launched.parent / "private-vault").exists())
        receipt = json.loads((self.cache / "current.json").read_text())
        receipt["directory"] = "../../outside"
        (self.cache / "current.json").write_text(json.dumps(receipt))
        self.assertNotEqual(self.publish(), launched)

    def test_shortcut_uses_same_protected_app_for_target_working_directory_and_icon(self):
        app = self.source / "Nexus Harness.exe"
        selected = shortcut.Launcher(app, [], self.source, "built", True, app)
        with mock.patch.object(shortcut, "_built_app", return_value=app):
            launched = shortcut.freeze_built_launcher(selected, self.root)
        self.assertNotEqual(launched.program, app)
        self.assertEqual(launched.working_folder, launched.program.parent)
        self.assertEqual(launched.icon, launched.program)
        installed = shortcut.Launcher(self.root / "installed/app.exe", [], self.root,
                                      "installed", True, self.root / "installed/app.exe")
        self.assertIs(shortcut.freeze_built_launcher(installed, self.root), installed)


if __name__ == "__main__":
    unittest.main()
