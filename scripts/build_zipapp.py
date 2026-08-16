from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the harness zipapp")
    parser.add_argument("--output", default="dist/harness.pyz")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "src"
    output = (root / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="harness-zipapp-") as temporary:
        stage = Path(temporary) / "app"
        shutil.copytree(
            source,
            stage,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.egg-info", "build", "dist"),
        )
        zipapp.create_archive(stage, output, interpreter="/usr/bin/env python3", main="our_harness.cli:main", compressed=True)
    print(f"Built {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
