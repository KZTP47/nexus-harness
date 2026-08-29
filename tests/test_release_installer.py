from __future__ import annotations

import importlib.util
import tempfile
import unittest
import json
import os
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "nexus_release_installer", ROOT / "scripts" / "install_nexus_harness.py"
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

BUILD_SPEC = importlib.util.spec_from_file_location(
    "nexus_build_info", ROOT / "scripts" / "prepare_build_info.py"
)
assert BUILD_SPEC and BUILD_SPEC.loader
build_info = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build_info)


class ReleaseInstallerTests(unittest.TestCase):
    def test_requires_exactly_one_installer_and_checksum(self):
        release = {
            "assets": [
                {"name": "Nexus-Harness-Setup-0.2.0.exe"},
                {"name": "Nexus-Harness-Setup-0.2.0.exe.sha256"},
            ]
        }
        executable, checksum = installer._assets(release)
        self.assertTrue(executable["name"].endswith(".exe"))
        self.assertTrue(checksum["name"].endswith(".sha256"))
        with self.assertRaises(installer.InstallError):
            installer._assets({"assets": release["assets"] + [{"name": "other.exe"}]})

    def test_checksum_must_name_the_exact_installer(self):
        with tempfile.TemporaryDirectory() as folder:
            checksum = Path(folder) / "release.sha256"
            checksum.write_text("a" * 64 + "  Nexus-Harness-Setup-0.2.0.exe\n", encoding="utf-8")
            self.assertEqual(
                installer._expected_digest(checksum, "Nexus-Harness-Setup-0.2.0.exe"),
                "a" * 64,
            )
            with self.assertRaises(installer.InstallError):
                installer._expected_digest(checksum, "different.exe")

    def test_downloads_may_not_leave_github(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(installer.InstallError):
                installer._download("https://example.com/setup.exe", Path(folder) / "x", 100)

    def test_existing_private_repository_token_is_sent_without_logging(self):
        with mock.patch.object(installer, "_github_token", return_value="private-token"):
            request = installer._request(
                "https://api.github.com/repos/KZTP47/nexus-harness/releases/assets/42",
                accept="application/octet-stream",
            )
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(request.get_header("Accept"), "application/octet-stream")
        self.assertNotIn("private-token", installer.__doc__ or "")

    def test_redirect_validation_happens_before_follow_and_strips_cross_host_auth(self):
        handler = installer._SafeGitHubRedirects()
        original = installer.urllib.request.Request(
            "https://api.github.com/repos/KZTP47/nexus-harness/releases/assets/42",
            headers={"Authorization": "Bearer private-token"},
        )
        redirected = handler.redirect_request(
            original, None, 302, "Found", {},
            "https://release-assets.githubusercontent.com/nexus/setup.exe",
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(installer.InstallError):
            handler.redirect_request(
                original, None, 302, "Found", {},
                "https://attacker.example/collect-token",
            )

    def test_authenticated_asset_download_uses_the_release_asset_api(self):
        asset = {
            "url": "https://api.github.com/repos/KZTP47/nexus-harness/releases/assets/42",
            "browser_download_url": "https://github.com/KZTP47/nexus-harness/releases/download/v0.2.0/x.exe",
        }
        self.assertEqual(
            installer._asset_download_url(asset, authenticated=True), asset["url"]
        )
        self.assertEqual(
            installer._asset_download_url(asset, authenticated=False),
            asset["browser_download_url"],
        )

    def test_windows_signature_must_be_valid_before_execution(self):
        valid = mock.Mock(returncode=0, stdout=json.dumps({
            "Status": "Valid", "Subject": "CN=Nexus Publisher", "Message": "",
        }), stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=valid):
            self.assertEqual(
                installer._authenticode_signer(Path("Nexus.exe"), "CN=Nexus Publisher"), "CN=Nexus Publisher"
            )
            with self.assertRaises(installer.InstallError):
                installer._authenticode_signer(Path("Nexus.exe"), "CN=Someone Else")
        invalid = mock.Mock(returncode=0, stdout=json.dumps({
            "Status": "NotSigned", "Subject": "", "Message": "not signed",
        }), stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=invalid):
            with self.assertRaises(installer.InstallError):
                installer._authenticode_signer(Path("Nexus.exe"), "CN=Nexus Publisher")
            self.assertEqual(
                installer._authenticode_signer(Path("Nexus.exe"), None),
                "SHA-256 verified; not Authenticode-signed",
            )

    def test_checksum_only_mode_rejects_an_unexpected_signature(self):
        signed = mock.Mock(returncode=0, stdout=json.dumps({
            "Status": "Valid", "Subject": "CN=Unknown", "Message": "",
        }), stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=signed):
            with self.assertRaises(installer.InstallError):
                installer._authenticode_signer(Path("Nexus.exe"), None)

    def test_tagged_unsigned_build_identifies_itself_as_a_release(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "build-info.json"
            with (
                mock.patch.object(build_info, "OUTPUT", output),
                mock.patch.object(build_info, "git", side_effect=["abc123", ""]),
                mock.patch.dict(os.environ, {
                    "NEXUS_UNSIGNED_RELEASE": "1", "NEXUS_SIGNED_BUILD": "",
                }),
            ):
                self.assertEqual(build_info.main(), 0)
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["build_kind"], "unsigned release")

    def test_clean_machine_release_and_first_run_contracts_are_wired(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
        panel = (ROOT / "src" / "our_harness" / "ui" / "app.js").read_text(encoding="utf-8")
        desktop = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
        package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        powershell_installer = (ROOT / "scripts" / "install_nexus_harness.ps1").read_text(encoding="utf-8")
        top_level = (ROOT / "Install Nexus Harness.cmd").read_text(encoding="utf-8")
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("npm run smoke:built", workflow)
        self.assertIn("Get-FileHash -Algorithm SHA256", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\s]+@v\d")
        self.assertIn("WINDOWS_CERTIFICATE_BASE64", workflow)
        self.assertIn("NEXUS_UNSIGNED_RELEASE", workflow)
        self.assertIn("explicitly unsigned installer", workflow)
        self.assertIn("Authenticode configuration is partial", workflow)
        self.assertIn("publisher is pinned", workflow)
        self.assertIn("Refusing to replace or append assets", workflow)
        self.assertIn("UNSIGNED-DEV", workflow)
        self.assertIn("Finish setup before starting", panel)
        self.assertIn("Nexus will not begin work it cannot verify", panel)
        self.assertIn("quickBootstrap", panel)
        self.assertIn("NEXUS BOOTSTRAP MODE", (ROOT / "src" / "our_harness" / "server.py").read_text(encoding="utf-8"))
        self.assertIn("requestSingleInstanceLock", desktop)
        self.assertIn("harness:diagnostics", desktop)
        self.assertIn("NEXUS_BUILD_COMMIT", desktop)
        self.assertEqual(package["build"]["win"]["target"], "nsis")
        self.assertTrue((ROOT / "requirements-runtime.lock").is_file())
        browser_lock = json.loads((ROOT / "runtime-playwright.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(browser_lock["schema_version"], 1)
        self.assertRegex(browser_lock["node"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(browser_lock["chromium"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("Get-AuthenticodeSignature", powershell_installer)
        self.assertIn("expectedPublisher", powershell_installer)
        self.assertIn("-UNSIGNED", powershell_installer)
        self.assertIn("SHA-256 verified", powershell_installer)
        self.assertIn("credential fill", powershell_installer)
        self.assertIn("GH_TOKEN", powershell_installer)
        self.assertIn("$installers[0].url", powershell_installer)
        self.assertTrue((ROOT / "release" / "windows-authenticode-publisher.txt").is_file())
        self.assertTrue((ROOT / "THIRD_PARTY_NOTICES.md").is_file())
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Node.js 22.18.0", notices)
        self.assertIn("Playwright 1.62.1", notices)
        self.assertIn("Chromium Headless Shell", notices)
        contained_smoke = (ROOT / "scripts" / "smoke_bundled_playwright.py").read_text(encoding="utf-8")
        broker_source = (ROOT / "src" / "our_harness" / "playwright_runtime.py").read_text(encoding="utf-8")
        self.assertIn("chromium.connectOverCDP", contained_smoke)
        self.assertIn("run_brokered_playwright_appcontainer", contained_smoke)
        self.assertIn("Browser.close", broker_source)
        self.assertIn("same-profile-appcontainer-loopback", broker_source)
        self.assertIn("NEXUS_DENIED_WRITE", contained_smoke)
        self.assertNotIn("grant_traverse_ancestors=True", contained_smoke)
        self.assertTrue(any(item.get("to") == "THIRD_PARTY_NOTICES.md" for item in package["build"]["extraResources"]))
        self.assertIn("Get-FileHash -Algorithm SHA256", powershell_installer)
        self.assertIn("install_nexus_harness.ps1", top_level)
        self.assertNotIn("where python", top_level.lower())


if __name__ == "__main__":
    unittest.main()
