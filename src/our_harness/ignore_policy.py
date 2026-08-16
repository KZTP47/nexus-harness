from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .models import HarnessError
from .safety import confined_walk_files, filesystem_case_key


HARD_IGNORED_DIRECTORIES = {".git", ".harness", "node_modules", "__pycache__", ".venv", "venv"}
_SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore", ".cer", ".crt"}
_SECRET_NAMES = {
    "credentials", "credentials.json", "service-account.json", "service_account.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc",
}


def _glob_regex(pattern: str) -> re.Pattern[str]:
    output = ""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            output += re.escape(pattern[index + 1])
            index += 2
            continue
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                # Git's `**/` form means zero or more complete directory
                # components.  Treating it as plain `.*` incorrectly requires
                # a slash, so `a/**/secret` would miss `a/secret` and
                # `**/secret` would miss a root-level file.
                if index + 2 < len(pattern) and pattern[index + 2] == "/":
                    output += r"(?:.*/)?"
                    index += 3
                else:
                    output += ".*"
                    index += 2
                continue
            output += "[^/]*"
        elif character == "?":
            output += "[^/]"
        elif character == "[":
            closing = pattern.find("]", index + 1)
            if closing < 0:
                output += r"\["
            else:
                body = pattern[index + 1 : closing]
                negated = body.startswith(("!", "^"))
                if negated:
                    body = body[1:]
                body = body.replace("\\", r"\\").replace("]", r"\]")
                output += "[" + ("^" if negated else "") + body + "]"
                index = closing
        else:
            output += re.escape(character)
        index += 1
    return re.compile(output)


@dataclass(frozen=True)
class _IgnoreRule:
    base: str
    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool

    def matches(self, path: str) -> bool:
        compared_path = filesystem_case_key(path)
        compared_base = filesystem_case_key(self.base)
        if compared_base:
            prefix = compared_base + "/"
            if not compared_path.startswith(prefix):
                return False
            compared_path = compared_path[len(prefix) :]
        pattern = filesystem_case_key(self.pattern)
        if not pattern:
            return False
        expression = _glob_regex(pattern).pattern
        expression = (r"(?:^|.*/)" if not self.anchored and "/" not in pattern else "^") + expression
        suffix = r"(?:/.*)?$" if self.directory_only else "$"
        return bool(
            re.match(expression + suffix, compared_path)
            or re.match(expression + r"/.*$", compared_path)
        )


def _is_builtin_secret(path: str) -> bool:
    parts = [part.lower() for part in path.split("/") if part]
    if not parts:
        return False
    name = parts[-1]
    return (
        name.startswith(".env")
        or name in _SECRET_NAMES
        or Path(name).suffix.lower() in _SECRET_SUFFIXES
        or any(part in {".ssh", ".gnupg"} for part in parts)
    )


class IgnorePolicy:
    """One Git-style workspace visibility policy for indexing and discovery tools."""

    def __init__(self, root: Path, configured_ignored_names: set[str] | None = None):
        self.root = root.resolve(strict=True)
        self.configured = set(configured_ignored_names or set())
        self.hard_names = set(HARD_IGNORED_DIRECTORIES)
        self.configured_rules = [
            _IgnoreRule(
                "",
                pattern.rstrip("/").lstrip("/"),
                False,
                pattern.endswith("/"),
                pattern.startswith("/"),
            )
            for pattern in sorted(self.configured)
            if pattern and not pattern.startswith("!")
        ]
        self.rules: list[_IgnoreRule] = []
        self._load_rules(self.root)

    def _load_rules(self, directory: Path) -> None:
        base = directory.relative_to(self.root).as_posix()
        if base == ".":
            base = ""
        for name in (".gitignore", ".ignore"):
            ignore_file = directory / name
            try:
                metadata = ignore_file.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or ignore_file.is_symlink():
                continue
            try:
                lines = ignore_file.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for raw in lines:
                line = raw.rstrip()
                if not line:
                    continue
                escaped_marker = line.startswith((r"\#", r"\!"))
                if line.startswith("#") and not escaped_marker:
                    continue
                if escaped_marker:
                    line = line[1:]
                negated = line.startswith("!") and not escaped_marker
                if negated:
                    line = line[1:]
                if line:
                    self.rules.append(
                        _IgnoreRule(
                            base,
                            line.rstrip("/").lstrip("/"),
                            negated,
                            line.endswith("/"),
                            line.startswith("/"),
                        )
                    )
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            return
        for child in children:
            if child.name in self.hard_names:
                continue
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if child.is_symlink() or bool(
                getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                relative = Path(child.path).relative_to(self.root).as_posix()
                if not self.is_ignored(relative, is_directory=True):
                    self._load_rules(Path(child.path))

    def is_ignored(self, path: str | Path, *, is_directory: bool = False) -> bool:
        normalized = Path(path).as_posix().strip("/")
        if not normalized or normalized == ".":
            return False
        parts = normalized.split("/")
        if any(part in self.hard_names for part in parts) or _is_builtin_secret(normalized):
            return True
        candidate = normalized + ("/" if is_directory else "")
        if any(rule.matches(candidate) for rule in self.configured_rules):
            return True
        ignored = False
        for rule in self.rules:
            if rule.matches(candidate):
                ignored = not rule.negated
        return ignored

    def require_visible(self, path: str | Path, *, is_directory: bool = False) -> None:
        if self.is_ignored(path, is_directory=is_directory):
            raise HarnessError(f"Workspace path is ignored or secret: {Path(path).as_posix()}")

    def walk_files(self) -> Iterator[Path]:
        for path in confined_walk_files(self.root, self.hard_names):
            relative = path.relative_to(self.root).as_posix()
            if not self.is_ignored(relative):
                yield path
