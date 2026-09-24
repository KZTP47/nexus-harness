from __future__ import annotations

import ast
import json
import os
import re
import subprocess
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
TEXT_SUFFIXES = {
    ".bat", ".cmd", ".cjs", ".css", ".html", ".js", ".json", ".md", ".ps1",
    ".py", ".sh", ".toml", ".yaml", ".yml",
}
EXCLUDED_PARTS = {
    ".git", ".venv", "dist", "build", "tests", "__pycache__",
    # Third-party trees are not ours to rewrite, and the harness never ships them.
    "node_modules", ".harness", "benchmark-archive", "benchmark-logs",
    # Generated, ignored release input. Its exact locked distributions and
    # imports are validated by prepare_windows_runtime.py; scanning vendor
    # source as if Nexus authored it creates false machine-path findings.
    "runtime", ".runtime-published", "kestra-runtime",
    # Local verification receipts can contain account state and are not shipped.
    "reports",
    # What a build put there, including the copy of this very code that the
    # desktop app carries. Read as source, the audit was reading its own output
    # and telling us off for it.
    "build-output", "win-unpacked",
}
RECORDED_AUDIT_NOTES = {"docs/AUDIT.md", "src/our_harness/audit.py", "our_harness/audit.py"}
# Repository-local agent policy is consumed by development tools and is never
# included in either the Python distribution or the Electron resources.  It
# can legitimately bind this checkout to private local resources, so scanning
# it as shipped application content produces a false portability failure.
NON_DISTRIBUTABLE_PROJECT_FILES = {"AGENTS.md", "CLAUDE.md", ".ai-project.json"}


def _inspect_text(label: str, text: str, findings: list[dict[str, str]]) -> bool:
    syntax_ok = True
    for number, line in enumerate(text.splitlines(), 1):
        path_material = line.replace("/workspace/{relative_cwd}", "")
        if "re.compile(" in path_material or "re.sub(" in path_material:
            path_material = ""
        if any(pattern.search(path_material) for pattern in ABSOLUTE_PATTERNS):
            findings.append({"path": label, "line": str(number), "message": "machine-specific absolute path"})
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


# Bound at import: a build script that fakes its own ``subprocess.run`` calls
# (node, electron-builder) must not answer, or swallow the answers meant for,
# the audit's Git query.
_run_git = subprocess.run


def _git_ignored(root: Path) -> set[str] | None:
    """Paths under ``root`` that Git ignores, or None when Git cannot say.

    Ignored directories are reported once, with a trailing slash. Anything
    that stops Git from answering (no Git, not a work tree, a timeout)
    returns None; the caller then skips nothing, so the audit fails closed.
    """
    try:
        inside = _run_git(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        listed = _run_git(
            ["git", "-C", str(root), "ls-files", "-z", "--others", "--ignored",
             "--exclude-standard", "--directory", "--", "."],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    return {
        entry for entry in listed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if entry
    }


def _glob_pattern(pattern: str) -> re.Pattern[str]:
    """An electron-builder style glob (``**``, ``*``, ``?``) as a regex."""

    out = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
        elif pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif pattern[index] == "*":
            out += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(pattern[index])
            index += 1
    return re.compile(out)


def _joined(base: str, relative: str) -> str | None:
    """``relative`` resolved from the repository-relative folder ``base``."""

    parts = [part for part in base.split("/") if part]
    for part in relative.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


class _ShippedTrees:
    """What the packagers copy straight from the working tree.

    They copy files, not Git's index: ``scripts/build_zipapp.py`` copies
    ``src`` minus its own ignore patterns, and electron-builder copies
    ``desktop/package.json``'s ``build.files`` plus every ``extraResources``
    source through that entry's ``filter``. A file Git ignores inside what one
    of them copies still ships.
    """

    # scripts/build_zipapp.py: shutil.ignore_patterns("__pycache__", "*.pyc",
    # "*.pyo", "*.egg-info", "build", "dist") applied to every path component.
    _ZIPAPP_LEAVES_OUT = re.compile(
        r"(?:.*/)?(?:__pycache__|[^/]*\.egg-info|build|dist)(?:/.*)?|(?:.*/)?[^/]*\.py[co]"
    )

    def __init__(self, root: Path) -> None:
        # (folder prefix, filter patterns that must match, patterns that must not)
        self.copies: list[tuple[str, list[re.Pattern[str]], list[re.Pattern[str]]]] = [
            ("src/", [], [self._ZIPAPP_LEAVES_OUT]),
        ]
        self.exact: set[str] = set()
        self.included: list[re.Pattern[str]] = []
        self.excluded: list[re.Pattern[str]] = []
        self.literal_starts: list[str] = []
        try:
            manifest = json.loads((root / "desktop" / "package.json").read_text(encoding="utf-8"))
            build = manifest.get("build") if isinstance(manifest, dict) else None
        except (OSError, ValueError):
            build = None
        if not isinstance(build, dict):
            # Without a readable desktop manifest, still count what it has
            # always shipped: all of ``src`` and two files outside it.
            self.copies.append(("src/", [], []))
            self.exact.update({"scripts/harness.py", "THIRD_PARTY_NOTICES.md"})
            return
        for entry in build.get("files") or []:
            if not isinstance(entry, str) or not entry:
                continue
            negated = entry.startswith("!")
            pattern = _joined("desktop", entry[1:] if negated else entry)
            if pattern is None:
                continue
            (self.excluded if negated else self.included).append(_glob_pattern(pattern))
            if not negated:
                self.literal_starts.append(re.split(r"[*?]", pattern, maxsplit=1)[0])
        for entry in build.get("extraResources") or []:
            source = entry.get("from") if isinstance(entry, dict) else entry
            if not isinstance(source, str) or not source:
                continue
            target = _joined("desktop", source)
            if not target:
                continue
            if not (root / target).is_dir():
                self.exact.add(target)
                continue
            wanted: list[re.Pattern[str]] = []
            unwanted: list[re.Pattern[str]] = []
            rules = entry.get("filter") if isinstance(entry, dict) else None
            for rule in ([rules] if isinstance(rules, str) else rules or []):
                if isinstance(rule, str) and rule:
                    if rule.startswith("!"):
                        unwanted.append(_glob_pattern(rule[1:]))
                    else:
                        wanted.append(_glob_pattern(rule))
            self.copies.append((f"{target}/", wanted, unwanted))

    def ships(self, label: str) -> bool:
        if label in self.exact:
            return True
        for prefix, wanted, unwanted in self.copies:
            if not label.startswith(prefix):
                continue
            inside = label[len(prefix):]
            if ((not wanted or any(pattern.fullmatch(inside) for pattern in wanted))
                    and not any(pattern.fullmatch(inside) for pattern in unwanted)):
                return True
        return (any(pattern.fullmatch(label) for pattern in self.included)
                and not any(pattern.fullmatch(label) for pattern in self.excluded))

    def may_contain(self, folder: str) -> bool:
        """Whether a shipped file could lie inside ``folder`` (ending in ``/``)."""

        starts = [*(prefix for prefix, _wanted, _unwanted in self.copies),
                  *self.exact, *self.literal_starts]
        return any(one.startswith(folder) or folder.startswith(one) for one in starts)


# Ignored on purpose and shipped on purpose: the exact commit/build label the
# release build writes from tracked sources. Still scanned for machine paths.
GENERATED_SHIPPED_INPUTS = {"desktop/build-info.json"}


def _is_ignored(label: str, ignored: set[str]) -> bool:
    if label in ignored:
        return True
    parts = label.rstrip("/").split("/")
    return any("/".join(parts[:count]) + "/" in ignored for count in range(1, len(parts)))


def audit_distribution(root: Path) -> dict[str, Any]:
    """Audit what a release built from this working tree could carry.

    Everything is scanned except the generated and third-party trees named in
    ``EXCLUDED_PARTS``. In a Git checkout, files Git ignores are skipped only
    outside the trees the packagers copy (``_ShippedTrees``): a scratch clone
    at the repository root never ships. An ignored file inside a shipped tree
    (a local ``.env``, a dump under ``src``) does ship, so it is still scanned
    and also reported as "ignored file would ship". When Git cannot say what
    is ignored, nothing is skipped.
    """

    root = root.resolve()
    findings: list[dict[str, str]] = []
    package_root = root / "src" / "our_harness"
    if not package_root.is_dir():
        findings.append({"path": str(package_root), "line": "0", "message": "source package scan root is absent"})
        return _result("source", 0, False, findings)
    scanned_files = 0
    syntax_ok = True
    ignored = _git_ignored(root)
    shipped = _ShippedTrees(root)
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root).as_posix()
        prefix = "" if relative == "." else f"{relative}/"
        subdirectories[:] = [
            name for name in subdirectories
            if name not in EXCLUDED_PARTS and not (
                ignored is not None
                and _is_ignored(f"{prefix}{name}/", ignored)
                and not shipped.may_contain(f"{prefix}{name}/")
            )
        ]
        for filename in filenames:
            path = Path(directory) / filename
            if not path.is_file():
                continue
            label = path.relative_to(root).as_posix()
            if ignored is not None and _is_ignored(label, ignored):
                if not shipped.ships(label):
                    continue
                if label not in GENERATED_SHIPPED_INPUTS:
                    findings.append({"path": label, "line": "0", "message": "ignored file would ship"})
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if (label in RECORDED_AUDIT_NOTES or label in NON_DISTRIBUTABLE_PROJECT_FILES
                    or filename.endswith((".test.js", ".test.cjs"))):
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
