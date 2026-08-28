"""Build the private, reproducible Python runtime carried by the Windows app."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
LOCK = ROOT / "requirements-runtime.lock"
PLAYWRIGHT_LOCK = ROOT / "runtime-playwright.lock.json"
PYTHON_VERSION = "3.11.9"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
RUNTIME_LOCK = DESKTOP / ".runtime-build.lock"
RUNTIME_PUBLISH_TIMEOUT_SECONDS = 300.0


def retry_owned_windows_operation(operation, description: str, timeout_seconds: float = 30.0):
    """Retry short-lived loader/AV locks on builder-owned staging artifacts."""

    started = time.monotonic()
    while True:
        try:
            return operation()
        except OSError as error:
            retryable = os.name == "nt" and getattr(error, "winerror", None) in {5, 32, 145}
            if not retryable or time.monotonic() - started >= timeout_seconds:
                raise
            time.sleep(0.1)


@contextlib.contextmanager
def runtime_build_lock(timeout_seconds: float = 1_800.0):
    """Serialize runtime publishers across parallel closeout/release jobs."""

    RUNTIME_LOCK.parent.mkdir(parents=True, exist_ok=True)
    stream = RUNTIME_LOCK.open("a+b")
    started = time.monotonic()
    try:
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    if stream.tell() == stream.seek(0, os.SEEK_END):
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout_seconds:
                    raise RuntimeError("Timed out waiting for the owned private-runtime build lock")
                time.sleep(0.2)
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def digest(path: Path) -> str:
    held = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            held.update(block)
    return held.hexdigest()


def sri_digest(path: Path, integrity: str) -> bool:
    """Validate one npm Subresource Integrity value without trusting npm."""

    algorithm, encoded = integrity.split("-", 1)
    if algorithm not in hashlib.algorithms_available:
        raise RuntimeError(f"Unsupported package integrity algorithm: {algorithm}")
    held = hashlib.new(algorithm)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            held.update(block)
    return held.digest() == base64.b64decode(encoded, validate=True)


def _download(url: str, destination: Path, *, sha256: str | None = None,
              integrity: str | None = None) -> None:
    urllib.request.urlretrieve(url, destination)
    if sha256 is not None and digest(destination) != sha256.casefold():
        raise RuntimeError(f"Downloaded archive checksum did not match: {url}")
    if integrity is not None and not sri_digest(destination, integrity):
        raise RuntimeError(f"Downloaded npm package integrity did not match: {url}")


def _safe_zip_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as packed:
        for item in packed.infolist():
            target = (destination / item.filename).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError(f"Archive member escapes its destination: {item.filename}")
        packed.extractall(destination)


def _safe_npm_extract(archive: Path, destination: Path) -> None:
    """Extract the npm-standard package/ tree and reject links/traversal."""

    destination.mkdir(parents=True, exist_ok=False)
    canonical = destination.resolve()
    with tarfile.open(archive, mode="r:gz") as packed:
        for item in packed.getmembers():
            parts = Path(item.name).parts
            if not parts or parts[0] != "package" or item.issym() or item.islnk():
                raise RuntimeError(f"Invalid npm archive member: {item.name}")
            relative = Path(*parts[1:])
            target = (canonical / relative).resolve()
            if target != canonical and canonical not in target.parents:
                raise RuntimeError(f"npm archive member escapes its destination: {item.name}")
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = packed.extractfile(item)
                if stream is None:
                    raise RuntimeError(f"Could not extract npm archive member: {item.name}")
                with target.open("wb") as output:
                    shutil.copyfileobj(stream, output)


def _runtime_lock() -> dict[str, object]:
    payload = json.loads(PLAYWRIGHT_LOCK.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported Playwright runtime lock schema")
    return payload


def validate_playwright_runtime(output: Path, locked: dict[str, object]) -> None:
    runtime = output / "playwright"
    chromium = locked["chromium"]
    assert isinstance(chromium, dict)
    script = r'''
const fs = require('node:fs');
const path = require('node:path');
const runtime = path.resolve(process.argv[2]);
const expected = process.argv[3];
const pw = require(path.join(runtime, 'node_modules', 'playwright'));
const test = require(path.join(runtime, 'node_modules', '@playwright', 'test'));
if (require(path.join(runtime, 'node_modules', 'playwright', 'package.json')).version !== expected)
  throw new Error('playwright version mismatch');
if (typeof test.test !== 'function' || typeof test.expect !== 'function')
  throw new Error('@playwright/test is incomplete');
(async () => {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage();
  await page.setContent('<button id="go" onclick="this.textContent=\'worked\'">run</button>');
  await page.locator('#go').click();
  if (await page.locator('#go').textContent() !== 'worked') throw new Error('browser interaction failed');
  await browser.close();
  process.stdout.write('bundled playwright interaction ok\n');
})().catch(error => { console.error(error); process.exit(1); });
'''
    probe = runtime / "validate_playwright.cjs"
    probe.write_text(script, encoding="utf-8")
    environment = {
        **os.environ,
        "PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers"),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
    }
    try:
        completed = subprocess.run(
            [str(runtime / "node.exe"), str(probe), str(runtime),
             str(locked["packages"][0]["version"])],
            cwd=ROOT, env=environment, check=False,
        )
    finally:
        probe.unlink(missing_ok=True)
    if completed.returncode:
        raise RuntimeError(f"Bundled Playwright runtime validation failed ({completed.returncode})")
    executable = runtime / "browsers" / str(chromium["install_dir"]) / str(chromium["executable"])
    if not executable.is_file():
        raise RuntimeError("Bundled Chromium executable is missing")


def _prepare_playwright(output: Path) -> dict[str, object]:
    locked = _runtime_lock()
    runtime = output / "playwright"
    runtime.mkdir()
    node = locked["node"]
    chromium = locked["chromium"]
    packages = locked["packages"]
    assert isinstance(node, dict) and isinstance(chromium, dict) and isinstance(packages, list)
    with tempfile.TemporaryDirectory(prefix="nexus-playwright-runtime-") as temporary:
        downloads = Path(temporary)
        node_archive = downloads / "node.zip"
        _download(str(node["url"]), node_archive, sha256=str(node["sha256"]))
        node_extract = downloads / "node"
        _safe_zip_extract(node_archive, node_extract)
        node_root = node_extract / str(node["archive_root"])
        shutil.copy2(node_root / "node.exe", runtime / "node.exe")
        shutil.copy2(node_root / "LICENSE", runtime / "NODE_LICENSE")

        modules = runtime / "node_modules"
        for package in packages:
            assert isinstance(package, dict)
            archive = downloads / (str(package["name"]).replace("/", "-") + ".tgz")
            _download(str(package["url"]), archive, integrity=str(package["integrity"]))
            destination = modules.joinpath(*str(package["name"]).split("/"))
            _safe_npm_extract(archive, destination)
            metadata = json.loads((destination / "package.json").read_text(encoding="utf-8"))
            if metadata.get("name") != package["name"] or metadata.get("version") != package["version"]:
                raise RuntimeError(f"npm package identity did not match: {package['name']}")

        browser_archive = downloads / "chromium.zip"
        _download(str(chromium["url"]), browser_archive, sha256=str(chromium["sha256"]))
        browser_root = runtime / "browsers" / str(chromium["install_dir"])
        browser_root.mkdir(parents=True)
        _safe_zip_extract(browser_archive, browser_root)
        (browser_root / "INSTALLATION_COMPLETE").write_text("", encoding="ascii")

    validate_playwright_runtime(output, locked)
    executable = runtime / "browsers" / str(chromium["install_dir"]) / str(chromium["executable"])
    return {
        "schema_version": 1,
        "lock_sha256": digest(PLAYWRIGHT_LOCK),
        "node_version": node["version"],
        "node_archive_sha256": node["sha256"],
        "node_executable_sha256": digest(runtime / "node.exe"),
        "playwright_version": packages[0]["version"],
        "package_integrities": {str(one["name"]): one["integrity"] for one in packages},
        "chromium_browser_version": chromium["browser_version"],
        "chromium_revision": chromium["revision"],
        "chromium_archive_sha256": chromium["sha256"],
        "chromium_executable_sha256": digest(executable),
        "node": "node.exe",
        "playwright_cli": "node_modules/playwright/cli.js",
        "playwright_module": "node_modules/playwright",
        "playwright_test_module": "node_modules/@playwright/test",
        "browsers_path": "browsers",
        "chromium_executable": str(executable.relative_to(runtime)).replace("\\", "/"),
    }


def validate_runtime(output: Path) -> None:
    """Prove the private interpreter can load the graph engine and every dependency."""

    script = r'''
import importlib.metadata
import re
from pathlib import Path
from packaging.markers import default_environment
from packaging.requirements import Requirement

site = Path(__file__).resolve().parent / "Lib" / "site-packages"
normal = lambda value: re.sub(r"[-_.]+", "-", value).lower()
installed = {normal(dist.metadata["Name"]): dist.version for dist in importlib.metadata.distributions(path=[site])}
environment = default_environment()
missing = []
for dist in importlib.metadata.distributions(path=[site]):
    for raw in dist.requires or []:
        required = Requirement(raw)
        if required.marker and not required.marker.evaluate(environment):
            continue
        found = installed.get(normal(required.name))
        if found is None or (required.specifier and found not in required.specifier):
            missing.append(f"{dist.metadata['Name']} requires {raw}; found {found or 'nothing'}")
if missing:
    raise SystemExit("\n".join(sorted(missing)))
from langgraph.graph import StateGraph
print(f"private runtime imports ok: {len(installed)} locked distributions")
'''
    probe = output / "validate_runtime.py"
    probe.write_text(script, encoding="utf-8")
    try:
        completed = subprocess.run([str(output / "python.exe"), str(probe)], cwd=ROOT, check=False)
    finally:
        probe.unlink(missing_ok=True)
    if completed.returncode:
        raise RuntimeError(f"Private runtime dependency validation failed ({completed.returncode})")


def _prepare_staging(output: Path) -> None:
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="nexus-python-runtime-") as temporary:
        archive = Path(temporary) / "python.zip"
        _download(PYTHON_URL, archive, sha256=PYTHON_SHA256)
        _safe_zip_extract(archive, output)

    packages = output / "Lib" / "site-packages"
    packages.mkdir(parents=True)
    command = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-compile", "--no-deps",
        "--only-binary=:all:", "--platform", "win_amd64",
        "--python-version", "3.11", "--implementation", "cp", "--abi", "cp311",
        "--target", str(packages), "-r", str(LOCK),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        raise RuntimeError(f"Locked runtime dependency installation failed ({completed.returncode})")

    pth = output / "python311._pth"
    lines = [line for line in pth.read_text(encoding="utf-8").splitlines() if line.strip() != "#import site"]
    lines += ["Lib/site-packages", "../harness/src", "import site"]
    pth.write_text("\n".join(dict.fromkeys(lines)) + "\n", encoding="utf-8")
    validate_runtime(output)
    playwright_manifest = _prepare_playwright(output)
    manifest = {
        "python": PYTHON_VERSION,
        "python_url": PYTHON_URL,
        "python_sha256": PYTHON_SHA256,
        "requirements_sha256": digest(LOCK),
        "playwright": playwright_manifest,
    }
    (output / "NEXUS_RUNTIME.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _remove_abandoned_runtime_trees() -> None:
    """Remove staging/rollback trees left by a dead, previously locked build.

    This is called only while holding ``runtime_build_lock``.  A live builder
    therefore cannot own one of these checkout-local trees, and every target is
    resolved and confined to the desktop directory before recursive removal.
    """
    desktop = DESKTOP.resolve()
    for pattern in (".runtime-stage-*", ".runtime-previous-*"):
        for candidate in desktop.glob(pattern):
            resolved = candidate.resolve()
            if resolved.parent != desktop or not resolved.is_dir():
                raise RuntimeError(f"Refusing unsafe abandoned runtime path: {candidate}")
            retry_owned_windows_operation(
                lambda target=resolved: shutil.rmtree(target),
                "remove abandoned private-runtime tree",
                timeout_seconds=RUNTIME_PUBLISH_TIMEOUT_SECONDS,
            )


def prepare(output: Path) -> Path:
    output = output.resolve()
    allowed = (DESKTOP / "runtime").resolve()
    if output != allowed:
        raise RuntimeError(f"Runtime output must be exactly {allowed}")
    with runtime_build_lock():
        _remove_abandoned_runtime_trees()
        staging = DESKTOP / f".runtime-stage-{uuid.uuid4().hex}"
        previous = DESKTOP / f".runtime-previous-{uuid.uuid4().hex}"
        try:
            _prepare_staging(staging)
            # Publish only a fully validated runtime. Builders never install
            # packages into the shared destination, so a parallel job cannot
            # observe or delete a half-populated site-packages tree.
            had_previous = output.exists()
            if had_previous:
                retry_owned_windows_operation(
                    lambda: output.replace(previous), "preserve previous private runtime",
                    timeout_seconds=RUNTIME_PUBLISH_TIMEOUT_SECONDS,
                )
            try:
                retry_owned_windows_operation(
                    lambda: staging.replace(output), "publish validated private runtime",
                    timeout_seconds=RUNTIME_PUBLISH_TIMEOUT_SECONDS,
                )
            except BaseException:
                if had_previous and previous.exists() and not output.exists():
                    retry_owned_windows_operation(
                        lambda: previous.replace(output), "restore previous private runtime",
                        timeout_seconds=RUNTIME_PUBLISH_TIMEOUT_SECONDS,
                    )
                raise
            if previous.exists():
                retry_owned_windows_operation(
                    lambda: shutil.rmtree(previous), "remove previous private runtime",
                    timeout_seconds=RUNTIME_PUBLISH_TIMEOUT_SECONDS,
                )
        finally:
            if staging.exists():
                retry_owned_windows_operation(
                    lambda: shutil.rmtree(staging), "remove private-runtime staging tree",
                    timeout_seconds=RUNTIME_PUBLISH_TIMEOUT_SECONDS,
                )
            if previous.exists() and output.exists():
                retry_owned_windows_operation(
                    lambda: shutil.rmtree(previous), "remove private-runtime rollback tree",
                    timeout_seconds=RUNTIME_PUBLISH_TIMEOUT_SECONDS,
                )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DESKTOP / "runtime")
    args = parser.parse_args(argv)
    prepared = prepare(args.output)
    print(f"Prepared private Python {PYTHON_VERSION} runtime at {prepared}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
