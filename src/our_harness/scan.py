"""Looking through the project for credentials somebody left in the code.

The one rule that matters here: if nothing was looked at, the check fails. The
older tool this replaces had a security gate that reported success whenever its
scanner was missing or its file list came back empty, so a project with keys in
it looked clean for months. A gate that passes when it did not run is worse than
having no gate, because everybody stops looking.

So this counts what it read. No files read, no pass. Scanner not installed, no
pass. Every answer says how many files were looked at.

Nothing found is ever printed in full. A report says the file, the line, and
what kind of thing it was, with the value itself taken out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import LoadedConfig
from .models import HarnessError
from .redaction import CredentialRedactor
from .safety import confined_path

# Folders never worth reading: other people's code, build output, and the Git
# folder, which holds credentials of its own.
SKIP_FOLDERS = (
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "site-packages",
)
# Files that are not text worth reading.
SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyz", ".whl", ".mp4", ".mp3", ".woff", ".woff2",
    ".ttf", ".otf", ".class", ".jar", ".bin", ".wasm",
)
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 5000
MAX_FINDINGS = 200
# A line saying this on purpose is left alone, and counted, so nobody can hide
# a real key without it showing up in the numbers.
ALLOW_MARK = "harness: allow secret"

# What a credential looks like. Each one has a plain name, because "matched
# rule 7" tells a person nothing.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an OpenAI key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    ("a GitHub token", re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("a Slack token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{12,}")),
    ("an Amazon key", re.compile(r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("a Google key", re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9])")),
    ("a private key file", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a signed web token", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("a password or key written into the code", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|client[_-]?secret)\b\s*[:=]\s*"
        r"['\"][^'\"\s]{8,}['\"]"
    )),
    ("an address with a password in it", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s:@]{3,}@")),
)
# Things that look like a key but are somebody saying "put your key here".
_OBVIOUS_EXAMPLES = re.compile(
    r"(?i)(your[_-]?key|example|placeholder|changeme|xxxx|dummy|sample|redacted|\.\.\.|<[^>]+>|"
    r"\$\{[^}]+\}|%[A-Z_]+%|process\.env|os\.environ|getenv)"
)


class ScanError(HarnessError):
    """A problem with the scan itself, which the user can fix."""


@dataclass(frozen=True)
class Finding:
    """One place worth looking at, with the value itself taken out."""

    path: str
    line: int
    kind: str
    excerpt: str
    allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "kind": self.kind,
            "excerpt": self.excerpt,
            "allowed": self.allowed,
        }

    def sentence(self) -> str:
        return f"{self.path} line {self.line} holds {self.kind}: {self.excerpt}"


@dataclass(frozen=True)
class Report:
    """What the scan looked at and what it found."""

    findings: tuple[Finding, ...]
    files_read: int
    files_skipped: int
    stopped_early: bool = False

    @property
    def real(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if not item.allowed)

    @property
    def allowed(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.allowed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [item.to_dict() for item in self.findings],
            "files_read": self.files_read,
            "files_skipped": self.files_skipped,
            "stopped_early": self.stopped_early,
        }


def looks_like_an_example(line: str) -> bool:
    """True when the line is plainly showing somebody where to put their key."""

    return bool(_OBVIOUS_EXAMPLES.search(line))


def covered(line: str, pattern: re.Pattern[str], redactor: CredentialRedactor) -> str:
    """The line with the thing that was found taken out of it.

    A report is read by people, pasted into messages, and kept in run folders,
    so the value itself must never appear in one. What was found is covered
    first, then the usual credential remover runs over what is left, in case
    the same line holds something else.
    """

    return redactor.text(pattern.sub("[REDACTED]", line))[:200]


def scan_text(text: str, path: str, redactor: CredentialRedactor | None = None) -> list[Finding]:
    """Every credential-shaped thing in one file, with the value removed."""

    hide = redactor or CredentialRedactor()
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 2000:
            # A single enormous line is usually packed or generated code. Look
            # at the start of it rather than skipping it altogether.
            line = line[:2000]
        for kind, pattern in PATTERNS:
            found_here = pattern.search(line)
            if not found_here:
                continue
            # Judge the thing that matched, not the whole line. A real key on a
            # line that also says the word "sample" or "os.environ" somewhere
            # else is still a real key.
            if looks_like_an_example(found_here.group(0)):
                break
            found.append(
                Finding(
                    path=path,
                    line=number,
                    kind=kind,
                    excerpt=covered(line.strip(), pattern, hide),
                    allowed=ALLOW_MARK in line,
                )
            )
            break
    return found


def _worth_reading(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    parts = {part.lower() for part in path.parts}
    return not parts & {folder.lower() for folder in SKIP_FOLDERS}


def as_pattern(glob: str) -> re.Pattern[str]:
    """Turn a file pattern into something that can be matched against a path.

    `**` stands for any number of folders, `*` for anything inside one folder
    name, and `?` for one character. Everything else is taken as it is written,
    so a dot is a dot and nothing in a pattern is ever treated as code.
    """

    out = ["^"]
    index = 0
    text = glob.replace("\\", "/")
    while index < len(text):
        char = text[index]
        if text.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif text.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append("$")
    return re.compile("".join(out))


def _files_under(root: Path) -> list[str]:
    """Every file worth reading, without walking into folders we never read.

    Reading the whole tree first and filtering afterwards is what makes this
    slow on real projects: one `node_modules` can hold hundreds of thousands of
    files that were never going to be looked at.
    """

    import os

    found: list[str] = []
    skip = {folder.lower() for folder in SKIP_FOLDERS}
    for here, folders, names in os.walk(root):
        folders[:] = sorted(name for name in folders if name.lower() not in skip)
        base = Path(here)
        for name in sorted(names):
            if Path(name).suffix.lower() in SKIP_SUFFIXES:
                continue
            found.append((base / name).relative_to(root).as_posix())
    return found


def scan_project(
    config: LoadedConfig,
    *,
    include: Sequence[str] = ("**/*",),
    skip: Iterable[str] = (),
    max_files: int = MAX_FILES,
) -> Report:
    """Read the project's own text files and report what is in them."""

    root = config.project_root
    hide = CredentialRedactor(config)
    wanted: list[re.Pattern[str]] = []
    for pattern in include:
        text = str(pattern)
        # A path starting with a slash is not "inside the project" on any
        # system, but Windows only calls it absolute once it has a drive letter,
        # so both shapes are refused here by hand.
        if (
            Path(text).is_absolute()
            or text.startswith(("/", "\\"))
            or ":" in text
            or ".." in re.split(r"[\\/]", text)
        ):
            raise ScanError(f"{pattern} must be a path inside the project")
        wanted.append(as_pattern(text))
    leave_alone = [as_pattern(str(item)) for item in skip]
    chosen = [
        name for name in _files_under(root) if any(rule.match(name) for rule in wanted)
    ]
    findings: list[Finding] = []
    read = 0
    skipped = 0
    stopped = False
    for relative in chosen:
        item = root / relative
        if any(rule.match(relative) for rule in leave_alone):
            skipped += 1
            continue
        if item.is_symlink():
            skipped += 1
            continue
        if read >= max_files:
            stopped = True
            break
        try:
            if item.stat().st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            text = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        read += 1
        findings.extend(scan_text(text, relative, hide))
        if len(findings) >= MAX_FINDINGS:
            stopped = True
            break
    return Report(
        findings=tuple(findings[:MAX_FINDINGS]),
        files_read=read,
        files_skipped=skipped,
        stopped_early=stopped,
    )


def reasons(report: Report, max_findings: int = 0) -> list[str]:
    """Why the scan failed, in plain sentences. Reading nothing is a failure."""

    lines: list[str] = []
    if report.files_read <= 0:
        # This is the whole point of the check. Nothing read means nothing
        # known, and nothing known must never look like nothing wrong.
        lines.append(
            "No files were read, so nothing was checked. "
            "A security check that did not run must not pass."
        )
        return lines
    real = report.real
    if len(real) > max_findings:
        things = "thing that looks" if len(real) == 1 else "things that look"
        files = "file" if report.files_read == 1 else "files"
        lines.append(
            f"Found {len(real)} {things} like credentials in {report.files_read} {files}, "
            f"more than the {max_findings} allowed."
        )
        lines.extend(f"  {item.sentence()}" for item in real[:10])
        if len(real) > 10:
            lines.append(f"  and {len(real) - 10} more")
    if report.stopped_early:
        lines.append(
            "The scan stopped early, so this is not the whole project. "
            "Narrow it down with paths, or fix what was found first."
        )
    return lines
