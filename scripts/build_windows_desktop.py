"""Serialize private-runtime publication, containment smoke, and Electron packaging."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
sys.path.insert(0, str(ROOT))

from scripts import prepare_windows_runtime as runtime  # noqa: E402
from scripts import prepare_kestra_runtime as kestra_runtime  # noqa: E402
from our_harness.distribution_gate import PRIVATE_NAMES, enforce_distribution_gate  # noqa: E402


def verify_product_source_privacy(resources: Path) -> None:
    """Reject build caches and local application state in shipped source."""
    source = resources / "harness"
    if not source.is_dir():
        raise RuntimeError("Packaged product source is missing")
    forbidden = {name.casefold() for name in {"__pycache__", "email-studio", *PRIVATE_NAMES}}
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if forbidden.intersection(part.casefold() for part in relative.parts) or item.suffix.lower() in {".pyc", ".pyo"}:
            raise RuntimeError(f"Private build or runtime state in package: {relative}")


def build(arguments: list[str] | None = None) -> Path:
    """Build while holding the publisher lease that owns the runtime selector."""

    arguments = list(arguments or [])
    if any(
        one == "-c" or one.startswith("-c=")
        or one in {"--project", "--projectDir"}
        or one.startswith("--project=") or one.startswith("--projectDir=")
        or one.startswith("--config") or "extraResources" in one
        for one in arguments
    ):
        raise RuntimeError("Private-runtime builder configuration cannot be overridden")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required to build the Nexus desktop application")
    # Every build, including post-work deployment, must pass the engine-owned
    # LangGraph gate before downloading, packaging, or replacing artifacts.
    enforce_distribution_gate(ROOT)
    with runtime.runtime_build_lock():
        kestra_runtime.prepare()
        selected = runtime._prepare_locked(DESKTOP / "runtime")
        expected_tree = runtime.runtime_tree_digest(selected)
        environment = {
            **os.environ,
            "NEXUS_PLAYWRIGHT_RUNTIME": str(selected / "playwright"),
        }
        smoke = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "smoke_bundled_playwright.py")],
            cwd=DESKTOP, env=environment, check=False,
        )
        if smoke.returncode:
            raise RuntimeError(f"Bundled Playwright containment smoke failed ({smoke.returncode})")
        command = [
            node,
            str(DESKTOP / "node_modules" / "electron-builder" / "cli.js"),
            "--config", "electron-builder.config.cjs",
            *arguments,
        ]
        packaged = subprocess.run(command, cwd=DESKTOP, env=environment, check=False)
        if packaged.returncode:
            raise RuntimeError(f"Electron desktop packaging failed ({packaged.returncode})")
        verify_product_source_privacy(DESKTOP / "build-output" / "win-unpacked" / "resources")
        packaged_runtime = DESKTOP / "build-output" / "win-unpacked" / "resources" / "runtime"
        if (
            not packaged_runtime.is_dir()
            or runtime.runtime_tree_digest(selected) != expected_tree
            or runtime.runtime_tree_digest(packaged_runtime) != expected_tree
        ):
            raise RuntimeError(
                "Packaged private runtime does not exactly match the verified selected runtime"
            )
        packaged_kestra = DESKTOP / "build-output" / "win-unpacked" / "resources" / "kestra-runtime"
        if not kestra_runtime.verify(packaged_kestra):
            raise RuntimeError("Packaged Kestra distribution differs from its pinned manifest")
        return selected


def main(argv: list[str] | None = None) -> int:
    selected = build(list(argv or []))
    print(f"Packaged Nexus with verified private runtime {selected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
