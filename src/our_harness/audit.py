from __future__ import annotations

import ast
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable


ABSOLUTE_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"),
    # A UNC server/share begins with filesystem-name characters.  Requiring
    # those prevents escaped source-code fragments such as ``'\\\\','/'``
    # from being mistaken for machine paths while still finding real UNC roots.
    re.compile(r"(?<![\\/])\\\\[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_$.-]+"),
    re.compile(
        r"(?<![:A-Za-z0-9_])/(?:Users|home|root|tmp|var|opt|srv|mnt|media|Volumes|workspace|workspaces|project|projects|repo|repos)(?:/[^\s'\"`]+)+",
        re.IGNORECASE,
    ),
]
# Names that would mean this package still points at the project it was built
# beside, rather than standing on its own.
SOURCE_BINDINGS = ("RPG Maker", "RGSS")
# The account name is only a problem when it appears as part of a path, which
# is a leftover from one machine. In a repository address it is the publisher,
# written on purpose, and every public project has one.
ACCOUNT_IN_A_PATH = re.compile(r"[\/]KZTP47(?=[\/]|$)", re.IGNORECASE)
_AN_ADDRESS = re.compile(r"[a-z][a-z0-9+.\-]*://\S*", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".json", ".md", ".toml", ".ps1", ".sh", ".html", ".css", ".js"}
EXCLUDED_PARTS = {
    ".git", ".venv", "dist", "build", "tests", "__pycache__",
    # Third-party trees are not ours to rewrite, and the harness never ships them.
    "node_modules", ".harness", "benchmark-archive", "benchmark-logs",
    # Generated, ignored release input. Its exact locked distributions and
    # imports are validated by prepare_windows_runtime.py; scanning vendor
    # source as if Nexus authored it creates false machine-path findings.
    "runtime",
    # What a build put there, including the copy of this very code that the
    # desktop app carries. Read as source, the audit was reading its own output
    # and telling us off for it.
    "build-output", "win-unpacked",
}
RECORDED_AUDIT_NOTES = {"docs/AUDIT.md", "src/our_harness/audit.py", "our_harness/audit.py"}


def _inspect_text(label: str, text: str, findings: list[dict[str, str]]) -> bool:
    syntax_ok = True
    for number, line in enumerate(text.splitlines(), 1):
        path_material = line.replace("/workspace/{relative_cwd}", "")
        if "re.compile(" in path_material or "re.sub(" in path_material:
            path_material = ""
        if any(pattern.search(path_material) for pattern in ABSOLUTE_PATTERNS):
            findings.append({"path": label, "line": str(number), "message": "machine-specific absolute path"})
        if any(binding in line for binding in SOURCE_BINDINGS):
            findings.append({"path": label, "line": str(number), "message": "source-project binding"})
        # A web address is not a folder on anybody's machine, so the part after
        # the host is not a path and is not read as one.
        without_addresses = _AN_ADDRESS.sub(" ", path_material)
        if ACCOUNT_IN_A_PATH.search(without_addresses):
            findings.append({"path": label, "line": str(number), "message": "machine-specific account path"})
    if label.endswith(".py"):
        try:
            ast.parse(text, filename=label)
        except SyntaxError as exc:
            syntax_ok = False
            findings.append({"path": label, "line": str(exc.lineno or 0), "message": f"Python syntax error: {exc.msg}"})
    return syntax_ok


def _result(mode: str, scanned_files: int, syntax_ok: bool, findings: list[dict[str, str]]) -> dict[str, Any]:
    if scanned_files == 0:
        findings.append({"path": mode, "line": "0", "message": "audit scan found no distributable files"})
    return {
        "passed": scanned_files > 0 and syntax_ok and not findings,
        "python_compiled": syntax_ok,
        "mode": mode,
        "scanned_files": scanned_files,
        "findings": findings,
    }


def audit_distribution(root: Path) -> dict[str, Any]:
    root = root.resolve()
    findings: list[dict[str, str]] = []
    package_root = root / "src" / "our_harness"
    if not package_root.is_dir():
        findings.append({"path": str(package_root), "line": "0", "message": "source package scan root is absent"})
        return _result("source", 0, False, findings)
    scanned_files = 0
    syntax_ok = True
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        label = path.relative_to(root).as_posix()
        if label in RECORDED_AUDIT_NOTES:
            continue
        scanned_files += 1
        syntax_ok = _inspect_text(label, path.read_text(encoding="utf-8", errors="replace"), findings) and syntax_ok
    return _result("source", scanned_files, syntax_ok, findings)


def _resource_files(root: Any, prefix: str = "our_harness") -> Iterable[tuple[str, Any]]:
    for child in root.iterdir():
        label = f"{prefix}/{child.name}"
        if child.is_dir():
            if child.name not in EXCLUDED_PARTS and not child.name.endswith(".egg-info"):
                yield from _resource_files(child, label)
        elif child.is_file() and Path(child.name).suffix.lower() in TEXT_SUFFIXES:
            yield label, child


def audit_installed_distribution() -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    scanned_files = 0
    syntax_ok = True
    try:
        package_root = files("our_harness")
        resources = _resource_files(package_root)
        for label, resource in resources:
            if label in RECORDED_AUDIT_NOTES:
                continue
            scanned_files += 1
            try:
                text = resource.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                findings.append({"path": label, "line": "0", "message": f"cannot read installed resource: {exc}"})
                syntax_ok = False
                continue
            syntax_ok = _inspect_text(label, text, findings) and syntax_ok
    except (ModuleNotFoundError, OSError) as exc:
        findings.append({"path": "our_harness", "line": "0", "message": f"installed package scan is unavailable: {exc}"})
        syntax_ok = False
    return _result("installed", scanned_files, syntax_ok, findings)
