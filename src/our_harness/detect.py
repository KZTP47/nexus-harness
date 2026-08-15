from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import Detection


def _node_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def _node_detection(root: Path) -> Detection | None:
    package = root / "package.json"
    if not package.is_file():
        return None
    evidence = ["package.json"]
    commands: list[list[str]] = []
    lint: list[list[str]] = []
    build: list[list[str]] = []
    manager = _node_manager(root)
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
        scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        scripts = {}
    run = [manager, "run"] if manager != "yarn" else [manager]
    if "test" in scripts:
        commands.append([*run, "test"])
    if "lint" in scripts:
        lint.append([*run, "lint"])
    if "build" in scripts:
        build.append([*run, "build"])
    stack = "typescript" if (root / "tsconfig.json").exists() else "node"
    if stack == "typescript":
        evidence.append("tsconfig.json")
    return Detection(stack, evidence, commands, lint, build, 0.95)


def detect_project(root: Path) -> list[Detection]:
    detections: list[Detection] = []
    node = _node_detection(root)
    if node:
        detections.append(node)
    python_markers = [name for name in ("pyproject.toml", "requirements.txt", "setup.py", "tox.ini") if (root / name).exists()]
    if python_markers:
        test = [["python", "-m", "pytest"]] if (root / "pytest.ini").exists() or (root / "tests").exists() else [["python", "-m", "unittest", "discover"]]
        lint = [["python", "-m", "ruff", "check", "."]] if shutil.which("ruff") else []
        detections.append(Detection("python", python_markers, test, lint, [], 0.9))
    if (root / "Cargo.toml").exists():
        detections.append(Detection("rust", ["Cargo.toml"], [["cargo", "test"]], [["cargo", "clippy", "--all-targets"]], [["cargo", "build"]], 1.0))
    if (root / "go.mod").exists():
        detections.append(Detection("go", ["go.mod"], [["go", "test", "./..."]], [["go", "vet", "./..."]], [["go", "build", "./..."]], 1.0))
    if (root / "pom.xml").exists():
        detections.append(Detection("java-maven", ["pom.xml"], [["mvn", "test"]], [], [["mvn", "package", "-DskipTests"]], 1.0))
    if (root / "gradlew").exists() or (root / "gradlew.bat").exists():
        wrapper = "gradlew.bat" if (root / "gradlew.bat").exists() else "./gradlew"
        detections.append(Detection("java-gradle", [Path(wrapper).name], [[wrapper, "test"]], [], [[wrapper, "build"]], 1.0))
    solutions = list(root.glob("*.sln"))
    projects = list(root.glob("*.csproj"))
    if solutions or projects:
        evidence = [path.name for path in (solutions + projects)[:8]]
        detections.append(Detection("dotnet", evidence, [["dotnet", "test"]], [["dotnet", "format", "--verify-no-changes"]], [["dotnet", "build"]], 0.98))
    if (root / "CMakeLists.txt").exists():
        detections.append(Detection("cmake", ["CMakeLists.txt"], [["ctest", "--test-dir", "build"]], [], [["cmake", "--build", "build"]], 0.85))
    if list(root.glob("Gemfile")) or list(root.glob("*.gemspec")):
        detections.append(Detection("ruby", ["Gemfile"], [["bundle", "exec", "rake", "test"]], [], [], 0.85))
    if not detections:
        detections.append(Detection("unknown", [], [], [], [], 0.0))
    return detections


def combined_commands(detections: list[Detection], kind: str) -> list[list[str]]:
    output: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    attribute = {"test": "test_commands", "lint": "lint_commands", "build": "build_commands"}.get(kind)
    if attribute is None:
        return []
    for detection in detections:
        for command in getattr(detection, attribute):
            key = tuple(command)
            if key not in seen:
                output.append(command)
                seen.add(key)
    return output
