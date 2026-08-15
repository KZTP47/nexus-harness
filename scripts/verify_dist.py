from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path


def package_files(source: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        result[path.relative_to(source.parent).as_posix()] = path.read_bytes()
    return result


def verify_archive(path: Path, expected: dict[str, bytes]) -> None:
    if not path.is_file():
        raise SystemExit(f"Missing distribution artifact: {path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        unexpected = sorted(
            name
            for name in names
            if name.startswith("our_harness/") and not name.endswith("/") and name not in expected
        )
        if unexpected:
            raise SystemExit(f"{path.name} contains unexpected package files: {', '.join(unexpected)}")
        for name, content in expected.items():
            if name not in names:
                raise SystemExit(f"{path.name} is missing {name}")
            if archive.read(name) != content:
                raise SystemExit(f"{path.name} contains a stale {name}")


def run_probe(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or f"Probe failed: {command}")


def run_benchmark_probe(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or f"Benchmark probe failed: {command}")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Benchmark probe did not return JSON: {exc}") from exc
    if report.get("deterministic_score") != 100 or report.get("case_summary", {}).get("failed") != 0:
        failures = ", ".join(report.get("critical_failures", [])) or "non-critical case failure"
        raise SystemExit(f"Benchmark probe failed its deterministic contract: {failures}")
    if report.get("agentic_score") != "not_run":
        raise SystemExit("Provider-free benchmark probe unexpectedly ran an agentic provider")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify built harness artifacts against current package sources")
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dist = (root / args.dist).resolve()
    expected = package_files(root / "src" / "our_harness")
    pyz = dist / "harness.pyz"
    wheels = sorted(dist.glob("our_harness_cli-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one harness wheel in the distribution directory")
    wheel = wheels[0]
    verify_archive(pyz, expected)
    verify_archive(wheel, expected)
    for command in (
        [sys.executable, str(pyz), "--version"],
        [sys.executable, str(pyz), "runs", "--help"],
        [sys.executable, str(pyz), "benchmark", "--help"],
    ):
        run_probe(command, root)
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import our_harness.agent_tools, our_harness.review_panel, our_harness.runstate; "
        "from our_harness.workflow import HarnessApplication; "
        "assert hasattr(HarnessApplication, 'resume_task'); assert hasattr(HarnessApplication, 'runtime_metrics')"
    )
    run_probe([sys.executable, "-I", "-c", probe, str(wheel)], root)
    wheel_benchmark = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); "
        "from our_harness.benchmark import run_benchmark; "
        "print(json.dumps(run_benchmark(seed=20260814)))"
    )
    run_benchmark_probe(
        [sys.executable, "-B", str(pyz), "benchmark", "--seed", "20260814", "--format", "json"],
        root,
    )
    run_benchmark_probe([sys.executable, "-B", "-I", "-c", wheel_benchmark, str(wheel)], root)
    print(f"Verified {len(expected)} package files in {pyz.name} and {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
