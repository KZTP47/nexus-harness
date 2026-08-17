"""Pipelines somebody can start from, rather than a blank board.

A blank board is the hardest thing to give a person who has not done this
before. The checks view has had ready-made checks since the beginning and the
pipelines board had one example, so this does the same job for pipelines: a
handful of shapes people really want, ready to run, each saying plainly what it
is for and when to reach for it.

Every one of these is made of the same steps anybody can drag out of the list.
There is nothing here a person could not have drawn themselves; the point is
that they do not have to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import HarnessError


class StarterError(HarnessError):
    """A ready-made pipeline that does not exist."""


@dataclass(frozen=True)
class Starter:
    """One ready-made pipeline, and when to reach for it."""

    key: str
    title: str
    when: str
    draws: dict[str, Any]
    # The words somebody would type looking for this. A gallery nobody can
    # search is a list nobody reads past the third item.
    found_by: tuple[str, ...] = ()
    # What sort of job this is, so the gallery can be grouped rather than being
    # one long column.
    group: str = "Every day"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "when": self.when,
            "steps": len(self.draws["nodes"]),
            "found_by": list(self.found_by),
            "group": self.group,
        }


def _at(x: int, y: int) -> dict[str, int]:
    return {"x": x, "y": y}


MORE: tuple[Starter, ...] = (
    Starter(
        key="ask-before-a-release",
        title="Ask me before it goes out",
        when="Everything green first, then it stops and waits for you to say yes.",
        group="Before it goes out",
        found_by=("release", "approve", "sign off", "wait", "person", "gate"),
        draws={
            "name": "Ask me before it goes out",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 160)},
                {"id": "checks", "kind": "suite", "label": "Every check",
                 "settings": {}, "at": _at(290, 60)},
                {"id": "scan", "kind": "security_scan", "label": "No credentials in the code",
                 "settings": {}, "at": _at(290, 260)},
                {"id": "gate", "kind": "gate", "label": "Both of those passed",
                 "settings": {"needs": "all"}, "at": _at(540, 160)},
                {"id": "ask", "kind": "wait_for_a_person", "label": "Say yes to release",
                 "settings": {"question": "Everything passed. Shall this go out?"},
                 "at": _at(780, 160)},
                {"id": "kept", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(1020, 160)},
            ],
            "edges": [
                {"from": "start", "to": "checks"},
                {"from": "start", "to": "scan"},
                {"from": "checks", "to": "gate"},
                {"from": "scan", "to": "gate"},
                {"from": "gate", "to": "ask"},
                {"from": "ask", "to": "kept"},
            ],
        },
    ),
    Starter(
        key="one-part-at-a-time",
        title="One part of the checks at a time",
        when="Asks which tag to run when you press Run, so one pipeline covers them all.",
        group="Every day",
        found_by=("tag", "ask", "input", "question", "part", "choose"),
        draws={
            "name": "One part at a time",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 140)},
                {"id": "checks", "kind": "suite", "label": "The checks you choose",
                 "settings": {"tag": "fast", "asks": ["tag"]}, "at": _at(320, 140)},
                {"id": "kept", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(620, 140)},
            ],
            "edges": [
                {"from": "start", "to": "checks"},
                {"from": "checks", "to": "kept"},
            ],
        },
    ),
    Starter(
        key="the-long-one",
        title="The long one, made of the short ones",
        when="Runs your other saved pipelines one after another, instead of copying their steps.",
        group="Bigger jobs",
        found_by=("subflow", "another", "reuse", "nested", "chain", "long"),
        draws={
            "name": "The long one",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 140)},
                {"id": "quick", "kind": "another_pipeline", "label": "The quick one first",
                 "settings": {"pipeline": "Before a commit"}, "at": _at(300, 140)},
                {"id": "gate", "kind": "gate", "label": "That passed",
                 "settings": {"needs": "all"}, "at": _at(580, 140)},
                {"id": "slow", "kind": "another_pipeline", "label": "Then the slow one",
                 "settings": {"pipeline": "Before a release"}, "at": _at(820, 140)},
            ],
            "edges": [
                {"from": "start", "to": "quick"},
                {"from": "quick", "to": "gate"},
                {"from": "gate", "to": "slow"},
            ],
        },
    ),
    Starter(
        key="what-changed-here",
        title="What am I about to commit",
        when="Reads the branch and what is uncommitted, then runs the quick checks.",
        group="Every day",
        found_by=("git", "branch", "uncommitted", "commit", "changed", "status"),
        draws={
            "name": "What am I about to commit",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 140)},
                {"id": "repo", "kind": "git_repo", "label": "Read the repo",
                 "settings": {}, "at": _at(300, 140)},
                {"id": "checks", "kind": "suite", "label": "The quick checks",
                 "settings": {"tag": "fast"}, "at": _at(560, 140)},
                {"id": "kept", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(820, 140)},
            ],
            "edges": [
                {"from": "start", "to": "repo"},
                {"from": "repo", "to": "checks"},
                {"from": "checks", "to": "kept"},
            ],
        },
    ),
    Starter(
        key="just-the-security",
        title="Just the security one",
        when="Reads your files for credentials, and stops everything if it finds any.",
        group="Before it goes out",
        found_by=("security", "credentials", "secrets", "keys", "scan", "leak"),
        draws={
            "name": "Just the security one",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 140)},
                {"id": "scan", "kind": "security_scan", "label": "Read every file",
                 "settings": {}, "at": _at(300, 140)},
                {"id": "gate", "kind": "security_gate", "label": "Nothing was found",
                 "settings": {"needs": "all"}, "at": _at(580, 140)},
                {"id": "kept", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(840, 140)},
            ],
            "edges": [
                {"from": "start", "to": "scan"},
                {"from": "scan", "to": "gate"},
                {"from": "gate", "to": "kept"},
            ],
        },
    ),
)


STARTERS: tuple[Starter, ...] = MORE + (
    Starter(
        key="before-a-commit",
        found_by=("commit", "quick", "checks", "credentials", "every day"),
        group="Every day",
        title="Before a commit",
        when="The quick one. Your own checks, and a look for credentials, in parallel.",
        draws={
            "name": "Before a commit",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 160)},
                {"id": "checks", "kind": "suite", "label": "Your checks",
                 "settings": {}, "at": _at(300, 60)},
                {"id": "scan", "kind": "security_scan", "label": "No credentials in the code",
                 "settings": {}, "at": _at(300, 260)},
                {"id": "gate", "kind": "gate", "label": "Both of those passed",
                 "settings": {"needs": "all"}, "at": _at(580, 160)},
            ],
            "edges": [
                {"from": "start", "to": "checks"},
                {"from": "start", "to": "scan"},
                {"from": "checks", "to": "gate"},
                {"from": "scan", "to": "gate"},
            ],
        },
    ),
    Starter(
        key="before-a-pull-request",
        found_by=("pull request", "review", "pr", "checks", "lint"),
        group="Every day",
        title="Before a pull request",
        when="What you want green before somebody else reads your change.",
        draws={
            "name": "Before a pull request",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 200)},
                {"id": "repo", "kind": "git_repo", "label": "Which branch, and what is uncommitted",
                 "settings": {}, "at": _at(280, 40)},
                {"id": "scan", "kind": "security_scan", "label": "No credentials in the code",
                 "settings": {}, "at": _at(280, 200)},
                {"id": "quick", "kind": "suite", "label": "The quick checks",
                 "settings": {"tag": "fast"}, "at": _at(280, 360)},
                {"id": "gate", "kind": "security_gate", "label": "Safe to carry on",
                 "settings": {"needs": "all"}, "at": _at(600, 120)},
                {"id": "tests", "kind": "unit_test", "label": "Your own tests",
                 "settings": {"command_kind": "test", "tries": 2}, "at": _at(600, 320)},
                {"id": "keep", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(880, 200)},
            ],
            "edges": [
                {"from": "start", "to": "repo"},
                {"from": "start", "to": "scan"},
                {"from": "start", "to": "quick"},
                {"from": "scan", "to": "gate"},
                {"from": "repo", "to": "gate"},
                {"from": "gate", "to": "tests"},
                {"from": "quick", "to": "tests"},
                {"from": "tests", "to": "keep"},
            ],
        },
    ),
    Starter(
        key="before-a-release",
        found_by=("release", "ship", "everything", "slow", "full"),
        group="Before it goes out",
        title="Before a release",
        when="The long one. Everything, in order, with the evidence kept at the end.",
        draws={
            "name": "Before a release",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 240)},
                {"id": "repo", "kind": "git_repo", "label": "The repository",
                 "settings": {}, "at": _at(260, 40)},
                {"id": "scan", "kind": "security_scan", "label": "No credentials in the code",
                 "settings": {}, "at": _at(260, 200)},
                {"id": "gate", "kind": "security_gate", "label": "Nothing to worry about",
                 "settings": {"needs": "all"}, "at": _at(520, 120)},
                {"id": "lint", "kind": "unit_test", "label": "Your style checks",
                 "settings": {"command_kind": "lint"}, "at": _at(520, 320)},
                {"id": "checks", "kind": "suite", "label": "Every one of your checks",
                 "settings": {}, "at": _at(780, 120)},
                {"id": "tests", "kind": "unit_test", "label": "Your own tests",
                 "settings": {"command_kind": "test", "tries": 2}, "at": _at(780, 320)},
                {"id": "build", "kind": "unit_test", "label": "It still builds",
                 "settings": {"command_kind": "build"}, "at": _at(1040, 220)},
                {"id": "keep", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(1300, 220)},
            ],
            "edges": [
                {"from": "start", "to": "repo"},
                {"from": "start", "to": "scan"},
                {"from": "start", "to": "lint"},
                {"from": "repo", "to": "gate"},
                {"from": "scan", "to": "gate"},
                {"from": "gate", "to": "checks"},
                {"from": "lint", "to": "tests"},
                {"from": "checks", "to": "build"},
                {"from": "tests", "to": "build"},
                {"from": "build", "to": "keep"},
            ],
        },
    ),
    Starter(
        key="nightly",
        found_by=("nightly", "overnight", "schedule", "long", "every night"),
        group="Bigger jobs",
        title="Nightly",
        when="Everything, twice as patient. For running while nobody is watching.",
        draws={
            "name": "Nightly",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 160)},
                {"id": "scan", "kind": "security_scan", "label": "No credentials in the code",
                 "settings": {}, "at": _at(280, 40)},
                {"id": "checks", "kind": "suite", "label": "Every one of your checks",
                 "settings": {"tries": 3}, "at": _at(280, 200)},
                {"id": "tests", "kind": "unit_test", "label": "Your own tests",
                 "settings": {"command_kind": "test", "tries": 3}, "at": _at(280, 360)},
                {"id": "gate", "kind": "gate", "label": "Did everything pass?",
                 "settings": {"needs": "all"}, "at": _at(600, 200)},
                {"id": "keep", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(860, 200)},
            ],
            "edges": [
                {"from": "start", "to": "scan"},
                {"from": "start", "to": "checks"},
                {"from": "start", "to": "tests"},
                {"from": "scan", "to": "gate"},
                {"from": "checks", "to": "gate"},
                {"from": "tests", "to": "gate"},
                {"from": "gate", "to": "keep"},
            ],
        },
    ),
    Starter(
        key="let-a-model-write-a-test",
        found_by=("ai", "model", "write a test", "draft", "assistant"),
        group="Bigger jobs",
        title="Let a model draft a test",
        when="Ask the model you have set up to draft a test, then read it yourself.",
        draws={
            "name": "Let a model draft a test",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {},
                 "at": _at(40, 160)},
                {"id": "repo", "kind": "git_repo", "label": "What has changed",
                 "settings": {}, "at": _at(280, 160)},
                {"id": "draft", "kind": "ai_unit_test", "label": "Draft a test",
                 "settings": {
                     "instructions": "Write one test for the part of this project that "
                                     "adds things up. Cover the empty case and one "
                                     "ordinary case. No explanation, only the file.",
                     "write_to": "drafted.test.js",
                 }, "at": _at(540, 160)},
                {"id": "keep", "kind": "artifact", "label": "Keep the evidence",
                 "settings": {}, "at": _at(820, 160)},
            ],
            "edges": [
                {"from": "start", "to": "repo"},
                {"from": "repo", "to": "draft"},
                {"from": "draft", "to": "keep"},
            ],
        },
    ),
)


def listed() -> list[dict[str, Any]]:
    return [starter.to_dict() for starter in STARTERS]


def build(key: str) -> dict[str, Any]:
    """The drawing for one ready-made pipeline."""

    for starter in STARTERS:
        if starter.key == key:
            import copy

            return copy.deepcopy(starter.draws)
    raise StarterError(f"There is no ready-made pipeline called {key}")
