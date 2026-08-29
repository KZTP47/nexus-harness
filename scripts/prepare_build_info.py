"""Write truthful build identity consumed by the packaged desktop app."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "desktop" / "build-info.json"


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments], capture_output=True,
        text=True, timeout=20, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def main() -> int:
    commit = git("rev-parse", "HEAD") or "unknown"
    dirty = bool(git("status", "--porcelain"))
    signed = os.environ.get("NEXUS_SIGNED_BUILD") == "1"
    unsigned_release = os.environ.get("NEXUS_UNSIGNED_RELEASE") == "1"
    if signed and unsigned_release:
        raise RuntimeError("A Nexus build cannot be both signed and explicitly unsigned")
    build_kind = (
        "signed release" if signed
        else "unsigned release" if unsigned_release
        else "unsigned development build"
    )
    value = {
        "commit": commit,
        "dirty": dirty,
        "build_kind": build_kind,
    }
    OUTPUT.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared {value['build_kind']} identity {commit}{'+dirty' if dirty else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
