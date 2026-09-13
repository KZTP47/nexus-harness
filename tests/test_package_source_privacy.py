"""Shipped source must not contain build-machine caches or local account state."""
import tempfile
import unittest
from pathlib import Path

from scripts.build_windows_desktop import verify_product_source_privacy


class PackageSourcePrivacyTests(unittest.TestCase):
    def test_clean_source_under_arbitrary_installation_root(self):
        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory) / "arbitrary-install" / "resources"
            source = resources / "harness" / "src"
            source.mkdir(parents=True)
            (source / "mail.py").write_text("account = detect_signed_in_account()\n")
            verify_product_source_privacy(resources)

    def test_rejects_cache_and_private_state(self):
        for name in ("src/__pycache__/mail.pyc", "src/mail.pyo", ".harness/email-studio/mail.db", "src/config.local.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                resources = Path(directory)
                bad = resources / "harness" / name
                bad.parent.mkdir(parents=True)
                bad.write_bytes(b"synthetic-private-build-state")
                with self.assertRaises(RuntimeError):
                    verify_product_source_privacy(resources)

    def test_missing_package_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                verify_product_source_privacy(Path(directory))
