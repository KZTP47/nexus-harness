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
import contextlib
import errno
import hashlib
import hmac
import html
import json
import math
import os
import re
import socket
import ssl
import stat
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import contracts, datasets, images, scan
from .config import LoadedConfig
from .execution import CommandRunner
from .models import HarnessError
from .redaction import CredentialRedactor
from .safety import confined_path
from .safety import put_this_file_in_place
from .safety import take_the_file_away

SUITE_SCHEMA_VERSION = 1
CASE_KINDS = ("command", "file", "http", "browser", "visual", "secrets", "crawl")
BASELINE_DIR = ".harness/qa/baselines"
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


class PipelinePreservationBusy(QaError):
    """A live QA run owns the pipeline-preservation transaction."""


class BoardPreservationBusy(QaError):
    """A live QA run owns the saved-board preservation transaction."""


@dataclass(frozen=True)
class CheckKind:
    """A kind of check added by a plugin.

    A plugin says what extra fields its cases carry, what its `expect` block may
    ask for, and how to run one. The suite reader then treats it exactly like a
    built-in kind: unknown fields are still refused, and a mistake is still
    caught when the suite is read.
    """

    name: str
    summary: str
    fields: frozenset[str] = frozenset()
    expectations: frozenset[str] = frozenset()
    # run(case, runner) returns (reasons, short evidence, full evidence).
    # No reasons means the case passed. Raise QaSkipped when it cannot run here.
    run: Callable[["QaCase", "QaRunner"], tuple[tuple[str, ...], str, str]] | None = None

    def validate(self) -> None:
        if not isinstance(self.name, str) or not _ID_PATTERN.fullmatch(self.name):
            raise QaError(
                "A plugin check kind needs a lowercase name of letters, digits, dash, or underscore"
            )
        if self.name in CASE_KINDS:
            raise QaError(f"A plugin may not replace the built-in {self.name} check kind")
        if not callable(self.run):
            raise QaError(f"The {self.name} check kind has no way to run")
        # A case field and an expectation live in different places, so a kind
        # may call an expectation "rows" even though a case field may not.
        taken_fields = sorted(self.fields & _COMMON_CASE_FIELDS)
        if taken_fields:
            raise QaError(
                f"The {self.name} check kind reuses a case field the suite already owns: {taken_fields[0]}"
            )
        built_in_expectations = set().union(*_EXPECT_FIELDS_BY_KIND.values())
        taken_expectations = sorted(self.expectations & built_in_expectations)
        if taken_expectations:
            raise QaError(
                f"The {self.name} check kind reuses an expectation the suite already owns: "
                f"{taken_expectations[0]}"
            )


def validated_kinds(kinds: Iterable[CheckKind] | Mapping[str, CheckKind] | None) -> dict[str, CheckKind]:
    """Check every plugin kind once, and index it by name."""

    if not kinds:
        return {}
    found = list(kinds.values()) if isinstance(kinds, Mapping) else list(kinds)
    indexed: dict[str, CheckKind] = {}
    for kind in found:
        if not isinstance(kind, CheckKind):
            raise QaError("A plugin check kind must be a CheckKind")
        kind.validate()
        if kind.name in indexed:
            raise QaError(f"Two plugins both add a check kind named {kind.name}")
        indexed[kind.name] = kind
    return indexed


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
    # How slow and how heavy a page may be.
    max_load_ms: int | None = None
    max_first_paint_ms: int | None = None
    max_requests: int | None = None
    max_transfer_bytes: int | None = None
    # The shape an answer must have. Held as JSON text, so an expectation
    # stays a plain, unchangeable value.
    contract: str = ""
    contract_file: str = ""
    # How much a screenshot may differ from its saved baseline picture.
    max_changed_percent: float | None = None
    max_changed_pixels: int | None = None
    allowed_color_drift: int | None = None
    # How many credential-shaped things a scan may find.
    max_findings: int | None = None
    # What a walk over a whole site may find.
    max_broken_pages: int | None = None
    min_pages: int | None = None
    # Whatever a plugin check kind declared for itself.
    extra: tuple[tuple[str, Any], ...] = ()

    @property
    def wants_speed(self) -> bool:
        """True when the case asks about how fast or how heavy the page is."""

        return any(
            getattr(self, name) is not None
            for name in ("max_load_ms", "max_first_paint_ms", "max_requests", "max_transfer_bytes")
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name in (
            "exit_code", "max_duration_ms", "exists", "min_bytes", "max_bytes", "status",
            "max_console_errors", "max_page_errors", "max_failed_requests",
            "max_accessibility_problems", "max_changed_percent", "max_changed_pixels",
            "allowed_color_drift", "max_load_ms", "max_first_paint_ms", "max_requests",
            "max_transfer_bytes", "max_findings", "max_broken_pages", "min_pages",
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
        if self.contract:
            value["contract"] = json.loads(self.contract)
        if self.contract_file:
            value["contract_file"] = self.contract_file
        for key, item in self.extra:
            value[key] = item
        return value


@dataclass(frozen=True)
class QaCase:
    index: int
    id: str
    title: str
    kind: str
    expect: QaExpectation
    tags: tuple[str, ...] = ()
    # Things this check changes while it runs - a folder of notes, a settings
    # file, a row in a table. Two checks that name the same thing never run at
    # the same time, because they would be standing on each other's work.
    touches: tuple[str, ...] = ()
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
    # How far a crawl goes, and where it may not leave.
    max_pages: int = 20
    stay_under: str = ""
    # Which files a scan reads, and which it leaves alone.
    paths: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    # When to keep a picture of the page while the steps run.
    pictures: str = "failure"
    # Screenshot checks: where the saved picture lives and what to photograph.
    baseline: str = ""
    selector: str = ""
    full_page: bool = False
    # Whatever a plugin check kind declared for itself.
    extra: tuple[tuple[str, Any], ...] = ()
    # A table this check runs once for each row of, and the row it is running.
    rows: tuple[datasets.Row, ...] = ()
    rows_file: str = ""
    row: datasets.Row | None = None

    def field(self, name: str, default: Any = None) -> Any:
        """One of this case's plugin fields, or the default."""

        for key, value in self.extra:
            if key == name:
                return value
        return default

    def expect_extra(self, name: str, default: Any = None) -> Any:
        """One of this case's plugin expectations, or the default."""

        for key, value in self.expect.extra:
            if key == name:
                return value
        return default

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"id": self.id, "title": self.title, "kind": self.kind}
        if self.tags:
            value["tags"] = list(self.tags)
        if self.touches:
            value["touches"] = list(self.touches)
        if self.retries:
            value["retries"] = self.retries
        if self.timeout_seconds:
            # A whole number stays a whole number, so adding a check does not
            # rewrite every other line of the file as well.
            whole = float(self.timeout_seconds).is_integer()
            value["timeout_seconds"] = int(self.timeout_seconds) if whole else self.timeout_seconds
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
            if self.pictures != "failure":
                value["pictures"] = self.pictures
        elif self.kind == "crawl":
            value["url"] = self.url
            value["viewport"] = {"width": self.viewport[0], "height": self.viewport[1]}
            value["max_pages"] = self.max_pages
            if self.stay_under:
                value["stay_under"] = self.stay_under
            if not self.check_accessibility:
                value["check_accessibility"] = False
        elif self.kind == "secrets":
            value["paths"] = list(self.paths)
            if self.skip:
                value["skip"] = list(self.skip)
        elif self.kind == "visual":
            value["url"] = self.url
            if self.routes:
                value["routes"] = list(self.routes)
            value["viewport"] = {"width": self.viewport[0], "height": self.viewport[1]}
            if self.baseline:
                value["baseline"] = self.baseline
            if self.selector:
                value["selector"] = self.selector
            if self.full_page:
                value["full_page"] = True
            if self.steps:
                value["steps"] = [dict(step) for step in self.steps]
        for key, item in self.extra:
            value[key] = item
        expect = self.expect.to_dict()
        if expect:
            value["expect"] = expect
        if self.rows:
            value["rows"] = [row.mapping() for row in self.rows]
        if self.rows_file:
            value["rows_file"] = self.rows_file
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
    # Which part of the suite this run covered, when it was split across
    # several machines. (0, 0) means the whole thing ran here.
    part: tuple[int, int] = (0, 0)

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
            "part": {"number": self.part[0], "of": self.part[1]} if self.part[1] else None,
            "cases": [case.to_dict() for case in self.cases],
        }


# ---------------------------------------------------------------------------
# Suite parsing
# ---------------------------------------------------------------------------


def _one_part_of(chosen: list[QaCase], number: int, of: int) -> list[QaCase]:
    """The checks one machine out of several should run.

    Two things have to hold at once.

    Every check is in exactly one part. A check that fell between two parts
    would be a check nobody ran, on a build that stayed green.

    And checks that name the same thing under `touches` stay together. Holding
    them apart only works inside one run: split across four machines, each has
    its own idea of what is busy, and two checks that must never overlap would
    start at the same instant on two machines. So they are dealt as a group,
    not one at a time.
    """

    # Which checks belong together: anything sharing a touch name, and anything
    # sharing a check with something that shares a touch name.
    group_of: dict[str, str] = {}
    for case in chosen:
        found = [group_of[thing] for thing in case.touches if thing in group_of]
        name = min([*found, *case.touches]) if (found or case.touches) else case.id
        for thing in case.touches:
            group_of[thing] = name
        for thing, was in list(group_of.items()):
            if was in found:
                group_of[thing] = name

    groups: dict[str, list[QaCase]] = {}
    order: list[str] = []
    for case in chosen:
        name = min((group_of[thing] for thing in case.touches if thing in group_of), default=case.id)
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(case)

    # Dealt to whichever part has least so far, biggest group first, so a group
    # that has to stay whole does not leave one machine doing everything. Ties
    # go to the earlier part, so the same suite always splits the same way.
    parts: list[list[QaCase]] = [[] for _ in range(of)]
    heaviest = sorted(order, key=lambda name: (-len(groups[name]), order.index(name)))
    for name in heaviest:
        smallest = min(range(of), key=lambda spot: (len(parts[spot]), spot))
        parts[smallest].extend(groups[name])
    mine = {case.id for case in parts[number - 1]}
    return [case for case in chosen if case.id in mine]


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
        "contract", "contract_file",
    }),
    "browser": frozenset({
        "max_duration_ms", "body_contains", "body_not_contains", "max_console_errors",
        "max_page_errors", "max_failed_requests", "max_accessibility_problems",
        "max_load_ms", "max_first_paint_ms", "max_requests", "max_transfer_bytes",
    }),
    "visual": frozenset({
        "max_duration_ms", "max_changed_percent", "max_changed_pixels", "allowed_color_drift",
    }),
    "secrets": frozenset({"max_duration_ms", "max_findings"}),
    "crawl": frozenset({
        "max_duration_ms", "max_broken_pages", "max_console_errors", "max_page_errors",
        "max_accessibility_problems", "min_pages",
    }),
}


def _parse_expectation(
    value: object, kind: str, label: str, extra: frozenset[str] = frozenset()
) -> QaExpectation:
    data = _require_object(value if value is not None else {}, label)
    allowed = _EXPECT_FIELDS_BY_KIND.get(kind, frozenset()) | extra
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
    for name in ("max_load_ms", "max_first_paint_ms"):
        if name in data:
            fields[name] = _require_whole_number(data[name], f"{label}.{name}", 1, 600_000)
    if "max_requests" in data:
        fields["max_requests"] = _require_whole_number(
            data["max_requests"], f"{label}.max_requests", 0, 10_000
        )
    if "max_findings" in data:
        fields["max_findings"] = _require_whole_number(
            data["max_findings"], f"{label}.max_findings", 0, 10_000
        )
    if "max_broken_pages" in data:
        fields["max_broken_pages"] = _require_whole_number(
            data["max_broken_pages"], f"{label}.max_broken_pages", 0, 10_000
        )
    if "min_pages" in data:
        fields["min_pages"] = _require_whole_number(
            data["min_pages"], f"{label}.min_pages", 1, 10_000
        )
    if "max_transfer_bytes" in data:
        fields["max_transfer_bytes"] = _require_whole_number(
            data["max_transfer_bytes"], f"{label}.max_transfer_bytes", 0, 1_000_000_000
        )
    if "contract" in data and "contract_file" in data:
        raise QaError(f"{label} may hold a contract or name a file holding one, not both")
    if "contract" in data:
        shape = data["contract"]
        if not isinstance(shape, (dict, bool)):
            raise QaError(f"{label}.contract must be an object describing the shape of the answer")
        # Read the contract now, so a rule this tool cannot enforce is refused
        # while the suite is being read, not quietly skipped during a run.
        contracts.check_schema(shape, f"{label}.contract")
        fields["contract"] = json.dumps(shape, sort_keys=True)
    if "contract_file" in data:
        fields["contract_file"] = _relative_project_path(
            data["contract_file"], f"{label}.contract_file"
        )
    if "max_changed_pixels" in data:
        fields["max_changed_pixels"] = _require_whole_number(
            data["max_changed_pixels"], f"{label}.max_changed_pixels", 0, images.MAX_PIXELS
        )
    if "allowed_color_drift" in data:
        fields["allowed_color_drift"] = _require_whole_number(
            data["allowed_color_drift"], f"{label}.allowed_color_drift", 0, 255
        )
    if "max_changed_percent" in data:
        share = data["max_changed_percent"]
        if isinstance(share, bool) or not isinstance(share, (int, float)):
            raise QaError(f"{label}.max_changed_percent must be a number from 0 to 100")
        if isinstance(share, float) and not math.isfinite(share):
            raise QaError(f"{label}.max_changed_percent must be a real number")
        if not 0 <= float(share) <= 100:
            raise QaError(
                f"{label}.max_changed_percent must be from 0 to 100. "
                "It is already a percentage, so 1 means one percent of the picture."
            )
        fields["max_changed_percent"] = float(share)
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
    if extra:
        plugin_values = []
        for name in sorted(extra):
            if name not in data:
                continue
            plugin_values.append((name, _plain_value(data[name], f"{label}.{name}")))
        if plugin_values:
            fields["extra"] = tuple(plugin_values)
    return QaExpectation(**fields)


_CASE_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "command": frozenset({"command", "cwd", "stdin"}),
    "file": frozenset({"path"}),
    "http": frozenset({"url", "method", "headers", "body"}),
    "browser": frozenset({
        "url", "routes", "viewport", "click_all", "check_accessibility", "steps", "pictures",
    }),
    "visual": frozenset({"url", "routes", "viewport", "steps", "baseline", "selector", "full_page"}),
    "secrets": frozenset({"paths", "skip"}),
    "crawl": frozenset({"url", "viewport", "max_pages", "stay_under", "check_accessibility"}),
}

# The fields a table row or a named setting may be put into. Every case field
# that holds text belongs here; the ones left out hold a size, a flag, or a
# word that was already checked when the suite was read.
_NOT_FILLABLE = frozenset({"viewport", "click_all", "check_accessibility", "full_page", "method"})
FILLABLE_CASE_FIELDS: tuple[str, ...] = tuple(
    sorted(set().union(*_CASE_FIELDS_BY_KIND.values()) - _NOT_FILLABLE)
)

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
    "expect_count": frozenset({"target", "count"}),
    "run": frozenset({"script"}),
    "wait": frozenset({"ms"}),
}
# What a step may say beyond the fields its action needs.
_STEP_EXTRAS: dict[str, frozenset[str]] = {
    "run": frozenset({"text"}),
}
_MAX_STEPS = 60
_COMMON_CASE_FIELDS = frozenset({
    "id", "title", "kind", "tags", "touches", "retries", "timeout_seconds", "expect",
    "rows", "rows_file",
})
# What a check may say it touches: plain words, so the reason two checks are
# held apart reads as a reason and not as a code.
_TOUCHES_PATTERN = re.compile(r"^[a-z0-9][a-z0-9 '_-]{0,39}$")
_MOST_TOUCHES = 6


_MAX_PLUGIN_VALUE_CHARS = 20_000
_MAX_PLUGIN_LIST = 100


def _plain_value(value: object, label: str) -> Any:
    """A value a suite may hand a plugin: text, a number, a flag, or a flat list."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise QaError(f"{label} must be a real number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_PLUGIN_VALUE_CHARS:
            raise QaError(f"{label} must be at most {_MAX_PLUGIN_VALUE_CHARS} characters")
        return value
    if isinstance(value, list):
        if len(value) > _MAX_PLUGIN_LIST:
            raise QaError(f"{label} must hold at most {_MAX_PLUGIN_LIST} values")
        for item in value:
            if isinstance(item, (dict, list)):
                raise QaError(f"{label} must be a flat list, not a list holding lists or objects")
        found = [_plain_value(item, f"{label} entry") for item in value]
        # A hundred values that are each under the limit can still add up, so
        # cap the whole list as well as each value in it.
        total = sum(len(item) for item in found if isinstance(item, str))
        if total > _MAX_PLUGIN_VALUE_CHARS:
            raise QaError(
                f"{label} holds {total} characters in total, more than the "
                f"{_MAX_PLUGIN_VALUE_CHARS} allowed"
            )
        return found
    raise QaError(
        f"{label} must be text, a number, true, false, null, or a flat list of those"
    )


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
        allowed = (
            needed
            | _STEP_EXTRAS.get(action, frozenset())
            | {"do", "note", "timeout_ms", "always"}
        )
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
        if "count" in data:
            step["count"] = _require_whole_number(data["count"], f"{place}.count", 0, 10_000)
        if "script" in data:
            step["script"] = _require_text(data["script"], f"{place}.script", limit=4000)
        step["timeout_ms"] = _require_whole_number(
            data.get("timeout_ms", 10_000), f"{place}.timeout_ms", 100, 120_000
        )
        # A step that runs even when an earlier one failed. This is where a
        # check that changed something puts it back. Without it, the one thing
        # that must happen after a failure is the one thing that never does.
        if "always" in data:
            if not isinstance(data["always"], bool):
                raise QaError(f"{place}.always must be true or false")
            step["always"] = data["always"]
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


def _parse_page_fields(data: Mapping[str, Any], label: str) -> dict[str, Any]:
    """The fields both page kinds share: which pages, how big, what to do there."""

    fields: dict[str, Any] = {"url": _parse_web_url(data.get("url"), f"{label}.url")}
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
    fields["steps"] = _parse_steps(data.get("steps", []), f"{label}.steps")
    if len(fields["routes"]) > 1 and fields["steps"]:
        raise QaError(f"{label} runs its steps on one page, so name at most one route")
    return fields


def _parse_case(
    value: object,
    index: int,
    seen: set[str],
    extra_kinds: Mapping[str, CheckKind] | None = None,
) -> QaCase:
    label = f"cases[{index}]"
    data = _require_object(value, label)
    kind = _require_text(data.get("kind"), f"{label}.kind")
    plugin_kind = (extra_kinds or {}).get(kind)
    if kind not in CASE_KINDS and plugin_kind is None:
        known = [*CASE_KINDS, *sorted(extra_kinds or {})]
        raise QaError(f"{label}.kind must be one of: {', '.join(known)}")
    allowed = _COMMON_CASE_FIELDS | _CASE_FIELDS_BY_KIND.get(kind, frozenset())
    if plugin_kind is not None:
        allowed = allowed | plugin_kind.fields
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
    touches_value = data.get("touches", [])
    if not isinstance(touches_value, list):
        raise QaError(f"{label}.touches must be a list of things this check changes")
    if len(touches_value) > _MOST_TOUCHES:
        raise QaError(f"{label}.touches must name at most {_MOST_TOUCHES} things")
    touches: list[str] = []
    for thing in touches_value:
        text = _require_text(thing, f"{label}.touches entry", limit=40).lower()
        if not _TOUCHES_PATTERN.fullmatch(text):
            raise QaError(
                f"{label}.touches entry must be plain lowercase words, like \"the vault\""
            )
        if text not in touches:
            touches.append(text)
    retries = _require_whole_number(data.get("retries", 0), f"{label}.retries", 0, _MAX_RETRIES)
    timeout_value = data.get("timeout_seconds", 0)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise QaError(f"{label}.timeout_seconds must be a number")
    timeout_seconds = float(timeout_value)
    if timeout_seconds and not 0 < timeout_seconds <= 86_400:
        raise QaError(f"{label}.timeout_seconds must be between 0 and 86400")
    expect = _parse_expectation(
        data.get("expect"),
        kind,
        f"{label}.expect",
        plugin_kind.expectations if plugin_kind is not None else frozenset(),
    )
    if "rows" in data and "rows_file" in data:
        raise QaError(f"{label} may name a table or hold one, not both")
    rows = datasets.rows_from_list(data["rows"], f"{label}.rows") if "rows" in data else ()
    rows_file = (
        _relative_project_path(data["rows_file"], f"{label}.rows_file") if "rows_file" in data else ""
    )

    fields: dict[str, Any] = {}
    if plugin_kind is not None:
        # A plugin owns its fields, but the suite is still only a data file. It
        # may hold plain values of a bounded size, so a checked-in suite cannot
        # hand a plugin something huge or deeply nested to choke on.
        fields["extra"] = tuple(
            (name, _plain_value(data[name], f"{label}.{name}"))
            for name in sorted(plugin_kind.fields)
            if name in data
        )
    elif kind == "command":
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
        fields.update(_parse_page_fields(data, label))
        pictures = _require_text(data.get("pictures", "failure"), f"{label}.pictures", limit=16)
        if pictures not in ("never", "failure", "every_step"):
            raise QaError(
                f"{label}.pictures must be never, failure, or every_step"
            )
        fields["pictures"] = pictures
        for name in ("click_all", "check_accessibility"):
            found = data.get(name, False)
            if not isinstance(found, bool):
                raise QaError(f"{label}.{name} must be true or false")
            fields[name] = found
        if expect.to_dict() == {}:
            expect = QaExpectation(max_console_errors=0, max_page_errors=0)
    elif kind == "secrets":
        for name in ("paths", "skip"):
            value_list = data.get(name, [])
            if not isinstance(value_list, list):
                raise QaError(f"{label}.{name} must be a list of file patterns")
            if len(value_list) > 100:
                raise QaError(f"{label}.{name} must hold at most 100 patterns")
            chosen: list[str] = []
            for item in value_list:
                text = _relative_project_path(item, f"{label}.{name} entry")
                if text not in chosen:
                    chosen.append(text)
            fields[name] = tuple(chosen)
        if not fields["paths"]:
            fields["paths"] = ("**/*",)
        if expect.to_dict() == {}:
            # Nothing said means nothing allowed.
            expect = QaExpectation(max_findings=0)
    elif kind == "crawl":
        fields["url"] = _parse_web_url(data.get("url"), f"{label}.url")
        viewport = _require_object(data.get("viewport", {}), f"{label}.viewport")
        unknown_viewport = sorted(set(viewport) - {"width", "height"})
        if unknown_viewport:
            raise QaError(f"{label}.viewport only understands width and height")
        fields["viewport"] = (
            _require_whole_number(viewport.get("width", 1280), f"{label}.viewport.width", 200, 5000),
            _require_whole_number(viewport.get("height", 800), f"{label}.viewport.height", 200, 5000),
        )
        fields["max_pages"] = _require_whole_number(
            data.get("max_pages", 20), f"{label}.max_pages", 1, 200
        )
        if "stay_under" in data:
            fields["stay_under"] = _parse_web_url(data["stay_under"], f"{label}.stay_under")
        found = data.get("check_accessibility", True)
        if not isinstance(found, bool):
            raise QaError(f"{label}.check_accessibility must be true or false")
        fields["check_accessibility"] = found
        if expect.to_dict() == {}:
            expect = QaExpectation(max_broken_pages=0, max_page_errors=0)
    elif kind == "visual":
        fields.update(_parse_page_fields(data, label))
        if len(fields["routes"]) > 1:
            raise QaError(f"{label} takes one picture, so name at most one route")
        if "baseline" in data:
            baseline = _relative_project_path(data["baseline"], f"{label}.baseline")
            if not baseline.lower().endswith(".png"):
                raise QaError(f"{label}.baseline must be a .png file")
            fields["baseline"] = baseline
        if "selector" in data:
            fields["selector"] = _require_text(data["selector"], f"{label}.selector", limit=500)
        found = data.get("full_page", False)
        if not isinstance(found, bool):
            raise QaError(f"{label}.full_page must be true or false")
        if found and fields.get("selector"):
            raise QaError(
                f"{label} may photograph the whole page or one part of it, not both"
            )
        fields["full_page"] = found
        if expect.to_dict() == {}:
            # Nothing said, so nothing may change.
            expect = QaExpectation(max_changed_percent=0.0)
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
        touches=tuple(touches),
        retries=retries,
        timeout_seconds=timeout_seconds,
        rows=rows,
        rows_file=rows_file,
        **fields,
    )


def parse_suite(
    data: object,
    *,
    source: str = "",
    extra_kinds: Mapping[str, CheckKind] | None = None,
) -> QaSuite:
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
    # Check the plugin kinds before reading a single case, so a broken or
    # shadowing kind is refused here and not halfway through a run.
    checked = validated_kinds(extra_kinds)
    cases = tuple(
        _parse_case(item, index, seen, checked) for index, item in enumerate(cases_value)
    )
    return QaSuite(name=name, cases=cases, source=source)


def suite_path(config: LoadedConfig, override: str | None = None) -> Path:
    relative = override or str(config.get("qa.suite", ".harness/qa/suite.json"))
    return _control_path(config.project_root, relative)


def load_suite(
    config: LoadedConfig,
    override: str | None = None,
    extra_kinds: Mapping[str, CheckKind] | None = None,
) -> QaSuite:
    path = suite_path(config, override)
    if not path.is_file():
        raise QaError(
            f"No test suite at {path.name}. Run 'harness qa init' to write a starter suite."
        )
    # Writing the suite swaps a whole new file into place. On Windows there is
    # a moment during that swap when opening the file is refused outright, so a
    # panel refreshing at exactly the wrong instant used to be told the project
    # had no readable suite. The swap is over in a moment, so this waits.
    raw = ""
    for wait in (0.02, 0.05, 0.1, 0.2, 0.4, 0):
        try:
            raw = path.read_text(encoding="utf-8")
            break
        except PermissionError as exc:
            if not wait:
                raise QaError(
                    f"Cannot read the test suite: something else is holding {path.name} "
                    "open. Close whatever has it and try again."
                ) from exc
            time.sleep(wait)
        except OSError as exc:
            raise QaError(f"Cannot read the test suite: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QaError(f"The test suite is not valid JSON: line {exc.lineno}, column {exc.colno}") from exc
    return parse_suite(data, source=str(path), extra_kinds=extra_kinds)


def write_suite(config: LoadedConfig, suite: QaSuite, override: str | None = None) -> Path:
    """Write the suite so that nobody can ever read half of it.

    Writing straight over the file empties it first and fills it after. A panel
    reading it in that moment saw an empty file and said the project had no
    checks at all. So: write the whole thing beside it, then move it into
    place, which is one step as far as anybody watching is concerned.
    """

    path = suite_path(config, override)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = json.dumps(suite.to_dict(), indent=2, sort_keys=False) + "\n"
    # One name each. Two writers sharing one file beside the suite meant one
    # change disappeared without a word, and the other fell over on a file that
    # had already been moved away.
    beside = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    beside.write_text(written, encoding="utf-8")
    # Windows will not let the file be moved into place while somebody has it
    # open, even to read. That somebody is a panel refreshing, and it lets go
    # in a moment, so this waits rather than giving up on the change.
    for wait in (0.02, 0.05, 0.1, 0.2, 0.4, 0.8):
        try:
            os.replace(beside, path)
            return path
        except PermissionError:
            time.sleep(wait)
    try:
        os.replace(beside, path)
    except PermissionError as exc:
        beside.unlink(missing_ok=True)
        raise QaError(
            f"The suite file could not be written: something else is holding {path.name} "
            "open. Close whatever is reading it and try again. Nothing was changed."
        ) from exc
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


def _quote(value: str, limit: int = 60) -> str:
    short = value if len(value) <= limit else value[: max(1, limit - 3)] + "..."
    return short.replace("\n", " ")


def baseline_file(case: QaCase) -> str:
    """The project path of the saved picture a screenshot check compares with."""

    if case.baseline:
        return case.baseline
    plain = re.sub(r"[^a-z0-9_-]+", "-", case.id.lower()).strip("-") or "check"
    return f"{BASELINE_DIR}/{plain}.png"


def speed_reasons(case: QaCase, measurements: Sequence[Mapping[str, Any]]) -> list[str]:
    """Why a page was too slow or too heavy, page by page.

    A page the browser could not measure is a failure, not a pass. The older
    tool asked the browser a question it no longer answers, got zeros back, and
    reported every page as fast enough.
    """

    expect = case.expect
    if not expect.wants_speed:
        return []
    if not measurements:
        return ["The page was never measured, so the speed limits could not be judged"]
    reasons: list[str] = []
    for found in measurements:
        route = str(found.get("route") or "/")
        if not found.get("measured"):
            reasons.append(
                f"The browser did not measure {route}, so the speed limits could not be judged"
            )
            continue
        checks = (
            ("loadMs", expect.max_load_ms, "took {value} ms to finish loading", "ms"),
            ("firstPaintMs", expect.max_first_paint_ms, "first showed anything after {value} ms", "ms"),
            ("requests", expect.max_requests, "asked for {value} files", "files"),
            ("bytes", expect.max_transfer_bytes, "pulled down {value} bytes", "bytes"),
        )
        for key, limit, wording, unit in checks:
            if limit is None:
                continue
            value = found.get(key)
            if value is None:
                reasons.append(
                    f"The browser did not measure how {route} loaded, so the {unit} limit "
                    "could not be judged"
                )
                continue
            if key == "bytes" and int(found.get("unmeasured") or 0):
                # Some other site would not say how big its files were, so the
                # total is only a floor. Saying "under budget" would be a guess.
                reasons.append(
                    f"{route} loaded {found['unmeasured']} files from somewhere that would not "
                    f"say how big they were, so the {limit} byte limit cannot be judged. "
                    "It is at least " + str(value) + " bytes."
                )
                continue
            if value > limit:
                reasons.append(f"{route} " + wording.format(value=value) + f", more than the {limit} allowed")
    return reasons


def folder_of(url: str) -> str:
    """The part of an address a walk may stay inside."""

    return _folder_of(url)


def _folder_of(url: str) -> str:
    """The part of an address a walk may stay inside.

    Starting at /shop/index.html means the shop, not that one page, so the last
    piece of the path is dropped unless the address already ends in a slash.
    """

    split = urllib.parse.urlsplit(url)
    path = split.path or "/"
    if not path.endswith("/"):
        path = path[: path.rfind("/") + 1] or "/"
    return urllib.parse.urlunsplit((split.scheme, split.netloc, path, "", ""))


def crawl_summary(report: Mapping[str, Any]) -> str:
    """One line about a walk over a site."""

    pages = report.get("pages") or []
    broken = [page for page in pages if int(page.get("status") or 0) >= 400 or not page.get("status")]
    more = int(report.get("morePages") or 0)
    return (
        f"Opened {len(pages)} page{'' if len(pages) == 1 else 's'}, "
        f"{len(broken)} did not answer properly"
        + (f", and {more} more were still waiting" if more else "")
        + "."
    )


def crawl_reasons(case: QaCase, report: Mapping[str, Any]) -> tuple[str, ...]:
    """Why a walk over a site failed, page by page."""

    expect = case.expect
    reasons: list[str] = []
    fatal = str(report.get("fatal") or "")
    if fatal:
        reasons.append(f"The browser stopped early: {_quote(fatal)}")
    pages = report.get("pages") or []
    if not pages:
        # Nothing was opened, so nothing was checked. That is a failure, not a
        # quiet pass.
        reasons.append("No page was opened, so nothing was checked")
        return tuple(reasons)
    refused = report.get("refused") or []
    if refused:
        reasons.append(
            f"The walk was sent to {len(refused)} address{'' if len(refused) == 1 else 'es'} this "
            f"project may not open, and did not read {'it' if len(refused) == 1 else 'them'}. "
            f"The first was {refused[0]}"
        )
    broken = [
        page for page in pages
        if int(page.get("status") or 0) >= 400 or not page.get("status") or page.get("problem")
    ]
    allowed_broken = expect.max_broken_pages if expect.max_broken_pages is not None else 0
    if len(broken) > allowed_broken:
        first = broken[0]
        reasons.append(
            f"{len(broken)} of {len(pages)} pages did not answer properly, more than the "
            f"{allowed_broken} allowed. The first was {first.get('url')} with "
            f"{first.get('status') or first.get('problem') or 'no answer'}"
        )
    if expect.min_pages is not None and len(pages) < expect.min_pages:
        counted_pages = "1 page was" if len(pages) == 1 else f"{len(pages)} pages were"
        reasons.append(
            f"Only {counted_pages} found, fewer than the {expect.min_pages} expected. "
            "Check the starting address and the links on it."
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
        first = found[0]
        detail = first.get("text") or first.get("problem") or ""
        if first.get("problem") and first.get("detail"):
            detail = f"{first['problem']}: {first['detail']}"
        plural = "" if len(found) == 1 else "s"
        reasons.append(
            f"Found {len(found)} {label}{plural} while walking the site, more than the "
            f"{limit} allowed. On {first.get('route', 'a page')}: {_quote(str(detail), 160)}"
        )
    return tuple(reasons)


def visual_reasons(case: QaCase, difference: images.Difference, baseline: str) -> list[str]:
    """Why a screenshot check failed, in numbers a person can act on.

    The allowed amounts are read exactly as they were written. A percentage is
    a percentage, and a count of pixels is a count of pixels. Nothing is
    converted twice on the way here.
    """

    reasons: list[str] = []
    if not difference.same_size:
        reasons.append(
            f"The page is now {difference.after_size[0]} by {difference.after_size[1]} pixels, "
            f"but the saved picture {baseline} is {difference.before_size[0]} by "
            f"{difference.before_size[1]}. A page that changed size has changed."
        )
    percent = case.expect.max_changed_percent
    count = case.expect.max_changed_pixels
    if percent is None and count is None:
        # Nobody said how much may change, so nothing may change.
        percent = 0.0
    if percent is not None and difference.percent > percent:
        reasons.append(
            f"{difference.changed} of {difference.compared} pixels look different "
            f"({difference.percent:.2f}%), which is more than the {percent:g}% allowed. "
            f"The difference picture in the run folder marks them in red."
        )
    if count is not None and difference.changed > count:
        reasons.append(
            f"{difference.changed} pixels look different, which is more than the "
            f"{count} allowed."
        )
    return reasons



# Where a copy of the board goes before checks run against it, and stays. Kept
# rather than tidied away: a run that is killed outright never reaches any
# putting-back, and the whole reason this exists is that one was.
WHERE_THE_BOARD_IS_COPIED = "boards-before-checks"
BOARD_RECOVERY_NOTICE_INDEX = "recovery-notices.index"
BOARD_RECOVERY_NOTICE_INDEX_SCHEMA = 1
# How many copies to keep. One per run, and a run is a thing somebody starts, so
# this is weeks of them and still a number.
MOST_BOARD_COPIES = 30
# A displaced copy may be the only surviving bytes of a save made while a
# killed check was being recovered.  Those are never aged out automatically.
# Once this many need human review, starting another board-touching check is
# refused instead of silently deleting evidence to make room.
MOST_RETAINED_BOARD_RECOVERIES = 30
_BOARD_PRESERVATION_LOCK = threading.RLock()
BOARD_QA_CAPABILITY_HEADER = "X-Nexus-Board-QA-Capability"
BOARD_QA_CAPABILITY_SCHEMA = 1
BOARD_QA_CAPABILITY_DIRECTORY = "board-qa-capabilities"
# The lease on disk is deliberately brief.  A heartbeat keeps it alive while
# a genuinely long QA run still owns both proof locks, so long checks are not
# cut off merely because they are long.  A killed issuer becomes unusable as
# soon as its OS lock is released, and no later than this lease even on a
# filesystem whose locking implementation unexpectedly fails closed.
BOARD_QA_CAPABILITY_LEASE_SECONDS = 120.0
BOARD_QA_CAPABILITY_HEARTBEAT_SECONDS = 30.0
BOARD_QA_CAPABILITY_CLOCK_SLOP_SECONDS = 5.0
_ACTIVE_BOARD_QA_CAPABILITIES: dict[str, tuple[str, float]] = {}
_ACTIVE_BOARD_QA_CAPABILITIES_LOCK = threading.Lock()


def _board_qa_identity(live: Path) -> str:
    """The exact local board authority a QA capability belongs to."""

    return os.path.normcase(os.path.abspath(str(live)))


def _board_qa_identity_digest(live: Path) -> str:
    return hashlib.sha256(_board_qa_identity(live).encode("utf-8")).hexdigest()


def _board_qa_capability_paths(token: str, live: Path) -> tuple[Path, Path]:
    token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    where = live.parent / BOARD_QA_CAPABILITY_DIRECTORY
    return where / f"{token_digest}.json", where / f"{token_digest}.lock"


def _lock_board_qa_proof(stream, *, blocking: bool) -> bool:
    """Lock one proof byte, returning false only for ordinary contention."""

    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(
                stream.fileno(),
                msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,
                1,
            )
        else:
            import fcntl

            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(stream.fileno(), operation)
        return True
    except (OSError, BlockingIOError) as exc:
        if not blocking and getattr(exc, "errno", None) in {
            errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
        }:
            return False
        raise


def _unlock_board_qa_proof(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_board_qa_capability_record(
    record: Path, token: str, live: Path, issued_at: float,
) -> float:
    expires_at = time.time() + BOARD_QA_CAPABILITY_LEASE_SECONDS
    document = {
        "schema_version": BOARD_QA_CAPABILITY_SCHEMA,
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "board_sha256": _board_qa_identity_digest(live),
        "issuer_pid": os.getpid(),
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    put_this_file_in_place(
        record, json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
    )
    with _ACTIVE_BOARD_QA_CAPABILITIES_LOCK:
        _ACTIVE_BOARD_QA_CAPABILITIES[token] = (
            _board_qa_identity(live), expires_at,
        )
    return expires_at


def _board_qa_capability_record_is_current(
    record: Path, proof: Path, token: str, live: Path,
) -> bool:
    """Validate an authenticated live lease issued by another QA process."""

    try:
        metadata = record.stat(follow_symlinks=False)
        proof_metadata = proof.stat(follow_symlinks=False)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISREG(proof_metadata.st_mode)
            or record.is_symlink()
            or proof.is_symlink()
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or bool(getattr(proof_metadata, "st_file_attributes", 0) & reparse)
            or metadata.st_size > 4096
        ):
            return False
        held = json.loads(record.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    if not isinstance(held, dict) or held.get("schema_version") != BOARD_QA_CAPABILITY_SCHEMA:
        return False
    token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    board_digest = _board_qa_identity_digest(live)
    if not (
        isinstance(held.get("token_sha256"), str)
        and isinstance(held.get("board_sha256"), str)
        and hmac.compare_digest(held["token_sha256"], token_digest)
        and hmac.compare_digest(held["board_sha256"], board_digest)
    ):
        return False
    try:
        issued_at = float(held["issued_at"])
        expires_at = float(held["expires_at"])
        issuer_pid = int(held["issuer_pid"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    now = time.time()
    if (
        not math.isfinite(issued_at)
        or not math.isfinite(expires_at)
        or issuer_pid <= 0
        or issued_at > now + BOARD_QA_CAPABILITY_CLOCK_SLOP_SECONDS
        or expires_at <= now
        or expires_at > now + BOARD_QA_CAPABILITY_LEASE_SECONDS
        + BOARD_QA_CAPABILITY_CLOCK_SLOP_SECONDS
    ):
        return False
    # The digest-bearing record alone is not authority: the issuing process
    # must still hold this second, token-specific OS lock.  Thus a copied,
    # stale, or crash-left record cannot reopen the board transaction.
    try:
        with proof.open("r+b") as stream:
            acquired = _lock_board_qa_proof(stream, blocking=False)
            if acquired:
                _unlock_board_qa_proof(stream)
                return False
            return True
    except (OSError, BlockingIOError):
        return False


@contextlib.contextmanager
def _active_board_qa_capability(live: Path) -> "Iterator[str]":
    """Issue a revocable cross-process lease for exactly one preserved board."""

    # Two UUID4 values provide 244 random bits. The token is never written to a
    # suite, artifact, board, response body, or generated browser script; only
    # real case workers and requests belonging to this transaction receive it.
    token = uuid.uuid4().hex + uuid.uuid4().hex
    record, proof = _board_qa_capability_paths(token, live)
    record.parent.mkdir(parents=True, exist_ok=True)
    issued_at = time.time()
    stop_heartbeat = threading.Event()
    proof_stream = proof.open("x+b")
    proof_locked = False
    try:
        if not _lock_board_qa_proof(proof_stream, blocking=False):
            raise QaError("Nexus could not acquire its unique board-check proof lock.")
        proof_locked = True
        _write_board_qa_capability_record(record, token, live, issued_at)

        def keep_the_lease_live() -> None:
            while not stop_heartbeat.wait(BOARD_QA_CAPABILITY_HEARTBEAT_SECONDS):
                try:
                    _write_board_qa_capability_record(record, token, live, issued_at)
                except BaseException:  # fail closed when the old lease expires
                    return

        heartbeat = threading.Thread(
            target=keep_the_lease_live,
            name="nexus-board-qa-capability-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            yield token
        finally:
            stop_heartbeat.set()
            heartbeat.join(BOARD_QA_CAPABILITY_HEARTBEAT_SECONDS + 1.0)
    finally:
        with _ACTIVE_BOARD_QA_CAPABILITIES_LOCK:
            _ACTIVE_BOARD_QA_CAPABILITIES.pop(token, None)
        try:
            record.unlink(missing_ok=True)
        finally:
            try:
                if proof_locked:
                    _unlock_board_qa_proof(proof_stream)
            finally:
                proof_stream.close()
                proof.unlink(missing_ok=True)
                try:
                    record.parent.rmdir()
                except OSError:
                    pass


def board_qa_capability_is_active(token: str, live: Path) -> bool:
    """Validate a request capability against the still-live transaction."""

    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        return False
    now = time.time()
    with _ACTIVE_BOARD_QA_CAPABILITIES_LOCK:
        held = _ACTIVE_BOARD_QA_CAPABILITIES.get(token)
    if held is not None:
        held_identity, expires_at = held
        return expires_at > now and hmac.compare_digest(
            held_identity, _board_qa_identity(live)
        )
    record, proof = _board_qa_capability_paths(token, live)
    return _board_qa_capability_record_is_current(
        record, proof, token, live,
    )


def _a_case_can_touch_the_board(case: QaCase) -> bool:
    return any("board" in str(thing).lower() for thing in case.touches)


def _board_notice_index_path(where: Path) -> Path:
    return where / BOARD_RECOVERY_NOTICE_INDEX


def _write_board_notice_index(where: Path, names: Iterable[str]) -> None:
    clean = sorted({
        str(name) for name in names
        if isinstance(name, str)
        and Path(name).name == name
        and name.endswith("-transaction.json")
    })
    put_this_file_in_place(
        _board_notice_index_path(where),
        json.dumps({
            "schema_version": BOARD_RECOVERY_NOTICE_INDEX_SCHEMA,
            "retained_transactions": clean,
        }, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _read_board_notice_index(where: Path) -> list[str]:
    path = _board_notice_index_path(where)
    try:
        if path.stat().st_size > 64_000:
            raise ValueError("the index is unexpectedly large")
        held = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QaError(
            f"The compact saved-board recovery notice index at {path} cannot be read."
        ) from exc
    names = held.get("retained_transactions") if isinstance(held, dict) else None
    if (
        not isinstance(held, dict)
        or held.get("schema_version") != BOARD_RECOVERY_NOTICE_INDEX_SCHEMA
        or not isinstance(names, list)
        or len(names) > MOST_RETAINED_BOARD_RECOVERIES
        or any(
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith("-transaction.json")
            for name in names
        )
    ):
        raise QaError(
            f"The compact saved-board recovery notice index at {path} is invalid."
        )
    return list(dict.fromkeys(names))


def _remember_board_recovery_notice(transaction: Path, retained: bool) -> None:
    where = transaction.parent
    try:
        names = _read_board_notice_index(where)
    except QaError:
        # Rebuilding is deliberately exceptional. Ordinary status polling never
        # opens the payload-bearing journals.
        names = []
        for candidate in sorted(where.glob("*-transaction.json")):
            if _read_board_transaction(candidate).get("displaced_copy_retained"):
                names.append(candidate.name)
    if retained and transaction.name not in names:
        names.append(transaction.name)
    elif not retained:
        names = [name for name in names if name != transaction.name]
    _write_board_notice_index(where, names)


@contextlib.contextmanager
def _board_preservation_file_lock(
    live: Path, timeout_seconds: float = 30.0,
) -> "Iterator[None]":
    """Serialize board QA preservation across Nexus processes."""

    lock_path = live.parent / "board-qa-preservation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    wait_for = max(0.0, float(timeout_seconds))
    acquired_thread = _BOARD_PRESERVATION_LOCK.acquire(timeout=wait_for)
    if not acquired_thread:
        raise BoardPreservationBusy(
            "Another board-touching check is still preserving the saved boards."
        )
    try:
        with lock_path.open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            acquired = False
            deadline = time.monotonic() + wait_for
            try:
                while not acquired:
                    try:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except (OSError, BlockingIOError):
                        if time.monotonic() >= deadline:
                            raise BoardPreservationBusy(
                                "Another Nexus process is still preserving boards for "
                                "a visual check. Wait for it to finish and try again."
                            )
                        time.sleep(0.05)
                yield
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        _BOARD_PRESERVATION_LOCK.release()

# A browser check drives the real Automations page, so Save and Delete act on
# the same project library as the person running the check. Keep a durable
# transaction outside that library. If the runner is killed, the OS releases
# the lock and the next pipeline check restores this journal before it opens
# the page or takes a new snapshot.
WHERE_PIPELINES_ARE_COPIED = ".harness/qa/pipelines-before-checks"
PIPELINE_PRESERVATION_SCHEMA = 1
MAX_RETAINED_PIPELINE_RECOVERIES = 20
MAX_PIPELINE_RECOVERY_COPY_BYTES = 512_000_000
MAX_DISPLACED_COPIES_PER_RECOVERY = 8
PIPELINE_DIRECTORY_REPLACE_RETRY_SECONDS = (
    0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2,
)
_TRANSIENT_WINDOWS_DIRECTORY_REPLACE_ERRORS = {5, 32, 33}
_PIPELINE_PRESERVATION_LOCK = threading.RLock()


def _a_case_can_touch_pipelines(case: QaCase) -> bool:
    return (
        "pipelines" in {str(tag).lower() for tag in case.tags}
        or any(
            word in str(thing).lower()
            for thing in case.touches
            for word in ("pipeline", "automation")
        )
    )


@contextlib.contextmanager
def _pipeline_preservation_file_lock(
    root: Path, timeout_seconds: float = 30.0
) -> "Iterator[None]":
    """Serialize snapshots and recovery across QA processes."""

    lock_path = _control_path(root, ".harness/qa/pipeline-preservation.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    wait_for = max(0.0, float(timeout_seconds))
    thread_acquired = _PIPELINE_PRESERVATION_LOCK.acquire(timeout=wait_for)
    if not thread_acquired:
        raise PipelinePreservationBusy(
            "A pipeline check is actively preserving the saved automation library."
        )
    try:
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            acquired = False
            deadline = time.monotonic() + wait_for
            try:
                while not acquired:
                    try:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except (OSError, BlockingIOError):
                        if time.monotonic() >= deadline:
                            raise PipelinePreservationBusy(
                                "Another pipeline check is still preserving the saved "
                                "automation library. Wait for it to finish and try again."
                            )
                        time.sleep(0.05)
                yield
            finally:
                # A timeout/error before acquisition must not be hidden by an
                # attempted unlock of a lock this process never held.
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        _PIPELINE_PRESERVATION_LOCK.release()


def _pipeline_tree(where: Path) -> tuple[bool, tuple[str, ...], dict[str, bytes]]:
    """Read one exact regular-file tree without following links or reparses."""

    if not where.exists():
        return False, (), {}
    try:
        root_stat = where.stat(follow_symlinks=False)
    except OSError as exc:
        raise QaError(f"The saved automation library could not be inspected: {exc}") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if where.is_symlink() or bool(getattr(root_stat, "st_file_attributes", 0) & reparse):
        raise QaError("The saved automation library is a link or reparse point; checks left it alone.")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise QaError("The saved automation library is not a folder; checks left it alone.")

    dirs: list[str] = []
    files: dict[str, bytes] = {}
    pending = [where]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as found:
                entries = sorted(found, key=lambda item: item.name)
        except OSError as exc:
            raise QaError(f"The saved automation library could not be read: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(where).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise QaError(f"Saved automation path {relative} could not be inspected: {exc}") from exc
            if entry.is_symlink() or bool(
                getattr(metadata, "st_file_attributes", 0) & reparse
            ):
                raise QaError(
                    f"Saved automation path {relative} is a link or reparse point; "
                    "checks left the library alone."
                )
            if stat.S_ISDIR(metadata.st_mode):
                dirs.append(relative)
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                try:
                    files[relative] = path.read_bytes()
                except OSError as exc:
                    raise QaError(f"Saved automation file {relative} could not be read: {exc}") from exc
            else:
                raise QaError(
                    f"Saved automation path {relative} is not a regular file or folder; "
                    "checks left the library alone."
                )
    return True, tuple(sorted(dirs)), dict(sorted(files.items()))


def _put_pipeline_bytes(where: Path, body: bytes) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    beside = where.with_name(f"{where.name}.{os.getpid()}-{uuid.uuid4().hex}.part")
    try:
        beside.write_bytes(body)
        for wait in (0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2):
            try:
                os.replace(beside, where)
                return
            except PermissionError:
                time.sleep(wait)
        os.replace(beside, where)
    finally:
        try:
            beside.unlink(missing_ok=True)
        except OSError:
            pass


def _replace_pipeline_directory_once(stage: Path, destination: Path) -> None:
    """One atomic same-volume placement, split out for failure-injected tests."""

    os.replace(stage, destination)


def _transient_pipeline_directory_replace_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, PermissionError):
        # An injected/portable PermissionError has no Windows number. Real
        # Windows access/sharing/lock denials use only this bounded allowlist.
        return winerror is None or winerror in _TRANSIENT_WINDOWS_DIRECTORY_REPLACE_ERRORS
    return (
        os.name == "nt"
        and winerror in _TRANSIENT_WINDOWS_DIRECTORY_REPLACE_ERRORS
    )


def _put_pipeline_directory_in_place(stage: Path, destination: Path) -> None:
    """Atomically place a completed recovery tree without overwriting evidence.

    Windows virus scanners and indexers can briefly retain a handle after the
    last file is closed. Retry only those known transient denials. The unique
    ``.incomplete`` tree remains byte-for-byte recoverable if all attempts are
    exhausted, and a destination which appears is never replaced or guessed at.
    """

    delays = (*PIPELINE_DIRECTORY_REPLACE_RETRY_SECONDS, None)
    last_error: OSError | None = None
    for delay in delays:
        if os.path.lexists(destination):
            raise QaError(
                "Nexus found an existing pipeline recovery destination while "
                f"placing {stage}. It kept both paths and did not overwrite either."
            )
        try:
            _replace_pipeline_directory_once(stage, destination)
            return
        except OSError as exc:
            if not _transient_pipeline_directory_replace_error(exc):
                raise
            last_error = exc
            # A move can become externally visible even when a platform API
            # reports failure. Never retry over that evidence.
            if os.path.lexists(destination):
                raise QaError(
                    "The pipeline recovery destination appeared while Windows "
                    f"reported a move failure. Nexus kept {stage} and {destination} "
                    "for review and did not overwrite either."
                ) from exc
            if delay is None:
                break
            time.sleep(delay)
    raise QaError(
        "Windows kept denying the atomic placement of a verified pipeline "
        f"recovery copy. The exact incomplete evidence remains at {stage}; "
        "the live automation library was not changed. Retry recovery after "
        "the scanner or file handle releases it."
    ) from last_error


def _pipeline_manifest(
    exists: bool, dirs: tuple[str, ...], files: Mapping[str, bytes]
) -> dict[str, Any]:
    return {
        "original_exists": exists,
        "directories": list(dirs),
        "files": {
            name: {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in files.items()
        },
    }


def _pipeline_manifest_bytes(manifest: Mapping[str, Any]) -> int:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise QaError("A pipeline-check recovery manifest has no valid file inventory.")
    try:
        total = sum(int(item["bytes"]) for item in files.values())
    except (KeyError, TypeError, ValueError) as exc:
        raise QaError("A pipeline-check recovery manifest has an invalid byte inventory.") from exc
    if total < 0:
        raise QaError("A pipeline-check recovery manifest has an invalid byte inventory.")
    return total


def _write_pipeline_copy(
    where: Path, tree: tuple[bool, tuple[str, ...], dict[str, bytes]]
) -> None:
    _exists, dirs, files = tree
    where.mkdir(parents=True, exist_ok=False)
    for directory in dirs:
        (where / Path(directory)).mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        _put_pipeline_bytes(where / Path(name), body)


def _manifest_matches(
    manifest: Mapping[str, Any], tree: tuple[bool, tuple[str, ...], dict[str, bytes]]
) -> bool:
    exists, dirs, files = tree
    return dict(manifest) == _pipeline_manifest(exists, dirs, files)


def _pipeline_manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _save_displaced_pipeline_copy(
    transaction: Path,
    destination: Path,
    current: tuple[bool, tuple[str, ...], dict[str, bytes]],
    expected_copy: Mapping[str, Any],
) -> dict[str, Any]:
    """Place or validate one copy while bounding interrupted full-tree stages."""

    if os.path.lexists(destination):
        return _pipeline_manifest(*_pipeline_tree(destination))

    # Deterministic on purpose. Repeated startup recovery while an indexer
    # keeps the move denied reuses these exact verified bytes rather than
    # accumulating a new up-to-512MB UUID tree on every attempt.
    stage = transaction / f".{destination.name}.incomplete"
    if os.path.lexists(stage):
        if not _manifest_matches(expected_copy, _pipeline_tree(stage)):
            raise QaError(
                "An earlier incomplete pipeline recovery copy contains different "
                f"bytes at {stage}. Nexus kept that evidence and the live library "
                "unchanged; review it before retrying recovery."
            )
    else:
        _write_pipeline_copy(stage, current)
        if not _manifest_matches(expected_copy, _pipeline_tree(stage)):
            raise QaError(
                "The incomplete pipeline recovery copy failed validation; the live "
                "automation library was not changed."
            )
    _put_pipeline_directory_in_place(stage, destination)
    return _pipeline_manifest(*_pipeline_tree(destination))


def _begin_pipeline_preservation(root: Path, run_id: str) -> Path:
    """Create a recoverable snapshot; callers serialize this with the file lock."""

    live = _control_path(root, ".harness/pipelines")
    # Reading twice means an actively changing library does not get blessed as
    # a half-old, half-new snapshot. Nothing has been changed by QA yet.
    stable = None
    for _attempt in range(3):
        first = _pipeline_tree(live)
        second = _pipeline_tree(live)
        if first == second:
            stable = second
            break
    if stable is None:
        raise QaError(
            "The saved automation library was changing while checks tried to preserve it. "
            "No pipeline check was started; try again when the current save has finished."
        )
    exists, dirs, files = stable
    snapshot_size = _pipeline_manifest_bytes(_pipeline_manifest(exists, dirs, files))
    if snapshot_size > MAX_PIPELINE_RECOVERY_COPY_BYTES:
        raise QaError(
            f"The saved automation tree is {snapshot_size:,} bytes, larger than the "
            f"{MAX_PIPELINE_RECOVERY_COPY_BYTES:,}-byte recoverable QA boundary. "
            "No pipeline check was started and the library was not changed."
        )
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "-", str(run_id or "checks"))[:80]
    transaction = _control_path(
        root, f"{WHERE_PIPELINES_ARE_COPIED}/{safe_run}-{uuid.uuid4().hex}"
    )
    backup = transaction / "tree"
    _write_pipeline_copy(backup, stable)
    journal = {
        "schema_version": PIPELINE_PRESERVATION_SCHEMA,
        "state": "prepared",
        "run_id": str(run_id or ""),
        "live": ".harness/pipelines",
        "manifest": _pipeline_manifest(exists, dirs, files),
    }
    put_this_file_in_place(
        transaction / "journal.json", json.dumps(journal, indent=2, sort_keys=True) + "\n"
    )
    return transaction


def _read_pipeline_journal(transaction: Path) -> dict[str, Any]:
    try:
        journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QaError(
            f"The interrupted pipeline-check recovery journal at {transaction} is unreadable; "
            "the saved automation library was not changed."
        ) from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != PIPELINE_PRESERVATION_SCHEMA
        or journal.get("live") != ".harness/pipelines"
        or not isinstance(journal.get("manifest"), dict)
    ):
        raise QaError(
            f"The interrupted pipeline-check recovery journal at {transaction} is not valid; "
            "the saved automation library was not changed."
        )
    return journal


def _remove_pipeline_tree(where: Path) -> None:
    exists, dirs, files = _pipeline_tree(where)
    if not exists:
        return
    for name in files:
        take_the_file_away(where / Path(name), missing_ok=True)
    for directory in sorted(dirs, key=lambda item: (item.count("/"), item), reverse=True):
        (where / Path(directory)).rmdir()
    where.rmdir()


def _retained_pipeline_recoveries(root: Path) -> int:
    where = _control_path(root, WHERE_PIPELINES_ARE_COPIED)
    if not where.is_dir():
        return 0
    retained = 0
    for transaction in (one for one in where.iterdir() if one.is_dir()):
        journal_path = transaction / "journal.json"
        if not journal_path.is_file():
            continue
        journal = _read_pipeline_journal(transaction)
        if bool(journal.get("keep_recovery_copy")):
            _validate_displaced_pipeline_metadata(journal)
            retained += 1
    return retained


def _validate_displaced_pipeline_metadata(journal: Mapping[str, Any]) -> None:
    copies = journal.get("displaced_copies") or []
    if not isinstance(copies, list) or len(copies) > MAX_DISPLACED_COPIES_PER_RECOVERY:
        raise QaError("A retained pipeline recovery has an invalid displaced-copy inventory.")
    for held in copies:
        if not isinstance(held, dict):
            raise QaError("A retained pipeline recovery has an invalid displaced copy.")
        name = held.get("path")
        source_manifest = held.get("source_manifest")
        copy_manifest = held.get("copy_manifest")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.startswith("displaced-after-interruption")
            or not isinstance(source_manifest, dict)
            or not isinstance(copy_manifest, dict)
        ):
            raise QaError("A retained pipeline recovery has an invalid displaced copy.")
        if _pipeline_manifest_bytes(source_manifest) > MAX_PIPELINE_RECOVERY_COPY_BYTES:
            raise QaError("A retained pipeline recovery exceeds its disclosed byte boundary.")


def _validate_displaced_pipeline_copies(
    transaction: Path, journal: Mapping[str, Any]
) -> None:
    """Explicit deep validation; ordinary inventory uses journal metadata only."""

    _validate_displaced_pipeline_metadata(journal)
    for held in journal.get("displaced_copies") or []:
        name = held["path"]
        copy_manifest = held["copy_manifest"]
        if not _manifest_matches(copy_manifest, _pipeline_tree(transaction / name)):
            raise QaError(
                f"The retained post-interruption copy at {transaction / name} failed its checksum."
            )


def _preserve_displaced_pipeline_tree(
    root: Path,
    transaction: Path,
    journal: dict[str, Any],
    current: tuple[bool, tuple[str, ...], dict[str, bytes]],
) -> dict[str, Any]:
    """Keep post-crash bytes before an old journal overwrites or removes them."""

    source_manifest = _pipeline_manifest(*current)
    copies = journal.get("displaced_copies") or []
    if not isinstance(copies, list):
        raise QaError("A pipeline-check recovery journal has an invalid displaced-copy list.")
    for held in copies:
        if not isinstance(held, dict) or not isinstance(held.get("source_manifest"), dict):
            raise QaError("A pipeline-check recovery journal has an invalid displaced copy.")
        if held["source_manifest"] == source_manifest:
            return journal
    if len(copies) >= MAX_DISPLACED_COPIES_PER_RECOVERY:
        raise QaError(
            "This interrupted pipeline check already has the maximum of "
            f"{MAX_DISPLACED_COPIES_PER_RECOVERY} preserved post-crash variants. "
            f"Resolve the copies at {transaction} before retrying; live files were not changed."
        )
    if not copies and _retained_pipeline_recoveries(root) >= MAX_RETAINED_PIPELINE_RECOVERIES:
        raise QaError(
            "Nexus is already retaining the maximum of "
            f"{MAX_RETAINED_PIPELINE_RECOVERIES} interrupted pipeline recovery copies. "
            "Resolve those copies before retrying; live files were not changed."
        )
    size = _pipeline_manifest_bytes(source_manifest)
    if size > MAX_PIPELINE_RECOVERY_COPY_BYTES:
        raise QaError(
            f"The current saved automation tree is {size:,} bytes, larger than the "
            f"{MAX_PIPELINE_RECOVERY_COPY_BYTES:,}-byte recovery-copy boundary. "
            "It was not overwritten or removed."
        )

    number = len(copies) + 1
    name = "displaced-after-interruption" + (f"-{number}" if number > 1 else "")
    destination = transaction / name
    expected_copy = dict(source_manifest)
    expected_copy["original_exists"] = True
    # A killed copy attempt may have reached the atomic directory move before
    # its journal update. Preserve and validate that evidence instead of
    # replacing it; a later distinct live variant receives the next name.
    copy_manifest = _save_displaced_pipeline_copy(
        transaction, destination, current, expected_copy,
    )
    if copy_manifest != expected_copy:
        # Do not guess that an unjournaled folder is the current variant. Give
        # the current bytes another durable name and retain both. The suffix is
        # content-derived so the same interrupted attempt is also bounded.
        fingerprint = _pipeline_manifest_fingerprint(expected_copy)[:12]
        destination = transaction / f"{name}-{fingerprint}"
        copy_manifest = _save_displaced_pipeline_copy(
            transaction, destination, current, expected_copy,
        )
        if copy_manifest != expected_copy:
            raise QaError(
                "The post-crash pipeline recovery copy failed validation; live files were not changed."
            )
    copies.append({
        "path": destination.name,
        "source_manifest": source_manifest,
        "copy_manifest": expected_copy,
    })
    journal["displaced_copies"] = copies
    journal["keep_recovery_copy"] = True
    journal["recovery_note"] = (
        "The pre-check automation library was restored. Files found when QA ended or "
        "recovered are retained in the displaced-after-interruption folder(s) and are never "
        "automatically deleted."
    )
    put_this_file_in_place(
        transaction / "RECOVERY_NOTICE.txt",
        "Nexus preserved the automation library found when pipeline QA ended or recovered.\n\n"
        "It restored the verified automation library from before the check. The folder(s) "
        "named displaced-after-interruption contain the complete bytes found immediately "
        "before restoration, including any legitimate work saved while QA was active or "
        "after an interruption. They can also contain artifacts made by the check. "
        "Nexus will not delete this evidence automatically. Compare or copy out what you "
        "need, then remove this recovery folder yourself when satisfied.\n",
    )
    put_this_file_in_place(
        transaction / "journal.json", json.dumps(journal, indent=2, sort_keys=True) + "\n"
    )
    return journal


def _restore_pipeline_transaction(
    root: Path, transaction: Path, *, preserve_displaced: bool = False
) -> None:
    journal = _read_pipeline_journal(transaction)
    if journal.get("state") == "restored":
        return
    if journal.get("state") != "prepared":
        raise QaError(
            f"The interrupted pipeline-check recovery at {transaction} has an unknown state; "
            "the saved automation library was not changed."
        )
    manifest = journal["manifest"]
    backup = transaction / "tree"
    copied = _pipeline_tree(backup)
    expected_backup = dict(manifest)
    expected_backup["original_exists"] = True
    if not _manifest_matches(expected_backup, copied):
        raise QaError(
            f"The pipeline-check recovery copy at {transaction} failed its checksum; "
            "the saved automation library was not changed."
        )

    live = _control_path(root, ".harness/pipelines")
    original_exists = bool(manifest.get("original_exists"))
    current = _pipeline_tree(live)
    if preserve_displaced and not _manifest_matches(manifest, current):
        journal = _preserve_displaced_pipeline_tree(
            root, transaction, journal, current
        )
    if not original_exists:
        _remove_pipeline_tree(live)
    else:
        _exists, wanted_dirs, wanted_files = copied
        current_exists, current_dirs, current_files = current
        if not current_exists:
            live.mkdir(parents=True, exist_ok=True)
        # Restore file/folder type changes before ordinary content changes.
        # These paths had the opposite type in the verified snapshot, so the
        # transaction proves they cannot be pre-existing user definitions.
        file_type_conflicts = set(current_files) & set(wanted_dirs)
        for name in sorted(file_type_conflicts):
            take_the_file_away(live / Path(name), missing_ok=True)
        directory_type_conflicts = set(current_dirs) & set(wanted_files)
        for directory in sorted(
            directory_type_conflicts,
            key=lambda item: (item.count("/"), item), reverse=True,
        ):
            prefix = directory + "/"
            for name in sorted(
                (item for item in current_files if item.startswith(prefix)), reverse=True
            ):
                take_the_file_away(live / Path(name), missing_ok=True)
            for child in sorted(
                (item for item in current_dirs if item.startswith(prefix)),
                key=lambda item: (item.count("/"), item), reverse=True,
            ):
                (live / Path(child)).rmdir()
            (live / Path(directory)).rmdir()
        for directory in wanted_dirs:
            (live / Path(directory)).mkdir(parents=True, exist_ok=True)
        for name, body in wanted_files.items():
            if current_files.get(name) != body:
                _put_pipeline_bytes(live / Path(name), body)
        # Existing user files are restored byte-for-byte. Only paths absent
        # from the before-check manifest qualify as transaction artifacts.
        for name in sorted(set(current_files) - set(wanted_files) - file_type_conflicts):
            take_the_file_away(live / Path(name), missing_ok=True)
        for directory in sorted(
            set(current_dirs) - set(wanted_dirs) - directory_type_conflicts,
            key=lambda item: (item.count("/"), item), reverse=True,
        ):
            try:
                (live / Path(directory)).rmdir()
            except FileNotFoundError:
                pass
        if not _manifest_matches(manifest, _pipeline_tree(live)):
            raise QaError(
                "The saved automation library could not be restored exactly after the checks. "
                f"The verified recovery copy remains at {transaction}."
            )
    journal["state"] = "restored"
    put_this_file_in_place(
        transaction / "journal.json", json.dumps(journal, indent=2, sort_keys=True) + "\n"
    )


def _recover_pipeline_transactions(root: Path) -> None:
    where = _control_path(root, WHERE_PIPELINES_ARE_COPIED)
    if not where.is_dir():
        return
    for transaction in sorted(one for one in where.iterdir() if one.is_dir()):
        if not (transaction / "journal.json").is_file():
            continue
        journal = _read_pipeline_journal(transaction)
        if journal.get("state") == "restored" and journal.get("keep_recovery_copy"):
            _validate_displaced_pipeline_metadata(journal)
            continue
        _restore_pipeline_transaction(root, transaction, preserve_displaced=True)
        journal = _read_pipeline_journal(transaction)
        if journal.get("keep_recovery_copy"):
            continue
        try:
            _remove_pipeline_tree(transaction)
        except (OSError, HarnessError):
            # Its journal says restored, so retrying cannot alter the library.
            # Leaving a verified copy is safer than making cleanup destructive.
            continue


def recover_abandoned_pipeline_transactions(root: Path) -> bool:
    """Recover dead QA transactions during an ordinary library refresh.

    Return false immediately when a live QA run owns the lock. A panel refresh
    must never wait for or interfere with the browser check which is currently
    using the temporary library state.
    """

    try:
        with _pipeline_preservation_file_lock(root, timeout_seconds=0.0):
            _recover_pipeline_transactions(root)
    except PipelinePreservationBusy:
        return False
    return True


def retained_pipeline_recovery_notices(root: Path) -> list[str]:
    """Actionable inventory warnings for displaced bytes awaiting review."""

    try:
        with _pipeline_preservation_file_lock(root, timeout_seconds=0.0):
            where = _control_path(root, WHERE_PIPELINES_ARE_COPIED)
            if not where.is_dir():
                return []
            notices: list[str] = []
            for transaction in sorted(one for one in where.iterdir() if one.is_dir()):
                if not (transaction / "journal.json").is_file():
                    continue
                journal = _read_pipeline_journal(transaction)
                if not journal.get("keep_recovery_copy"):
                    continue
                _validate_displaced_pipeline_metadata(journal)
                notice = transaction / "RECOVERY_NOTICE.txt"
                notices.append(
                    "Recovered automation changes need review at "
                    f"{transaction}. Open {notice.name}, then copy or import any wanted "
                    "automation JSON from its displaced-after-interruption folder(s). "
                    "Remove the recovery folder only after you have reviewed it."
                )
            return notices
    except PipelinePreservationBusy:
        return []


@contextlib.contextmanager
def _pipeline_definitions_put_back_afterwards(
    root: Path, cases: Sequence[QaCase], run_id: str = ""
) -> "Iterator[None]":
    if not any(_a_case_can_touch_pipelines(case) for case in cases):
        yield
        return
    with _pipeline_preservation_file_lock(root):
        _recover_pipeline_transactions(root)
        retained = _retained_pipeline_recoveries(root)
        if retained >= MAX_RETAINED_PIPELINE_RECOVERIES:
            raise QaError(
                "Nexus is already retaining the maximum of "
                f"{MAX_RETAINED_PIPELINE_RECOVERIES} pipeline recovery copies. "
                "Resolve those copies before starting another pipeline check; "
                "no check was run and the saved automation library was not changed."
            )
        transaction = _begin_pipeline_preservation(root, run_id)
        try:
            yield
        finally:
            _restore_pipeline_transaction(
                root, transaction, preserve_displaced=True
            )
            journal = _read_pipeline_journal(transaction)
            if not journal.get("keep_recovery_copy"):
                try:
                    _remove_pipeline_tree(transaction)
                except (OSError, HarnessError):
                    pass


@contextlib.contextmanager
def _the_board_put_back_afterwards(cases, run_id: str = "") -> "Iterator[str]":
    """Keep the agent board safe while checks run against it.

    A copy is written first and left there afterwards. Then the board is put
    back the way it was - and if the putting-back cannot happen, because the run
    was killed or because something has the file open, the copy is still on the
    disk with the date on it and can be put back by hand.

    Only when a check says it touches the board, so an ordinary run that never
    goes near it is not slowed down or rewritten for no reason.
    """

    from . import swarm as swarm_lab

    if not any(_a_case_can_touch_the_board(case) for case in cases):
        yield ""
        return
    live = swarm_lab.where_it_lives()
    kept_ones = swarm_lab.where_the_kept_ones_live()
    with _board_preservation_file_lock(live):
        _recover_board_transactions(live, kept_ones)
        was = _read_or_nothing(live)
        saved_directory_existed, saved_was = _snapshot_saved_boards(kept_ones)
        transaction = _keep_a_copy_of_the_board(
            live, was, saved_was, run_id,
            saved_directory_existed=saved_directory_existed,
        )
        try:
            # The file lock deliberately is not re-entrant across threads. A
            # browser/HTTP check reaches the server on a different worker, so
            # only requests carrying this exact live transaction capability
            # may cross the lock they are testing behind.
            with _active_board_qa_capability(live) as capability:
                yield capability
        finally:
            displaced_live = _read_or_nothing(live)
            displaced_exists, displaced_saved = _snapshot_saved_boards(kept_ones)
            _record_displaced_boards(
                transaction,
                displaced_live,
                displaced_saved,
                saved_directory_existed=displaced_exists,
                differs=(
                    displaced_live != was
                    or displaced_exists != saved_directory_existed
                    or displaced_saved != saved_was
                ),
            )
            _put_the_board_back(live, was)
            _put_the_saved_boards_back(
                kept_ones, saved_was,
                directory_existed=saved_directory_existed,
            )
            _mark_board_transaction_restored(transaction)


def _read_or_nothing(where: "Path") -> str | None:
    try:
        return where.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise QaError(
            f"The existing agent-board file at {where} cannot be read. Nexus "
            "refused to run a board-touching check because absence and unreadable "
            "data are not the same thing."
        ) from exc


def _snapshot_saved_boards(where: "Path") -> tuple[bool, dict[str, str]]:
    try:
        if not where.exists():
            return False, {}
        if not where.is_dir():
            raise QaError(
                f"The saved-board location {where} is not a folder. No check was run."
            )
        saved: dict[str, str] = {}
        for one in sorted(where.iterdir()):
            if one.suffix.lower() != ".json":
                continue
            if not one.is_file() or one.is_symlink():
                raise QaError(
                    f"Saved board {one} is not a regular local JSON file. No check was run."
                )
            saved[one.name] = one.read_text(encoding="utf-8")
        return True, saved
    except QaError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise QaError(
            f"The saved-board library at {where} cannot be snapshotted exactly. "
            "Nexus refused to run a board-touching check."
        ) from exc


def _keep_a_copy_of_the_board(
    live: "Path", was: str | None, saved_was: dict, run_id: str, *,
    saved_directory_existed: bool,
) -> "Path":
    """Write the board aside before anything runs, and leave it there.

    This is the part that holds when nothing else does. A run killed outright
    never reaches the putting-back; a file something has open cannot be moved
    over. Neither of those can touch a copy that was already written and is not
    tidied away afterwards.
    """

    try:
        where = live.parent / WHERE_THE_BOARD_IS_COPIED
        where.mkdir(parents=True, exist_ok=True)
        _throw_away_the_oldest_copies(where)
        _refuse_when_retained_board_recovery_is_full(where)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        identity = f"{time.time_ns():020d}"
        safe_run = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id or "checks")[:80]
        path = where / f"{stamp}-{identity}-{safe_run}-transaction.json"
        put_this_file_in_place(path, json.dumps({
            "schema_version": 1,
            "kind": "saved-board-check-preservation",
            "state": "active",
            "why": "Copied before checks ran, in case they leave it wrong.",
            "when": stamp,
            "run": run_id,
            "live_existed": was is not None,
            "board": was,
            "saved_directory_existed": bool(saved_directory_existed),
            "saved_boards": saved_was,
        }, ensure_ascii=False, indent=2) + "\n")
        _throw_away_the_oldest_copies(where)
        return path
    except QaError:
        raise
    except (OSError, HarnessError, ValueError) as exc:
        raise QaError(
            "Nexus could not create and verify the pre-check saved-board backup. "
            "No board-touching check was run."
        ) from exc


def _throw_away_the_oldest_copies(where: "Path") -> None:
    """Prune only settled backups that contain no displaced user candidate."""

    try:
        held = sorted(where.glob("*-transaction.json"))
    except OSError:
        return
    ordinary: list[Path] = []
    for one in held:
        try:
            record = _read_board_transaction(one)
        except QaError:
            # An unknown journal is evidence, not disposable cache.
            continue
        if (
            record["state"] == "restored"
            and not record.get("displaced_copy_retained")
            and not record.get("displaced_copies")
        ):
            ordinary.append(one)
    for one in ordinary[:max(0, len(ordinary) - MOST_BOARD_COPIES + 1)]:
        try:
            one.unlink()
        except OSError:
            continue


def _refuse_when_retained_board_recovery_is_full(where: "Path") -> None:
    try:
        transactions = sorted(where.glob("*-transaction.json"))
    except OSError as exc:
        raise QaError(
            f"Nexus cannot inspect saved-board recovery journals at {where}."
        ) from exc
    retained = 0
    for transaction in transactions:
        record = _read_board_transaction(transaction)
        if record.get("displaced_copy_retained") or record.get("displaced_copies"):
            retained += 1
    if retained >= MOST_RETAINED_BOARD_RECOVERIES:
        raise QaError(
            "Nexus retained the maximum number of board recovery candidates "
            f"({MOST_RETAINED_BOARD_RECOVERIES}). Review and deliberately archive "
            f"or remove resolved journals in {where} before running another "
            "board-touching check; Nexus will not discard possible user saves."
        )


def _read_board_transaction(where: "Path") -> dict[str, Any]:
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QaError(
            f"Saved-board preservation journal {where} cannot be read. Nexus "
            "refused to guess whether a check had finished restoring data."
        ) from exc
    saved = held.get("saved_boards") if isinstance(held, dict) else None
    displaced_copies = held.get("displaced_copies", []) if isinstance(held, dict) else []
    if (
        not isinstance(held, dict)
        or held.get("schema_version") != 1
        or held.get("kind") != "saved-board-check-preservation"
        or held.get("state") not in {"active", "restored"}
        or not isinstance(held.get("live_existed"), bool)
        or not isinstance(held.get("saved_directory_existed"), bool)
        or not isinstance(saved, dict)
        or any(
            not isinstance(name, str)
            or Path(name).name != name
            or not name.lower().endswith(".json")
            or not isinstance(body, str)
            for name, body in saved.items()
        )
        or (held["live_existed"] and not isinstance(held.get("board"), str))
        or (not held["live_existed"] and held.get("board") is not None)
        or not isinstance(displaced_copies, list)
        or any(
            not isinstance(copy, dict)
            or not isinstance(copy.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", copy["sha256"]) is None
            or not isinstance(copy.get("live_existed"), bool)
            or not isinstance(copy.get("saved_directory_existed"), bool)
            or not isinstance(copy.get("saved_boards"), dict)
            or any(
                not isinstance(name, str)
                or Path(name).name != name
                or not name.lower().endswith(".json")
                or not isinstance(body, str)
                for name, body in copy["saved_boards"].items()
            )
            or (copy["live_existed"] and not isinstance(copy.get("board"), str))
            or (not copy["live_existed"] and copy.get("board") is not None)
            for copy in displaced_copies
        )
    ):
        raise QaError(
            f"Saved-board preservation journal {where} is invalid. Nexus refused "
            "to perform a partial recovery."
        )
    for copy in displaced_copies:
        without_digest = {key: value for key, value in copy.items() if key != "sha256"}
        digest = hashlib.sha256(json.dumps(
            without_digest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(copy["sha256"], digest):
            raise QaError(
                f"Saved-board recovery candidate in {where} failed its SHA-256 "
                "integrity check. Nexus preserved it and refused to guess."
            )
    return held


def _mark_board_transaction_restored(where: "Path") -> dict[str, Any]:
    held = _read_board_transaction(where)
    held["state"] = "restored"
    held["restored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    put_this_file_in_place(
        where, json.dumps(held, ensure_ascii=False, indent=2) + "\n",
    )
    return held


def _record_displaced_boards(
    where: "Path", live: str | None, saved: dict[str, str], *,
    saved_directory_existed: bool, differs: bool,
) -> dict[str, Any]:
    """Keep any bytes a check/concurrent UI save left before restoration."""

    held = _read_board_transaction(where)
    if differs:
        candidate = {
            "live_existed": live is not None,
            "board": live,
            "saved_directory_existed": bool(saved_directory_existed),
            "saved_boards": saved,
        }
        canonical = json.dumps(
            candidate, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        candidate["sha256"] = hashlib.sha256(canonical).hexdigest()
        copies = list(held.get("displaced_copies") or [])
        # Migrate the original single-copy field without changing or replacing
        # its bytes.  Older callers may still inspect displaced_after_check.
        previous = held.get("displaced_after_check")
        if isinstance(previous, dict) and not copies:
            legacy = dict(previous)
            legacy_canonical = json.dumps(
                legacy, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            legacy["sha256"] = hashlib.sha256(legacy_canonical).hexdigest()
            copies.append(legacy)
        if not any(one.get("sha256") == candidate["sha256"] for one in copies):
            copies.append(candidate)
        held["displaced_copies"] = copies
        held.setdefault("displaced_after_check", {
            key: value for key, value in candidate.items() if key != "sha256"
        })
        held["displaced_copy_retained"] = True
        held["recovery_note"] = (
            "The exact board state present when the check ended is retained here. "
            "It may contain check fixtures or a concurrent user save; review/import "
            "wanted saved-board JSON instead of guessing."
        )
    put_this_file_in_place(
        where, json.dumps(held, ensure_ascii=False, indent=2) + "\n",
    )
    _remember_board_recovery_notice(
        where, bool(held.get("displaced_copy_retained")),
    )
    return held


def _recover_board_transactions(live: "Path", saved_folder: "Path") -> None:
    where = live.parent / WHERE_THE_BOARD_IS_COPIED
    try:
        transactions = sorted(where.glob("*-transaction.json"))
    except OSError as exc:
        raise QaError(
            f"Nexus cannot inspect saved-board recovery journals at {where}. "
            "No board-touching check was run."
        ) from exc
    retained: list[str] = []
    for transaction in transactions:
        held = _read_board_transaction(transaction)
        if held["state"] == "active":
            displaced_live = _read_or_nothing(live)
            displaced_exists, displaced_saved = _snapshot_saved_boards(saved_folder)
            held = _record_displaced_boards(
                transaction, displaced_live, displaced_saved,
                saved_directory_existed=displaced_exists,
                differs=(
                    displaced_live != (held["board"] if held["live_existed"] else None)
                    or displaced_exists != held["saved_directory_existed"]
                    or displaced_saved != held["saved_boards"]
                ),
            )
            _put_the_board_back(
                live, held["board"] if held["live_existed"] else None,
            )
            _put_the_saved_boards_back(
                saved_folder, held["saved_boards"],
                directory_existed=held["saved_directory_existed"],
            )
            held = _mark_board_transaction_restored(transaction)
        if held.get("displaced_copy_retained"):
            retained.append(transaction.name)
    where.mkdir(parents=True, exist_ok=True)
    _write_board_notice_index(where, retained)


def recover_abandoned_board_transactions() -> bool:
    """Recover killed board QA on ordinary startup without racing a live check."""

    from . import swarm as swarm_lab

    live = swarm_lab.where_it_lives()
    saved = swarm_lab.where_the_kept_ones_live()
    try:
        with _board_preservation_file_lock(live, timeout_seconds=0.0):
            _recover_board_transactions(live, saved)
    except BoardPreservationBusy:
        return False
    return True


def retained_board_recovery_notices() -> list[str]:
    """Name preserved post-check bytes that may include a concurrent user save."""

    from . import swarm as swarm_lab

    live = swarm_lab.where_it_lives()
    where = live.parent / WHERE_THE_BOARD_IS_COPIED
    try:
        names = _read_board_notice_index(where)
    except QaError as exc:
        return [
            f"{exc} Review the recovery folder at {where}; Nexus did not hide "
            "the recovery problem."
        ]
    return [
        "A board-touching check restored your pre-check boards and retained "
        f"the exact displaced state for review at {where / name}. It may "
        "contain check fixtures or a concurrent save."
        for name in names
        if (where / name).is_file()
    ]


def _put_the_board_back(live: "Path", was: str | None) -> None:
    """Put the live board back the way it was found.

    Through the patient writer, because Windows will not move a file over one
    that anything has open - and a panel reading the board is exactly that. Left
    to a plain move this threw, and the throw was swallowed, and the board was
    left as the check left it.
    """

    try:
        now = _read_or_nothing(live)
    except QaError:
        now = object()
    if now == was:
        return
    if was is None:
        try:
            live.unlink(missing_ok=True)
        except OSError as exc:
            _say_it_could_not_be_put_back(live)
            raise QaError(
                f"The check-created live board at {live} could not be removed."
            ) from exc
        return
    try:
        put_this_file_in_place(live, was)
    except (OSError, HarnessError):
        # The copy written before the run is still on the disk, so nothing is
        # gone for good. Said out loud rather than swallowed: somebody has to
        # know their board is not what they left it.
        _say_it_could_not_be_put_back(live)
        raise QaError(
            f"The agent board at {live} could not be restored automatically."
        )


def _put_the_saved_boards_back(
    where: "Path", saved_was: dict, *, directory_existed: bool = True,
) -> None:
    """Put back any board somebody had saved under a name.

    A check can delete one of these as easily as it can change the live board,
    and deleting somebody's saved arrangement is the worse of the two.
    """

    try:
        current = set()
        if where.exists():
            if not where.is_dir():
                raise OSError(f"{where} is not a folder")
            current = {
                one.name for one in where.iterdir()
                if one.suffix.lower() == ".json"
            }
        elif saved_was or directory_existed:
            where.mkdir(parents=True, exist_ok=True)
        for extra in current - set(saved_was):
            (where / extra).unlink()
        for name, held in saved_was.items():
            one = where / name
            try:
                current_text = one.read_text(encoding="utf-8")
            except FileNotFoundError:
                current_text = None
            except (OSError, UnicodeDecodeError):
                current_text = object()
            if current_text != held:
                put_this_file_in_place(one, held)
        if not directory_existed and where.exists() and not any(where.iterdir()):
            where.rmdir()
    except (OSError, HarnessError) as exc:
        _say_it_could_not_be_put_back(where)
        raise QaError(
            f"The saved-board library at {where} could not be restored exactly."
        ) from exc


def _say_it_could_not_be_put_back(where: "Path") -> None:
    """Say so, where the first version of this said nothing at all.

    Swallowed, somebody is left with a board that is not the one they arranged
    and no reason to think anything happened to it.
    """

    import sys

    print(
        f"The agent board at {where} could not be put back after the checks. "
        f"A copy from before they ran is in "
        f"{where.parent / WHERE_THE_BOARD_IS_COPIED}.",
        file=sys.stderr,
    )


class QaRunner:
    """Runs a suite. Only the process runner and local file reads touch disk."""

    def __init__(
        self,
        config: LoadedConfig,
        *,
        command_runner: CommandRunner | None = None,
        http_fetch: Callable[[QaCase, float], tuple[int, str, int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        extra_kinds: Mapping[str, CheckKind] | None = None,
        environment: str = "",
        update_baselines: bool = False,
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
        self.extra_kinds = validated_kinds(extra_kinds)
        self.environment_name, self.environment = datasets.chosen_environment(config, environment)
        self.update_baselines = bool(update_baselines)
        self._board_qa_capability = ""
        self._browser_ready: bool | None = None
        self._browser_why = ""
        # Screenshot checks save pictures while they run. Cases run side by side,
        # so the list of saved files is kept under a lock, one entry per case.
        self._pictures: dict[str, list[str]] = {}
        self._picture_lock = threading.Lock()

    # -- selection ---------------------------------------------------------

    def expand(self, case: QaCase) -> tuple[QaCase, ...]:
        """One written check becomes one run for each row of its table.

        Every piece of text in the case is filled in from the row and from the
        chosen settings, so the rest of the harness sees ordinary cases and
        every report, retry and flaky rule works unchanged.
        """

        rows = case.rows
        if case.rows_file and not rows:
            rows = datasets.read_rows(self.config, case.rows_file)
        if not rows:
            if self.environment:
                return (self._filled(case, None),)
            return (case,)
        built: list[QaCase] = []
        for row in rows:
            filled = self._filled(case, row)
            built.append(
                replace(
                    filled,
                    id=f"{case.id}#{row.number}",
                    title=f"{case.title} [{row.label}]",
                    row=row,
                    rows=(),
                    rows_file="",
                )
            )
        return tuple(built)

    def _filled(self, case: QaCase, row: datasets.Row | None) -> QaCase:
        values = row.mapping() if row is not None else {}
        where = f"Check {case.id}"
        changes: dict[str, Any] = {}
        for name in FILLABLE_CASE_FIELDS:
            found = getattr(case, name)
            if found:
                changes[name] = datasets.fill_value(found, values, self.environment, where)
        if case.extra:
            changes["extra"] = tuple(
                (key, datasets.fill_value(item, values, self.environment, where))
                for key, item in case.extra
            )
        expect_changes: dict[str, Any] = {}
        for name in (
            "stdout_contains", "stdout_not_contains", "stderr_contains", "stderr_not_contains",
            "contains", "not_contains", "body_contains", "body_not_contains",
        ):
            found = getattr(case.expect, name)
            if found:
                expect_changes[name] = tuple(
                    datasets.fill(item, values, self.environment, where) for item in found
                )
        if case.expect.json_fields:
            expect_changes["json_fields"] = tuple(
                (key, datasets.fill_value(item, values, self.environment, where))
                for key, item in case.expect.json_fields
            )
        if case.expect.extra:
            expect_changes["extra"] = tuple(
                (key, datasets.fill_value(item, values, self.environment, where))
                for key, item in case.expect.extra
            )
        if expect_changes:
            changes["expect"] = replace(case.expect, **expect_changes)
        return replace(case, **changes) if changes else case

    def select(
        self,
        suite: QaSuite,
        *,
        tags: Iterable[str] = (),
        ids: Iterable[str] = (),
        part: tuple[int, int] = (0, 0),
    ) -> tuple[QaCase, ...]:
        wanted_tags = {str(tag).lower() for tag in tags}
        wanted_ids = {str(item).lower() for item in ids}
        unknown_ids = wanted_ids - {case.id for case in suite.cases}
        if unknown_ids:
            raise QaError(f"No case has this id: {sorted(unknown_ids)[0]}")
        unknown_tags = wanted_tags - set(suite.tags())
        if unknown_tags:
            raise QaError(f"No case has this tag: {sorted(unknown_tags)[0]}")
        chosen: list[QaCase] = []
        for case in suite.cases:
            if wanted_ids and case.id not in wanted_ids:
                continue
            if wanted_tags and not wanted_tags & set(case.tags):
                continue
            chosen.extend(self.expand(case))
        if not chosen:
            raise QaError("The filter matched no cases")
        number, of = part
        if of:
            if not 1 <= number <= of:
                raise QaError(
                    f"There is no part {number} of {of}. Number the parts from 1 up to {of}."
                )
            chosen = _one_part_of(chosen, number, of)
            if not chosen:
                raise QaError(
                    f"Part {number} of {of} holds no checks. There are fewer checks than "
                    "parts, so some machines would have nothing to do."
                )
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
        immutable_artifacts: bool = False,
        part: tuple[int, int] = (0, 0),
    ) -> QaRunResult:
        selected = self.select(suite, tags=tags, ids=ids, part=part)
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
            try:
                artifacts_root.mkdir(parents=True, exist_ok=not immutable_artifacts)
            except FileExistsError as exc:
                raise QaError(
                    f"The immutable QA artifact run {identifier} already exists; it was not overwritten."
                ) from exc
        started = self.clock()
        results: dict[str, QaCaseResult] = {}
        # One lock for each thing any check says it touches. Checks still run
        # several at a time; two that change the same thing simply wait for
        # each other rather than fighting over it.
        held = {thing: threading.Lock() for case in selected for thing in case.touches}
        # The board somebody actually uses, put aside before any of this runs.
        # Checks that rearrange the board put it back themselves at the end, and
        # that is no help at all when a check is killed part way through: the
        # step that puts it back never runs, and somebody's agents are gone. It
        # cost a real person their board once, which is once too often.
        with _the_board_put_back_afterwards(selected, identifier) as board_capability:
            self._board_qa_capability = board_capability
            try:
                with _pipeline_definitions_put_back_afterwards(
                    self.root, selected, identifier
                ):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                        futures = {
                            pool.submit(self._run_case, case, artifacts_root, held): case
                            for case in selected
                        }
                        for future in concurrent.futures.as_completed(futures):
                            case = futures[future]
                            results[case.id] = future.result()
            finally:
                self._board_qa_capability = ""
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
            part=part,
            artifacts_dir=(
                artifacts_root.relative_to(self.root).as_posix() if artifacts_root else ""
            ),
        )
        if artifacts_root is not None:
            result_body = json.dumps(
                CredentialRedactor(self.config).value(result.to_dict()),
                indent=2, sort_keys=True,
            )
            mode = "x" if immutable_artifacts else "w"
            with (artifacts_root / "result.json").open(mode, encoding="utf-8") as stream:
                stream.write(result_body)
            if not immutable_artifacts:
                self._trim_runs()
        return result

    def _trim_runs(self) -> None:
        keep = int(self.config.get("qa.keep_runs", 20))
        if keep <= 0:
            return
        base = _control_path(self.root, str(self.config.get("qa.artifacts_dir", ".harness/qa/runs")))
        if not base.is_dir():
            return
        # Only folders this tool made itself, which means the ones holding a
        # result it wrote. Nothing else here is touched: if someone points the
        # runs folder at a place that already holds their own work, tidying up
        # old runs must not take that with it.
        folders = sorted(
            (
                item for item in base.iterdir()
                if item.is_dir() and (item / "result.json").is_file()
            ),
            key=lambda item: item.name,
        )
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

    def _run_case(
        self,
        case: QaCase,
        artifacts_root: Path | None,
        held: Mapping[str, threading.Lock] | None = None,
    ) -> QaCaseResult:
        # Always in the same order, so two checks that touch the same two
        # things can never sit waiting for each other forever.
        locks = [held[thing] for thing in sorted(case.touches)] if held else []
        with contextlib.ExitStack() as waiting:
            for lock in locks:
                waiting.enter_context(lock)
            if _a_case_can_touch_the_board(case) and self._board_qa_capability:
                from . import swarm as swarm_lab

                waiting.enter_context(swarm_lab._using_board_qa_request_capability(  # noqa: SLF001
                    self._board_qa_capability
                ))
            return self._really_run_case(case, artifacts_root)

    def _really_run_case(self, case: QaCase, artifacts_root: Path | None) -> QaCaseResult:
        attempts: list[QaAttempt] = []
        artifacts: list[str] = []
        started = self.clock()
        case_folder = artifacts_root / case.id if artifacts_root is not None else None
        for number in range(1, case.retries + 2):
            attempt, evidence_text = self._attempt(case, number, case_folder)
            attempts.append(attempt)
            if case_folder is not None and evidence_text:
                case_folder.mkdir(parents=True, exist_ok=True)
                name = f"attempt-{number}.txt"
                (case_folder / name).write_text(
                    CredentialRedactor(self.config).text(evidence_text),
                    encoding="utf-8", errors="replace",
                )
                artifacts.append(f"{case.id}/{name}")
            if attempt.passed:
                break
        with self._picture_lock:
            artifacts.extend(self._pictures.pop(case.id, []))
        duration = int((self.clock() - started) * 1000)
        last = attempts[-1]
        # A skipped attempt says "this could not be tried here". It must never
        # wipe out a real failure that an earlier attempt already found, so the
        # result is worked out from the attempts that really ran.
        ran = [item for item in attempts if not item.skipped]
        if not ran:
            status = STATUS_SKIPPED
            reasons = last.reasons
        elif not ran[-1].passed:
            status = STATUS_FAILED
            reasons = ran[-1].reasons
        elif any(not item.passed for item in ran):
            status = STATUS_FLAKY
            reasons = (
                f"Passed on attempt {len(attempts)} after failing earlier. "
                "A test that only passes sometimes is not trustworthy yet.",
            )
        else:
            status = STATUS_PASSED
            reasons = ()
        if status != STATUS_SKIPPED and last.skipped:
            # Say plainly that the last try never happened, so nobody wonders
            # why the numbers do not add up.
            reasons = (*reasons, f"A later attempt was skipped: {last.reasons[0] if last.reasons else 'no reason given'}")
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

    def _attempt(
        self, case: QaCase, number: int, folder: Path | None = None
    ) -> tuple[QaAttempt, str]:
        started = self.clock()
        skipped = ""
        try:
            if case.kind in self.extra_kinds:
                handler = self.extra_kinds[case.kind].run
                reasons, evidence, full = handler(case, self)
            elif case.kind == "command":
                reasons, evidence, full = self._check_command(case)
            elif case.kind == "file":
                reasons, evidence, full = self._check_file(case)
            elif case.kind == "browser":
                reasons, evidence, full = self._check_browser(case, number, folder)
            elif case.kind == "visual":
                reasons, evidence, full = self._check_visual(case, number, folder)
            elif case.kind == "secrets":
                reasons, evidence, full = self._check_secrets(case)
            elif case.kind == "crawl":
                reasons, evidence, full = self._check_crawl(case)
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
        if expect.contract or expect.contract_file:
            reasons.extend(self._contract_reasons(case, body))
        full = f"{case.method} {case.url}\nstatus: {status}\n\n{body}\n"
        return tuple(reasons), _excerpt(body, 400), full

    def _contract_reasons(self, case: QaCase, body: str) -> list[str]:
        """Compare the answer with the shape the case says it should have."""

        expect = case.expect
        if expect.contract_file:
            if re.split(r"[\\/]", expect.contract_file)[0].lower() == ".git":
                raise QaError("A contract may not be read from inside the .git folder")
            path = _control_path(self.root, expect.contract_file)
            if not path.is_file():
                # A missing contract means nothing was checked, so it fails.
                # Passing here would be the worst kind of quiet.
                return [f"There is no contract at {expect.contract_file}, so nothing was checked"]
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                return [f"Cannot read the contract {expect.contract_file}: {exc}"]
            except json.JSONDecodeError as exc:
                return [f"The contract {expect.contract_file} is not valid JSON: {exc.msg}"]
        else:
            schema = json.loads(expect.contract)
        try:
            answer = json.loads(body)
        except json.JSONDecodeError as exc:
            return [f"The answer is not JSON, so its shape could not be checked: {exc.msg}"]
        try:
            return list(contracts.problems(answer, schema, "the answer"))
        except HarnessError as exc:
            return [str(exc)]

    def walk_over(self, case: QaCase) -> tuple[tuple[str, ...], str, str]:
        """Open every page a site links to, and say what was found.

        The same walk the crawl check does, offered by name so other parts of
        the harness can ask for one without pretending to be a check.
        """

        return self._check_crawl(case)

    def _check_crawl(self, case: QaCase) -> tuple[tuple[str, ...], str, str]:
        """Open every page the site links to, from one starting address."""

        self._check_host(case.url)
        timeout = case.timeout_seconds or max(self.default_timeout, 180.0)
        self._ready_for_browser()
        plan = self._browser_plan(case, timeout)
        plan["routes"] = []
        plan["checkAccessibility"] = case.check_accessibility
        # A walk follows links, so every place it could reach has to be checked,
        # not only the one it starts from. Without this, one field in a suite
        # file could send a real browser at any address on the network.
        boundary = case.stay_under or _folder_of(case.url)
        self._check_host(boundary)
        plan["crawl"] = {
            "maxPages": case.max_pages,
            # Links are only followed while they stay under this address, so a
            # walk of your own site never wanders onto somebody else's.
            "stayUnder": boundary,
            # And the page checks the host of every link as well, so a redirect
            # or an odd address cannot slip past the text comparison.
            "allowedHosts": list(self.allowed_hosts),
        }
        report = self._drive_browser(case, plan, timeout)
        return crawl_reasons(case, report), crawl_summary(report), json.dumps(report, indent=2)

    def _check_secrets(self, case: QaCase) -> tuple[tuple[str, ...], str, str]:
        """Read the project's own files and look for credentials left in them.

        This one never reports "skipped". A security check that did not run
        must say so as a failure, or nobody will ever look again.
        """

        report = scan.scan_project(
            self.config, include=case.paths or ("**/*",), skip=case.skip
        )
        allowed = case.expect.max_findings if case.expect.max_findings is not None else 0
        reasons = scan.reasons(report, allowed)
        summary = (
            f"Read {report.files_read} files, skipped {report.files_skipped}. "
            f"Found {len(report.real)} to look at"
            + (f", and {len(report.allowed)} marked as allowed on purpose" if report.allowed else "")
            + "."
        )
        return tuple(reasons), summary, json.dumps(report.to_dict(), indent=2)

    def _browser_plan(self, case: QaCase, timeout: float) -> dict[str, Any]:
        plan = {
            "url": case.url,
            "routes": list(case.routes) or ["/"],
            "viewport": {"width": case.viewport[0], "height": case.viewport[1]},
            "clickAll": case.click_all,
            "checkAccessibility": case.check_accessibility,
            "steps": [dict(step) for step in case.steps],
            "timeoutMs": int(min(timeout, 120) * 1000),
            "settleMs": 250,
            "screenshot": None,
            "measure": case.expect.wants_speed,
            "crawl": None,
            "pictures": "never",
            "picturesFolder": "",
            "attempt": 1,
        }
        if _a_case_can_touch_the_board(case) and self._board_qa_capability:
            plan["boardQaCapability"] = {
                "origin": urllib.parse.urlsplit(case.url)._replace(
                    path="", query="", fragment=""
                ).geturl().rstrip("/"),
                "header": BOARD_QA_CAPABILITY_HEADER,
                "token": self._board_qa_capability,
            }
        return plan

    def _ready_for_browser(self) -> None:
        if not self.browser_available():
            why = getattr(self, "_browser_why", "")
            raise QaSkipped(
                "This machine has no Playwright browser driver yet. Install Node.js, then run "
                "'npm install playwright' and 'npx playwright install chromium' in the project."
                + (f" What it tried: {why}" if why else "")
            )

    def _drive_browser(
        self, case: QaCase, plan: Mapping[str, Any], timeout: float, keep: Path | None = None
    ) -> dict[str, Any]:
        """Write the one-off Playwright script, run it, and read what it says.

        `keep` is a folder the script may leave files in, such as a screenshot.
        Without it the working folder is removed as soon as the script ends.
        """

        # Each case gets its own folder. Two runs of the same project can then
        # clean up after themselves without one removing the other's script.
        base = _control_path(self.root, ".harness/qa/tmp")
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QaError(f"Cannot use the working folder {base}: {exc}") from exc
        folder = keep or Path(tempfile.mkdtemp(prefix=f"{case.id}-", dir=base))
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / "browser.js"
        browser_plan = dict(plan)
        environment_overrides: dict[str, str] | None = None
        capability = browser_plan.get("boardQaCapability")
        if isinstance(capability, dict) and capability.get("token"):
            token = str(capability["token"])
            capability = dict(capability)
            capability.pop("token", None)
            capability["tokenEnvironment"] = "NEXUS_BOARD_QA_CAPABILITY"
            browser_plan["boardQaCapability"] = capability
            environment_overrides = {"NEXUS_BOARD_QA_CAPABILITY": token}
        script.write_text(browser_script(browser_plan), encoding="utf-8")
        try:
            run_arguments: dict[str, Any] = {
                "cwd": ".", "timeout": timeout,
            }
            if environment_overrides:
                run_arguments["environment_overrides"] = environment_overrides
            result = self.commands.run(
                ["node", script.relative_to(self.root).as_posix()], **run_arguments
            )
        finally:
            # Tidying up must never be the thing that fails a check. On Windows
            # something else - a virus scanner, a file indexer, the browser
            # letting go a moment late - can still be holding this file, and a
            # check that passed should not be reported as broken because of a
            # temporary file nobody will ever look at.
            _let_go_of(script, folder if keep is None else None)
        marker = "<<<QA_REPORT>>>"
        if marker not in result.stdout:
            detail = (result.stderr or result.stdout).strip()
            raise QaError(f"The browser check did not report back: {_excerpt(detail, 600)}")
        try:
            report = json.loads(result.stdout.split(marker, 1)[1])
        except json.JSONDecodeError as exc:
            raise QaError(f"The browser report is not valid JSON: {exc.msg}") from exc
        return report

    def _check_browser(
        self, case: QaCase, number: int = 1, folder: Path | None = None
    ) -> tuple[tuple[str, ...], str, str]:
        self._check_host(case.url)
        timeout = case.timeout_seconds or max(self.default_timeout, 120.0)
        self._ready_for_browser()
        plan = self._browser_plan(case, timeout)
        keep: Path | None = None
        if case.steps and case.pictures != "never":
            # Pictures land in the run folder beside the rest of the evidence,
            # so somebody can see the page as it was when a step went wrong.
            keep = folder or _control_path(self.root, f".harness/qa/tmp/{case.id}-pictures")
            keep.mkdir(parents=True, exist_ok=True)
            plan["pictures"] = case.pictures
            plan["picturesFolder"] = keep.relative_to(self.root).as_posix()
            # Each try keeps its own pictures, so a second attempt never paints
            # over what the first one saw.
            plan["attempt"] = number
        report = self._drive_browser(case, plan, timeout, keep=keep)
        if keep is not None:
            for step in report.get("steps") or []:
                name = str(step.get("picture") or "")
                if name and (keep / name).is_file():
                    self._remember_picture(case.id, f"{case.id}/{name}")
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
            picture = str(step.get("picture") or "")
            # A tidy-up step is named as one, because it failing means
            # something this check changed has been left changed.
            what = "The tidying up after step" if step.get("tidyUp") else "Step"
            reasons.append(
                f"{what} {position} of {len(case.steps)} did not work: {named or 'the step'}. "
                f"The browser said: {_quote(str(step.get('text') or 'nothing'))}"
                + (f" A picture of the page is in the run folder: {picture}" if picture else "")
            )
        if case.steps and len(steps) < len(case.steps) and not fatal:
            skipped = len(case.steps) - len(steps)
            if any(not step.get("ok") for step in steps):
                # Deliberate: once a step has failed the rest of the workflow
                # means nothing, so it is not run and not reported as broken.
                reasons.append(
                    f"{skipped} later step{'' if skipped == 1 else 's'} "
                    f"{'was' if skipped == 1 else 'were'} skipped after that, "
                    f"so {'it was' if skipped == 1 else 'they were'} never checked"
                )
            else:
                reasons.append(
                    f"Only {len(steps)} of {len(case.steps)} steps ran, "
                    "so the rest were never checked"
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
            # An accessibility problem is only useful with the thing it is
            # about, so say which heading, image, or pair of links it means.
            if first.get("problem") and first.get("detail"):
                detail = f"{first['problem']}: {first['detail']}"
            reasons.append(
                f"Found {len(found)} {label}{plural}, more than the {limit} allowed. "
                # A problem needs enough room to name the thing it is about.
                f"First one: {_quote(str(detail), 160)}"
            )
        for route in report.get("routes") or []:
            status = int(route.get("status") or 0)
            if status >= 400 or status == 0:
                reasons.append(f"The page {route.get('route')} answered with {status or 'no status'}")
        text = str(report.get("text") or "")
        reasons.extend(_text_reasons("The page text", text, expect.body_contains, expect.body_not_contains))
        reasons.extend(speed_reasons(case, report.get("speed") or []))
        summary = json.dumps(
            {
                "routes": report.get("routes"),
                "speed": report.get("speed"),
                "console_errors": len(report.get("consoleErrors") or []),
                "page_errors": len(report.get("pageErrors") or []),
                "failed_requests": len(report.get("requestFailures") or []),
                "accessibility_problems": len(report.get("accessibility") or []),
                "steps": report.get("steps"),
            },
            indent=2,
        )
        return tuple(reasons), summary, json.dumps(report, indent=2)

    # -- screenshot checks -------------------------------------------------

    def baseline_path(self, case: QaCase) -> Path:
        """Where the saved picture for this check lives."""

        return _control_path(self.root, baseline_file(case))

    def _remember_picture(self, case_id: str, name: str) -> None:
        with self._picture_lock:
            self._pictures.setdefault(case_id, []).append(name)

    def _check_visual(
        self, case: QaCase, number: int, folder: Path | None
    ) -> tuple[tuple[str, ...], str, str]:
        self._check_host(case.url)
        timeout = case.timeout_seconds or max(self.default_timeout, 120.0)
        self._ready_for_browser()
        base = _control_path(self.root, ".harness/qa/tmp")
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QaError(f"Cannot use the working folder {base}: {exc}") from exc
        workspace = Path(tempfile.mkdtemp(prefix=f"{case.id}-shot-", dir=base))
        taken = workspace / "screenshot.png"
        plan = self._browser_plan(case, timeout)
        plan["screenshot"] = {
            "path": taken.relative_to(self.root).as_posix(),
            "selector": case.selector,
            "fullPage": case.full_page,
        }
        try:
            report = self._drive_browser(case, plan, timeout, keep=workspace)
            fatal = str(report.get("fatal") or "")
            if fatal:
                return (f"The browser stopped early: {_quote(fatal)}",), "", json.dumps(report, indent=2)
            failed_steps = [step for step in (report.get("steps") or []) if not step.get("ok")]
            if failed_steps:
                first = failed_steps[0]
                return (
                    (
                        f"The picture was never taken because a step did not work: "
                        f"{first.get('label') or 'the step'}. "
                        f"The browser said: {_quote(str(first.get('text') or 'nothing'))}",
                    ),
                    "",
                    json.dumps(report, indent=2),
                )
            if not taken.is_file():
                raise QaError(
                    "The browser did not save a picture. "
                    + (
                        f"Check that {case.selector} is on the page."
                        if case.selector
                        else "The page may never have finished loading."
                    )
                )
            raw = taken.read_bytes()
            return self._compare_to_baseline(
                case, number, folder, images.read_png(raw, "the new picture"), raw
            )
        finally:
            for leftover in workspace.glob("*"):
                leftover.unlink(missing_ok=True)
            try:
                workspace.rmdir()
            except OSError:
                pass

    def _compare_to_baseline(
        self, case: QaCase, number: int, folder: Path | None, fresh: images.Image, raw: bytes
    ) -> tuple[tuple[str, ...], str, str]:
        baseline = self.baseline_path(case)
        relative = baseline.relative_to(self.root).as_posix()
        if not baseline.is_file():
            if not self.update_baselines:
                raise QaSkipped(
                    f"There is no saved picture to compare with yet. Look at the page, then run "
                    f"'harness qa baseline --case {case.id}' to save {relative} as the one to keep."
                )
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_bytes(raw)
            return (), f"Saved the first picture as {relative}", ""
        old = images.read_png(baseline.read_bytes(), f"the saved picture {relative}")
        drift = case.expect.allowed_color_drift or 0
        difference = images.compare(old, fresh, drift)
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"attempt-{number}-now.png").write_bytes(raw)
            self._remember_picture(case.id, f"{case.id}/attempt-{number}-now.png")
            if difference.changed:
                (folder / f"attempt-{number}-difference.png").write_bytes(
                    images.write_png(difference.picture)
                )
                self._remember_picture(case.id, f"{case.id}/attempt-{number}-difference.png")
        if self.update_baselines:
            baseline.write_bytes(raw)
            return (), f"Saved the new picture as {relative}. {difference.summary()}", ""
        reasons = tuple(visual_reasons(case, difference, relative))
        summary = json.dumps(
            {
                "baseline": relative,
                "changed_pixels": difference.changed,
                "compared_pixels": difference.compared,
                "changed_percent": round(difference.percent, 4),
                "biggest_color_gap": difference.biggest_channel_gap,
                "size_before": list(difference.before_size),
                "size_after": list(difference.after_size),
            },
            indent=2,
        )
        return reasons, summary, summary

    def browser_available(self) -> bool:
        """True when Node.js can load Playwright from this project."""

        if self._browser_ready is None:
            try:
                probe = self.commands.run(
                    ["node", "-e", "require.resolve('playwright')"], cwd=".", timeout=30
                )
                self._browser_ready = probe.passed
                # What it tried, kept for the message. "No browser driver" with
                # nothing else said sends people to reinstall something that was
                # already there.
                self._browser_why = "" if probe.passed else (
                    f"node stopped with {probe.exit_code}: "
                    f"{(probe.stderr or probe.stdout or '').strip()[:300]}"
                )
            except HarnessError as exc:
                self._browser_ready = False
                self._browser_why = f"node could not be started at all: {exc}"
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
        if _a_case_can_touch_the_board(case) and self._board_qa_capability:
            request.add_header(BOARD_QA_CAPABILITY_HEADER, self._board_qa_capability)
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
// Written by Nexus Harness for one browser case. It is deleted after the run.
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
    // Something the page is not drawing right now cannot be read out either,
    // and nobody can reach it, so it is not a problem to report. It matters:
    // innerText is empty for anything inside a folded-away panel, so without
    // this every button in one would be called nameless when it is not.
    // The words on a control are its name whether the page is drawing them
    // this second or not. innerText is empty for anything inside a folded-away
    // panel, and reading only that called every button in one nameless when
    // each had a perfectly good name written on it.
    const name = (control.innerText || '').trim()
      || (control.textContent || '').trim()
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
  // Two links reading the same but going somewhere different. Somebody moving
  // through a page link by link hears "read more, read more" and cannot tell
  // them apart.
  const targetByWords = new Map();
  const alreadySaid = new Set();
  for (const link of document.querySelectorAll('a[href]')) {
    const words = (link.innerText || link.textContent || '').trim().replace(/\s+/g, ' ').toLowerCase();
    if (!words) continue;
    const target = link.getAttribute('href');
    if (!targetByWords.has(words)) { targetByWords.set(words, target); continue; }
    if (targetByWords.get(words) === target || alreadySaid.has(words)) continue;
    alreadySaid.add(words);
    add(
      'Two links say the same thing but go to different places',
      '"' + words.slice(0, 40) + '" goes to ' + targetByWords.get(words) + ' and to ' + target
    );
  }
  return problems;
}

function measurePage() {
  // The old way of asking, performance.timing, is gone from the standard and
  // reads as zeros in some pages, which made every speed check pass. These
  // entries are the current way and are either real or plainly missing.
  const entry = performance.getEntriesByType('navigation')[0];
  const paint = performance.getEntriesByName('first-contentful-paint')[0];
  const resources = performance.getEntriesByType('resource') || [];
  let bytes = entry && entry.transferSize ? entry.transferSize : 0;
  let unmeasured = 0;
  for (const item of resources) {
    if (item.transferSize > 0) bytes += item.transferSize;
    else if (item.duration > 0) unmeasured += 1;   // Another site would not say.
  }
  const useful = entry && entry.loadEventEnd > 0;
  return {
    measured: Boolean(useful),
    loadMs: useful ? Math.round(entry.loadEventEnd - entry.startTime) : null,
    readyMs: useful ? Math.round(entry.domContentLoadedEventEnd - entry.startTime) : null,
    firstPaintMs: paint ? Math.round(paint.startTime) : null,
    requests: resources.length + (entry ? 1 : 0),
    bytes,
    unmeasured,
  };
}

async function pictureOfStep(page, plan, number, worked) {
  // Pictures are only taken when the case asked for them: always, or only for
  // the step that went wrong.
  const wanted = plan.pictures || 'never';
  if (wanted === 'never' || (wanted === 'failure' && worked)) return '';
  const attempt = 'attempt-' + String(plan.attempt || 1).padStart(2, '0') + '-';
  const name = attempt + 'step-' + String(number).padStart(2, '0') + (worked ? '' : '-went-wrong') + '.png';
  try {
    await page.screenshot({ path: plan.picturesFolder + '/' + name, animations: 'disabled', scale: 'css' });
    return name;
  } catch (error) {
    return '';
  }
}

function describeStep(step) {
  if (step.do === 'wait') return 'wait ' + step.ms + ' ms';
  if (step.do === 'run') return 'run a snippet in the page';
  if (step.do === 'expect_count') return 'expect ' + step.count + ' of ' + step.target;
  if (step.text !== undefined) return step.do + ' "' + step.text + '" on ' + step.target;
  if (step.key !== undefined) return step.do + ' ' + step.key + ' on ' + step.target;
  if (step.value !== undefined) return step.do + ' ' + step.value + ' on ' + step.target;
  return step.do + ' ' + (step.target || '');
}

async function runStep(page, step) {
  const wait = step.timeout_ms || 10000;
  if (step.do === 'wait') { await page.waitForTimeout(step.ms); return; }
  if (step.do === 'run') {
    // The written snippet runs inside the page, the way a person would run it
    // in the browser's own console. Whatever it gives back is turned into text
    // and compared with what the step expects.
    const value = await page.evaluate('(async function () {' + step.script + '})()');
    const shown = value === undefined ? 'undefined' : String(value);
    if (step.text === undefined) {
      if (!value) throw new Error('the snippet gave back "' + shown + '", which is not a yes');
      return;
    }
    if (shown !== step.text) {
      throw new Error('expected "' + step.text + '" but the snippet gave back "' + shown.slice(0, 200) + '"');
    }
    return;
  }
  if (step.do === 'expect_count') {
    const deadline = Date.now() + wait;
    let seen = -1;
    while (Date.now() < deadline) {
      seen = await page.locator(step.target).count();
      if (seen === step.count) return;
      await page.waitForTimeout(100);
    }
    throw new Error('expected ' + step.count + ' of them but found ' + seen);
  }
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
        if (tag === 'input' || tag === 'textarea' || tag === 'select') {
          seen = (await target.inputValue()) || '';
          if (seen.includes(step.text)) return;
        } else {
          // Styling can change the letters on screen, for example by making
          // them all capitals. Accept the written text or the shown text.
          const shown = (await target.innerText()) || '';
          const written = (await target.textContent()) || '';
          seen = shown;
          if (shown.includes(step.text) || written.includes(step.text)) return;
        }
      }
      await page.waitForTimeout(100);
    }
    throw new Error('expected to read "' + step.text + '" but the page shows "' + seen.slice(0, 120) + '"');
  }
  throw new Error('unknown step: ' + step.do);
}

(async () => {
  const report = {
    routes: [], consoleErrors: [], pageErrors: [], requestFailures: [], accessibility: [],
    clicks: [], steps: [], text: '', fatal: '', screenshot: '', speed: [],
    pages: [], morePages: 0, refused: [],
  };
  let current = '/';
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: plan.viewport });
    const page = await context.newPage();
    if (plan.boardQaCapability) {
      // Route only requests to the checked Nexus origin. A page that reaches
      // elsewhere must never carry this local board-transaction capability.
      await page.route('**/*', async route => {
        const request = route.request();
        let sameOrigin = false;
        try {
          sameOrigin = new URL(request.url()).origin === plan.boardQaCapability.origin;
        } catch (_) {}
        if (!sameOrigin) {
          await route.continue();
          return;
        }
        const capabilityToken = process.env[plan.boardQaCapability.tokenEnvironment] || '';
        if (!capabilityToken) {
          throw new Error('board QA capability was not supplied to the browser worker');
        }
        await route.continue({ headers: {
          ...request.headers(),
          [plan.boardQaCapability.header]: capabilityToken,
        }});
      });
    }
    // The panel is used in the desktop app, and the app does not have prompt:
    // it is the one browser thing Electron takes out on purpose. A check that
    // runs somewhere more forgiving than where people use it is worse than no
    // check at all - seven buttons asked with prompt and did nothing whatsoever
    // in the app, while every check here passed. So the browser is made to
    // behave the same way, for every check, before any page loads.
    await page.addInitScript(() => {
      window.prompt = () => {
        throw new Error('prompt() is and will not be supported.');
      };
    });
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
    if (plan.crawl) {
      // Walk the site from one page, following its own links, and report what
      // each page answered. Nothing outside the starting address is opened.
      const allowedHost = (address) => {
        try {
          const host = new URL(address).hostname.replace(/^\[|\]$/g, '').toLowerCase();
          return (plan.crawl.allowedHosts || []).includes(host);
        } catch (error) {
          return false;
        }
      };
      if (!allowedHost(plan.url)) throw new Error('that address may not be opened by this project');
      // The boundary is a place in the site, not a piece of text. Comparing
      // the letters alone would let a walk of /blog wander into /blog-secret,
      // which is a different part of the site.
      const boundary = plan.crawl.stayUnder.endsWith('/')
        ? plan.crawl.stayUnder
        : plan.crawl.stayUnder + '/';
      const inside = (address) =>
        address === plan.crawl.stayUnder || address.startsWith(boundary);
      // Nothing at all is fetched from a host this project did not allow. This
      // covers a link, a redirect, and anything the page asks for afterwards,
      // instead of only the addresses we thought to compare.
      await page.route('**/*', (route) => {
        const wanted = route.request().url();
        if (allowedHost(wanted)) return route.continue();
        report.refused.push(wanted.slice(0, 200));
        return route.abort();
      });
      const seen = new Set();
      const waiting = [plan.url];
      while (waiting.length && report.pages.length < plan.crawl.maxPages) {
        const address = waiting.shift();
        if (seen.has(address)) continue;
        seen.add(address);
        current = address;
        let status = 0;
        try {
          const answer = await page.goto(address, { waitUntil: 'load', timeout: plan.timeoutMs });
          status = answer ? answer.status() : 0;
        } catch (error) {
          report.pages.push({ url: address, status: 0, problem: String((error && error.message) || error).slice(0, 200) });
          continue;
        }
        const landed = page.url();
        if (!allowedHost(landed)) {
          // It went somewhere this project may not open, so nothing on that
          // page is read.
          report.pages.push({ url: address, status, problem: 'it went to ' + landed + ', which this project may not open' });
          report.refused.push(landed.slice(0, 200));
          continue;
        }
        await page.waitForTimeout(plan.settleMs);
        let problems = [];
        if (plan.checkAccessibility) {
          problems = await page.evaluate(auditPage);
          for (const item of problems) report.accessibility.push({ route: address, ...item });
        }
        report.pages.push({ url: address, status, accessibility: problems.length });
        const links = await page.evaluate(() =>
          Array.from(document.querySelectorAll('a[href]')).map((link) => link.href)
        );
        for (const link of links) {
          const plain = String(link).split('#')[0];
          if (!inside(plain)) continue;
          if (!allowedHost(plain)) continue;
          if (seen.has(plain) || waiting.includes(plain)) continue;
          waiting.push(plain);
        }
      }
      report.morePages = waiting.length;
    }
    for (const route of plan.routes) {
      current = route;
      const target = new URL(route, plan.url).toString();
      const answer = await page.goto(target, { waitUntil: 'load', timeout: plan.timeoutMs });
      report.routes.push({ route, status: answer ? answer.status() : 0 });
      await page.waitForTimeout(plan.settleMs);
      report.text += '\n' + await page.evaluate(() => (document.body ? document.body.innerText : ''));
      if (plan.measure) {
        report.speed.push({ route, ...(await page.evaluate(measurePage)) });
      }
      if (plan.checkAccessibility) {
        const found = await page.evaluate(auditPage);
        for (const item of found) report.accessibility.push({ route, ...item });
      }
      let stepNumber = 0;
      let wentWrong = false;
      for (const step of plan.steps || []) {
        const label = step.note || describeStep(step);
        stepNumber += 1;
        // Once something has gone wrong the rest of the workflow means nothing,
        // so it is skipped. A step marked "always" is different: that is where
        // a check puts back whatever it changed, and skipping it would leave
        // the project in the state the failure caught it in.
        if (wentWrong && !step.always) continue;
        try {
          await runStep(page, step);
          report.steps.push({ route, label, ok: true, tidyUp: !!step.always, picture: await pictureOfStep(page, plan, stepNumber, true) });
        } catch (error) {
          report.steps.push({
            route, label, ok: false, tidyUp: !!step.always,
            text: String((error && error.message) || error).slice(0, 300),
            // A picture of the page at the moment it went wrong is worth more
            // than any wording, especially to somebody new.
            picture: await pictureOfStep(page, plan, stepNumber, false),
          });
          wentWrong = true;
        }
      }
      // A check runs whatever script it likes, so one of them could put an
      // answering prompt back and quietly get the forgiving browser again -
      // for itself, without anybody noticing. Asked here, after the steps, that
      // cannot pass unremarked.
      try {
        const putBack = await page.evaluate(() => {
          try { window.prompt('x'); return true; } catch (error) { return false; }
        });
        if (putBack) {
          report.steps.push({
            route: current,
            label: 'this check put prompt back, which the app does not have',
            ok: false,
            tidyUp: false,
            text: 'A check may make the rules stricter, never softer. The app has '
              + 'no prompt, so a check that gives it an answer is checking a '
              + 'browser nobody uses.',
            picture: '',
          });
          wentWrong = true;
        }
      } catch (error) { /* the page went away; the steps already said so */ }
      if (plan.screenshot) {
        // Wait for the letters to settle and stop anything that moves, so the
        // same page gives the same picture twice.
        try { await page.evaluate(() => document.fonts && document.fonts.ready); } catch (error) { /* older browser */ }
        await page.waitForTimeout(plan.settleMs);
        const shot = {
          path: plan.screenshot.path,
          animations: 'disabled',
          caret: 'hide',
          scale: 'css',
          type: 'png',
        };
        if (plan.screenshot.selector) {
          const part = page.locator(plan.screenshot.selector);
          const found = await part.count();
          if (found !== 1) {
            throw new Error(
              'the picture needs exactly one part of the page, but ' + plan.screenshot.selector
              + ' matches ' + found
            );
          }
          await part.first().screenshot(shot);
        } else {
          await page.screenshot({ ...shot, fullPage: !!plan.screenshot.fullPage });
        }
        report.screenshot = plan.screenshot.path;
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


def _let_go_of(script: Path, folder: Path | None) -> None:
    """Remove a temporary file, waiting a moment if something still holds it.

    A file another program has open cannot be removed on Windows, and the
    something is usually a virus scanner reading a file that was written a
    second ago. Waiting briefly clears it. Failing to clear it is still not
    worth failing a check over: the folder is a temporary one, and what is left
    behind is a few lines of JavaScript nobody will read.
    """

    for wait in (0.0, 0.1, 0.3, 0.6):
        if wait:
            time.sleep(wait)
        try:
            script.unlink(missing_ok=True)
            break
        except OSError:
            continue
    if folder is None:
        return
    try:
        folder.rmdir()
    except OSError:
        pass


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


def _same_json_value(found: Any, expected: Any) -> bool:
    """Is this the same value, in the same shape?

    Python says True equals 1 and False equals 0. JSON does not: an answer
    saying true where a case expects the number 1 is a different answer, and
    letting it pass means a check that reports success while the thing it
    watches has changed.
    """

    if isinstance(found, bool) != isinstance(expected, bool):
        return False
    if isinstance(found, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(found) == len(expected) and all(
            _same_json_value(one, two) for one, two in zip(found, expected)
        )
    if isinstance(found, dict) and isinstance(expected, dict):
        return set(found) == set(expected) and all(
            _same_json_value(found[key], expected[key]) for key in found
        )
    return found == expected


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
        elif not _same_json_value(found, expected):
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
            {"id": plain_xml_text(case.id), "status": case.status, "duration_ms": case.duration_ms}
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


def _median(values: Sequence[int]) -> float:
    """The middle value, which one odd result cannot drag around."""

    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return 0.0
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def check_health(
    config: LoadedConfig,
    suite: QaSuite | None = None,
    runs: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Look at the recorded runs and say what to do about the checks.

    Every finding is something a person can act on, and every one says why it
    was raised. A check with too little history is left alone.
    """

    history = list(runs if runs is not None else load_history(config))
    minimum = int(config.get("qa.flaky_min_runs", 5))
    seen: dict[str, list[dict[str, Any]]] = {}
    for run in history:
        for case in run.get("cases") or []:
            case_id = str(case.get("id") or "")
            if case_id:
                seen.setdefault(case_id, []).append(dict(case))

    findings: list[dict[str, Any]] = []
    for entry in flaky_report(config, history):
        findings.append({
            "id": entry["id"],
            "problem": "keeps changing its mind",
            "why": f"{entry['why']}. It failed {entry['failures']} of {entry['runs']} runs.",
            "what_to_do": (
                "Find what differs between runs, such as a time, a port, a temporary file, or "
                "another check running beside it. A check that only works sometimes hides real faults."
            ),
            "weight": 3 + entry["instability"],
        })

    unstable_ids = {item["id"] for item in findings}
    for case_id, records in sorted(seen.items()):
        ran = [item for item in records if str(item.get("status")) != STATUS_SKIPPED]
        if case_id in unstable_ids or len(ran) < minimum:
            continue
        statuses = [str(item.get("status")) for item in ran]
        if all(status == STATUS_FAILED for status in statuses):
            findings.append({
                "id": case_id,
                "problem": "has never passed",
                "why": f"It failed all {len(ran)} recorded runs.",
                "what_to_do": (
                    "Either fix what it is checking, or change the check if it asks for the wrong "
                    "thing. A check that always fails stops telling you anything."
                ),
                "weight": 5.0,
            })
            continue
        durations = [int(item.get("duration_ms") or 0) for item in ran]
        # A run time moves about with whatever else the machine is doing, so
        # only say a check got slower when every recent run is slow. One slow
        # run at the end proves nothing, and a wrong claim about someone's
        # tests is worse than saying nothing at all.
        window = len(durations) // 3
        if window >= 2:
            was = _median(durations[:window])
            slowest_recent = min(durations[-window:])
            if was >= 100 and slowest_recent >= was * 2 and slowest_recent - was >= 1000:
                findings.append({
                    "id": case_id,
                    "problem": "got a lot slower",
                    "why": (
                        f"Each of its last {window} runs took at least "
                        f"{round(slowest_recent / 1000, 1)} seconds. Its first {window} runs "
                        f"took about {round(was / 1000, 1)} seconds."
                    ),
                    "what_to_do": (
                        "Look at what the check runs. A check that grows slower every week ends up "
                        "being skipped, and then it is not protecting anything."
                    ),
                    "weight": 2.0,
                })

    skipped_only = [
        case_id for case_id, records in sorted(seen.items())
        if len(records) >= minimum and all(str(item.get("status")) == STATUS_SKIPPED for item in records)
    ]
    for case_id in skipped_only:
        findings.append({
            "id": case_id,
            "problem": "never actually runs",
            "why": f"It was skipped in all {len(seen[case_id])} recorded runs.",
            "what_to_do": (
                "Install what it needs, or take it out. A check that never runs gives no cover."
            ),
            "weight": 4.0,
        })

    if suite is not None and history:
        known = set(seen)
        for case in suite.cases:
            if case.id not in known:
                findings.append({
                    "id": case.id,
                    "problem": "has never been run",
                    "why": "It is in the suite but does not appear in any recorded run.",
                    "what_to_do": "Run the whole suite once so this check has a history to judge.",
                    "weight": 1.0,
                })

    findings.sort(key=lambda item: (-float(item["weight"]), item["id"]))
    for item in findings:
        item.pop("weight", None)
    return findings


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _for_people(redactor: "CredentialRedactor | None") -> "CredentialRedactor":
    """The remover a written report uses.

    A report is a file somebody sends: attached to a ticket, committed beside
    the build, pasted into a chat. What a check saw is a program's own output,
    and a program prints whatever it was given, keys included. So credentials
    come out of every report meant to be read by a person, and a caller that
    forgets to pass a remover gets one anyway.

    The run folder itself is left as it is. That is this machine's own record,
    like a log file, and hiding things there would only give false comfort.
    """

    return redactor or CredentialRedactor(None)


def _one_line(text: str) -> str:
    """A table cell holds one line, whatever the program printed.

    A reason with a line break in it split one row into two, and every row
    after it stopped being part of the table. The upright bar is the column
    mark, so that goes too.
    """

    return " ".join(str(text).split()).replace("|", "/")


def report_markdown(result: QaRunResult, redactor: "CredentialRedactor | None" = None) -> str:
    hide = _for_people(redactor).text
    counts = result.counts
    lines = [
        f"# Test run {result.run_id}",
        "",
        f"Suite: {result.suite_name}",
        f"Started: {result.started_at}",
        f"Took: {result.duration_ms} ms with {result.workers} at a time",
        *([
            f"This was part {result.part[0]} of {result.part[1]}. The other parts "
            "ran somewhere else, and this report says nothing about them.",
        ] if result.part[1] else []),
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
            f"| {_one_line(hide(case.id))} | {case.status} | {case.duration_ms} ms | "
            f"{_one_line(hide(reason))} |"
        )
    failures = [case for case in result.cases if case.status == STATUS_FAILED]
    if failures:
        lines += ["", "## Failures", ""]
        for case in failures:
            lines.append(f"### {hide(case.id)}: {hide(case.title)}")
            for reason in case.reasons:
                lines.append(f"- {hide(reason)}")
            evidence = case.attempts[-1].evidence if case.attempts else ""
            if evidence:
                lines += ["", "```text", hide(evidence), "```"]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def plain_xml_text(value: str) -> str:
    """Text an XML reader will accept.

    What a check saw comes from a program's own output, which can hold bytes
    that mean "ring the bell" or "start a colour". XML has no way to write
    those, so a build server would refuse the whole report over one stray byte
    in one check. They become spaces here, and the words stay.
    """

    keep = ("\t", "\n", "\r")

    def allowed(character: str) -> bool:
        if character in keep:
            return True
        number = ord(character)
        if number < 0x20:
            return False
        # XML has no way to write these either, however ordinary they look in
        # Python: the two non-characters at the end of each block, the block
        # kept aside in the middle, and half of a pair that lost its other
        # half. One of them anywhere made a build server refuse the whole
        # report, and a lone half stopped the file being written at all.
        if 0xD800 <= number <= 0xDFFF:
            return False
        if 0xFDD0 <= number <= 0xFDEF:
            return False
        if number in (0xFFFE, 0xFFFF):
            return False
        return True

    return "".join(character if allowed(character) else " " for character in str(value))


def report_junit_xml(result: QaRunResult, redactor: "CredentialRedactor | None" = None) -> str:
    hide = _for_people(redactor).text
    counts = result.counts
    suites = ElementTree.Element(
        "testsuites",
        {
            "name": plain_xml_text(result.suite_name),
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
            "name": plain_xml_text(result.suite_name),
            "tests": str(counts["total"]),
            "failures": str(counts[STATUS_FAILED]),
            "skipped": str(counts[STATUS_SKIPPED]),
            "time": f"{result.duration_ms / 1000:.3f}",
            "timestamp": plain_xml_text(result.started_at),
        },
    )
    if result.part[1]:
        held = ElementTree.SubElement(suite, "properties")
        ElementTree.SubElement(held, "property", {
            "name": "part",
            "value": f"{result.part[0]} of {result.part[1]}",
        })
        ElementTree.SubElement(suite, "system-out").text = (
            f"This was part {result.part[0]} of {result.part[1]}. The other parts ran "
            "somewhere else, and this file says nothing about them."
        )
    for case in result.cases:
        node = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "name": plain_xml_text(hide(case.title)),
                "classname": plain_xml_text(f"{result.suite_name}.{case.kind}"),
                "id": plain_xml_text(hide(case.id)),
                "time": f"{case.duration_ms / 1000:.3f}",
            },
        )
        if case.status == STATUS_FAILED:
            failure = ElementTree.SubElement(
                node,
                "failure",
                {"message": plain_xml_text(hide(case.reasons[0] if case.reasons else "failed"))},
            )
            failure.text = plain_xml_text(
                hide(
                    "\n".join(case.reasons)
                    + ("\n\n" + case.attempts[-1].evidence if case.attempts else "")
                )
            )
        elif case.status == STATUS_SKIPPED:
            ElementTree.SubElement(node, "skipped")
        elif case.status == STATUS_FLAKY:
            output = ElementTree.SubElement(node, "system-out")
            output.text = plain_xml_text(hide("\n".join(case.reasons)))
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


def report_html(result: QaRunResult, redactor: "CredentialRedactor | None" = None) -> str:
    hide = _for_people(redactor).text
    counts = result.counts
    headline = "All checks passed" if result.passed else "Some checks failed"
    rows = []
    for case in result.cases:
        reason = "; ".join(case.reasons) if case.reasons else "As expected"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(hide(case.id))}</code><br>{html.escape(hide(case.title))}</td>"
            f'<td class="status {case.status}">{case.status}</td>'
            f"<td>{case.duration_ms} ms</td>"
            f"<td>{html.escape(hide(reason))}</td>"
            "</tr>"
        )
    details = []
    for case in result.cases:
        if case.status == STATUS_PASSED or not case.attempts:
            continue
        evidence = case.attempts[-1].evidence
        details.append(
            f"<details><summary>{html.escape(hide(case.id))}: {html.escape(hide(case.title))}</summary>"
            + "".join(f"<p>{html.escape(hide(reason))}</p>" for reason in case.reasons)
            + (f"<pre>{html.escape(hide(evidence))}</pre>" if evidence else "")
            + "</details>"
        )
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>Test run {html.escape(result.run_id)}</title>\n<style>{_HTML_STYLE}</style>\n"
        "</head>\n<body>\n"
        f"<h1>Test run {html.escape(result.run_id)}</h1>\n"
        f"<p>Suite {html.escape(result.suite_name)}, started {html.escape(result.started_at)}.</p>\n"
        + (
            f"<p><strong>This was part {result.part[0]} of {result.part[1]}.</strong> "
            "The other parts ran somewhere else, and this page says nothing about "
            "them.</p>\n"
            if result.part[1]
            else ""
        )
        + f'<div class="summary" role="status"><p><strong>{headline}.</strong> '
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


def render_report(
    result: QaRunResult,
    output_format: str,
    redactor: "CredentialRedactor | None" = None,
) -> str:
    if output_format == "json":
        # This machine's own record, the same thing the run folder already
        # holds. Hiding things here would only give false comfort.
        return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if output_format == "markdown":
        return report_markdown(result, redactor)
    if output_format == "junit":
        return report_junit_xml(result, redactor) + "\n"
    if output_format == "html":
        return report_html(result, redactor)
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
    """Find the one JSON object in a model answer, however it was wrapped.

    A model may put prose before or after it, wrap it in a fenced block, or do
    both. Anything that is not one readable object is refused, rather than
    guessed at.
    """

    if not isinstance(text, str) or not text.strip():
        raise QaError("The model answered with nothing")
    stripped = text.strip()
    fenced = re.findall(r"```(?:[a-zA-Z0-9_-]*)\r?\n(.*?)```", stripped, re.DOTALL)
    candidates = [block.strip() for block in fenced]
    candidates.append(stripped)
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    # Say which problem it was, so a person can see what the model did wrong.
    if "{" not in stripped:
        raise QaError("The model answer did not hold a JSON object")
    raise QaError("The model answer is not valid JSON")


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


def parse_generated_cases(
    text: str,
    existing_ids: Iterable[str] = (),
    extra_kinds: Mapping[str, CheckKind] | None = None,
) -> list[dict[str, Any]]:
    """Turn a model answer into validated candidate cases with warnings attached."""

    body = _json_object(text)
    raw = body.get("cases")
    if raw is None and body.get("kind"):
        # A model sometimes answers with one case instead of a list of them.
        raw = [body]
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
        case = _parse_case(item, index, seen, extra_kinds)
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
