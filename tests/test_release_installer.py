from __future__ import annotations

import importlib.util
import inspect
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
    start = source.index("function Get-GitHubHeaders")
    end = source.index("\nfunction Invoke-NexusInstallerAndVerify", start)
    return source[start:end]


OFFLINE_CONTRACT_FINGERPRINT = (
    "d85e8a719bc8d49df4fbac3b617736b12aa10b7ff1418d5b6462e26e4d6f55cb"
)


def _windows_powershell() -> str:
    hosts = _powershell_hosts()
    preferred = next(
        (host for host in hosts if Path(host).name.casefold() == "powershell.exe"),
        hosts[0] if hosts else "",
    )
    if not preferred:
        raise unittest.SkipTest("Windows PowerShell is required for the installer contract")
    return preferred


def _compile_unsigned_windows_executable(
    output: Path, *, version: str = "9.8.7", nexus_metadata: bool = True,
    exit_code: int = 0, marker_path: Path | None = None,
) -> None:
    framework = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET"
    candidates = (
        framework / "Framework64" / "v4.0.30319" / "csc.exe",
        framework / "Framework" / "v4.0.30319" / "csc.exe",
    )
    compiler = next((candidate for candidate in candidates if candidate.is_file()), None)
    if compiler is None:
        raise unittest.SkipTest("The Windows .NET Framework C# compiler is unavailable")
    attributes = ""
    if nexus_metadata:
        attributes = f'''\
[assembly: AssemblyTitle("Desktop window for the Nexus Harness control panel")]
[assembly: AssemblyCompany("Nexus Harness")]
[assembly: AssemblyProduct("Nexus Harness")]
[assembly: AssemblyFileVersion("{version}.0")]
[assembly: AssemblyInformationalVersion("{version}")]
'''
    source = output.with_suffix(".cs")
    marker_statement = ""
    if marker_path is not None:
        marker_statement = (
            'System.IO.File.WriteAllText(@"'
            + str(marker_path).replace('"', '""')
            + '", "executed"); '
        )
    source.write_text(
        "using System.Reflection;\n"
        + attributes
        + "public static class Program { public static int Main(string[] args) { "
        + marker_statement
        + f"return {exit_code}; }} }}\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(compiler), "/nologo", "/target:winexe", f"/out:{output}", str(source)],
        text=True,
        capture_output=True,
        timeout=30,
        errors="replace",
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)


def _offline_manifest(installer_path: Path, *, version: str = "9.8.7") -> dict[str, object]:
    import hashlib

    digest = hashlib.sha256(installer_path.read_bytes()).hexdigest()
    return {
        "schema_version": 2,
        "contract": "nexus-harness.windows-offline-bundle",
        "contract_fingerprint": OFFLINE_CONTRACT_FINGERPRINT,
        "product": "Nexus Harness",
        "version": version,
        "installer": installer_path.name,
        "checksum": installer_path.name + ".sha256",
        "installer_bytes": installer_path.stat().st_size,
        "installer_sha256": digest,
        "signature_mode": "unsigned" if installer_path.stem.endswith("-UNSIGNED") else "signed",
        "publisher": "",
        "signer_certificate_sha256": "",
    }


def _write_offline_manifest_and_checksum(root: Path, installer: Path, *, version: str) -> str:
    import hashlib

    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    (root / (installer.name + ".sha256")).write_text(
        f"{digest}  {installer.name}", encoding="ascii"
    )
    (root / "Nexus-Harness-Offline-Bundle.json").write_text(
        json.dumps(_offline_manifest(installer, version=version), indent=2),
        encoding="utf-8",
    )
    return digest


def _copy_product_bound_bootstrap(destination: Path, *, version: str, digest: str) -> Path:
    scripts = destination / "scripts"
    publisher = destination / "release"
    scripts.mkdir(parents=True, exist_ok=True)
    publisher.mkdir(parents=True, exist_ok=True)
    source = (ROOT / "scripts" / "install_nexus_harness.ps1").read_text(encoding="utf-8")
    if source.count("__NEXUS_OFFLINE_BUNDLE_VERSION__") != 1:
        raise AssertionError("offline version binding placeholder drifted")
    if source.count("__NEXUS_OFFLINE_INSTALLER_SHA256__") != 1:
        raise AssertionError("offline digest binding placeholder drifted")
    source = source.replace("__NEXUS_OFFLINE_BUNDLE_VERSION__", version)
    source = source.replace("__NEXUS_OFFLINE_INSTALLER_SHA256__", digest)
    target = scripts / "install_nexus_harness.ps1"
    target.write_text(source, encoding="utf-8-sig")
    shutil.copy2(ROOT / "release" / "windows-authenticode-publisher.txt", publisher)
    shutil.copy2(
        ROOT / "release" / "windows-authenticode-certificate-sha256.txt", publisher
    )
    return target


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

    def test_legacy_python_installer_never_executes_path_credential_helpers(self):
        clean_environment = {
            key: value for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN"}
        }
        with (
            mock.patch.dict(os.environ, clean_environment, clear=True),
            mock.patch.object(
                installer.subprocess, "run",
                side_effect=AssertionError("PATH credential helper executed"),
            ),
        ):
            self.assertEqual(installer._github_token(), "")
        with mock.patch.dict(
            os.environ,
            {"GH_TOKEN": " explicit-gh ", "GITHUB_TOKEN": "fallback"},
            clear=True,
        ):
            self.assertEqual(installer._github_token(), "explicit-gh")

    def test_legacy_python_installer_is_a_fail_closed_compatibility_shim(self):
        with (
            mock.patch.object(
                installer, "_release", side_effect=AssertionError("network reached")
            ),
            mock.patch.object(
                installer.subprocess, "run", side_effect=AssertionError("process started")
            ),
        ):
            with self.assertRaisesRegex(installer.InstallError, "legacy Python installer is disabled"):
                installer.install()
        install_source = inspect.getsource(installer.install)
        self.assertNotIn("_download", install_source)
        self.assertNotIn("subprocess", install_source)
        self.assertNotIn("TemporaryDirectory", install_source)

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

    @unittest.skipUnless(os.name == "nt", "PowerShell credential isolation is Windows-specific")
    def test_powershell_credentials_ignore_path_decoys_and_use_only_explicit_tokens(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        functions = _installer_header_function_source()
        harness = """\
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$anonymous = Get-GitHubHeaders
$env:GITHUB_TOKEN = 'explicit-github-token'
$github = Get-GitHubHeaders
$env:GH_TOKEN = 'explicit-gh-token'
$gh = Get-GitHubHeaders
[pscustomobject]@{
    Anonymous = $anonymous.ContainsKey('Authorization')
    GithubToken = [string]$github.Authorization -ceq 'Bearer explicit-github-token'
    GhTokenPrecedence = [string]$gh.Authorization -ceq 'Bearer explicit-gh-token'
    ResolvedGh = [bool](Get-Command gh.exe -ErrorAction SilentlyContinue)
    ResolvedGit = [bool](Get-Command git.exe -ErrorAction SilentlyContinue)
    GcmInteractive = [string]$env:GCM_INTERACTIVE
} | ConvertTo-Json -Compress
""".replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            folder_path = Path(folder)
            marker = folder_path / "PATH-DECOY-EXECUTED.txt"
            decoy = folder_path / "credential-decoy.exe"
            _compile_unsigned_windows_executable(
                decoy, nexus_metadata=False, marker_path=marker
            )
            shutil.copy2(decoy, folder_path / "gh.exe")
            shutil.copy2(decoy, folder_path / "git.exe")
            harness_path = folder_path / "probe.ps1"
            harness_path.write_text(harness, encoding="utf-8")
            environment = os.environ.copy()
            environment["PATH"] = folder
            environment["GCM_INTERACTIVE"] = "leave-parent-state-alone"
            environment.pop("GH_TOKEN", None)
            environment.pop("GITHUB_TOKEN", None)
            for host in hosts:
                with self.subTest(host=Path(host).name):
                    result = subprocess.run(
                        [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(harness_path)],
                        text=True, capture_output=True, timeout=30, cwd=folder,
                        errors="replace", env=environment,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout.strip().splitlines()[-1])
                    self.assertFalse(payload["Anonymous"])
                    self.assertTrue(payload["GithubToken"])
                    self.assertTrue(payload["GhTokenPrecedence"])
                    self.assertTrue(payload["ResolvedGh"])
                    self.assertTrue(payload["ResolvedGit"])
                    self.assertEqual(payload["GcmInteractive"], "leave-parent-state-alone")
                    self.assertFalse(marker.exists(), "a PATH-resolved credential command ran")

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

    @unittest.skipUnless(os.name == "nt", "The distributed bootstrap is Windows-specific")
    def test_top_level_cmd_passes_its_unicode_offline_folder_from_an_unrelated_cwd(self):
        powershell = _windows_powershell()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            bundle = base / "OneDrive - Fabrikam AB" / "Åsa & O'Brien (QA)"
            unrelated = base / "completely unrelated working directory"
            (bundle / "scripts").mkdir(parents=True)
            unrelated.mkdir()
            shutil.copy2(ROOT / "Install Nexus Harness.cmd", bundle)
            wrapper = r'''[CmdletBinding()]
param([string] $BundleRoot)
$ErrorActionPreference = 'Stop'
$expected = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$actual = [IO.Path]::GetFullPath($BundleRoot)
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($actual.TrimEnd('\'), $expected.TrimEnd('\'))) {
    throw "The cmd passed '$actual' instead of '$expected'."
}
[ordered]@{
    BundleRoot = $actual
    WorkingDirectory = [IO.Path]::GetFullPath((Get-Location).Path)
} | ConvertTo-Json -Compress | Set-Content -Encoding utf8 -LiteralPath (Join-Path $expected 'cmd-observed.json')
'''
            (bundle / "scripts" / "install_nexus_harness.ps1").write_text(
                wrapper, encoding="utf-8-sig"
            )
            # A bare `powershell.exe` resolves through the caller's working
            # directory before the Windows host. This invalid decoy makes the
            # test fail unless the distributed CMD pins the built-in host.
            (unrelated / "powershell.exe").write_bytes(b"not a Windows executable")
            environment = os.environ.copy()
            environment["NEXUS_INSTALLER_NO_PAUSE"] = "1"
            result = subprocess.run(
                f'"{bundle / "Install Nexus Harness.cmd"}"',
                shell=True,
                cwd=unrelated,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed = json.loads(
                (bundle / "cmd-observed.json").read_text(encoding="utf-8-sig")
            )
            self.assertEqual(Path(observed["BundleRoot"]), bundle)
            self.assertEqual(Path(observed["WorkingDirectory"]), unrelated)
            self.assertIn(str(Path(powershell).name).casefold(), "powershell.exe")

    @unittest.skipUnless(os.name == "nt", "Bundled CMD locking is Windows-specific")
    def test_bundled_cmd_holds_verified_bootstrap_locked_through_execution(self):
        import hashlib

        _windows_powershell()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            bundle = base / "shared OneDrive Åsa & O'Brien"
            scripts = bundle / "scripts"
            unrelated = base / "unrelated cwd"
            scripts.mkdir(parents=True)
            unrelated.mkdir()
            trusted_marker = base / "trusted-bootstrap-ran.txt"
            malicious_marker = base / "malicious-bootstrap-ran.txt"
            lock_result = base / "verified-handle-lock.json"
            backup = base / "unexpected-bootstrap-backup.ps1"
            trusted_source = (
                "[CmdletBinding()]\r\n"
                "param([string] $BundleRoot, [switch] $OfflineOnly, [string] $BootstrapResourceRoot)\r\n"
                "$replaceDenied=$false; $writeDenied=$false\r\n"
                "try{[IO.File]::Replace($env:NEXUS_MALICIOUS_SOURCE,$env:NEXUS_BOOTSTRAP_PATH,$env:NEXUS_BACKUP_PATH)}catch{if($_.Exception -is [IO.IOException] -or $_.Exception.InnerException -is [IO.IOException]){$replaceDenied=$true}else{throw}}\r\n"
                "try{[IO.File]::WriteAllText($env:NEXUS_BOOTSTRAP_PATH,'mutated')}catch{if($_.Exception -is [IO.IOException] -or $_.Exception.InnerException -is [IO.IOException]){$writeDenied=$true}else{throw}}\r\n"
                "$inMemory=[string]::IsNullOrEmpty($PSCommandPath)\r\n"
                "[IO.File]::WriteAllText($env:NEXUS_LOCK_RESULT,('{\"ReplaceDenied\":'+$replaceDenied.ToString().ToLowerInvariant()+',\"WriteDenied\":'+$writeDenied.ToString().ToLowerInvariant()+',\"InMemory\":'+$inMemory.ToString().ToLowerInvariant()+'}'))\r\n"
                "[IO.File]::WriteAllText($env:NEXUS_TRUSTED_MARKER, 'trusted')\r\n"
            )
            bootstrap = scripts / "install_nexus_harness.ps1"
            bootstrap.write_text(trusted_source, encoding="utf-8-sig", newline="")
            digest = hashlib.sha256(bootstrap.read_bytes()).hexdigest()
            cmd_source = (ROOT / "Install Nexus Harness.cmd").read_text(encoding="utf-8")
            self.assertEqual(cmd_source.count("__NEXUS_OFFLINE_MODE__"), 1)
            self.assertEqual(cmd_source.count("__NEXUS_OFFLINE_BOOTSTRAP_SHA256__"), 1)
            cmd_source = cmd_source.replace("__NEXUS_OFFLINE_MODE__", "1")
            cmd_source = cmd_source.replace("__NEXUS_OFFLINE_BOOTSTRAP_SHA256__", digest)
            command = bundle / "Install Nexus Harness.cmd"
            command.write_text(cmd_source, encoding="ascii", newline="")
            malicious = scripts / "malicious.ps1"
            malicious.write_text(
                "[CmdletBinding()] param([string] $BundleRoot, [switch] $OfflineOnly)\r\n"
                "[IO.File]::WriteAllText($env:NEXUS_MALICIOUS_MARKER, 'malicious')\r\n",
                encoding="utf-8-sig",
                newline="",
            )
            environment = os.environ.copy()
            environment["NEXUS_INSTALLER_NO_PAUSE"] = "1"
            environment["NEXUS_TRUSTED_MARKER"] = str(trusted_marker)
            environment["NEXUS_MALICIOUS_MARKER"] = str(malicious_marker)
            environment["NEXUS_LOCK_RESULT"] = str(lock_result)
            environment["NEXUS_MALICIOUS_SOURCE"] = str(malicious)
            environment["NEXUS_BACKUP_PATH"] = str(backup)
            result = subprocess.run(
                [
                    str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"),
                    "/d", "/c", "call", str(command),
                ],
                cwd=unrelated,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(trusted_marker.is_file(), result.stdout + result.stderr)
            self.assertFalse(malicious_marker.exists(), result.stdout + result.stderr)
            self.assertEqual(
                json.loads(lock_result.read_text(encoding="utf-8")),
                {"ReplaceDenied": True, "WriteDenied": True, "InMemory": True},
            )
            self.assertFalse(backup.exists())

    @unittest.skipUnless(os.name == "nt", "Offline package installation is Windows-specific")
    def test_product_bound_offline_bundle_installs_repairs_and_follows_desktop_redirection(self):
        powershell = _windows_powershell()
        version = "9.8.7"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            bundle = base / "OneDrive - Fabrikam AB" / "Åsa & O'Brien (QA)"
            bundle.mkdir(parents=True)
            installer_path = bundle / f"Nexus-Harness-Setup-{version}-UNSIGNED.exe"
            _compile_unsigned_windows_executable(installer_path, version=version)
            digest = _write_offline_manifest_and_checksum(
                bundle, installer_path, version=version
            )
            bootstrap = _copy_product_bound_bootstrap(
                bundle, version=version, digest=digest
            )
            installed_folder = base / "Policy-redirected User Programs" / "Nexus Harness"
            installed_folder.mkdir(parents=True)
            installed = installed_folder / "Nexus Harness.exe"
            shutil.copy2(installer_path, installed)
            wrong_installed = base / "wrong-version-installed.exe"
            _compile_unsigned_windows_executable(
                wrong_installed, version="9.8.6", nexus_metadata=True
            )
            desktop_one = base / "OneDrive - Fabrikam AB" / "Desktop"
            common_one = base / "Public Desktop before policy change"
            desktop_two = base / "OneDrive - Fabrikam AB" / "Ny Desktop Åsa"
            common_two = base / "Public Desktop after policy change"
            for item in (desktop_one, common_one, desktop_two, common_two):
                item.mkdir(parents=True)
            unrelated = base / "completely unrelated working directory"
            unrelated.mkdir()
            harness = r'''param(
    [string] $Bootstrap, [string] $Bundle, [string] $Installed,
    [string] $DesktopOne, [string] $CommonOne,
    [string] $DesktopTwo, [string] $CommonTwo, [string] $ExpectedVersion,
    [string] $WrongInstalled, [string] $ResultPath
)
$ErrorActionPreference = 'Stop'
. $Bootstrap -BundleRoot $Bundle -LoadFunctionsOnly
$script:networkTouched = $false
$script:installCount = 0
$script:executedPaths = @()
$script:activeDesktop = $DesktopOne
$script:activeCommon = $CommonOne
function Get-NexusDesktopFolders {
    param([string] $DesktopFolder = '', [string] $CommonDesktopFolder = '')
    return @($script:activeDesktop, $script:activeCommon)
}
function Download-ReleaseAsset {
    $script:networkTouched = $true
    throw 'NETWORK MUST NOT BE TOUCHED'
}
$installInvoker = {
    param([string] $InstallerPath, [string] $Version)
    if ($Version -cne $ExpectedVersion) { throw "Unexpected version $Version" }
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) { throw 'Private installer copy is missing' }
    if ([IO.Path]::GetFileName((Split-Path -Parent $InstallerPath)) -cnotmatch '\Anexus-harness-execute-[0-9a-f]{32}\z') {
        throw "Installer did not execute from a private current-user copy: $InstallerPath"
    }
    $privateDirectory = Get-Item -LiteralPath (Split-Path -Parent $InstallerPath)
    if (($privateDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Private execution directory is a reparse point'
    }
    $privateAcl = $privateDirectory.GetAccessControl()
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if (-not $privateAcl.AreAccessRulesProtected -or
        -not $privateAcl.GetOwner([Security.Principal.SecurityIdentifier]).Equals($currentSid)) {
        throw 'Private execution directory ACL was not preserved'
    }
    $script:executedPaths += $InstallerPath
    $process = Start-Process -FilePath $InstallerPath -ArgumentList '/currentuser' -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Fixture installer stopped with $($process.ExitCode)" }
    $script:installCount++
}
$resolver = {
    param([string] $Version)
    if ($Version -cne $ExpectedVersion) { throw "Unexpected installed version $Version" }
    return $Installed
}
$shell = New-Object -ComObject WScript.Shell
$stale = $shell.CreateShortcut((Join-Path $script:activeDesktop 'Nexus Harness.lnk'))
$stale.TargetPath = $WrongInstalled
$stale.Arguments = '--preserved-by-fixture-nsis'
$stale.WorkingDirectory = Split-Path -Parent $WrongInstalled
$stale.IconLocation = "$WrongInstalled,9"
$stale.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($stale)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$first = Invoke-NexusHarnessInstallation -RequestedBundleRoot $Bundle `
    -InstallInvoker $installInvoker -InstalledApplicationResolver $resolver
$firstShortcut = $first.Shortcut.ShortcutPath
Remove-Item -LiteralPath $firstShortcut -Force
$second = Invoke-NexusHarnessInstallation -RequestedBundleRoot $Bundle `
    -InstallInvoker $installInvoker -InstalledApplicationResolver $resolver
$repairedShortcut = $second.Shortcut.ShortcutPath
$script:activeDesktop = $DesktopTwo
$script:activeCommon = $CommonTwo
$third = Invoke-NexusHarnessInstallation -RequestedBundleRoot $Bundle `
    -InstallInvoker $installInvoker -InstalledApplicationResolver $resolver
Copy-Item -LiteralPath $WrongInstalled -Destination $Installed -Force
try {
    [void](Invoke-NexusHarnessInstallation -RequestedBundleRoot $Bundle `
        -InstallInvoker $installInvoker -InstalledApplicationResolver $resolver)
    $installedMismatch = ''
} catch { $installedMismatch = [string]$_.Exception.Message }
[pscustomobject]@{
    Source = $third.Source
    Version = $third.Version
    InstallCount = $script:installCount
    NetworkTouched = $script:networkTouched
    FirstShortcut = $firstShortcut
    RepairedShortcut = $repairedShortcut
    RedirectedShortcut = $third.Shortcut.ShortcutPath
    RedirectedTarget = $third.Shortcut.TargetPath
    RedirectedIcon = $third.Shortcut.IconPath
    ExecutedPaths = @($script:executedPaths)
    PrivateCopiesCleaned = @($script:executedPaths | Where-Object { Test-Path -LiteralPath $_ }).Count -eq 0
    InstalledMismatch = $installedMismatch
} | ConvertTo-Json -Depth 5 -Compress | Set-Content -Encoding utf8 -LiteralPath $ResultPath
'''
            harness_path = base / "exercise-offline-bundle.ps1"
            harness_path.write_text(harness, encoding="utf-8-sig")
            result_path = base / "offline-result.json"
            result = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(harness_path),
                    str(bootstrap), str(bundle), str(installed), str(desktop_one),
                    str(common_one), str(desktop_two), str(common_two), version,
                    str(wrong_installed), str(result_path),
                ],
                cwd=unrelated,
                text=True,
                capture_output=True,
                timeout=60,
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(payload["Source"], "offline bundle")
            self.assertEqual(payload["Version"], version)
            self.assertEqual(payload["InstallCount"], 4)
            self.assertFalse(payload["NetworkTouched"])
            self.assertEqual(Path(payload["FirstShortcut"]).parent, desktop_one)
            self.assertEqual(payload["FirstShortcut"], payload["RepairedShortcut"])
            self.assertEqual(Path(payload["RedirectedShortcut"]).parent, desktop_two)
            self.assertEqual(Path(payload["RedirectedTarget"]), installed)
            self.assertEqual(Path(payload["RedirectedIcon"]), installed)
            self.assertEqual(len(payload["ExecutedPaths"]), 4)
            self.assertTrue(all(Path(item) != installer_path for item in payload["ExecutedPaths"]))
            self.assertTrue(payload["PrivateCopiesCleaned"])
            self.assertIn(
                "installed application Windows product version does not match",
                payload["InstalledMismatch"],
            )

    @unittest.skipUnless(os.name == "nt", "Offline bundle assembly is Windows-specific")
    def test_offline_bundle_builder_creates_exact_product_bound_archive(self):
        import zipfile

        powershell = _windows_powershell()
        version = json.loads(
            (ROOT / "desktop" / "package.json").read_text(encoding="utf-8")
        )["version"]
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            inputs = base / "already built Åsa & O'Brien"
            output = base / "portable output"
            extracted = base / "extracted elsewhere"
            unrelated = base / "unrelated cwd"
            for item in (inputs, output, extracted, unrelated):
                item.mkdir(parents=True)
            # Product packaging must not resolve its validation host through a
            # caller-controlled PATH. The invalid decoy is selected by the old
            # implementation but ignored when the Windows-owned host is pinned.
            (unrelated / "powershell.exe").write_bytes(b"not a Windows executable")
            installer_path = inputs / f"Nexus-Harness-Setup-{version}-UNSIGNED.exe"
            _compile_unsigned_windows_executable(installer_path, version=version)
            _write_offline_manifest_and_checksum(inputs, installer_path, version=version)
            # The builder intentionally consumes only the already-built stable
            # installer and checksum; it creates its own trusted manifest.
            (inputs / "Nexus-Harness-Offline-Bundle.json").unlink()
            environment = os.environ.copy()
            environment["PATH"] = str(unrelated) + os.pathsep + environment.get("PATH", "")
            result = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "scripts" / "build_windows_offline_bundle.ps1"),
                    "-InstallerPath", str(installer_path),
                    "-OutputDirectory", str(output),
                ],
                cwd=unrelated,
                env=environment,
                text=True,
                capture_output=True,
                timeout=60,
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = output / f"Nexus-Harness-Windows-Offline-{version}.zip"
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as package:
                file_members = {
                    info.filename.replace("\\", "/")
                    for info in package.infolist()
                    if not info.is_dir()
                }
                manifest_members = [
                    name for name in file_members
                    if name.endswith("/Nexus-Harness-Offline-Bundle.json")
                ]
                self.assertEqual(len(manifest_members), 1, sorted(file_members))
                prefix = manifest_members[0].removesuffix(
                    "Nexus-Harness-Offline-Bundle.json"
                )
                relative_files = {name.removeprefix(prefix) for name in file_members}
                expected_files = {
                    "Install Nexus Harness.cmd",
                    "Nexus-Harness-Offline-Bundle.json",
                    "README-OFFLINE.txt",
                    installer_path.name,
                    installer_path.name + ".sha256",
                    "release/windows-authenticode-certificate-sha256.txt",
                    "release/windows-authenticode-publisher.txt",
                    "scripts/install_nexus_harness.ps1",
                }
                self.assertEqual(relative_files, expected_files)
                package.extractall(extracted)

            bundle = extracted / f"Nexus-Harness-Windows-Offline-{version}"
            manifest = json.loads(
                (bundle / "Nexus-Harness-Offline-Bundle.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["version"], version)
            self.assertEqual(manifest["contract_fingerprint"], OFFLINE_CONTRACT_FINGERPRINT)
            self.assertEqual(manifest["publisher"], "")
            self.assertEqual(manifest["signer_certificate_sha256"], "")
            bundled_bootstrap = bundle / "scripts" / "install_nexus_harness.ps1"
            bundled_source = bundled_bootstrap.read_text(encoding="utf-8-sig")
            self.assertNotIn("__NEXUS_OFFLINE_BUNDLE_VERSION__", bundled_source)
            self.assertNotIn("__NEXUS_OFFLINE_INSTALLER_SHA256__", bundled_source)
            self.assertIn(f"$offlineBundlePinnedVersion = '{version}'", bundled_source)
            self.assertIn(manifest["installer_sha256"], bundled_source)
            import hashlib

            bootstrap_digest = hashlib.sha256(bundled_bootstrap.read_bytes()).hexdigest()
            bundled_cmd = (bundle / "Install Nexus Harness.cmd").read_text(
                encoding="ascii"
            )
            self.assertNotIn("__NEXUS_OFFLINE_MODE__", bundled_cmd)
            self.assertNotIn("__NEXUS_OFFLINE_BOOTSTRAP_SHA256__", bundled_cmd)
            self.assertIn("NEXUS_BUNDLED_OFFLINE_MODE=1", bundled_cmd)
            self.assertIn(bootstrap_digest, bundled_cmd)
            self.assertIn("-OfflineOnly", bundled_cmd)
            environment = os.environ.copy()
            environment["HTTPS_PROXY"] = "http://127.0.0.1:1"
            validate = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(bundled_bootstrap),
                    "-BundleRoot", str(bundle), "-ValidateOfflineBundleOnly",
                ],
                cwd=unrelated,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                errors="replace",
            )
            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)
            self.assertIn("Offline bundle verified without execution", validate.stdout)

            # The product CMD must reject a replaced PS1 before any of that
            # replacement's code runs. The outer ZIP/CMD still needs trusted
            # transport; this specifically closes sibling-PS1 substitution.
            tamper_marker = base / "tampered-bootstrap-ran.txt"
            bundled_bootstrap.write_text(
                "[IO.File]::WriteAllText($env:NEXUS_TAMPER_MARKER, 'executed')\nexit 0\n",
                encoding="utf-8-sig",
            )
            cmd_environment = environment.copy()
            cmd_environment["NEXUS_INSTALLER_NO_PAUSE"] = "1"
            cmd_environment["NEXUS_TAMPER_MARKER"] = str(tamper_marker)
            tampered = subprocess.run(
                [
                    str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"),
                    "/d", "/c", "call", str(bundle / "Install Nexus Harness.cmd"),
                ],
                cwd=unrelated,
                env=cmd_environment,
                text=True,
                capture_output=True,
                timeout=30,
                errors="replace",
            )
            self.assertNotEqual(tampered.returncode, 0, tampered.stdout + tampered.stderr)
            self.assertIn("failed its product-owned SHA-256 check", tampered.stdout)
            self.assertFalse(tamper_marker.exists(), "tampered PS1 was executed")

            rerun = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "scripts" / "build_windows_offline_bundle.ps1"),
                    "-InstallerPath", str(installer_path),
                    "-OutputDirectory", str(output),
                ],
                cwd=unrelated,
                text=True,
                capture_output=True,
                timeout=30,
                errors="replace",
            )
            self.assertNotEqual(rerun.returncode, 0)
            self.assertIn("refusing to overwrite", rerun.stdout + rerun.stderr)

    @unittest.skipUnless(os.name == "nt", "Offline package trust is Windows-specific")
    def test_self_authored_unsigned_bundle_never_reaches_start_process(self):
        powershell = _windows_powershell()
        version = "9.8.7"
        with tempfile.TemporaryDirectory() as folder:
            import hashlib

            base = Path(folder)
            bundle = base / "self-authored candidate"
            bundle.mkdir()
            arbitrary = bundle / f"Nexus-Harness-Setup-{version}-UNSIGNED.exe"
            _compile_unsigned_windows_executable(
                arbitrary, version=version, nexus_metadata=True, exit_code=7
            )
            _write_offline_manifest_and_checksum(
                bundle, arbitrary, version=version
            )
            trusted = base / "trusted-product-build.exe"
            _compile_unsigned_windows_executable(
                trusted, version=version, nexus_metadata=True, exit_code=0
            )
            trusted_digest = hashlib.sha256(trusted.read_bytes()).hexdigest()
            self.assertNotEqual(
                hashlib.sha256(arbitrary.read_bytes()).hexdigest(), trusted_digest
            )
            product_bound = _copy_product_bound_bootstrap(
                base / "product-bound bootstrap", version=version, digest=trusted_digest
            )
            harness = r'''param([string] $Bootstrap, [string] $Bundle)
$ErrorActionPreference = 'Stop'
. $Bootstrap -LoadFunctionsOnly
$script:networkTouched = $false
$script:installRan = $false
function Download-ReleaseAsset { $script:networkTouched = $true; throw 'NETWORK' }
$install = { param($Path, $Version); $script:installRan = $true; throw 'INSTALL_RAN' }
try {
    [void](Invoke-NexusHarnessInstallation -RequestedBundleRoot $Bundle -InstallInvoker $install `
        -InstalledApplicationResolver { throw 'RESOLVER_RAN' } -ShortcutVerifier { throw 'VERIFIER_RAN' })
    $message = ''
} catch { $message = [string]$_.Exception.Message }
[pscustomobject]@{
    Message = $message
    InstallRan = $script:installRan
    NetworkTouched = $script:networkTouched
} | ConvertTo-Json -Compress
'''
            harness_path = base / "reject-self-authored.ps1"
            harness_path.write_text(harness, encoding="utf-8-sig")
            cases = (
                (ROOT / "scripts" / "install_nexus_harness.ps1", "not product-bound"),
                (product_bound, "does not trust the offline installer digest"),
            )
            for bootstrap, expected in cases:
                with self.subTest(bootstrap=bootstrap.parent.name):
                    result = subprocess.run(
                        [
                            powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-File", str(harness_path),
                            str(bootstrap), str(bundle),
                        ],
                        text=True,
                        capture_output=True,
                        timeout=30,
                        errors="replace",
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    payload = json.loads(result.stdout.strip().splitlines()[-1])
                    self.assertIn(expected, payload["Message"])
                    self.assertFalse(payload["InstallRan"])
                    self.assertFalse(payload["NetworkTouched"])

    @unittest.skipUnless(os.name == "nt", "Offline bundle failures are Windows-specific")
    def test_invalid_offline_material_fails_closed_without_install_or_network(self):
        powershell = _windows_powershell()
        version = "9.8.7"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            template = base / "template"
            template.mkdir()
            installer_path = template / f"Nexus-Harness-Setup-{version}-UNSIGNED.exe"
            _compile_unsigned_windows_executable(installer_path, version=version)
            digest = _write_offline_manifest_and_checksum(
                template, installer_path, version=version
            )
            bootstrap = _copy_product_bound_bootstrap(
                base / "bound-bootstrap", version=version, digest=digest
            )

            expected: dict[str, str] = {}

            def make_case(name: str) -> Path:
                target = base / "cases" / name
                shutil.copytree(template, target)
                return target

            target = make_case("missing-manifest")
            (target / "Nexus-Harness-Offline-Bundle.json").unlink()
            expected[target.name] = "not exactly one case-exact"

            target = make_case("corrupt-manifest")
            (target / "Nexus-Harness-Offline-Bundle.json").write_text("{", encoding="utf-8")
            expected[target.name] = "not valid JSON"

            target = make_case("extra-manifest-like-file")
            shutil.copy2(
                target / "Nexus-Harness-Offline-Bundle.json",
                target / "Nexus-Harness-Offline-Bundle-copy.json",
            )
            expected[target.name] = "not exactly one case-exact"

            target = make_case("ambiguous-installers")
            shutil.copy2(installer_path, target / f"Nexus-Harness-Setup-{version}.exe")
            expected[target.name] = "exactly one stable"

            target = make_case("version-mismatch")
            manifest_path = target / "Nexus-Harness-Offline-Bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = "9.8.8"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            expected[target.name] = "not product-bound"

            target = make_case("missing-checksum")
            (target / (installer_path.name + ".sha256")).unlink()
            expected[target.name] = "exactly one stable"

            target = make_case("checksum-mismatch")
            (target / (installer_path.name + ".sha256")).write_text(
                "0" * 64 + f"  {installer_path.name}", encoding="ascii"
            )
            expected[target.name] = "checksum did not match"

            target = make_case("ambiguous-checksum-records")
            checksum_path = target / (installer_path.name + ".sha256")
            checksum_path.write_text(
                checksum_path.read_text(encoding="ascii")
                + "\n" + digest + f"  {installer_path.name}",
                encoding="ascii",
            )
            expected[target.name] = "exactly one record"

            target = make_case("wrong-size")
            manifest_path = target / "Nexus-Harness-Offline-Bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["installer_bytes"] = int(manifest["installer_bytes"]) + 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            expected[target.name] = "size does not match"

            target = make_case("wrong-contract-fingerprint")
            manifest_path = target / "Nexus-Harness-Offline-Bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["contract_fingerprint"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            expected[target.name] = "contract fingerprint"

            target = make_case("development-installer")
            dev_name = f"Nexus-Harness-Setup-{version}-UNSIGNED-DEV.exe"
            (target / installer_path.name).rename(target / dev_name)
            old_checksum = target / (installer_path.name + ".sha256")
            old_checksum.rename(target / (dev_name + ".sha256"))
            manifest_path = target / "Nexus-Harness-Offline-Bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["installer"] = dev_name
            manifest["checksum"] = dev_name + ".sha256"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            expected[target.name] = "names do not match exactly"

            target = make_case("signature-mode-mismatch")
            signed_name = f"Nexus-Harness-Setup-{version}.exe"
            (target / installer_path.name).rename(target / signed_name)
            old_checksum = target / (installer_path.name + ".sha256")
            old_checksum.unlink()
            (target / (signed_name + ".sha256")).write_text(
                digest + f"  {signed_name}", encoding="ascii"
            )
            manifest_path = target / "Nexus-Harness-Offline-Bundle.json"
            manifest = _offline_manifest(target / signed_name, version=version)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            expected[target.name] = "no complete Authenticode identity pin"

            harness = r'''param([string] $Bootstrap, [string] $CasesRoot)
$ErrorActionPreference = 'Stop'
. $Bootstrap -LoadFunctionsOnly
$results = @()
foreach ($case in @(Get-ChildItem -LiteralPath $CasesRoot -Directory | Sort-Object Name)) {
    $script:networkTouched = $false
    $script:installRan = $false
    function Download-ReleaseAsset { $script:networkTouched = $true; throw 'NETWORK_RAN' }
    $install = { param($Path, $Version); $script:installRan = $true; throw 'INSTALL_RAN' }
    try {
        [void](Invoke-NexusHarnessInstallation -RequestedBundleRoot $case.FullName `
            -InstallInvoker $install -InstalledApplicationResolver { throw 'RESOLVER_RAN' } `
            -ShortcutVerifier { throw 'VERIFIER_RAN' })
        $message = ''
    } catch { $message = [string]$_.Exception.Message }
    $results += [pscustomobject]@{
        Name = $case.Name
        Message = $message
        InstallRan = $script:installRan
        NetworkTouched = $script:networkTouched
    }
}
$results | ConvertTo-Json -Depth 4 -Compress
'''
            harness_path = base / "reject-invalid-bundles.ps1"
            harness_path.write_text(harness, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(harness_path),
                    str(bootstrap), str(base / "cases"),
                ],
                text=True,
                capture_output=True,
                timeout=60,
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            by_name = {item["Name"]: item for item in payload}
            self.assertEqual(set(by_name), set(expected))
            for name, phrase in expected.items():
                with self.subTest(case=name):
                    self.assertIn(phrase, by_name[name]["Message"])
                    self.assertFalse(by_name[name]["InstallRan"])
                    self.assertFalse(by_name[name]["NetworkTouched"])

    @unittest.skipUnless(os.name == "nt", "Offline-only mode is Windows-specific")
    def test_offline_only_mode_never_falls_back_to_credentials_or_network(self):
        powershell = _windows_powershell()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            empty = base / "empty offline folder"
            incomplete = base / "incomplete offline folder"
            empty.mkdir()
            incomplete.mkdir()
            (incomplete / "Nexus-Harness-Setup-9.8.7-UNSIGNED.exe").write_bytes(b"MZ")
            harness = r'''param([string] $Bootstrap, [string] $Empty, [string] $Incomplete)
$ErrorActionPreference = 'Stop'
. $Bootstrap -LoadFunctionsOnly
$script:headersTouched = $false
$script:networkTouched = $false
$script:installRan = $false
function Get-GitHubHeaders { $script:headersTouched = $true; throw 'HEADERS_RAN' }
function Download-ReleaseAsset { $script:networkTouched = $true; throw 'NETWORK_RAN' }
$install = { $script:installRan = $true; throw 'INSTALL_RAN' }
$results = @()
foreach ($candidate in @($Empty, $Incomplete)) {
    try {
        [void](Invoke-NexusHarnessInstallation -RequestedBundleRoot $candidate -OfflineOnly `
            -InstallInvoker $install -InstalledApplicationResolver { throw 'RESOLVER_RAN' } `
            -ShortcutVerifier { throw 'VERIFIER_RAN' })
        $message = ''
    } catch { $message = [string]$_.Exception.Message }
    $results += [pscustomobject]@{ Folder = $candidate; Message = $message }
}
[pscustomobject]@{
    Results = $results
    HeadersTouched = $script:headersTouched
    NetworkTouched = $script:networkTouched
    InstallRan = $script:installRan
} | ConvertTo-Json -Depth 5 -Compress
'''
            script = base / "offline-only.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script),
                    str(ROOT / "scripts" / "install_nexus_harness.ps1"),
                    str(empty), str(incomplete),
                ],
                text=True, capture_output=True, timeout=30, errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertIn("never falls back to the network", payload["Results"][0]["Message"])
            self.assertIn("not exactly one case-exact", payload["Results"][1]["Message"])
            self.assertFalse(payload["HeadersTouched"])
            self.assertFalse(payload["NetworkTouched"])
            self.assertFalse(payload["InstallRan"])

    @unittest.skipUnless(os.name == "nt", "Private execution copy is Windows-specific")
    def test_private_execution_copy_revalidates_after_copy_and_cleans_on_race(self):
        powershell = _windows_powershell()
        version = "9.8.7"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            bundle = base / "verified input"
            bundle.mkdir()
            installer_path = bundle / f"Nexus-Harness-Setup-{version}-UNSIGNED.exe"
            _compile_unsigned_windows_executable(installer_path, version=version)
            digest = _write_offline_manifest_and_checksum(
                bundle, installer_path, version=version
            )
            bootstrap = _copy_product_bound_bootstrap(
                bundle, version=version, digest=digest
            )
            harness = r'''param([string] $Bootstrap, [string] $Bundle)
$ErrorActionPreference = 'Stop'
. $Bootstrap -LoadFunctionsOnly
$before = @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) `
    -Directory -Filter 'nexus-harness-execute-*' -ErrorAction SilentlyContinue | `
    ForEach-Object { $_.FullName })
$candidate = Get-NexusLocalBundleAssets $Bundle
$validated = Assert-NexusInstallerCandidate $candidate
$hook = {
    param([string] $PrivateInstaller, [string] $PrivateChecksum)
    [IO.File]::WriteAllBytes($PrivateInstaller, [byte[]](77, 90, 1, 2, 3))
}
try {
    [void](Copy-NexusInstallerToPrivateExecutionCandidate $candidate $validated $hook)
    $message = ''
} catch { $message = [string]$_.Exception.Message }
$after = @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) `
    -Directory -Filter 'nexus-harness-execute-*' -ErrorAction SilentlyContinue | `
    ForEach-Object { $_.FullName })
$leaks = @($after | Where-Object { $before -cnotcontains $_ })
[pscustomobject]@{
    Message = $message
    Leaks = $leaks
    OriginalStillTrusted = (Get-NexusFileSha256 $candidate.InstallerPath) -ceq $validated.Sha256
} | ConvertTo-Json -Depth 4 -Compress
'''
            script = base / "private-copy-race.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script),
                    str(bootstrap), str(bundle),
                ],
                text=True, capture_output=True, timeout=30, errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertIn("checksum did not match", payload["Message"])
            self.assertEqual(payload["Leaks"], [])
            self.assertTrue(payload["OriginalStillTrusted"])

    @unittest.skipUnless(os.name == "nt", "NTFS Mark-of-the-Web is Windows-specific")
    def test_private_execution_copy_preserves_zone_identifier_when_supported(self):
        powershell = _windows_powershell()
        version = "9.8.7"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            bundle = base / "downloaded bundle"
            bundle.mkdir()
            installer_path = bundle / f"Nexus-Harness-Setup-{version}-UNSIGNED.exe"
            _compile_unsigned_windows_executable(installer_path, version=version)
            zone = "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://github.com/\r\n"
            try:
                with open(str(installer_path) + ":Zone.Identifier", "w", encoding="ascii", newline="") as stream:
                    stream.write(zone)
            except OSError as exc:
                self.skipTest(f"temporary filesystem does not support NTFS streams: {exc}")
            digest = _write_offline_manifest_and_checksum(
                bundle, installer_path, version=version
            )
            bootstrap = _copy_product_bound_bootstrap(
                bundle, version=version, digest=digest
            )
            harness = r'''param([string] $Bootstrap, [string] $Bundle)
$ErrorActionPreference = 'Stop'
. $Bootstrap -LoadFunctionsOnly
$candidate = Get-NexusLocalBundleAssets $Bundle
$validated = Assert-NexusInstallerCandidate $candidate
$copy = Copy-NexusInstallerToPrivateExecutionCandidate $candidate $validated
try {
    $privateInstaller = [string]$copy.Validated.InstallerPath
    [string]$zone = Microsoft.PowerShell.Management\Get-Content -Raw `
        -LiteralPath $privateInstaller -Stream 'Zone.Identifier'
    $privatePath = $privateInstaller
} finally {
    Remove-NexusPrivateExecutionDirectory ([string]$copy.Directory)
}
[pscustomobject]@{
    Zone = $zone
    PrivatePath = $privatePath
    Cleaned = -not (Test-Path -LiteralPath ([string]$copy.Directory))
} | ConvertTo-Json -Compress
'''
            script = base / "zone-copy.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script),
                    str(bootstrap), str(bundle),
                ],
                text=True, capture_output=True, timeout=30, errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["Zone"], zone)
            self.assertNotEqual(Path(payload["PrivatePath"]), installer_path)
            self.assertTrue(payload["Cleaned"])

    @unittest.skipUnless(os.name == "nt", "Authenticode pin loading is Windows-specific")
    def test_authenticode_publisher_and_certificate_pins_are_atomic(self):
        powershell = _windows_powershell()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for name, publisher, certificate in (
                (
                    "publisher-only",
                    "CN=Nexus Harness Release",
                    "UNCONFIGURED - certificate pin",
                ),
                (
                    "certificate-only",
                    "UNCONFIGURED - publisher pin",
                    "0" * 64,
                ),
            ):
                case = base / name
                (case / "scripts").mkdir(parents=True)
                (case / "release").mkdir()
                shutil.copy2(
                    ROOT / "scripts" / "install_nexus_harness.ps1", case / "scripts"
                )
                (case / "release" / "windows-authenticode-publisher.txt").write_text(
                    publisher, encoding="utf-8"
                )
                (
                    case / "release" / "windows-authenticode-certificate-sha256.txt"
                ).write_text(certificate, encoding="utf-8")
                result = subprocess.run(
                    [
                        powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File",
                        str(case / "scripts" / "install_nexus_harness.ps1"),
                        "-LoadFunctionsOnly",
                    ],
                    text=True, capture_output=True, timeout=20, errors="replace",
                )
                with self.subTest(case=name):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("configuration is partial", result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "Authenticode pin verification is Windows-specific")
    def test_authenticode_requires_both_exact_subject_and_certificate_sha256(self):
        powershell = _windows_powershell()
        functions = _installer_header_function_source()
        harness = r'''
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$script:publisherConfigured = $true
$script:expectedPublisher = 'CN=Nexus Harness Release'
$raw = [byte[]](1, 3, 3, 7, 9, 11)
$certificate = [pscustomobject]@{
    Subject = 'CN=Nexus Harness Release'
    RawData = $raw
}
function Get-NexusAuthenticodeSignature {
    return [pscustomobject]@{ Status = 'Valid'; SignerCertificate = $certificate }
}
$script:expectedSignerCertificateSha256 = Get-NexusCertificateSha256 $certificate
try { Assert-NexusInstallerSignature 'fixture.exe' $false; $exact = $true } catch { $exact = $false }
$script:expectedSignerCertificateSha256 = '0' * 64
try { Assert-NexusInstallerSignature 'fixture.exe' $false; $digestMessage = '' } catch { $digestMessage = [string]$_.Exception.Message }
$script:expectedSignerCertificateSha256 = Get-NexusCertificateSha256 $certificate
$certificate.Subject = 'CN=Lookalike Publisher'
try { Assert-NexusInstallerSignature 'fixture.exe' $false; $subjectMessage = '' } catch { $subjectMessage = [string]$_.Exception.Message }
[pscustomobject]@{
    ExactAccepted = $exact
    DigestMessage = $digestMessage
    SubjectMessage = $subjectMessage
} | ConvertTo-Json -Compress
'''.replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "certificate-pin.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script),
                ],
                text=True, capture_output=True, timeout=30, errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["ExactAccepted"])
            self.assertIn("certificate SHA-256 is unexpected", payload["DigestMessage"])
            self.assertIn("unexpected publisher", payload["SubjectMessage"])

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
$shortcut.WorkingDirectory = $installedFolder
$shortcut.IconLocation = "$installed,0"
$shortcut.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$before = [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath))
$first = Assert-NexusDesktopShortcut $installed $desktop $commonDesktop
$second = Assert-NexusDesktopShortcut $installed $desktop $commonDesktop
$missingCommon = Join-Path $root 'missing public desktop'
$missingCommonAccepted = Assert-NexusDesktopShortcut $installed $desktop $missingCommon
function Assert-NexusDesktopFolderListable([string] $Path) {
    if ([StringComparer]::OrdinalIgnoreCase.Equals(
            [IO.Path]::GetFullPath($Path), [IO.Path]::GetFullPath($commonDesktop))) {
        throw [UnauthorizedAccessException]::new('company policy denied Common Desktop')
    }
    $probe = [IO.Directory]::EnumerateFileSystemEntries($Path).GetEnumerator()
    try { [void]$probe.MoveNext() } finally { $probe.Dispose() }
}
$deniedCommonAccepted = Assert-NexusDesktopShortcut $installed $desktop $commonDesktop
$driveRoot = Get-NexusCanonicalPath ([IO.Path]::GetPathRoot($root)) 'The drive root'
$after = [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath))
[pscustomobject]@{
    ShortcutPath = $second.ShortcutPath
    TargetPath = $second.TargetPath
    IconPath = $second.IconPath
    Arguments = $second.Arguments
    WorkingDirectory = $second.WorkingDirectory
    LinkCount = @(
        Get-ChildItem -LiteralPath $desktop, $commonDesktop -Filter 'Nexus Harness*.lnk' -File -Force
    ).Count
    Unchanged = $before -ceq $after
    SameResult = $first.ShortcutPath -ceq $second.ShortcutPath
    MissingCommonAccepted = $missingCommonAccepted.ShortcutPath -ceq $shortcutPath
    DeniedCommonAccepted = $deniedCommonAccepted.ShortcutPath -ceq $shortcutPath
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
                    self.assertEqual(
                        Path(payload["WorkingDirectory"]),
                        Path(folder) / "installed application",
                    )
                    self.assertEqual(payload["LinkCount"], 1)
                    self.assertTrue(payload["Unchanged"])
                    self.assertTrue(payload["SameResult"])
                    self.assertTrue(payload["MissingCommonAccepted"])
                    self.assertTrue(payload["DeniedCommonAccepted"])
                    self.assertTrue(
                        payload["DriveRoot"].endswith(("\\", "/")),
                        payload["DriveRoot"],
                    )

    @unittest.skipUnless(os.name == "nt", "Windows shortcut repair is Windows-specific")
    def test_powershell_shortcut_repair_replaces_only_exact_stale_user_link(self):
        hosts = _powershell_hosts()
        self.assertTrue(hosts, "Windows PowerShell is required for the installer contract")
        functions = _installer_header_function_source()
        harness = r'''
$ErrorActionPreference = 'Stop'
__INSTALLER_FUNCTIONS__
$root = [IO.Path]::GetFullPath($args[0])
$installedFolder = Join-Path $root 'Policy redirected Programs'
$desktop = Join-Path $root 'OneDrive - Åsa & Company Desktop'
$commonDesktop = Join-Path $root 'Public Desktop'
$outsideFolder = Join-Path $root 'old development checkout'
New-Item -ItemType Directory -Force -Path `
    $installedFolder, $desktop, $commonDesktop, $outsideFolder | Out-Null
$installed = Join-Path $installedFolder 'Nexus Harness.exe'
$outside = Join-Path $outsideFolder 'Nexus Harness.exe'
[IO.File]::WriteAllBytes($installed, [byte[]](77, 90))
[IO.File]::WriteAllBytes($outside, [byte[]](77, 90))
$shortcutPath = Join-Path $desktop 'Nexus Harness.lnk'
$shell = New-Object -ComObject WScript.Shell
$stale = $shell.CreateShortcut($shortcutPath)
$stale.TargetPath = $outside
$stale.Arguments = '--stale-development-argument'
$stale.WorkingDirectory = $outsideFolder
$stale.IconLocation = "$outside,7"
$stale.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($stale)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$aliasPath = Join-Path $outsideFolder 'stale shortcut hard-link alias.lnk'
[void](New-Item -ItemType HardLink -Path $aliasPath -Target $shortcutPath)
$aliasBefore = [Convert]::ToBase64String([IO.File]::ReadAllBytes($aliasPath))

$repairedStale = Ensure-NexusDesktopShortcut $installed $desktop $commonDesktop
$hardLinkAliasUnchanged = (
    $aliasBefore -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($aliasPath))
)
Remove-Item -LiteralPath $shortcutPath -Force
$repairedMissing = Repair-NexusDesktopShortcut $installed $desktop $commonDesktop
[IO.File]::SetAttributes(
    $shortcutPath,
    [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System
)
$repairedHidden = Ensure-NexusDesktopShortcut $installed $desktop $commonDesktop
$visibleAttributes = [int](Get-Item -LiteralPath $shortcutPath -Force).Attributes
$visibleOrdinary = (($visibleAttributes -band (
    [int][IO.FileAttributes]::Hidden -bor
    [int][IO.FileAttributes]::System -bor
    [int][IO.FileAttributes]::ReparsePoint
)) -eq 0)

$shell = New-Object -ComObject WScript.Shell
$actual = $shell.CreateShortcut($shortcutPath)
$workingDirectory = [string]$actual.WorkingDirectory
$description = [string]$actual.Description
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($actual)
$duplicate = $shell.CreateShortcut((Join-Path $desktop 'Nexus Harness old copy.lnk'))
$duplicate.TargetPath = $installed
$duplicate.WorkingDirectory = $installedFolder
$duplicate.IconLocation = "$installed,0"
$duplicate.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($duplicate)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$duplicatePath = Join-Path $desktop 'Nexus Harness old copy.lnk'
$exactBeforeConflict = [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath))
$duplicateBeforeConflict = [Convert]::ToBase64String([IO.File]::ReadAllBytes($duplicatePath))
try {
    [void](Repair-NexusDesktopShortcut $installed $desktop $commonDesktop)
    $duplicateRejected = $false
} catch {
    $duplicateRejected = [string]$_.Exception.Message -like '*conflicting visible shortcuts exist*'
}
$conflictsUnchanged = (
    $exactBeforeConflict -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath)) -and
    $duplicateBeforeConflict -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($duplicatePath))
)
Remove-Item -LiteralPath $shortcutPath, $duplicatePath -Force
$shell = New-Object -ComObject WScript.Shell
$publicPath = Join-Path $commonDesktop 'Nexus Harness.lnk'
$public = $shell.CreateShortcut($publicPath)
$public.TargetPath = $installed
$public.WorkingDirectory = $installedFolder
$public.IconLocation = "$installed,0"
$public.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($public)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$publicBefore = [Convert]::ToBase64String([IO.File]::ReadAllBytes($publicPath))
try {
    [void](Repair-NexusDesktopShortcut $installed $desktop $commonDesktop)
    $publicOnlyRejected = $false
} catch {
    $publicOnlyRejected = [string]$_.Exception.Message -like '*conflicting visible shortcuts exist*'
}
$publicOnlyUnchanged = (
    -not (Test-Path -LiteralPath $shortcutPath) -and
    $publicBefore -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($publicPath))
)
Remove-Item -LiteralPath $publicPath -Force
$shell = New-Object -ComObject WScript.Shell
$locked = $shell.CreateShortcut($shortcutPath)
$locked.TargetPath = $outside
$locked.WorkingDirectory = $outsideFolder
$locked.IconLocation = "$outside,0"
$locked.Save()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($locked)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
$lockedBefore = [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath))
$lockedHandle = [IO.File]::Open(
    $shortcutPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None
)
try {
    try {
        [void](Repair-NexusDesktopShortcut $installed $desktop $commonDesktop)
        $lockedRejected = $false
    } catch {
        $lockedRejected = [string]$_.Exception.Message -like '*failed safely*'
    }
} finally {
    $lockedHandle.Dispose()
}
$lockedUnchanged = (
    $lockedBefore -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($shortcutPath))
)
$repairArtifacts = @(Get-ChildItem -LiteralPath $desktop -Force | Where-Object {
    $_.Name -like '.nexus-harness-shortcut-*'
}).Count
Remove-Item -LiteralPath $shortcutPath -Force
New-Item -ItemType Directory -Path $shortcutPath | Out-Null
try {
    [void](Repair-NexusDesktopShortcut $installed $desktop $commonDesktop)
    $directoryRejected = $false
} catch {
    $directoryRejected = [string]$_.Exception.Message -like '*is not a file*'
}
$directoryUnchanged = Test-Path -LiteralPath $shortcutPath -PathType Container
[pscustomobject]@{
    StaleTarget = $repairedStale.TargetPath
    MissingTarget = $repairedMissing.TargetPath
    HiddenTarget = $repairedHidden.TargetPath
    Arguments = $repairedMissing.Arguments
    IconPath = $repairedMissing.IconPath
    IconIndex = $repairedMissing.IconIndex
    WorkingDirectory = $workingDirectory
    Description = $description
    DuplicateRejected = $duplicateRejected
    ConflictsUnchanged = $conflictsUnchanged
    PublicOnlyRejected = $publicOnlyRejected
    PublicOnlyUnchanged = $publicOnlyUnchanged
    HardLinkAliasUnchanged = $hardLinkAliasUnchanged
    VisibleOrdinary = $visibleOrdinary
    LockedRejected = $lockedRejected
    LockedUnchanged = $lockedUnchanged
    RepairArtifacts = $repairArtifacts
    DirectoryRejected = $directoryRejected
    DirectoryUnchanged = $directoryUnchanged
    OutsideStillExists = Test-Path -LiteralPath $outside
} | ConvertTo-Json -Compress
'''.replace("__INSTALLER_FUNCTIONS__", functions)
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "shortcut-repair.ps1"
            script.write_text(harness, encoding="utf-8-sig")
            for host in hosts:
                with self.subTest(host=Path(host).name):
                    case_folder = Path(folder) / f"case-{Path(host).stem}"
                    case_folder.mkdir()
                    result = subprocess.run(
                        [host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), str(case_folder)],
                        text=True, capture_output=True, timeout=30, errors="replace",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout.strip().splitlines()[-1])
                    expected = case_folder / "Policy redirected Programs" / "Nexus Harness.exe"
                    self.assertEqual(Path(payload["StaleTarget"]), expected)
                    self.assertEqual(Path(payload["MissingTarget"]), expected)
                    self.assertEqual(Path(payload["HiddenTarget"]), expected)
                    self.assertEqual(payload["Arguments"], "")
                    self.assertEqual(Path(payload["IconPath"]), expected)
                    self.assertEqual(payload["IconIndex"], 0)
                    self.assertEqual(
                        Path(payload["WorkingDirectory"]),
                        case_folder / "Policy redirected Programs",
                    )
                    self.assertEqual(payload["Description"], "Nexus Harness")
                    self.assertTrue(payload["DuplicateRejected"])
                    self.assertTrue(payload["ConflictsUnchanged"])
                    self.assertTrue(payload["PublicOnlyRejected"])
                    self.assertTrue(payload["PublicOnlyUnchanged"])
                    self.assertTrue(payload["HardLinkAliasUnchanged"])
                    self.assertTrue(payload["VisibleOrdinary"])
                    self.assertTrue(payload["LockedRejected"])
                    self.assertTrue(payload["LockedUnchanged"])
                    self.assertEqual(payload["RepairArtifacts"], 0)
                    self.assertTrue(payload["DirectoryRejected"])
                    self.assertTrue(payload["DirectoryUnchanged"])
                    self.assertTrue(payload["OutsideStillExists"])

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
    $shortcut.WorkingDirectory = if ($case -eq 'wrong-working-directory') {
        $root
    } else {
        $installedFolder
    }
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
    if ($case -eq 'exact-hidden') {
        [IO.File]::SetAttributes(
            (Join-Path $desktop 'Nexus Harness.lnk'), [IO.FileAttributes]::Hidden
        )
    }
    if ($case -eq 'exact-system') {
        [IO.File]::SetAttributes(
            (Join-Path $desktop 'Nexus Harness.lnk'), [IO.FileAttributes]::System
        )
    }
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
            "wrong-working-directory": "desktop shortcut working directory",
            "exact-hidden": "not a visible ordinary file",
            "exact-system": "not a visible ordinary file",
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
$shortcut.WorkingDirectory = $installedFolder
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

    @unittest.skipUnless(os.name == "nt", "Authenticode verification is Windows-specific")
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

    @unittest.skipUnless(os.name == "nt", "Authenticode verification is Windows-specific")
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
        desktop_install_guide = (
            ROOT / "docs" / "THE_THING_ON_YOUR_DESKTOP.md"
        ).read_text(encoding="utf-8")
        release_guide = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("npm run smoke:built", workflow)
        self.assertIn("Security.Cryptography.SHA256", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\s]+@v\d")
        self.assertIn("WINDOWS_CERTIFICATE_BASE64", workflow)
        self.assertIn("NEXUS_UNSIGNED_RELEASE", workflow)
        self.assertIn("explicitly unsigned installer", workflow)
        self.assertIn("Authenticode configuration is partial", workflow)
        self.assertIn("actualSignerCertificateSha256", workflow)
        self.assertIn("Get-PfxData", workflow)
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
        self.assertIn("Unsigned release mode verified", powershell_installer)
        self.assertNotIn("credential fill", powershell_installer)
        self.assertNotIn("Get-Command gh", powershell_installer)
        self.assertNotIn("Get-Command git", powershell_installer)
        self.assertIn("GH_TOKEN", powershell_installer)
        self.assertIn("$installerAsset.url", powershell_installer)
        self.assertIn("Assert-NexusDesktopShortcut", powershell_installer)
        self.assertIn("Get-NexusStableReleaseAssets", powershell_installer)
        self.assertIn("DesktopDirectory", powershell_installer)
        self.assertIn("CommonDesktopDirectory", powershell_installer)
        self.assertIn("RegistryView]::Registry64", powershell_installer)
        self.assertIn("e52322ab-f15e-5dc0-963b-7588e3739e89", powershell_installer)
        self.assertIn("$installerArguments = if", powershell_installer)
        self.assertIn("@('/currentuser')", powershell_installer)
        self.assertIn("ArgumentList = $installerArguments", powershell_installer)
        self.assertIn("Desktop shortcut verified", powershell_installer)
        self.assertIn("$shortcut.Arguments", powershell_installer)
        self.assertIn("-File -Force", powershell_installer)
        self.assertIn(
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe",
            top_level,
        )
        self.assertNotRegex(top_level, r"(?im)^\s*powershell\.exe\b")
        self.assertIn("Remove-Item -LiteralPath $shortcut -Force", workflow)
        self.assertIn("CommonDesktopDirectory", workflow)
        self.assertIn("--stale-development-shortcut", workflow)
        self.assertIn("Stale-shortcut reinstall stopped", workflow)
        self.assertIn("!macro customInstall", nsis_include)
        self.assertIn('Delete "$newDesktopLink"', nsis_include)
        self.assertIn('CreateShortCut "$newDesktopLink" "$appExe"', nsis_include)
        self.assertIn("RegistryView]::Registry64", workflow)
        self.assertIn("@('/S', '/currentuser')", workflow)
        self.assertIn("The recreated desktop shortcut did not launch", workflow)
        self.assertIn("A prior package smoke left the installed app running", workflow)
        self.assertIn("Stop-InstalledProcessTrees", workflow)
        self.assertIn("did not stop", workflow)
        self.assertIn("Prove the stable release is anonymously installable", workflow)
        self.assertIn("releases/latest", workflow)
        self.assertIn("build_windows_offline_bundle.ps1", workflow)
        self.assertIn("Nexus-Harness-Windows-Offline-", workflow)
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
        self.assertIn("Get-NexusFileSha256", powershell_installer)
        self.assertIn("Get-NexusLocalBundleAssets", powershell_installer)
        self.assertIn("NEXUS_INSTALLER_SILENT", powershell_installer)
        self.assertIn("@('/S', '/currentuser')", powershell_installer)
        self.assertIn("offlineBundlePinnedInstallerSha256", powershell_installer)
        self.assertIn("expectedSignerCertificateSha256", powershell_installer)
        self.assertIn("private current-user execution copy", powershell_installer)
        self.assertIn("-OfflineOnly", top_level)
        self.assertIn("NEXUS_BOOTSTRAP_EXPECTED_SHA256", top_level)
        self.assertTrue((ROOT / "scripts" / "build_windows_offline_bundle.ps1").is_file())
        bundle_builder = (
            ROOT / "scripts" / "build_windows_offline_bundle.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("signer_certificate_sha256", bundle_builder)
        self.assertIn("BootstrapSha256", bundle_builder)
        self.assertTrue(
            (ROOT / "release" / "windows-authenticode-certificate-sha256.txt").is_file()
        )
        self.assertIn("install_nexus_harness.ps1", top_level)
        self.assertIn('-BundleRoot "%~dp0."', top_level)
        self.assertNotIn("where python", top_level.lower())
        self.assertIn("versioned v2 identity manifest", desktop_install_guide)
        self.assertNotIn("v1 manifest", desktop_install_guide)
        self.assertNotIn("gh auth login", desktop_install_guide)
        self.assertNotIn("Git Credential Manager", desktop_install_guide)
        self.assertIn("executes them in memory", release_guide)
        self.assertIn("without resolving the mutable source path again", release_guide)


if __name__ == "__main__":
    unittest.main()
