"""What changed between two runs of the same checks.

A list of results tells you the state of things now. The useful question, most
mornings, is different: what is different from last time? A check that has
failed for a week is not news. A check that passed yesterday and fails today is
the whole story, and it is easy to miss in a list of forty green lines and two
red ones.

So this reads two run reports and says only what moved: what started failing,
what got fixed, what is new, what went away, and what got a lot slower.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import LoadedConfig
from .models import HarnessError
from .redaction import CredentialRedactor
from .safety import confined_path

# A check has to be this much slower before it is worth mentioning: both a lot
# slower in itself, and slower by enough time for a person to notice.
SLOWER_BY = 2.0
SLOWER_THAN_MS = 500

# What a run folder may be called. A name with a slash or a dot in it is not a
# name, it is a way out of the project, so nothing else is accepted.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}")


class ComparisonError(HarnessError):
    """A problem reading the runs to compare."""


@dataclass
class Change:
    """One thing that is different from last time."""

    case_id: str
    title: str
    kind: str
    was: str
    now: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "kind": self.kind,
            "was": self.was,
            "now": self.now,
            "detail": self.detail,
        }

    def sentence(self) -> str:
        return f"{self.title or self.case_id}: {self.detail}"


@dataclass
class Comparison:
    """Everything that moved between two runs."""

    before_id: str
    after_id: str
    broke: list[Change] = field(default_factory=list)
    fixed: list[Change] = field(default_factory=list)
    added: list[Change] = field(default_factory=list)
    gone: list[Change] = field(default_factory=list)
    slower: list[Change] = field(default_factory=list)
    still_failing: list[Change] = field(default_factory=list)

    @property
    def anything_changed(self) -> bool:
        return bool(self.broke or self.fixed or self.added or self.gone or self.slower)

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before_id,
            "after": self.after_id,
            "broke": [item.to_dict() for item in self.broke],
            "fixed": [item.to_dict() for item in self.fixed],
            "added": [item.to_dict() for item in self.added],
            "gone": [item.to_dict() for item in self.gone],
            "slower": [item.to_dict() for item in self.slower],
            "still_failing": [item.to_dict() for item in self.still_failing],
        }

    def lines(self) -> list[str]:
        """Plain lines for printing, worst news first."""

        out: list[str] = []
        if not self.anything_changed:
            out.append("Nothing changed since the run before.")
            if self.still_failing:
                out.append(
                    f"{len(self.still_failing)} check"
                    f"{'' if len(self.still_failing) == 1 else 's'} were already failing and still are."
                )
            return out
        groups = (
            ("Started failing", self.broke),
            ("Fixed", self.fixed),
            ("New", self.added),
            ("Gone", self.gone),
            ("Much slower", self.slower),
        )
        for label, items in groups:
            if not items:
                continue
            out.append(f"{label}:")
            out.extend(f"  {item.sentence()}" for item in items)
        if self.still_failing:
            out.append(
                f"Still failing from before: "
                + ", ".join(item.case_id for item in self.still_failing[:10])
            )
        return out


def _cases(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    cases = report.get("cases")
    # A piece of text is a sequence too, and a report holding one is not a
    # report, so it is refused rather than read letter by letter.
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
        raise ComparisonError("That is not a run report this tool understands")
    found: dict[str, dict[str, Any]] = {}
    for item in cases:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        found[str(item["id"])] = dict(item)
    return found


def _ms(value: object) -> int:
    """How long something took, in whole milliseconds.

    A run report is a file on disk, and a file can hold anything. A time
    written as words is a report worth nothing, not a reason to stop with a
    stack trace, so it is read as no time at all.
    """

    if isinstance(value, bool) or value is None:
        return 0
    try:
        found = int(float(value))
    except (TypeError, ValueError):
        return 0
    return found if found > 0 else 0


def _first_reason(case: Mapping[str, Any]) -> str:
    """Why a check failed, as one sentence.

    A run report is a file on disk and can hold anything. A reasons field that
    is a number crashed this; one that was a piece of text gave back its first
    letter and presented it as the reason, which is worse, because it looks
    like an answer.
    """

    reasons = case.get("reasons")
    if isinstance(reasons, str) or not isinstance(reasons, Sequence):
        return "no reason given"
    for reason in reasons:
        if reason is None:
            continue
        said = str(reason).strip()
        if said:
            return said
    return "no reason given"


def _change(
    case: Mapping[str, Any],
    was: str,
    now: str,
    detail: str,
    redactor: CredentialRedactor,
) -> Change:
    return Change(
        case_id=redactor.text(str(case.get("id") or "")),
        title=redactor.text(str(case.get("title") or case.get("id") or "")),
        kind=str(case.get("kind") or ""),
        was=was,
        now=now,
        detail=redactor.text(detail),
    )


def compare(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    redactor: CredentialRedactor | None = None,
) -> Comparison:
    """What is different in the second run compared with the first.

    Why a check failed is text a program printed, and a program prints whatever
    it was given, including the key it was told to use. This summary is meant to
    be read and passed on, so credentials come out of it here. There is no way
    to turn that off: a caller that forgets to pass a remover gets one anyway.
    """

    hide = redactor or CredentialRedactor(None)
    old = _cases(before)
    new = _cases(after)
    answer = Comparison(
        before_id=hide.text(str(before.get("run_id") or "the earlier run")),
        after_id=hide.text(str(after.get("run_id") or "the later run")),
    )
    bad = {"failed", "flaky"}
    for case_id, case in new.items():
        status = str(case.get("status") or "")
        if case_id not in old:
            answer.added.append(
                _change(case, "not there", status, f"is new, and {status}", hide)
            )
            continue
        before_status = str(old[case_id].get("status") or "")
        if before_status not in bad and status in bad:
            reason = _first_reason(case)
            answer.broke.append(
                _change(case, before_status, status, f"was {before_status}, now {status}. {reason}", hide)
            )
        elif before_status in bad and status not in bad:
            answer.fixed.append(
                _change(case, before_status, status, f"was {before_status}, now {status}", hide)
            )
        elif before_status in bad and status in bad:
            answer.still_failing.append(_change(case, before_status, status, f"still {status}", hide))
        was_ms = _ms(old[case_id].get("duration_ms"))
        now_ms = _ms(case.get("duration_ms"))
        # A check that used to take no time at all and now takes seconds is the
        # biggest change there is. Ignoring it because there is nothing to
        # multiply would hide the very worst case.
        grew = (
            now_ms >= was_ms * SLOWER_BY if was_ms > 0 else now_ms >= SLOWER_THAN_MS
        )
        if grew and now_ms - was_ms >= SLOWER_THAN_MS:
            answer.slower.append(
                _change(case, f"{was_ms} ms", f"{now_ms} ms", f"took {was_ms} ms, now takes {now_ms} ms", hide)
            )
    for case_id, case in old.items():
        if case_id not in new:
            answer.gone.append(
                _change(case, str(case.get("status") or ""), "not there", "is no longer in the suite", hide)
            )
    return answer


def kept_runs(config: LoadedConfig, limit: int = 20) -> list[Path]:
    """The kept run folders, oldest first."""

    base = confined_path(
        config.project_root,
        str(config.get("qa.artifacts_dir", ".harness/qa/runs")),
        allow_missing=True,
        allow_control=True,
    )
    if not base.is_dir():
        return []
    folders = sorted((item for item in base.iterdir() if item.is_dir()), key=lambda item: item.name)
    return folders[-limit:] if limit > 0 else folders


def read_report(path: Path) -> dict[str, Any]:
    report = path / "result.json" if path.is_dir() else path
    if not report.is_file():
        raise ComparisonError(f"There is no run report at {report}")
    try:
        body = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"Cannot read {report.name}: {exc}") from exc
    if not isinstance(body, Mapping):
        raise ComparisonError(f"{report.name} does not hold a run report")
    return dict(body)


def last_two(config: LoadedConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two most recent runs that were kept, older one first."""

    folders = [folder for folder in kept_runs(config) if (folder / "result.json").is_file()]
    if len(folders) < 2:
        how_many = (
            "There are no kept runs yet"
            if not folders
            else "There is only one kept run so far"
        )
        raise ComparisonError(
            f"{how_many}, so there is nothing to compare. "
            "Run the checks again, twice if this is the first time."
        )
    return read_report(folders[-2]), read_report(folders[-1])


def kept_run_folder(config: LoadedConfig, name: str) -> Path:
    """One kept run, named the way it is written on disk.

    The name comes from whoever typed the command, so it is checked here rather
    than joined onto a folder and trusted. Anything but a plain name is refused,
    and the answer has to sit directly in the runs folder.
    """

    wanted = str(name or "").strip()
    if not wanted or not _PLAIN_NAME.fullmatch(wanted):
        raise ComparisonError(
            f"{wanted or 'That'} is not the name of a kept run. "
            "Use the folder name, such as 20260101-120000."
        )
    base = confined_path(
        config.project_root,
        str(config.get("qa.artifacts_dir", ".harness/qa/runs")),
        allow_missing=True,
        allow_control=True,
    )
    folder = (base / wanted).resolve()
    if folder.parent != base.resolve():
        raise ComparisonError(f"There is no kept run called {wanted}")
    return folder
