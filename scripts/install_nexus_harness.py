"""Deprecated compatibility surface for the Windows release installer.

The supported entry point is ``Install Nexus Harness.cmd`` backed by
``install_nexus_harness.ps1``.  This module retains small parsing helpers for
tests and old imports, but its CLI/install function fail closed so it cannot
bypass the product-bound bootstrap, certificate pin, private execution-copy,
installed-identity, or shortcut contracts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


REPOSITORY = "KZTP47/nexus-harness"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_INSTALLER_BYTES = 350 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
PUBLISHER_FILE = Path(__file__).resolve().parents[1] / "release" / "windows-authenticode-publisher.txt"


def _expected_publisher() -> str | None:
    try:
        subject = PUBLISHER_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InstallError("The pinned Windows publisher identity is missing; nothing was downloaded.") from exc
    if not subject or subject.startswith("UNCONFIGURED"):
        return None
    return subject


class InstallError(RuntimeError):
    pass


def _github_token() -> str:
    """Use only an explicit process-scoped token; never execute credential tools."""

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    return token.strip()


def _request(url: str, *, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    headers = {"Accept": accept, "User-Agent": "Nexus-Harness-Installer"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _allowed_address(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_DOWNLOAD_HOSTS


class _SafeGitHubRedirects(urllib.request.HTTPRedirectHandler):
    """Validate before following and never carry a bearer token across hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not _allowed_address(newurl):
            raise InstallError(
                "The release download redirected away from GitHub; nothing was run."
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if urlparse(req.full_url).hostname != urlparse(newurl).hostname:
            redirected.remove_header("Authorization")
            redirected.headers.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


_SAFE_DOWNLOAD_OPENER = urllib.request.build_opener(_SafeGitHubRedirects())


def _download(url: str, destination: Path, maximum: int) -> None:
    if not _allowed_address(url):
        raise InstallError("GitHub returned an unexpected download address; nothing was run.")
    total = 0
    try:
        with _SAFE_DOWNLOAD_OPENER.open(
            _request(url, accept="application/octet-stream"), timeout=60,
        ) as response, destination.open("wb") as output:
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                raise InstallError("The release download redirected away from GitHub; nothing was run.")
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > maximum:
                    raise InstallError("The downloaded release was unexpectedly large; nothing was run.")
                output.write(block)
    except (OSError, urllib.error.URLError) as exc:
        raise InstallError(f"The release could not be downloaded: {exc}") from exc


def _release(api_url: str = LATEST_RELEASE_API) -> dict:
    if not _allowed_address(api_url):
        raise InstallError(
            "The release metadata address is not an approved GitHub HTTPS host."
        )
    try:
        with _SAFE_DOWNLOAD_OPENER.open(_request(api_url), timeout=30) as response:
            body = response.read(2 * 1024 * 1024 + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise InstallError(f"GitHub releases could not be reached: {exc}") from exc
    if len(body) > 2 * 1024 * 1024:
        raise InstallError("GitHub returned an unexpectedly large release description.")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("GitHub returned an unreadable release description.") from exc
    if not isinstance(value, dict) or value.get("draft") or value.get("prerelease"):
        raise InstallError("There is no stable Nexus Harness release to install yet.")
    return value


def _assets(release: dict) -> tuple[dict, dict]:
    assets = [item for item in release.get("assets", []) if isinstance(item, dict)]
    installers = [item for item in assets if str(item.get("name", "")).lower().endswith(".exe")]
    checksums = [item for item in assets if str(item.get("name", "")).lower().endswith(".sha256")]
    if len(installers) != 1 or len(checksums) != 1:
        raise InstallError("The release is missing its one installer or checksum; nothing was run.")
    return installers[0], checksums[0]


def _asset_download_url(asset: dict, *, authenticated: bool) -> str:
    """Use GitHub's asset API for private/authenticated release downloads."""

    key = "url" if authenticated else "browser_download_url"
    address = str(asset.get(key) or "")
    if not address:
        raise InstallError(f"The release asset has no {key}; nothing was downloaded.")
    return address


def _expected_digest(checksum_file: Path, installer_name: str) -> str:
    lines = checksum_file.read_text(encoding="utf-8-sig").splitlines()
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == installer_name:
            digest = parts[0].lower()
            if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
                return digest
    raise InstallError("The published checksum does not name the installer; nothing was run.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _authenticode_signer(path: Path, expected_subject: str | None) -> str:
    """Verify the declared release mode and return its human-readable trust label."""

    script = (
        "$signature = Get-AuthenticodeSignature -LiteralPath $args[0]; "
        "[PSCustomObject]@{Status=[string]$signature.Status; "
        "Subject=[string]$signature.SignerCertificate.Subject; "
        "Message=[string]$signature.StatusMessage} | ConvertTo-Json -Compress"
    )
    try:
        system_root = Path(os.environ.get("SystemRoot") or "")
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not system_root.is_absolute() or not powershell.is_file():
            raise InstallError(
                "Windows did not provide its built-in PowerShell signature verifier; nothing was run."
            )
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise InstallError(f"Windows could not verify the installer signature: {exc}") from exc
    status = str(value.get("Status") or "")
    subject = str(value.get("Subject") or "")
    if expected_subject is None:
        if status != "NotSigned" or subject:
            detail = str(value.get("Message") or result.stderr or status or "unexpected signature state")
            raise InstallError(
                "The checksum-only release did not have the expected unsigned Windows signature state "
                f"({detail}); nothing was run."
            )
        return "SHA-256 verified; not Authenticode-signed"
    if status != "Valid" or not subject:
        detail = str(value.get("Message") or result.stderr or status or "no signature")
        raise InstallError(f"The installer does not have a valid Authenticode signature ({detail}); nothing was run.")
    if subject != expected_subject:
        raise InstallError(
            f"The installer is signed by an unexpected publisher ({subject}); expected {expected_subject}. Nothing was run."
        )
    return subject


def install(*, api_url: str = LATEST_RELEASE_API, quiet: bool = False) -> str:
    del api_url, quiet
    raise InstallError(
        "This legacy Python installer is disabled so it cannot bypass the hardened Windows bootstrap. "
        "Run the repository's exact 'Install Nexus Harness.cmd', or the copy inside the complete "
        "product-built offline ZIP."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deprecated Nexus Harness installer compatibility shim")
    parser.add_argument("--api-url", default=LATEST_RELEASE_API, help=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", help="Run the NSIS installer unattended")
    args = parser.parse_args(argv)
    try:
        version = install(api_url=args.api_url, quiet=args.quiet)
    except InstallError as exc:
        print(f"Installation stopped safely: {exc}", file=sys.stderr)
        print(
            "Use the exact top-level Install Nexus Harness.cmd. For a private fork's source-mode "
            "download, set GH_TOKEN or GITHUB_TOKEN only for that process. If you are developing "
            "Nexus itself, see docs/DESKTOP.md instead.",
            file=sys.stderr,
        )
        return 1
    print(f"Nexus Harness {version} is installed. Open it from the Start menu or desktop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
