from __future__ import annotations

import importlib.util
import tempfile
import unittest
import json
import os
import re
import shutil
import subprocess
import tomllib
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


def _powershell_hosts() -> list[str]:
    hosts: list[str] = []
    for name in ("powershell.exe", "pwsh.exe"):
        resolved = shutil.which(name)
        if resolved and resolved.casefold() not in {item.casefold() for item in hosts}:
            hosts.append(resolved)
    return hosts


def _installer_header_function_source() -> str:
    source = (ROOT / "scripts" / "install_nexus_harness.ps1").read_text(encoding="utf-8")
    start = source.index("function Invoke-OptionalNativeCommand")
    end = source.index("\n$githubHeaders = Get-GitHubHeaders", start)
    return source[start:end]


class ReleaseInstallerTests(unittest.TestCase):
    def test_public_version_surfaces_cannot_drift(self):
        expected = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))["version"]
        root_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        root_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        desktop_lock = json.loads((ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
        with (ROOT / "pyproject.toml").open("rb") as handle:
            python_project = tomllib.load(handle)
        init_source = (ROOT / "src" / "our_harness" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_source, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual({
            root_package["version"],
            root_lock["version"],
            root_lock["packages"][""]["version"],
            desktop_lock["version"],
            desktop_lock["packages"][""]["version"],
            python_project["project"]["version"],
            match.group(1),
        }, {expected})
        self.assertIn('_CLIENT_INFO = {"name": "our-harness", "version": __version__}',
                      (ROOT / "src" / "our_harness" / "mcp.py").read_text(encoding="utf-8"))
        self.assertIn('"serverInfo": {"name": WHAT_WE_ARE_CALLED, "version": __version__}',
                      (ROOT / "src" / "our_harness" / "editor.py").read_text(encoding="utf-8"))

    def test_requires_exactly_one_installer_and_checksum(self):
        release = {
            "assets": [
                {"name": "Nexus-Harness-Setup-0.2.1.exe"},
                {"name": "Nexus-Harness-Setup-0.2.1.exe.sha256"},
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
            checksum.write_text("a" * 64 + "  Nexus-Harness-Setup-0.2.1.exe\n", encoding="utf-8")
            self.assertEqual(
                installer._expected_digest(checksum, "Nexus-Harness-Setup-0.2.1.exe"),
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
            "browser_download_url": "https://github.com/KZTP47/nexus-harness/releases/download/v0.2.1/x.exe",
        }
        self.assertEqual(
            installer._asset_download_url(asset, authenticated=True), asset["url"]
        )
        self.assertEqual(
            installer._asset_download_url(asset, authenticated=False),
            asset["browser_download_url"],
        )

    @unittest.skipUnless(os.name == "nt", "PowerShell credential probes are Windows-specific")
    def test_failed_optional_powershell_credential_probes_stay_anonymous(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        native_failure = Path(os.environ["SystemRoot"]) / "System32" / "net.exe"
        self.assertTrue(native_failure.is_file())
        functions = _installer_header_function_source()
        harness = """\
$ErrorActionPreference = 'Stop'
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $global:PSNativeCommandUseErrorActionPreference = $true
}
__INSTALLER_FUNCTIONS__
$global:LASTEXITCODE = 73
$headers = Get-GitHubHeaders
[pscustomobject]@{
    HasAuthorization = $headers.ContainsKey('Authorization')
    ResolvedGh = [bool](Get-Command gh.exe -ErrorAction SilentlyContinue)
    ResolvedGit = [bool](Get-Command git.exe -ErrorAction SilentlyContinue)
    GcmInteractive = [string]$env:GCM_INTERACTIVE
    LastExitCode = $global:LASTEXITCODE
    ErrorActionPreference = [string]$ErrorActionPreference
} | ConvertTo-Json -Compress
""".replace("__INSTALLER_FUNCTIONS__", functions)

        probes = (
            ("gh.exe", ("auth", "token"), None),
            ("git.exe", ("credential", "fill"), "protocol=https\nhost=github.com\n\n"),
        )
        for fake_name, arguments, standard_input in probes:
            with self.subTest(probe=fake_name), tempfile.TemporaryDirectory() as folder:
                folder_path = Path(folder)
                fake = folder_path / fake_name
                shutil.copy2(native_failure, fake)
                fake_result = subprocess.run(
                    [str(fake), *arguments], input=standard_input, text=True,
                    capture_output=True, timeout=5, cwd=folder, errors="replace",
                    env={**os.environ, "PATH": folder},
                )
                self.assertNotEqual(fake_result.returncode, 0)
                self.assertTrue(fake_result.stderr.strip(), "the fake must exercise native stderr")
                harness_path = folder_path / "probe.ps1"
                harness_path.write_text(harness, encoding="utf-8")
                environment = os.environ.copy()
                environment["PATH"] = folder
                environment["GCM_INTERACTIVE"] = "leave-parent-state-alone"
                environment.pop("GH_TOKEN", None)
                environment.pop("GITHUB_TOKEN", None)
                for host in hosts:
                    with self.subTest(probe=fake_name, host=Path(host).name):
                        result = subprocess.run(
                            [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(harness_path)],
                            text=True, capture_output=True, timeout=30, cwd=folder,
                            errors="replace", env=environment,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        payload = json.loads(result.stdout.strip().splitlines()[-1])
                        self.assertFalse(payload["HasAuthorization"])
                        self.assertEqual(payload["ResolvedGh"], fake_name == "gh.exe")
                        self.assertEqual(payload["ResolvedGit"], fake_name == "git.exe")
                        self.assertEqual(payload["GcmInteractive"], "leave-parent-state-alone")
                        self.assertEqual(payload["LastExitCode"], 73)
                        self.assertEqual(payload["ErrorActionPreference"], "Stop")

    @unittest.skipUnless(os.name == "nt", "PowerShell installer messaging is Windows-specific")
    def test_latest_release_404_does_not_claim_the_repository_is_private(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        functions = _installer_header_function_source()
        harness = """\
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$anonymous = @{ Accept = 'application/vnd.github+json' }
$authenticated = @{ Authorization = 'Bearer private-token' }
[pscustomobject]@{
    AnonymousLatest = Get-InstallationFailureMessage 'GitHub returned HTTP 404 while downloading a release asset.' $anonymous $false
    LaterAsset = Get-InstallationFailureMessage 'GitHub returned HTTP 404 while downloading a release asset.' $anonymous $true
    Authenticated = Get-InstallationFailureMessage 'GitHub returned HTTP 404 while downloading a release asset.' $authenticated $false
    UnrelatedNotFound = Get-InstallationFailureMessage 'A local file was Not Found.' $anonymous $false
    Http4040 = Get-InstallationFailureMessage 'GitHub returned HTTP 4040 while downloading a release asset.' $anonymous $false
} | ConvertTo-Json -Compress
""".replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            harness_path = Path(folder) / "message.ps1"
            harness_path.write_text(harness, encoding="utf-8")
            for host in hosts:
                with self.subTest(host=Path(host).name):
                    result = subprocess.run(
                        [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(harness_path)],
                        text=True, capture_output=True, timeout=15, errors="replace",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout.strip().splitlines()[-1])
                    message = payload["AnonymousLatest"]
                    self.assertIn("No installable Nexus Harness release is available", message)
                    self.assertIn("latest stable release", message)
                    self.assertIn("No Windows installer ran", message)
                    self.assertIn("no desktop shortcut could be created", message)
                    self.assertNotIn("This repository is private", message)
                    self.assertEqual(payload["LaterAsset"], "GitHub returned HTTP 404 while downloading a release asset.")
                    self.assertEqual(payload["Authenticated"], message)
                    self.assertEqual(payload["UnrelatedNotFound"], "A local file was Not Found.")
                    self.assertEqual(payload["Http4040"], "GitHub returned HTTP 4040 while downloading a release asset.")

    @unittest.skipUnless(os.name == "nt", "PowerShell release binding is Windows-specific")
    def test_powershell_stable_release_binds_tag_installer_and_checksum_versions(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        functions = _installer_header_function_source()
        harness = r'''
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
function Test-Release([string] $Tag, [string] $Installer, [string] $Checksum) {
    $release = [pscustomobject]@{
        draft = $false
        prerelease = $false
        tag_name = $Tag
        assets = @(
            [pscustomobject]@{ name = $Installer; url = 'https://api.github.com/installer' },
            [pscustomobject]@{ name = $Checksum; url = 'https://api.github.com/checksum' }
        )
    }
    try {
        $bound = Get-NexusStableReleaseAssets $release
        return [pscustomobject]@{
            Accepted = $true
            Version = $bound.Version
            Installer = $bound.Installer.name
            Checksum = $bound.Checksum.name
            Message = ''
        }
    } catch {
        return [pscustomobject]@{
            Accepted = $false
            Version = ''
            Installer = ''
            Checksum = ''
            Message = [string]$_.Exception.Message
        }
    }
}
[pscustomobject]@{
    Exact = Test-Release 'v0.2.1' 'Nexus-Harness-Setup-0.2.1-UNSIGNED.exe' 'Nexus-Harness-Setup-0.2.1-UNSIGNED.exe.sha256'
    WrongInstaller = Test-Release 'v0.2.2' 'Nexus-Harness-Setup-0.2.1.exe' 'Nexus-Harness-Setup-0.2.1.exe.sha256'
    WrongChecksum = Test-Release 'v0.2.1' 'Nexus-Harness-Setup-0.2.1.exe' 'Nexus-Harness-Setup-0.2.0.exe.sha256'
    LooseTag = Test-Release 'release-0.2.1' 'Nexus-Harness-Setup-0.2.1.exe' 'Nexus-Harness-Setup-0.2.1.exe.sha256'
} | ConvertTo-Json -Depth 5 -Compress
'''.replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            harness_path = Path(folder) / "release-binding.ps1"
            harness_path.write_text(harness, encoding="utf-8-sig")
            for host in hosts:
                with self.subTest(host=Path(host).name):
                    result = subprocess.run(
                        [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(harness_path)],
                        text=True, capture_output=True, timeout=20, errors="replace",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout.strip().splitlines()[-1])
                    self.assertTrue(payload["Exact"]["Accepted"])
                    self.assertEqual(payload["Exact"]["Version"], "0.2.1")
                    self.assertEqual(payload["Exact"]["Installer"],
                                     "Nexus-Harness-Setup-0.2.1-UNSIGNED.exe")
                    self.assertFalse(payload["WrongInstaller"]["Accepted"])
                    self.assertIn("does not match installer", payload["WrongInstaller"]["Message"])
                    self.assertFalse(payload["WrongChecksum"]["Accepted"])
                    self.assertIn("does not name the exact", payload["WrongChecksum"]["Message"])
                    self.assertFalse(payload["LooseTag"]["Accepted"])
                    self.assertIn("not an exact Nexus Harness version", payload["LooseTag"]["Message"])

    @unittest.skipUnless(os.name == "nt", "Windows install metadata is Windows-specific")
    def test_powershell_installed_application_uses_exact_redirected_registry_location(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        functions = _installer_header_function_source()
        harness = r'''
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$installLocation = Join-Path ([IO.Path]::GetFullPath($args[0])) "Policy-redirected User Programs\Nexus Harness"
New-Item -ItemType Directory -Force -Path $installLocation | Out-Null
$application = Join-Path $installLocation 'Nexus Harness.exe'
$uninstaller = Join-Path $installLocation 'Uninstall Nexus Harness.exe'
[IO.File]::WriteAllBytes($application, [byte[]](77, 90))
[IO.File]::WriteAllBytes($uninstaller, [byte[]](77, 90))
$installMetadata = [pscustomobject]@{
    InstallLocation = $installLocation
    KeepShortcuts = 'true'
    ShortcutName = 'Nexus Harness'
}
$uninstallMetadata = [pscustomobject]@{
    DisplayName = 'Nexus Harness'
    DisplayVersion = '0.2.1'
    Publisher = 'Nexus Harness'
    UninstallString = '"{0}" /currentuser' -f $uninstaller
    QuietUninstallString = '"{0}" /currentuser /S' -f $uninstaller
}
$accepted = Assert-NexusInstalledApplicationMetadata '0.2.1' $installMetadata $uninstallMetadata
$wrongVersionRejected = $false
try {
    $wrongVersion = $uninstallMetadata.PSObject.Copy()
    $wrongVersion.DisplayVersion = '0.2.0'
    [void](Assert-NexusInstalledApplicationMetadata '0.2.1' $installMetadata $wrongVersion)
} catch { $wrongVersionRejected = $_.Exception.Message -like '*product and version*' }
$allUsersRejected = $false
try {
    $allUsers = $uninstallMetadata.PSObject.Copy()
    $allUsers.UninstallString = '"{0}" /allusers' -f $uninstaller
    [void](Assert-NexusInstalledApplicationMetadata '0.2.1' $installMetadata $allUsers)
} catch { $allUsersRejected = $_.Exception.Message -like '*current-user mode*' }
[pscustomobject]@{
    Accepted = $accepted
    Expected = $application
    WrongVersionRejected = $wrongVersionRejected
    AllUsersRejected = $allUsersRejected
} | ConvertTo-Json -Compress
'''.replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "installed-metadata.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            for host in hosts:
                with self.subTest(host=Path(host).name):
                    result = subprocess.run(
                        [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), folder],
                        text=True, capture_output=True, timeout=30, errors="replace",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout.strip().splitlines()[-1])
                    self.assertEqual(Path(payload["Accepted"]), Path(payload["Expected"]))
                    self.assertIn("Policy-redirected User Programs", payload["Accepted"])
                    self.assertTrue(payload["WrongVersionRejected"])
                    self.assertTrue(payload["AllUsersRejected"])

    @unittest.skipUnless(os.name == "nt", "Windows shortcut verification is Windows-specific")
    def test_powershell_shortcut_verifier_handles_redirected_desktops_idempotently(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        functions = _installer_header_function_source()
        harness = r'''
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$root = [IO.Path]::GetFullPath($args[0])
$installedFolder = Join-Path $root 'installed application'
$desktop = Join-Path $root "Karo's redirected OneDrive desktop"
$commonDesktop = Join-Path $root 'redirected Public Desktop'
New-Item -ItemType Directory -Force -Path $installedFolder, $desktop, $commonDesktop | Out-Null
$installed = Join-Path $installedFolder 'Nexus Harness.exe'
[IO.File]::WriteAllBytes($installed, [byte[]](77, 90))
$shortcutPath = Join-Path $desktop 'Nexus Harness.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $installed
$shortcut.IconLocation = "$installed,0"
$shortcut.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$before = [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath))
$first = Assert-NexusDesktopShortcut $installed $desktop $commonDesktop
$second = Assert-NexusDesktopShortcut $installed $desktop $commonDesktop
$driveRoot = Get-NexusCanonicalPath ([IO.Path]::GetPathRoot($root)) 'The drive root'
$after = [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath))
[pscustomobject]@{
    ShortcutPath = $second.ShortcutPath
    TargetPath = $second.TargetPath
    IconPath = $second.IconPath
    Arguments = $second.Arguments
    LinkCount = @(
        Get-ChildItem -LiteralPath $desktop, $commonDesktop -Filter 'Nexus Harness*.lnk' -File -Force
    ).Count
    Unchanged = $before -ceq $after
    SameResult = $first.ShortcutPath -ceq $second.ShortcutPath
    DriveRoot = $driveRoot
} | ConvertTo-Json -Compress
'''.replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "shortcut-positive.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            for host in hosts:
                with self.subTest(host=Path(host).name):
                    result = subprocess.run(
                        [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), folder],
                        text=True, capture_output=True, timeout=30, errors="replace",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout.strip().splitlines()[-1])
                    self.assertEqual(Path(payload["ShortcutPath"]).parent,
                                     Path(folder) / "Karo's redirected OneDrive desktop")
                    self.assertEqual(Path(payload["TargetPath"]).name, "Nexus Harness.exe")
                    self.assertEqual(Path(payload["IconPath"]).name, "Nexus Harness.exe")
                    self.assertEqual(payload["Arguments"], "")
                    self.assertEqual(payload["LinkCount"], 1)
                    self.assertTrue(payload["Unchanged"])
                    self.assertTrue(payload["SameResult"])
                    self.assertTrue(
                        payload["DriveRoot"].endswith(("\\", "/")),
                        payload["DriveRoot"],
                    )

    @unittest.skipUnless(os.name == "nt", "Windows shortcut verification is Windows-specific")
    def test_powershell_shortcut_verifier_rejects_missing_or_unowned_links(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        functions = _installer_header_function_source()
        harness = r'''
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$root = [IO.Path]::GetFullPath($args[0])
$case = [string]$args[1]
$installedFolder = Join-Path $root 'installed application'
$desktop = Join-Path $root 'redirected desktop with spaces'
$commonDesktop = Join-Path $root 'public desktop with spaces'
New-Item -ItemType Directory -Force -Path $installedFolder, $desktop, $commonDesktop | Out-Null
$installed = Join-Path $installedFolder 'Nexus Harness.exe'
$outside = Join-Path $root 'outside.exe'
$missingIcon = Join-Path $installedFolder 'missing.ico'
[IO.File]::WriteAllBytes($installed, [byte[]](77, 90))
[IO.File]::WriteAllBytes($outside, [byte[]](77, 90))
if ($case -notin @('missing', 'public-only')) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $desktop 'Nexus Harness.lnk'))
    $shortcut.TargetPath = if ($case -eq 'wrong-target') { $outside } else { $installed }
    if ($case -eq 'wrong-arguments') { $shortcut.Arguments = '--project "C:\wrong project"' }
    $shortcut.IconLocation = if ($case -eq 'outside-icon') {
        "$outside,0"
    } elseif ($case -eq 'missing-icon') {
        "$missingIcon,0"
    } elseif ($case -eq 'wrong-index') {
        "$installed,1"
    } else {
        "$installed,0"
    }
    $shortcut.Save()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}
if ($case -in @(
    'duplicate', 'hidden-duplicate', 'public-duplicate',
    'hidden-public-duplicate', 'public-only'
)) {
    $shell = New-Object -ComObject WScript.Shell
    $duplicateFolder = if ($case -in @(
        'public-duplicate', 'hidden-public-duplicate', 'public-only'
    )) { $commonDesktop } else { $desktop }
    $duplicateName = if ($case -eq 'public-only') { 'Nexus Harness.lnk' } else { 'Nexus Harness old.lnk' }
    $duplicatePath = Join-Path $duplicateFolder $duplicateName
    $duplicate = $shell.CreateShortcut($duplicatePath)
    $duplicate.TargetPath = $installed
    $duplicate.IconLocation = "$installed,0"
    $duplicate.Save()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($duplicate)
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    if ($case -in @('hidden-duplicate', 'hidden-public-duplicate')) {
        [IO.File]::SetAttributes($duplicatePath, [IO.FileAttributes]::Hidden)
    }
}
$result = $null
try {
    [void](Assert-NexusDesktopShortcut $installed $desktop $commonDesktop)
    $result = [pscustomobject]@{ Rejected = $false; Message = '' }
} catch {
    $result = [pscustomobject]@{ Rejected = $true; Message = [string]$_.Exception.Message }
}
$result | ConvertTo-Json -Compress
'''.replace("__INSTALLER_FUNCTIONS__", functions)
        expected = {
            "missing": "did not create exactly one visible desktop shortcut",
            "wrong-target": "instead of the installed application",
            "outside-icon": "does not use the installed application",
            "missing-icon": "desktop shortcut icon source does not exist",
            "wrong-index": "does not use the installed application",
            "wrong-arguments": "contains unexpected launch arguments",
            "duplicate": "did not create exactly one visible desktop shortcut",
            "hidden-duplicate": "did not create exactly one visible desktop shortcut",
            "public-duplicate": "did not create exactly one visible desktop shortcut",
            "hidden-public-duplicate": "did not create exactly one visible desktop shortcut",
            "public-only": "did not create exactly one visible desktop shortcut",
        }
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "shortcut-negative.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            for host in hosts:
                for case, phrase in expected.items():
                    case_root = Path(folder) / Path(host).stem / case
                    case_root.mkdir(parents=True)
                    with self.subTest(host=Path(host).name, case=case):
                        result = subprocess.run(
                            [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(script), str(case_root), case],
                            text=True, capture_output=True, timeout=30, errors="replace",
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        payload = json.loads(result.stdout.strip().splitlines()[-1])
                        self.assertTrue(payload["Rejected"])
                        self.assertIn(phrase, payload["Message"])

    @unittest.skipUnless(os.name == "nt", "Windows drive-root verification is Windows-specific")
    def test_powershell_shortcut_verifier_accepts_a_real_drive_root_desktop(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        subst = shutil.which("subst.exe")
        if not subst:
            self.skipTest("Windows subst.exe is unavailable")
        drive = next(
            (
                f"{letter}:" for letter in reversed("PQRSTUVWXYZ")
                if not Path(f"{letter}:\\").exists()
            ),
            "",
        )
        if not drive:
            self.skipTest("No unused drive letter is available for the root-path contract")
        functions = _installer_header_function_source()
        harness = r'''
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$desktop = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath([string]$args[0]))
$installedFolder = Join-Path $desktop 'installed application'
New-Item -ItemType Directory -Force -Path $installedFolder | Out-Null
$installed = Join-Path $installedFolder 'Nexus Harness.exe'
[IO.File]::WriteAllBytes($installed, [byte[]](77, 90))
$shortcutPath = Join-Path $desktop 'Nexus Harness.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $installed
$shortcut.IconLocation = "$installed,0"
$shortcut.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$verified = Assert-NexusDesktopShortcut $installed $desktop $desktop
[pscustomobject]@{
    Desktop = $desktop
    ShortcutPath = $verified.ShortcutPath
    TargetPath = $verified.TargetPath
    IconPath = $verified.IconPath
    IconIndex = $verified.IconIndex
} | ConvertTo-Json -Compress
'''.replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            mapped = subprocess.run(
                [subst, drive, folder], text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(mapped.returncode, 0, mapped.stderr)
            try:
                script = Path(folder) / "shortcut-drive-root.ps1"
                script.write_text(harness, encoding="utf-8-sig")
                for host in hosts:
                    with self.subTest(host=Path(host).name, drive=drive):
                        result = subprocess.run(
                            [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(script), f"{drive}\\"],
                            text=True, capture_output=True, timeout=30, errors="replace",
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        payload = json.loads(result.stdout.strip().splitlines()[-1])
                        self.assertEqual(payload["Desktop"].casefold(), f"{drive}\\".casefold())
                        self.assertEqual(Path(payload["ShortcutPath"]).parent, Path(f"{drive}\\"))
                        self.assertEqual(payload["TargetPath"], payload["IconPath"])
                        self.assertEqual(payload["IconIndex"], 0)
            finally:
                unmapped = subprocess.run(
                    [subst, drive, "/D"], text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(unmapped.returncode, 0, unmapped.stderr)

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
        nsis_include = (ROOT / "desktop" / "installer.nsh").read_text(encoding="utf-8")
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
        self.assertIn("release-metadata.json", workflow)
        self.assertIn("Measured download size", workflow)
        self.assertIn("UNSIGNED-DEV", workflow)
        self.assertIn("Finish setup before starting", panel)
        self.assertIn("Nexus will not begin work it cannot verify", panel)
        self.assertIn("quickBootstrap", panel)
        self.assertIn("NEXUS BOOTSTRAP MODE", (ROOT / "src" / "our_harness" / "server.py").read_text(encoding="utf-8"))
        self.assertIn("requestSingleInstanceLock", desktop)
        self.assertIn("harness:diagnostics", desktop)
        self.assertIn("NEXUS_BUILD_COMMIT", desktop)
        self.assertEqual(package["build"]["win"]["target"], "nsis")
        self.assertEqual(package["build"]["nsis"]["createDesktopShortcut"], "always")
        self.assertEqual(package["build"]["nsis"]["uninstallDisplayName"],
                         "Nexus Harness")
        self.assertEqual(package["build"]["nsis"]["guid"],
                         "e52322ab-f15e-5dc0-963b-7588e3739e89")
        self.assertEqual(package["build"]["nsis"]["include"], "installer.nsh")
        self.assertFalse(package["build"]["nsis"]["allowElevation"])
        self.assertIn("customInstallMode", nsis_include)
        self.assertIn("$isForceCurrentInstall \"1\"", nsis_include)
        self.assertIn("${isForAllUsers}", nsis_include)
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
        self.assertIn("Assert-NexusDesktopShortcut", powershell_installer)
        self.assertIn("Get-NexusStableReleaseAssets", powershell_installer)
        self.assertIn("DesktopDirectory", powershell_installer)
        self.assertIn("CommonDesktopDirectory", powershell_installer)
        self.assertIn("RegistryView]::Registry64", powershell_installer)
        self.assertIn("e52322ab-f15e-5dc0-963b-7588e3739e89", powershell_installer)
        self.assertIn("-ArgumentList '/currentuser'", powershell_installer)
        self.assertIn("Desktop shortcut verified", powershell_installer)
        self.assertIn("$shortcut.Arguments", powershell_installer)
        self.assertIn("-File -Force", powershell_installer)
        self.assertIn("Remove-Item -LiteralPath $shortcut -Force", workflow)
        self.assertIn("CommonDesktopDirectory", workflow)
        self.assertIn("RegistryView]::Registry64", workflow)
        self.assertIn("@('/S', '/currentuser')", workflow)
        self.assertIn("The recreated desktop shortcut did not launch", workflow)
        self.assertIn("A prior package smoke left the installed app running", workflow)
        self.assertIn("Stop-InstalledProcessTrees", workflow)
        self.assertIn("did not stop", workflow)
        self.assertIn("Prove the stable release is anonymously installable", workflow)
        self.assertIn("releases/latest", workflow)
        self.assertIn("cmp --silent", workflow)
        self.assertIn("sha256sum", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s*--head(?:\s|\\)$")
        self.assertIn("npm run smoke:long-horizon", workflow)
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
