"""Test lab: plain-language checks that run with or without a model.

A QA suite is a JSON file listing cases. Every case says what to do and what a
good result looks like. The runner executes cases in parallel, retries the ones
that are allowed to retry, marks a case that only passes after a retry as flaky,
and writes evidence to disk. Nothing here needs a provider. The generation
helpers at the bottom let a model propose new cases, but a proposal is stored as
a candidate and only becomes part of the suite after an explicit accept.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import re
import socket
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import LoadedConfig
from .execution import CommandRunner
from .models import HarnessError
from .safety import confined_path

SUITE_SCHEMA_VERSION = 1
CASE_KINDS = ("command", "file", "http", "browser")
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_FLAKY = "flaky"
STATUS_SKIPPED = "skipped"

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_MAX_CASES = 1000
_MAX_TITLE_CHARS = 200
_MAX_TAGS = 12
_MAX_EXPECT_STRINGS = 32
_MAX_EXPECT_STRING_CHARS = 2000
_MAX_HEADERS = 16
_MAX_BODY_CHARS = 200_000
_MAX_RETRIES = 5
_MAX_WORKERS = 32


class QaError(HarnessError):
    """A suite, candidate, or report problem that the user can fix."""


class QaSkipped(HarnessError):
    """A case cannot run here for a stated reason, and is not counted as a failure."""


@dataclass(frozen=True)
class QaExpectation:
    """What a passing case looks like. Every field is optional."""

    exit_code: int | None = None
    max_duration_ms: int | None = None
    stdout_contains: tuple[str, ...] = ()
    stdout_not_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()
    stderr_not_contains: tuple[str, ...] = ()
    exists: bool | None = None
    contains: tuple[str, ...] = ()
    not_contains: tuple[str, ...] = ()
    min_bytes: int | None = None
    max_bytes: int | None = None
    status: int | None = None
    body_contains: tuple[str, ...] = ()
    body_not_contains: tuple[str, ...] = ()
    json_fields: tuple[tuple[str, Any], ...] = ()
    max_console_errors: int | None = None
    max_page_errors: int | None = None
    max_failed_requests: int | None = None
    max_accessibility_problems: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name in (
            "exit_code", "max_duration_ms", "exists", "min_bytes", "max_bytes", "status",
            "max_console_errors", "max_page_errors", "max_failed_requests",
            "max_accessibility_problems",
        ):
            found = getattr(self, name)
            if found is not None:
                value[name] = found
        for name in (
            "stdout_contains", "stdout_not_contains", "stderr_contains", "stderr_not_contains",
            "contains", "not_contains", "body_contains", "body_not_contains",
        ):
            found = getattr(self, name)
            if found:
                value[name] = list(found)
        if self.json_fields:
            value["json_fields"] = {key: item for key, item in self.json_fields}
        return value


@dataclass(frozen=True)
class QaCase:
    index: int
    id: str
    title: str
    kind: str
    expect: QaExpectation
    tags: tuple[str, ...] = ()
    retries: int = 0
    timeout_seconds: float = 0.0
    command: tuple[str, ...] = ()
    cwd: str = "."
    stdin: str = ""
    path: str = ""
    url: str = ""
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: str = ""
    routes: tuple[str, ...] = ()
    viewport: tuple[int, int] = (1280, 800)
    click_all: bool = False
    check_accessibility: bool = False
    steps: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"id": self.id, "title": self.title, "kind": self.kind}
        if self.tags:
            value["tags"] = list(self.tags)
        if self.retries:
            value["retries"] = self.retries
        if self.timeout_seconds:
            value["timeout_seconds"] = self.timeout_seconds
        if self.kind == "command":
            value["command"] = list(self.command)
            if self.cwd != ".":
                value["cwd"] = self.cwd
            if self.stdin:
                value["stdin"] = self.stdin
        elif self.kind == "file":
            value["path"] = self.path
        elif self.kind == "http":
            value["url"] = self.url
            value["method"] = self.method
            if self.headers:
                value["headers"] = {name: item for name, item in self.headers}
            if self.body:
                value["body"] = self.body
        elif self.kind == "browser":
            value["url"] = self.url
            if self.routes:
                value["routes"] = list(self.routes)
            value["viewport"] = {"width": self.viewport[0], "height": self.viewport[1]}
            if self.click_all:
                value["click_all"] = True
            if self.check_accessibility:
                value["check_accessibility"] = True
            if self.steps:
                value["steps"] = [dict(step) for step in self.steps]
        expect = self.expect.to_dict()
        if expect:
            value["expect"] = expect
        return value


@dataclass(frozen=True)
class QaSuite:
    name: str
    cases: tuple[QaCase, ...]
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SUITE_SCHEMA_VERSION,
            "name": self.name,
            "cases": [case.to_dict() for case in self.cases],
        }

    def tags(self) -> tuple[str, ...]:
        found: list[str] = []
        for case in self.cases:
            for tag in case.tags:
                if tag not in found:
                    found.append(tag)
        return tuple(sorted(found))


@dataclass(frozen=True)
class QaAttempt:
    number: int
    passed: bool
    duration_ms: int
    reasons: tuple[str, ...] = ()
    evidence: str = ""
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "reasons": list(self.reasons),
            "evidence": self.evidence,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class QaCaseResult:
    id: str
    title: str
    kind: str
    status: str
    duration_ms: int
    attempts: tuple[QaAttempt, ...] = ()
    tags: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status in (STATUS_PASSED, STATUS_FLAKY, STATUS_SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "tags": list(self.tags),
            "reasons": list(self.reasons),
            "artifacts": list(self.artifacts),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


@dataclass(frozen=True)
class QaRunResult:
    run_id: str
    suite_name: str
    started_at: str
    duration_ms: int
    workers: int
    cases: tuple[QaCaseResult, ...]
    selected_tags: tuple[str, ...] = ()
    selected_ids: tuple[str, ...] = ()
    artifacts_dir: str = ""

    @property
    def counts(self) -> dict[str, int]:
        totals = {
            STATUS_PASSED: 0, STATUS_FAILED: 0, STATUS_FLAKY: 0, STATUS_SKIPPED: 0,
        }
        for case in self.cases:
            totals[case.status] = totals.get(case.status, 0) + 1
        totals["total"] = len(self.cases)
        return totals

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "suite": self.suite_name,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "workers": self.workers,
            "passed": self.passed,
            "counts": self.counts,
            "selected_tags": list(self.selected_tags),
            "selected_ids": list(self.selected_ids),
            "artifacts_dir": self.artifacts_dir,
            "cases": [case.to_dict() for case in self.cases],
        }


# ---------------------------------------------------------------------------
# Suite parsing
# ---------------------------------------------------------------------------


def _control_path(root: Path, relative: str) -> Path:
    """Resolve a harness-owned path under the project without leaving it."""

    return confined_path(root, relative, allow_missing=True, allow_control=True)


def _require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QaError(f"{label} must be an object")
    return value


def _require_text(value: object, label: str, *, allow_empty: bool = False, limit: int = 4000) -> str:
    if not isinstance(value, str):
        raise QaError(f"{label} must be text")
    if not allow_empty and not value.strip():
        raise QaError(f"{label} must not be empty")
    if len(value) > limit:
        raise QaError(f"{label} must be at most {limit} characters")
    return value


def _require_whole_number(value: object, label: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise QaError(f"{label} must be a whole number")
    if not minimum <= value <= maximum:
        raise QaError(f"{label} must be between {minimum} and {maximum}")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise QaError(f"{label} must be a list of text values")
    if len(value) > _MAX_EXPECT_STRINGS:
        raise QaError(f"{label} must hold at most {_MAX_EXPECT_STRINGS} values")
    return tuple(
        _require_text(item, f"{label} entry", limit=_MAX_EXPECT_STRING_CHARS) for item in value
    )


_EXPECT_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "command": frozenset({
        "exit_code", "max_duration_ms", "stdout_contains", "stdout_not_contains",
        "stderr_contains", "stderr_not_contains",
    }),
    "file": frozenset({"exists", "contains", "not_contains", "min_bytes", "max_bytes"}),
    "http": frozenset({
        "status", "max_duration_ms", "body_contains", "body_not_contains", "json_fields",
    }),
    "browser": frozenset({
        "max_duration_ms", "body_contains", "body_not_contains", "max_console_errors",
        "max_page_errors", "max_failed_requests", "max_accessibility_problems",
    }),
}


def _parse_expectation(value: object, kind: str, label: str) -> QaExpectation:
    data = _require_object(value if value is not None else {}, label)
    allowed = _EXPECT_FIELDS_BY_KIND[kind]
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise QaError(
            f"{label}.{unknown[0]} is not a check that a {kind} case understands. "
            f"Use one of: {', '.join(sorted(allowed))}"
        )
    fields: dict[str, Any] = {}
    if "exit_code" in data:
        fields["exit_code"] = _require_whole_number(data["exit_code"], f"{label}.exit_code", 0, 255)
    if "status" in data:
        fields["status"] = _require_whole_number(data["status"], f"{label}.status", 100, 599)
    if "max_duration_ms" in data:
        fields["max_duration_ms"] = _require_whole_number(
            data["max_duration_ms"], f"{label}.max_duration_ms", 1, 86_400_000
        )
    for name in ("min_bytes", "max_bytes"):
        if name in data:
            fields[name] = _require_whole_number(data[name], f"{label}.{name}", 0, 1_000_000_000)
    for name in (
        "max_console_errors", "max_page_errors", "max_failed_requests",
        "max_accessibility_problems",
    ):
        if name in data:
            fields[name] = _require_whole_number(data[name], f"{label}.{name}", 0, 10_000)
    if fields.get("min_bytes") is not None and fields.get("max_bytes") is not None:
        if fields["min_bytes"] > fields["max_bytes"]:
            raise QaError(f"{label}.min_bytes must not be larger than {label}.max_bytes")
    if "exists" in data:
        if not isinstance(data["exists"], bool):
            raise QaError(f"{label}.exists must be true or false")
        fields["exists"] = data["exists"]
    for name in (
        "stdout_contains", "stdout_not_contains", "stderr_contains", "stderr_not_contains",
        "contains", "not_contains", "body_contains", "body_not_contains",
    ):
        if name in data:
            fields[name] = _string_list(data[name], f"{label}.{name}")
    if "json_fields" in data:
        mapping = _require_object(data["json_fields"], f"{label}.json_fields")
        if len(mapping) > _MAX_EXPECT_STRINGS:
            raise QaError(f"{label}.json_fields must hold at most {_MAX_EXPECT_STRINGS} entries")
        pairs: list[tuple[str, Any]] = []
        for key, item in mapping.items():
            _require_text(key, f"{label}.json_fields key", limit=200)
            if isinstance(item, (dict, list)):
                raise QaError(
                    f"{label}.json_fields.{key} must be text, a number, true, false, or null"
                )
            pairs.append((key, item))
        fields["json_fields"] = tuple(pairs)
    return QaExpectation(**fields)


_CASE_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "command": frozenset({"command", "cwd", "stdin"}),
    "file": frozenset({"path"}),
    "http": frozenset({"url", "method", "headers", "body"}),
    "browser": frozenset({"url", "routes", "viewport", "click_all", "check_accessibility", "steps"}),
}

# What a written-down browser step may ask the page to do. Each one names the
# fields it needs, so a mistake is caught while the suite is read, not mid-run.
STEP_ACTIONS: dict[str, frozenset[str]] = {
    "click": frozenset({"target"}),
    "type": frozenset({"target", "text"}),
    "press": frozenset({"target", "key"}),
    "choose": frozenset({"target", "value"}),
    "expect_text": frozenset({"target", "text"}),
    "expect_visible": frozenset({"target"}),
    "expect_hidden": frozenset({"target"}),
    "wait": frozenset({"ms"}),
}
_MAX_STEPS = 60
_COMMON_CASE_FIELDS = frozenset({"id", "title", "kind", "tags", "retries", "timeout_seconds", "expect"})


def _relative_project_path(value: object, label: str) -> str:
    """Refuse a path that leaves the project while the suite is still being read."""

    text = _require_text(value, label, limit=1000)
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or text.startswith(("\\\\", "//")):
        raise QaError(f"{label} must be a path inside the project, not a full drive path")
    if ".." in re.split(r"[\\/]", text):
        raise QaError(f"{label} must not step outside the project with ..")
    return text


def _parse_steps(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise QaError(f"{label} must be a list of steps")
    if len(value) > _MAX_STEPS:
        raise QaError(f"{label} must hold at most {_MAX_STEPS} steps")
    steps: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        place = f"{label}[{index}]"
        data = _require_object(item, place)
        action = _require_text(data.get("do"), f"{place}.do", limit=32)
        if action not in STEP_ACTIONS:
            raise QaError(
                f"{place}.do must be one of: {', '.join(sorted(STEP_ACTIONS))}"
            )
        needed = STEP_ACTIONS[action]
        allowed = needed | {"do", "note", "timeout_ms"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise QaError(f"{place}.{unknown[0]} is not a field a {action} step understands")
        missing = sorted(needed - set(data))
        if missing:
            raise QaError(f"{place} needs a {missing[0]} field for a {action} step")
        step: dict[str, Any] = {"do": action}
        if "target" in data:
            step["target"] = _require_text(data["target"], f"{place}.target", limit=500)
        for name in ("text", "key", "value", "note"):
            if name in data:
                # Typing an empty string clears a box, which is a real action.
                # Expecting an empty string would match anything, so it is not.
                allow_empty = name in ("text", "value") and action != "expect_text"
                step[name] = _require_text(
                    data[name], f"{place}.{name}", allow_empty=allow_empty, limit=2000
                )
        if "ms" in data:
            step["ms"] = _require_whole_number(data["ms"], f"{place}.ms", 0, 60_000)
        step["timeout_ms"] = _require_whole_number(
            data.get("timeout_ms", 10_000), f"{place}.timeout_ms", 100, 120_000
        )
        steps.append(step)
    return tuple(steps)


def _parse_web_url(value: object, label: str) -> str:
    url = _require_text(value, label, limit=2000)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise QaError(f"{label} must start with http:// or https://")
    if not parsed.hostname:
        raise QaError(f"{label} must name a host")
    if parsed.username or parsed.password:
        raise QaError(f"{label} must not carry a user name or password")
    return url


def _parse_case(value: object, index: int, seen: set[str]) -> QaCase:
    label = f"cases[{index}]"
    data = _require_object(value, label)
    kind = _require_text(data.get("kind"), f"{label}.kind")
    if kind not in CASE_KINDS:
        raise QaError(f"{label}.kind must be one of: {', '.join(CASE_KINDS)}")
    allowed = _COMMON_CASE_FIELDS | _CASE_FIELDS_BY_KIND[kind]
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise QaError(f"{label}.{unknown[0]} is not a field a {kind} case understands")
    case_id = _require_text(data.get("id"), f"{label}.id", limit=64)
    if not _ID_PATTERN.fullmatch(case_id):
        raise QaError(
            f"{label}.id must be lowercase letters, digits, dash, or underscore, "
            "start with a letter or digit, and be at most 64 characters"
        )
    if case_id in seen:
        raise QaError(f"Case id is used twice: {case_id}")
    seen.add(case_id)
    title = _require_text(data.get("title", case_id), f"{label}.title", limit=_MAX_TITLE_CHARS)
    tags_value = data.get("tags", [])
    if not isinstance(tags_value, list):
        raise QaError(f"{label}.tags must be a list of short labels")
    if len(tags_value) > _MAX_TAGS:
        raise QaError(f"{label}.tags must hold at most {_MAX_TAGS} labels")
    tags: list[str] = []
    for tag in tags_value:
        text = _require_text(tag, f"{label}.tags entry", limit=32)
        if not _TAG_PATTERN.fullmatch(text):
            raise QaError(f"{label}.tags entry must be lowercase letters, digits, dash, or underscore")
        if text not in tags:
            tags.append(text)
    retries = _require_whole_number(data.get("retries", 0), f"{label}.retries", 0, _MAX_RETRIES)
    timeout_value = data.get("timeout_seconds", 0)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise QaError(f"{label}.timeout_seconds must be a number")
    timeout_seconds = float(timeout_value)
    if timeout_seconds and not 0 < timeout_seconds <= 86_400:
        raise QaError(f"{label}.timeout_seconds must be between 0 and 86400")
    expect = _parse_expectation(data.get("expect"), kind, f"{label}.expect")

    fields: dict[str, Any] = {}
    if kind == "command":
        argv = data.get("command")
        if not isinstance(argv, list) or not argv:
            raise QaError(f"{label}.command must be a non-empty list, one item per argument")
        if len(argv) > 64:
            raise QaError(f"{label}.command must hold at most 64 arguments")
        fields["command"] = tuple(
            _require_text(part, f"{label}.command entry", limit=4000) for part in argv
        )
        fields["cwd"] = _relative_project_path(data.get("cwd", "."), f"{label}.cwd")
        fields["stdin"] = _require_text(
            data.get("stdin", ""), f"{label}.stdin", allow_empty=True, limit=_MAX_BODY_CHARS
        )
        if expect.to_dict() == {}:
            expect = QaExpectation(exit_code=0)
    elif kind == "file":
        fields["path"] = _relative_project_path(data.get("path"), f"{label}.path")
        if expect.to_dict() == {}:
            expect = QaExpectation(exists=True)
    elif kind == "browser":
        fields["url"] = _parse_web_url(data.get("url"), f"{label}.url")
        routes_value = data.get("routes", [])
        if not isinstance(routes_value, list):
            raise QaError(f"{label}.routes must be a list of paths such as \"/about\"")
        if len(routes_value) > 50:
            raise QaError(f"{label}.routes must hold at most 50 paths")
        routes: list[str] = []
        for route in routes_value:
            text = _require_text(route, f"{label}.routes entry", limit=500)
            if not text.startswith("/"):
                raise QaError(f"{label}.routes entry must start with a slash")
            if "\\" in text or ".." in text:
                raise QaError(f"{label}.routes entry must not step outside the site")
            if text not in routes:
                routes.append(text)
        fields["routes"] = tuple(routes)
        viewport = _require_object(data.get("viewport", {}), f"{label}.viewport")
        unknown_viewport = sorted(set(viewport) - {"width", "height"})
        if unknown_viewport:
            raise QaError(f"{label}.viewport only understands width and height")
        fields["viewport"] = (
            _require_whole_number(viewport.get("width", 1280), f"{label}.viewport.width", 200, 5000),
            _require_whole_number(viewport.get("height", 800), f"{label}.viewport.height", 200, 5000),
        )
        for name in ("click_all", "check_accessibility"):
            found = data.get(name, False)
            if not isinstance(found, bool):
                raise QaError(f"{label}.{name} must be true or false")
            fields[name] = found
        fields["steps"] = _parse_steps(data.get("steps", []), f"{label}.steps")
        if len(fields["routes"]) > 1 and fields["steps"]:
            raise QaError(f"{label} runs its steps on one page, so name at most one route")
        if expect.to_dict() == {}:
            expect = QaExpectation(max_console_errors=0, max_page_errors=0)
    else:
        url = _parse_web_url(data.get("url"), f"{label}.url")
        fields["url"] = url
        method = _require_text(data.get("method", "GET"), f"{label}.method", limit=16).upper()
        if method not in _HTTP_METHODS:
            raise QaError(f"{label}.method must be one of: {', '.join(_HTTP_METHODS)}")
        fields["method"] = method
        headers_value = _require_object(data.get("headers", {}), f"{label}.headers")
        if len(headers_value) > _MAX_HEADERS:
            raise QaError(f"{label}.headers must hold at most {_MAX_HEADERS} entries")
        headers: list[tuple[str, str]] = []
        for name, item in headers_value.items():
            if not isinstance(name, str) or not _HEADER_NAME_PATTERN.fullmatch(name):
                raise QaError(f"{label}.headers has an unusable header name")
            text = _require_text(item, f"{label}.headers.{name}", allow_empty=True, limit=2000)
            if "\n" in text or "\r" in text:
                raise QaError(f"{label}.headers.{name} must be a single line")
            headers.append((name, text))
        fields["headers"] = tuple(headers)
        fields["body"] = _require_text(
            data.get("body", ""), f"{label}.body", allow_empty=True, limit=_MAX_BODY_CHARS
        )
        if expect.to_dict() == {}:
            expect = QaExpectation(status=200)
    return QaCase(
        index=index,
        id=case_id,
        title=title,
        kind=kind,
        expect=expect,
        tags=tuple(tags),
        retries=retries,
        timeout_seconds=timeout_seconds,
        **fields,
    )


def parse_suite(data: object, *, source: str = "") -> QaSuite:
    body = _require_object(data, "suite")
    unknown = sorted(set(body) - {"schema_version", "name", "cases"})
    if unknown:
        raise QaError(f"suite.{unknown[0]} is not a field a suite understands")
    version = body.get("schema_version", SUITE_SCHEMA_VERSION)
    if version != SUITE_SCHEMA_VERSION:
        raise QaError(f"suite.schema_version must be {SUITE_SCHEMA_VERSION}")
    name = _require_text(body.get("name", "default"), "suite.name", limit=64)
    cases_value = body.get("cases")
    if not isinstance(cases_value, list):
        raise QaError("suite.cases must be a list")
    if len(cases_value) > _MAX_CASES:
        raise QaError(f"suite.cases must hold at most {_MAX_CASES} cases")
    seen: set[str] = set()
    cases = tuple(_parse_case(item, index, seen) for index, item in enumerate(cases_value))
    return QaSuite(name=name, cases=cases, source=source)


def suite_path(config: LoadedConfig, override: str | None = None) -> Path:
    relative = override or str(config.get("qa.suite", ".harness/qa/suite.json"))
    return _control_path(config.project_root, relative)


def load_suite(config: LoadedConfig, override: str | None = None) -> QaSuite:
    path = suite_path(config, override)
    if not path.is_file():
        raise QaError(
            f"No test suite at {path.name}. Run 'harness qa init' to write a starter suite."
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QaError(f"Cannot read the test suite: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QaError(f"The test suite is not valid JSON: line {exc.lineno}, column {exc.colno}") from exc
    return parse_suite(data, source=str(path))


def write_suite(config: LoadedConfig, suite: QaSuite, override: str | None = None) -> Path:
    path = suite_path(config, override)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(suite.to_dict(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def starter_suite(
    test_commands: Sequence[Sequence[str]] = (),
    lint_commands: Sequence[Sequence[str]] = (),
    build_commands: Sequence[Sequence[str]] = (),
) -> QaSuite:
    """Build a first suite from the commands the project already has."""

    cases: list[dict[str, Any]] = []
    groups = (("tests", "Project tests", test_commands), ("lint", "Code style", lint_commands), ("build", "Build", build_commands))
    for tag, label, commands in groups:
        for position, argv in enumerate(commands, start=1):
            suffix = f"-{position}" if len(commands) > 1 else ""
            cases.append({
                "id": f"{tag}{suffix}",
                "title": f"{label} finish without an error",
                "kind": "command",
                "tags": [tag],
                "command": [str(part) for part in argv],
                "expect": {"exit_code": 0},
            })
    if not cases:
        cases.append({
            "id": "readme-exists",
            "title": "The project has a README file",
            "kind": "file",
            "tags": ["docs"],
            "path": "README.md",
            "expect": {"exists": True},
        })
    return parse_suite({"schema_version": SUITE_SCHEMA_VERSION, "name": "default", "cases": cases})


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _excerpt(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)] + "\n... (shortened)"


def _quote(value: str) -> str:
    short = value if len(value) <= 60 else value[:57] + "..."
    return short.replace("\n", " ")


class QaRunner:
    """Runs a suite. Only the process runner and local file reads touch disk."""

    def __init__(
        self,
        config: LoadedConfig,
        *,
        command_runner: CommandRunner | None = None,
        http_fetch: Callable[[QaCase, float], tuple[int, str, int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.root = config.project_root
        self.commands = command_runner or CommandRunner(config)
        self.http_fetch = http_fetch or self._fetch_http
        self.clock = clock
        self.evidence_limit = int(config.get("qa.max_evidence_chars", 4000))
        self.default_timeout = float(config.get("qa.default_timeout_seconds", 120))
        self.allowed_hosts = tuple(
            str(item).lower() for item in config.get("qa.allow_hosts", list(_LOOPBACK_HOSTS))
        )
        self._browser_ready: bool | None = None

    # -- selection ---------------------------------------------------------

    def select(
        self,
        suite: QaSuite,
        *,
        tags: Iterable[str] = (),
        ids: Iterable[str] = (),
    ) -> tuple[QaCase, ...]:
        wanted_tags = {str(tag).lower() for tag in tags}
        wanted_ids = {str(item).lower() for item in ids}
        unknown_ids = wanted_ids - {case.id for case in suite.cases}
        if unknown_ids:
            raise QaError(f"No case has this id: {sorted(unknown_ids)[0]}")
        unknown_tags = wanted_tags - set(suite.tags())
        if unknown_tags:
            raise QaError(f"No case has this tag: {sorted(unknown_tags)[0]}")
        chosen = []
        for case in suite.cases:
            if wanted_ids and case.id not in wanted_ids:
                continue
            if wanted_tags and not wanted_tags & set(case.tags):
                continue
            chosen.append(case)
        if not chosen:
            raise QaError("The filter matched no cases")
        return tuple(chosen)

    # -- public run --------------------------------------------------------

    def run(
        self,
        suite: QaSuite,
        *,
        tags: Iterable[str] = (),
        ids: Iterable[str] = (),
        workers: int | None = None,
        run_id: str | None = None,
        write_artifacts: bool = True,
    ) -> QaRunResult:
        selected = self.select(suite, tags=tags, ids=ids)
        selected_tags = tuple(sorted({str(tag).lower() for tag in tags}))
        selected_ids = tuple(sorted({str(item).lower() for item in ids}))
        configured_workers = int(self.config.get("qa.workers", 4))
        count = max(1, min(_MAX_WORKERS, int(workers or configured_workers), len(selected)))
        identifier = run_id or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        artifacts_root: Path | None = None
        if write_artifacts:
            artifacts_root = _control_path(
                self.root,
                f"{str(self.config.get('qa.artifacts_dir', '.harness/qa/runs')).rstrip('/')}/{identifier}",
            )
            artifacts_root.mkdir(parents=True, exist_ok=True)
        started = self.clock()
        results: dict[str, QaCaseResult] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = {
                pool.submit(self._run_case, case, artifacts_root): case for case in selected
            }
            for future in concurrent.futures.as_completed(futures):
                case = futures[future]
                results[case.id] = future.result()
        duration = int((self.clock() - started) * 1000)
        ordered = tuple(results[case.id] for case in selected)
        result = QaRunResult(
            run_id=identifier,
            suite_name=suite.name,
            started_at=started_at,
            duration_ms=duration,
            workers=count,
            cases=ordered,
            selected_tags=selected_tags,
            selected_ids=selected_ids,
            artifacts_dir=(
                artifacts_root.relative_to(self.root).as_posix() if artifacts_root else ""
            ),
        )
        if artifacts_root is not None:
            (artifacts_root / "result.json").write_text(
                json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
            )
            self._trim_runs()
        return result

    def _trim_runs(self) -> None:
        keep = int(self.config.get("qa.keep_runs", 20))
        if keep <= 0:
            return
        base = _control_path(self.root, str(self.config.get("qa.artifacts_dir", ".harness/qa/runs")))
        if not base.is_dir():
            return
        folders = sorted((item for item in base.iterdir() if item.is_dir()), key=lambda item: item.name)
        for stale in folders[:-keep]:
            for path in sorted(stale.rglob("*"), reverse=True):
                try:
                    path.unlink() if path.is_file() else path.rmdir()
                except OSError:
                    return
            try:
                stale.rmdir()
            except OSError:
                return

    # -- one case ----------------------------------------------------------

    def _run_case(self, case: QaCase, artifacts_root: Path | None) -> QaCaseResult:
        attempts: list[QaAttempt] = []
        artifacts: list[str] = []
        started = self.clock()
        for number in range(1, case.retries + 2):
            attempt, evidence_text = self._attempt(case, number)
            attempts.append(attempt)
            if artifacts_root is not None and evidence_text:
                folder = artifacts_root / case.id
                folder.mkdir(parents=True, exist_ok=True)
                name = f"attempt-{number}.txt"
                (folder / name).write_text(evidence_text, encoding="utf-8", errors="replace")
                artifacts.append(f"{case.id}/{name}")
            if attempt.passed:
                break
        duration = int((self.clock() - started) * 1000)
        last = attempts[-1]
        if last.skipped:
            status = STATUS_SKIPPED
        elif not last.passed:
            status = STATUS_FAILED
        elif len(attempts) > 1:
            status = STATUS_FLAKY
        else:
            status = STATUS_PASSED
        reasons = last.reasons if not last.passed or last.skipped else ()
        if status == STATUS_FLAKY:
            reasons = (
                f"Passed on attempt {len(attempts)} after failing earlier. "
                "A test that only passes sometimes is not trustworthy yet.",
            )
        return QaCaseResult(
            id=case.id,
            title=case.title,
            kind=case.kind,
            status=status,
            duration_ms=duration,
            attempts=tuple(attempts),
            tags=case.tags,
            reasons=reasons,
            artifacts=tuple(artifacts),
        )

    def _attempt(self, case: QaCase, number: int) -> tuple[QaAttempt, str]:
        started = self.clock()
        skipped = ""
        try:
            if case.kind == "command":
                reasons, evidence, full = self._check_command(case)
            elif case.kind == "file":
                reasons, evidence, full = self._check_file(case)
            elif case.kind == "browser":
                reasons, evidence, full = self._check_browser(case)
            else:
                reasons, evidence, full = self._check_http(case)
        except QaSkipped as exc:
            skipped = str(exc)
            reasons, evidence, full = (), "", ""
        except HarnessError as exc:
            reasons, evidence, full = (str(exc),), "", ""
        except Exception as exc:
            # One broken case must never take the whole run down with it.
            reasons = (f"The check itself broke: {type(exc).__name__}: {exc}",)
            evidence, full = "", ""
        duration = int((self.clock() - started) * 1000)
        if skipped:
            return (
                QaAttempt(
                    number=number,
                    passed=True,
                    duration_ms=duration,
                    reasons=(skipped,),
                    skipped=True,
                ),
                "",
            )
        if case.expect.max_duration_ms is not None and duration > case.expect.max_duration_ms:
            reasons = (
                *reasons,
                f"Took {duration} ms, which is longer than the {case.expect.max_duration_ms} ms limit",
            )
        return (
            QaAttempt(
                number=number,
                passed=not reasons,
                duration_ms=duration,
                reasons=tuple(reasons),
                evidence=_excerpt(evidence, self.evidence_limit),
            ),
            full,
        )

    # -- checks ------------------------------------------------------------

    def _check_command(self, case: QaCase) -> tuple[tuple[str, ...], str, str]:
        timeout = case.timeout_seconds or self.default_timeout
        result = self.commands.run(
            list(case.command),
            cwd=case.cwd,
            timeout=timeout,
            stdin_text=case.stdin or None,
        )
        reasons: list[str] = []
        if result.timed_out:
            reasons.append(f"The command ran longer than {timeout:g} seconds and was stopped")
        expected_exit = case.expect.exit_code
        if expected_exit is not None and not result.timed_out and result.exit_code != expected_exit:
            reasons.append(
                f"The command finished with code {result.exit_code}; the case expects {expected_exit}"
            )
        reasons.extend(_text_reasons("Screen output", result.stdout, case.expect.stdout_contains, case.expect.stdout_not_contains))
        reasons.extend(_text_reasons("Error output", result.stderr, case.expect.stderr_contains, case.expect.stderr_not_contains))
        full = (
            f"$ {' '.join(case.command)}\n"
            f"exit code: {result.exit_code}\n\n--- screen output ---\n{result.stdout}\n"
            f"--- error output ---\n{result.stderr}\n"
        )
        summary = result.stderr.strip() or result.stdout.strip()
        return tuple(reasons), summary, full

    def _check_file(self, case: QaCase) -> tuple[tuple[str, ...], str, str]:
        # A check reads, never writes, so it may look at the harness' own files.
        # The Git control folder stays off limits: it holds credentials.
        if re.split(r"[\\/]", case.path)[0].lower() == ".git":
            raise QaError("A check may not read anything inside the .git folder")
        path = _control_path(self.root, case.path)
        exists = path.is_file()
        reasons: list[str] = []
        expect = case.expect
        if expect.exists is False:
            if exists:
                reasons.append(f"{case.path} exists, but the case expects it to be gone")
            return tuple(reasons), "", ""
        if not exists:
            return (f"{case.path} was not found",), "", ""
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return (f"Cannot read {case.path}: {exc}",), "", ""
        size = len(raw)
        if expect.min_bytes is not None and size < expect.min_bytes:
            reasons.append(f"{case.path} holds {size} bytes, fewer than the {expect.min_bytes} expected")
        if expect.max_bytes is not None and size > expect.max_bytes:
            reasons.append(f"{case.path} holds {size} bytes, more than the {expect.max_bytes} allowed")
        text = raw.decode("utf-8", errors="replace")
        reasons.extend(_text_reasons(f"{case.path}", text, expect.contains, expect.not_contains))
        return tuple(reasons), _excerpt(text, 400), text

    def _check_http(self, case: QaCase) -> tuple[tuple[str, ...], str, str]:
        timeout = case.timeout_seconds or min(self.default_timeout, 60.0)
        self._check_host(case.url)
        status, body, _ = self.http_fetch(case, timeout)
        reasons: list[str] = []
        expect = case.expect
        if expect.status is not None and status != expect.status:
            reasons.append(f"The server answered with {status}; the case expects {expect.status}")
        reasons.extend(_text_reasons("The answer", body, expect.body_contains, expect.body_not_contains))
        if expect.json_fields:
            reasons.extend(_json_reasons(body, expect.json_fields))
        full = f"{case.method} {case.url}\nstatus: {status}\n\n{body}\n"
        return tuple(reasons), _excerpt(body, 400), full

    def _check_browser(self, case: QaCase) -> tuple[tuple[str, ...], str, str]:
        self._check_host(case.url)
        timeout = case.timeout_seconds or max(self.default_timeout, 120.0)
        if not self.browser_available():
            raise QaSkipped(
                "This machine has no Playwright browser driver yet. Install Node.js, then run "
                "'npm install playwright' and 'npx playwright install chromium' in the project."
            )
        plan = {
            "url": case.url,
            "routes": list(case.routes) or ["/"],
            "viewport": {"width": case.viewport[0], "height": case.viewport[1]},
            "clickAll": case.click_all,
            "checkAccessibility": case.check_accessibility,
            "steps": [dict(step) for step in case.steps],
            "timeoutMs": int(min(timeout, 120) * 1000),
            "settleMs": 250,
        }
        # Each case gets its own folder. Two runs of the same project can then
        # clean up after themselves without one removing the other's script.
        base = _control_path(self.root, ".harness/qa/tmp")
        base.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=f"{case.id}-", dir=base))
        script = folder / "browser.js"
        script.write_text(browser_script(plan), encoding="utf-8")
        try:
            result = self.commands.run(
                ["node", script.relative_to(self.root).as_posix()], cwd=".", timeout=timeout
            )
        finally:
            script.unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass
        marker = "<<<QA_REPORT>>>"
        if marker not in result.stdout:
            detail = (result.stderr or result.stdout).strip()
            raise QaError(f"The browser check did not report back: {_excerpt(detail, 600)}")
        try:
            report = json.loads(result.stdout.split(marker, 1)[1])
        except json.JSONDecodeError as exc:
            raise QaError(f"The browser report is not valid JSON: {exc.msg}") from exc
        return self._browser_reasons(case, report)

    def _browser_reasons(
        self, case: QaCase, report: Mapping[str, Any]
    ) -> tuple[tuple[str, ...], str, str]:
        reasons: list[str] = []
        expect = case.expect
        fatal = str(report.get("fatal") or "")
        if fatal:
            reasons.append(f"The browser stopped early: {_quote(fatal)}")
        steps = report.get("steps") or []
        for position, step in enumerate(steps, start=1):
            if step.get("ok"):
                continue
            named = str(step.get("label") or "").strip()
            if not named and position <= len(case.steps):
                named = str(case.steps[position - 1].get("do", "the step"))
            reasons.append(
                f"Step {position} of {len(case.steps)} did not work: {named or 'the step'}. "
                f"The browser said: {_quote(str(step.get('text') or 'nothing'))}"
            )
        if case.steps and len(steps) < len(case.steps) and not fatal:
            reasons.append(
                f"Only {len(steps)} of {len(case.steps)} steps ran, so the rest were never checked"
            )
        counted = (
            ("consoleErrors", expect.max_console_errors, "error message in the browser console"),
            ("pageErrors", expect.max_page_errors, "page script error"),
            ("requestFailures", expect.max_failed_requests, "failed network request"),
            ("accessibility", expect.max_accessibility_problems, "accessibility problem"),
        )
        for key, limit, label in counted:
            found = report.get(key) or []
            if limit is None or len(found) <= limit:
                continue
            plural = "" if len(found) == 1 else "s"
            first = found[0]
            detail = first.get("text") or first.get("url") or first.get("problem") or ""
            reasons.append(
                f"Found {len(found)} {label}{plural}, more than the {limit} allowed. "
                f"First one: {_quote(str(detail))}"
            )
        for route in report.get("routes") or []:
            status = int(route.get("status") or 0)
            if status >= 400 or status == 0:
                reasons.append(f"The page {route.get('route')} answered with {status or 'no status'}")
        text = str(report.get("text") or "")
        reasons.extend(_text_reasons("The page text", text, expect.body_contains, expect.body_not_contains))
        summary = json.dumps(
            {
                "routes": report.get("routes"),
                "console_errors": len(report.get("consoleErrors") or []),
                "page_errors": len(report.get("pageErrors") or []),
                "failed_requests": len(report.get("requestFailures") or []),
                "accessibility_problems": len(report.get("accessibility") or []),
                "steps": report.get("steps"),
            },
            indent=2,
        )
        return tuple(reasons), summary, json.dumps(report, indent=2)

    def browser_available(self) -> bool:
        """True when Node.js can load Playwright from this project."""

        if self._browser_ready is None:
            try:
                probe = self.commands.run(
                    ["node", "-e", "require.resolve('playwright')"], cwd=".", timeout=30
                )
                self._browser_ready = probe.passed
            except HarnessError:
                self._browser_ready = False
        return bool(self._browser_ready)

    def _check_host(self, url: str) -> None:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host in self.allowed_hosts:
            return
        raise QaError(
            f"This suite may not call {host}. Add the host to qa.allow_hosts in your local "
            "config if you meant to reach it."
        )

    def _fetch_http(self, case: QaCase, timeout: float) -> tuple[int, str, int]:
        data = case.body.encode("utf-8") if case.body else None
        request = urllib.request.Request(case.url, data=data, method=case.method)
        for name, value in case.headers:
            request.add_header(name, value)
        limit = max(1, int(self.config.get("qa.max_response_bytes", 1_000_000)))
        opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
        try:
            with opener.open(request, timeout=timeout) as answer:
                raw = answer.read(limit + 1)
                status = int(answer.status)
        except urllib.error.HTTPError as exc:  # a status is still a real answer
            raw = exc.read(limit + 1) if hasattr(exc, "read") else b""
            status = int(exc.code)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
            raise QaError(f"The request to {case.url} did not finish: {exc}") from exc
        truncated = len(raw) > limit
        text = raw[:limit].decode("utf-8", errors="replace")
        if truncated:
            text += "\n... (shortened)"
        return status, text, len(raw)


_BROWSER_SCRIPT = r"""
// Written by Our Harness for one browser case. It is deleted after the run.
const { chromium } = require('playwright');
const plan = __PLAN__;

function auditPage() {
  const problems = [];
  const add = (problem, detail) => problems.push({ problem, detail: String(detail).slice(0, 200) });
  if (!document.documentElement.getAttribute('lang')) {
    add('The page does not say which language it is written in', '<html lang="...">');
  }
  for (const image of document.querySelectorAll('img')) {
    if (image.getAttribute('alt') === null && image.getAttribute('role') !== 'presentation') {
      add('An image has no alt text', image.getAttribute('src') || '<img>');
    }
  }
  for (const field of document.querySelectorAll('input:not([type=hidden]), select, textarea')) {
    const id = field.getAttribute('id');
    const labelled = (id && document.querySelector('label[for="' + CSS.escape(id) + '"]'))
      || field.closest('label')
      || field.getAttribute('aria-label')
      || field.getAttribute('aria-labelledby')
      || field.getAttribute('title');
    if (!labelled) add('A form field has no label', field.outerHTML.slice(0, 120));
  }
  for (const control of document.querySelectorAll('button, a[href], [role="button"]')) {
    const name = (control.innerText || '').trim()
      || control.getAttribute('aria-label')
      || control.getAttribute('title');
    if (!name) add('A button or link has no readable name', control.outerHTML.slice(0, 120));
  }
  let previous = 0;
  for (const heading of document.querySelectorAll('h1, h2, h3, h4, h5, h6')) {
    const level = Number(heading.tagName.slice(1));
    if (previous && level > previous + 1) {
      add('A heading level was skipped', heading.tagName + ': ' + heading.innerText.slice(0, 60));
    }
    previous = level;
  }
  return problems;
}

function describeStep(step) {
  if (step.do === 'wait') return 'wait ' + step.ms + ' ms';
  if (step.text !== undefined) return step.do + ' "' + step.text + '" on ' + step.target;
  if (step.key !== undefined) return step.do + ' ' + step.key + ' on ' + step.target;
  if (step.value !== undefined) return step.do + ' ' + step.value + ' on ' + step.target;
  return step.do + ' ' + (step.target || '');
}

async function runStep(page, step) {
  const wait = step.timeout_ms || 10000;
  if (step.do === 'wait') { await page.waitForTimeout(step.ms); return; }
  const target = page.locator(step.target).first();
  if (step.do === 'click') { await target.click({ timeout: wait }); return; }
  if (step.do === 'type') { await target.fill(step.text, { timeout: wait }); return; }
  if (step.do === 'press') { await target.press(step.key, { timeout: wait }); return; }
  if (step.do === 'choose') { await target.selectOption(step.value, { timeout: wait }); return; }
  if (step.do === 'expect_visible') { await target.waitFor({ state: 'visible', timeout: wait }); return; }
  if (step.do === 'expect_hidden') { await target.waitFor({ state: 'hidden', timeout: wait }); return; }
  if (step.do === 'expect_text') {
    const deadline = Date.now() + wait;
    let seen = '';
    while (Date.now() < deadline) {
      if (await target.count()) {
        // A box the user types into holds its text as a value, not as page text.
        const tag = await target.evaluate((node) => node.tagName.toLowerCase());
        seen = (tag === 'input' || tag === 'textarea' || tag === 'select'
          ? await target.inputValue()
          : await target.innerText()) || '';
      }
      if (seen.includes(step.text)) return;
      await page.waitForTimeout(100);
    }
    throw new Error('expected to read "' + step.text + '" but the page shows "' + seen.slice(0, 120) + '"');
  }
  throw new Error('unknown step: ' + step.do);
}

(async () => {
  const report = {
    routes: [], consoleErrors: [], pageErrors: [], requestFailures: [], accessibility: [],
    clicks: [], steps: [], text: '', fatal: '',
  };
  let current = '/';
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: plan.viewport });
    page.on('console', (message) => {
      if (message.type() === 'error') {
        report.consoleErrors.push({ route: current, text: message.text().slice(0, 500) });
      }
    });
    page.on('pageerror', (error) => {
      report.pageErrors.push({ route: current, text: String((error && error.message) || error).slice(0, 500) });
    });
    page.on('requestfailed', (request) => {
      const failure = request.failure() || {};
      report.requestFailures.push({
        route: current, url: request.url().slice(0, 300), text: failure.errorText || 'request failed',
      });
    });
    for (const route of plan.routes) {
      current = route;
      const target = new URL(route, plan.url).toString();
      const answer = await page.goto(target, { waitUntil: 'load', timeout: plan.timeoutMs });
      report.routes.push({ route, status: answer ? answer.status() : 0 });
      await page.waitForTimeout(plan.settleMs);
      report.text += '\n' + await page.evaluate(() => (document.body ? document.body.innerText : ''));
      if (plan.checkAccessibility) {
        const found = await page.evaluate(auditPage);
        for (const item of found) report.accessibility.push({ route, ...item });
      }
      for (const step of plan.steps || []) {
        const label = step.note || describeStep(step);
        try {
          await runStep(page, step);
          report.steps.push({ route, label, ok: true });
        } catch (error) {
          report.steps.push({ route, label, ok: false, text: String((error && error.message) || error).slice(0, 300) });
          break;
        }
      }
      if (plan.clickAll) {
        const total = await page.evaluate(() => document.querySelectorAll(
          'button:not([type=submit]):not([data-qa-skip]), [role="button"]:not([data-qa-skip])'
        ).length);
        for (let index = 0; index < Math.min(total, 40); index += 1) {
          const controls = page.locator(
            'button:not([type=submit]):not([data-qa-skip]), [role="button"]:not([data-qa-skip])'
          );
          const control = controls.nth(index);
          let name = '';
          try {
            if (!(await control.isVisible()) || !(await control.isEnabled())) continue;
            name = ((await control.innerText()) || '').trim().slice(0, 60);
            await control.click({ timeout: 5000, noWaitAfter: true });
            await page.waitForTimeout(plan.settleMs);
            report.clicks.push({ route, name, ok: true });
          } catch (error) {
            report.clicks.push({ route, name, ok: false, text: String((error && error.message) || error).slice(0, 200) });
          }
          await page.goto(target, { waitUntil: 'load', timeout: plan.timeoutMs });
          await page.waitForTimeout(plan.settleMs);
        }
      }
    }
  } catch (error) {
    report.fatal = String((error && error.message) || error).slice(0, 500);
  } finally {
    if (browser) { try { await browser.close(); } catch (error) { /* already gone */ } }
  }
  process.stdout.write('<<<QA_REPORT>>>' + JSON.stringify(report));
})();
"""


def browser_script(plan: Mapping[str, Any]) -> str:
    """Build the standalone Playwright script for one browser case."""

    return _BROWSER_SCRIPT.replace("__PLAN__", json.dumps(dict(plan), sort_keys=True))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could leave the allowed host, so treat it as the final answer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _text_reasons(
    label: str, text: str, contains: Sequence[str], not_contains: Sequence[str]
) -> list[str]:
    reasons: list[str] = []
    for needle in contains:
        if needle not in text:
            reasons.append(f"{label} does not hold the text \"{_quote(needle)}\"")
    for needle in not_contains:
        if needle in text:
            reasons.append(f"{label} holds the text \"{_quote(needle)}\", which is not allowed")
    return reasons


def _json_reasons(body: str, fields: Sequence[tuple[str, Any]]) -> list[str]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return ["The answer is not JSON, so the JSON checks cannot run"]
    reasons: list[str] = []
    for dotted, expected in fields:
        found: Any = parsed
        missing = False
        for part in dotted.split("."):
            if isinstance(found, dict) and part in found:
                found = found[part]
            elif isinstance(found, list) and part.isdigit() and int(part) < len(found):
                found = found[int(part)]
            else:
                missing = True
                break
        if missing:
            reasons.append(f"The answer has no field named {dotted}")
        elif found != expected:
            reasons.append(
                f"The field {dotted} holds {json.dumps(found)[:80]}; the case expects {json.dumps(expected)}"
            )
    return reasons


# ---------------------------------------------------------------------------
# History and flaky scoring
# ---------------------------------------------------------------------------


_MAX_HISTORY_RUNS = 200


def history_path(config: LoadedConfig) -> Path:
    return _control_path(config.project_root, ".harness/qa/history.json")


def load_history(config: LoadedConfig) -> list[dict[str, Any]]:
    path = history_path(config)
    if not path.is_file():
        return []
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    runs = body.get("runs") if isinstance(body, dict) else None
    return [item for item in runs or [] if isinstance(item, dict)]


def record_history(config: LoadedConfig, result: QaRunResult) -> Path:
    """Append one run summary so flaky scoring has something to look at."""

    runs = load_history(config)
    runs.append({
        "run_id": result.run_id,
        "started_at": result.started_at,
        "suite": result.suite_name,
        "passed": result.passed,
        "duration_ms": result.duration_ms,
        "cases": [
            {"id": case.id, "status": case.status, "duration_ms": case.duration_ms}
            for case in result.cases
        ],
    })
    path = history_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": SUITE_SCHEMA_VERSION, "runs": runs[-_MAX_HISTORY_RUNS:]},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def flaky_report(
    config: LoadedConfig, runs: Sequence[Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Name the cases whose result keeps changing.

    A case is called unstable when it has enough recorded runs, has both passed
    and failed, and its failure rate sits away from both extremes. A case that
    always fails is broken, not unstable, so it is left out.
    """

    history = list(runs if runs is not None else load_history(config))
    minimum = int(config.get("qa.flaky_min_runs", 5))
    threshold = float(config.get("qa.flaky_threshold", 0.2))
    totals: dict[str, dict[str, Any]] = {}
    for run in history:
        for case in run.get("cases") or []:
            case_id = str(case.get("id") or "")
            if not case_id:
                continue
            entry = totals.setdefault(
                case_id, {"id": case_id, "runs": 0, "passes": 0, "failures": 0, "retried": 0, "recent": []}
            )
            status = str(case.get("status") or "")
            if status == STATUS_SKIPPED:
                continue
            entry["runs"] += 1
            if status == STATUS_FAILED:
                entry["failures"] += 1
            else:
                entry["passes"] += 1
            if status == STATUS_FLAKY:
                entry["retried"] += 1
            entry["recent"] = ([*entry["recent"], status])[-10:]
    report: list[dict[str, Any]] = []
    for entry in totals.values():
        if entry["runs"] < minimum:
            continue
        fail_rate = entry["failures"] / entry["runs"]
        retried = entry["retried"] > 0
        unstable = entry["passes"] and entry["failures"] and threshold <= fail_rate <= 1 - threshold
        if not unstable and not retried:
            continue
        score = round(min(fail_rate, 1 - fail_rate) * 2, 3) if unstable else round(entry["retried"] / entry["runs"], 3)
        report.append({
            **entry,
            "fail_rate": round(fail_rate, 3),
            "instability": score,
            "why": (
                "It has both passed and failed across runs"
                if unstable
                else "It has needed a retry to pass"
            ),
        })
    report.sort(key=lambda item: (-item["instability"], item["id"]))
    return report


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def report_markdown(result: QaRunResult) -> str:
    counts = result.counts
    lines = [
        f"# Test run {result.run_id}",
        "",
        f"Suite: {result.suite_name}",
        f"Started: {result.started_at}",
        f"Took: {result.duration_ms} ms with {result.workers} at a time",
        "",
        f"**{'All checks passed' if result.passed else 'Some checks failed'}**: "
        f"{counts[STATUS_PASSED]} passed, {counts[STATUS_FAILED]} failed, "
        f"{counts[STATUS_FLAKY]} flaky, {counts[STATUS_SKIPPED]} skipped.",
        "",
        "| Case | Status | Time | What happened |",
        "| --- | --- | --- | --- |",
    ]
    for case in result.cases:
        reason = "; ".join(case.reasons) if case.reasons else "As expected"
        lines.append(
            f"| {case.id} | {case.status} | {case.duration_ms} ms | {reason.replace('|', '/')} |"
        )
    failures = [case for case in result.cases if case.status == STATUS_FAILED]
    if failures:
        lines += ["", "## Failures", ""]
        for case in failures:
            lines.append(f"### {case.id}: {case.title}")
            for reason in case.reasons:
                lines.append(f"- {reason}")
            evidence = case.attempts[-1].evidence if case.attempts else ""
            if evidence:
                lines += ["", "```text", evidence, "```"]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_junit_xml(result: QaRunResult) -> str:
    counts = result.counts
    suites = ElementTree.Element(
        "testsuites",
        {
            "name": result.suite_name,
            "tests": str(counts["total"]),
            "failures": str(counts[STATUS_FAILED]),
            "skipped": str(counts[STATUS_SKIPPED]),
            "time": f"{result.duration_ms / 1000:.3f}",
        },
    )
    suite = ElementTree.SubElement(
        suites,
        "testsuite",
        {
            "name": result.suite_name,
            "tests": str(counts["total"]),
            "failures": str(counts[STATUS_FAILED]),
            "skipped": str(counts[STATUS_SKIPPED]),
            "time": f"{result.duration_ms / 1000:.3f}",
            "timestamp": result.started_at,
        },
    )
    for case in result.cases:
        node = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "name": case.title,
                "classname": f"{result.suite_name}.{case.kind}",
                "id": case.id,
                "time": f"{case.duration_ms / 1000:.3f}",
            },
        )
        if case.status == STATUS_FAILED:
            failure = ElementTree.SubElement(
                node, "failure", {"message": case.reasons[0] if case.reasons else "failed"}
            )
            failure.text = "\n".join(case.reasons) + (
                "\n\n" + case.attempts[-1].evidence if case.attempts else ""
            )
        elif case.status == STATUS_SKIPPED:
            ElementTree.SubElement(node, "skipped")
        elif case.status == STATUS_FLAKY:
            output = ElementTree.SubElement(node, "system-out")
            output.text = "\n".join(case.reasons)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ElementTree.tostring(
        suites, encoding="unicode"
    )


_HTML_STYLE = """
:root { color-scheme: light dark; --ok:#1a7f37; --bad:#b42318; --warn:#9a6700; --line:#8884; }
body { font: 16px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 60rem; padding: 1.5rem; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
.summary { border: 1px solid var(--line); border-radius: .5rem; padding: .75rem 1rem; margin: 1rem 0; }
table { border-collapse: collapse; width: 100%; }
caption { text-align: left; font-weight: 600; padding: .5rem 0; }
th, td { border-bottom: 1px solid var(--line); padding: .5rem; text-align: left; vertical-align: top; }
/* Every status carries a word and a mark, so colour is never the only signal. */
.status { font-weight: 700; white-space: nowrap; }
.status::before { display: inline-block; width: 1.4em; font-weight: 700; }
.passed { color: var(--ok); } .passed::before { content: "OK"; }
.failed { color: var(--bad); } .failed::before { content: "X"; }
.flaky { color: var(--warn); } .flaky::before { content: "?"; }
.skipped { color: inherit; } .skipped::before { content: "-"; }
pre { background: #8881; padding: .75rem; overflow-x: auto; border-radius: .375rem; }
details { margin: .5rem 0; }
"""


def report_html(result: QaRunResult) -> str:
    counts = result.counts
    headline = "All checks passed" if result.passed else "Some checks failed"
    rows = []
    for case in result.cases:
        reason = "; ".join(case.reasons) if case.reasons else "As expected"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(case.id)}</code><br>{html.escape(case.title)}</td>"
            f'<td class="status {case.status}">{case.status}</td>'
            f"<td>{case.duration_ms} ms</td>"
            f"<td>{html.escape(reason)}</td>"
            "</tr>"
        )
    details = []
    for case in result.cases:
        if case.status == STATUS_PASSED or not case.attempts:
            continue
        evidence = case.attempts[-1].evidence
        details.append(
            f"<details><summary>{html.escape(case.id)}: {html.escape(case.title)}</summary>"
            + "".join(f"<p>{html.escape(reason)}</p>" for reason in case.reasons)
            + (f"<pre>{html.escape(evidence)}</pre>" if evidence else "")
            + "</details>"
        )
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>Test run {html.escape(result.run_id)}</title>\n<style>{_HTML_STYLE}</style>\n"
        "</head>\n<body>\n"
        f"<h1>Test run {html.escape(result.run_id)}</h1>\n"
        f"<p>Suite {html.escape(result.suite_name)}, started {html.escape(result.started_at)}.</p>\n"
        f'<div class="summary" role="status"><p><strong>{headline}.</strong> '
        f"{counts[STATUS_PASSED]} passed, {counts[STATUS_FAILED]} failed, {counts[STATUS_FLAKY]} flaky, "
        f"{counts[STATUS_SKIPPED]} skipped in {result.duration_ms} ms.</p></div>\n"
        "<table>\n<caption>Every case in this run</caption>\n<thead><tr>"
        "<th scope=\"col\">Case</th><th scope=\"col\">Status</th><th scope=\"col\">Time</th>"
        "<th scope=\"col\">What happened</th></tr></thead>\n<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody>\n</table>\n"
        + ("<h2>Evidence</h2>\n" + "\n".join(details) if details else "")
        + "\n</body>\n</html>\n"
    )


REPORT_FORMATS = ("json", "markdown", "junit", "html")


def render_report(result: QaRunResult, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if output_format == "markdown":
        return report_markdown(result)
    if output_format == "junit":
        return report_junit_xml(result) + "\n"
    if output_format == "html":
        return report_html(result)
    raise QaError(f"Report format must be one of: {', '.join(REPORT_FORMATS)}")


# ---------------------------------------------------------------------------
# Model-assisted case proposals
# ---------------------------------------------------------------------------


GENERATION_INSTRUCTIONS = """You write test cases for a project.

Answer with one JSON object and nothing else:
{"cases": [ ... ]}

Every case is an object with these fields:
  id      lowercase letters, digits, dash or underscore; unique
  title   one short sentence saying what a pass means
  kind    "command", "file", or "http"
  tags    optional short labels
  expect  what a pass looks like

A "command" case also has "command" as a list of arguments (no shell string) and
may set "cwd" and "stdin". Its expect may use exit_code, max_duration_ms,
stdout_contains, stdout_not_contains, stderr_contains, stderr_not_contains.

A "file" case also has "path" relative to the project. Its expect may use
exists, contains, not_contains, min_bytes, max_bytes.

An "http" case also has "url", "method", "headers", "body". Its expect may use
status, max_duration_ms, body_contains, body_not_contains, json_fields.

Rules:
  - Only propose checks you can justify from the supplied project evidence.
  - Never propose a command that deletes, resets, pushes, installs, or downloads.
  - Only use http URLs on the loopback address.
  - Do not repeat a case id that already exists.
  - Prefer few strong cases over many weak ones."""


def generation_prompt(
    suite: QaSuite | None,
    detections: Sequence[Mapping[str, Any]] = (),
    focus: str = "",
    limit: int = 8,
) -> str:
    existing = ", ".join(case.id for case in suite.cases) if suite and suite.cases else "none"
    stacks = ", ".join(str(item.get("stack", "")) for item in detections) or "unknown"
    commands: list[str] = []
    for item in detections:
        for key in ("test_commands", "lint_commands", "build_commands"):
            for argv in item.get(key, []) or []:
                text = " ".join(str(part) for part in argv)
                if text and text not in commands:
                    commands.append(text)
    return (
        f"{GENERATION_INSTRUCTIONS}\n\n"
        f"Propose at most {limit} new cases.\n"
        f"Detected stacks: {stacks}\n"
        f"Known project commands: {'; '.join(commands) or 'none'}\n"
        f"Case ids already in the suite: {existing}\n"
        f"Focus: {focus or 'general coverage of the project checks'}\n"
    )


def _json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n", "", stripped)
        stripped = re.sub(r"\n```\s*$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise QaError("The model answer did not hold a JSON object")
    try:
        value = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise QaError(f"The model answer is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise QaError("The model answer must be a JSON object")
    return value


_DENIED_COMMAND_WORDS = (
    "rm", "rmdir", "del", "format", "mkfs", "shutdown", "reboot", "curl", "wget",
)
_DENIED_ARGUMENT_TEXT = (
    "--force", "reset --hard", "clean -fd", "push", "publish", "deploy", "install",
    "sudo", "rm -rf",
)


def review_generated_case(case: QaCase) -> tuple[str, ...]:
    """Plain-language warnings about a proposed case. Empty means nothing found."""

    warnings: list[str] = []
    if case.kind == "command":
        head = Path(case.command[0]).name.lower().removesuffix(".exe")
        if head in _DENIED_COMMAND_WORDS:
            warnings.append(f"The command starts with {head}, which can change or fetch things")
        joined = " ".join(part.lower() for part in case.command)
        for text in _DENIED_ARGUMENT_TEXT:
            if text in joined:
                warnings.append(f"The command holds \"{text}\", which is not safe in a test")
    if case.kind == "http":
        host = (urllib.parse.urlsplit(case.url).hostname or "").lower()
        if host not in _LOOPBACK_HOSTS:
            warnings.append(f"The case calls {host}, which is not on this machine")
    if not case.expect.to_dict():
        warnings.append("The case checks nothing, so it can never fail")
    return tuple(warnings)


def parse_generated_cases(text: str, existing_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Turn a model answer into validated candidate cases with warnings attached."""

    body = _json_object(text)
    raw = body.get("cases")
    if not isinstance(raw, list):
        raise QaError("The model answer must hold a \"cases\" list")
    if not raw:
        raise QaError("The model proposed no cases")
    if len(raw) > 50:
        raise QaError("The model proposed more than 50 cases")
    taken = {str(item) for item in existing_ids}
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        case = _parse_case(item, index, seen)
        if case.id in taken:
            raise QaError(f"The model reused an existing case id: {case.id}")
        candidates.append({"case": case.to_dict(), "warnings": list(review_generated_case(case))})
    return candidates


def candidates_path(config: LoadedConfig) -> Path:
    return _control_path(config.project_root, ".harness/qa/candidates.json")


def save_candidates(
    config: LoadedConfig, candidates: Sequence[Mapping[str, Any]], source: str = ""
) -> Path:
    path = candidates_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": SUITE_SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "candidates": [dict(item) for item in candidates],
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def load_candidates(config: LoadedConfig) -> list[dict[str, Any]]:
    path = candidates_path(config)
    if not path.is_file():
        return []
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QaError(f"Cannot read the proposed cases: {exc}") from exc
    found = body.get("candidates") if isinstance(body, dict) else None
    return [item for item in found or [] if isinstance(item, dict)]


def accept_candidates(
    config: LoadedConfig, ids: Sequence[str], *, suite_override: str | None = None
) -> tuple[QaSuite, tuple[str, ...]]:
    """Move named proposals into the suite. Returns the new suite and accepted ids."""

    wanted = [str(item) for item in ids]
    if not wanted:
        raise QaError("Name at least one case id to accept")
    candidates = load_candidates(config)
    by_id = {str(item.get("case", {}).get("id")): item for item in candidates}
    missing = [item for item in wanted if item not in by_id]
    if missing:
        raise QaError(f"There is no proposed case named {missing[0]}")
    try:
        suite = load_suite(config, suite_override)
    except QaError:
        suite = QaSuite(name="default", cases=())
    merged = suite.to_dict()
    merged["cases"].extend(by_id[item]["case"] for item in wanted)
    new_suite = parse_suite(merged, source=suite.source)
    write_suite(config, new_suite, suite_override)
    remaining = [item for item in candidates if str(item.get("case", {}).get("id")) not in wanted]
    save_candidates(config, remaining, source="after accept")
    return new_suite, tuple(wanted)


def reject_candidates(config: LoadedConfig, ids: Sequence[str]) -> tuple[str, ...]:
    wanted = {str(item) for item in ids}
    if not wanted:
        raise QaError("Name at least one case id to reject")
    candidates = load_candidates(config)
    known = {str(item.get("case", {}).get("id")) for item in candidates}
    missing = sorted(wanted - known)
    if missing:
        raise QaError(f"There is no proposed case named {missing[0]}")
    remaining = [item for item in candidates if str(item.get("case", {}).get("id")) not in wanted]
    save_candidates(config, remaining, source="after reject")
    return tuple(sorted(wanted))
