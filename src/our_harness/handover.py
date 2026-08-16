"""Handing the checks to a build server, and asking a model about a failure.

Two jobs that both start from "the checks pass on my machine, now what".

The first writes the file a build server needs so the same checks run on every
change. Nothing here is clever: it is the file somebody would otherwise copy
from a web page and get slightly wrong.

The second takes one failed check and asks the model the project is already set
up with to explain it in plain words and say what to try. That answer is
advice, never an edit: nothing is changed by asking.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import LoadedConfig
from .models import HarnessError
from .redaction import CredentialRedactor
from .safety import confined_path

SERVICES = {
    "github": ".github/workflows/checks.yml",
    "gitlab": ".gitlab-ci.yml",
}
MAX_ANSWER_CHARS = 4000


class HandoverError(HarnessError):
    """A problem writing a build file or asking about a failure."""


def _yaml_text(value: str) -> str:
    """Text that is safe inside a single-quoted YAML value."""

    return str(value).replace("'", "''")


def build_file(service: str, *, suite: str = "", python: str = "3.11") -> tuple[str, str]:
    """The path and the contents of the file a build server needs."""

    key = str(service or "").strip().lower()
    if key not in SERVICES:
        raise HandoverError(
            f"There is no build file for {service}. This tool writes: {', '.join(SERVICES)}"
        )
    if not isinstance(python, str) or not python.replace(".", "").isdigit():
        raise HandoverError("The Python version must look like 3.11")
    if suite:
        # This ends up on a command line a build server runs, so it may only be
        # a plain path. Anything a shell would treat as a second command is
        # refused rather than quoted and hoped for.
        if not isinstance(suite, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", suite):
            raise HandoverError(
                "The suite must be a plain path inside the project, "
                "such as .harness/qa/suite.json"
            )
        if ".." in suite.split("/"):
            raise HandoverError("The suite must not step outside the project with ..")
    suite_part = f" --suite {suite}" if suite else ""
    if key == "github":
        body = f"""# Runs the harness checks on every change.
# Written by Nexus Harness. Change it freely; it is only a starting point.
name: Checks

on:
  push:
  pull_request:

jobs:
  checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '{_yaml_text(python)}'
      - name: Install the harness
        run: python -m pip install .
      - name: Install a browser, for the checks that need one
        run: |
          npm install playwright
          npx playwright install --with-deps chromium
      - name: Run the checks
        run: harness qa run{suite_part} --format junit --output reports/checks.xml
      - name: Keep the report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: check-report
          path: |
            reports/checks.xml
            .harness/qa/runs
"""
    else:
        body = f"""# Runs the harness checks on every change.
# Written by Nexus Harness. Change it freely; it is only a starting point.
checks:
  image: python:{_yaml_text(python)}
  script:
    - python -m pip install .
    - harness qa run{suite_part} --format junit --output reports/checks.xml
  artifacts:
    when: always
    paths:
      - reports/checks.xml
      - .harness/qa/runs
    reports:
      junit: reports/checks.xml
"""
    return SERVICES[key], body


def write_build_file(
    config: LoadedConfig, service: str, *, suite: str = "", python: str = "3.11", replace: bool = False
) -> str:
    """Write the build file into the project and say where it went."""

    relative, body = build_file(service, suite=suite, python=python)
    path = confined_path(config.project_root, relative, allow_missing=True, allow_control=True)
    if path.exists() and not replace:
        raise HandoverError(
            f"{relative} is already there. Look at it first, then use replace if you still want ours."
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        raise HandoverError(f"Cannot write {relative}: {exc}") from exc
    return relative


# ---------------------------------------------------------------------------
# Asking a model about one failure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Explanation:
    """What a model said about one failed check."""

    case_id: str
    answer: str
    asked: str

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "answer": self.answer, "asked": self.asked}


def failure_question(
    case: Mapping[str, Any], evidence: str = "", hide: CredentialRedactor | None = None
) -> str:
    """The question to ask about one failed check.

    It says plainly what is wanted: an explanation a beginner can act on. The
    model is told not to invent details, because a confident wrong answer costs
    more time than no answer.

    What a check saw can hold a key or a password, and asking a model may send
    it off this machine. So the whole question goes through the same credential
    remover the rest of the harness uses before it leaves.
    """

    remover = hide or CredentialRedactor()
    reasons = "\n".join(f"- {line}" for line in (case.get("reasons") or [])) or "- no reason was given"
    body = (evidence or "").strip()
    if len(body) > 3000:
        body = body[:3000] + "\n... (shortened)"
    return remover.text(
        "A check in a test suite failed. Explain it to somebody who is new to testing.\n\n"
        f"Check: {case.get('title') or case.get('id')}\n"
        f"Kind: {case.get('kind')}\n"
        f"What the harness reported:\n{reasons}\n\n"
        f"What the check saw:\n{body or '(nothing was recorded)'}\n\n"
        "Answer in three short parts, using plain English and no jargon:\n"
        "1. What went wrong, in one or two sentences.\n"
        "2. The most likely cause.\n"
        "3. What to try first, as a single concrete step.\n\n"
        "Only use what is written above. If it is not enough to be sure, say which one thing "
        "you would need to see."
    )


def explain_failure(config: LoadedConfig, case: Mapping[str, Any], evidence: str = "") -> Explanation:
    """Ask the project's own model why a check failed.

    The model set up for this project is used, whatever it is: a key, a local
    model, or a command line the person is already signed in to. Nothing is
    changed by asking, and the answer is text.
    """

    from .models import ProviderRequest
    from .providers import create_provider

    if not isinstance(case, Mapping) or not case.get("id"):
        raise HandoverError("Name the check that failed")
    question = failure_question(case, evidence, CredentialRedactor(config))
    instructions = (
        "You explain failed software checks to somebody new to testing. "
        "Short sentences. Plain English. Never invent details that are not in the question."
    )
    try:
        provider = create_provider(config)
        answer = provider.complete(
            ProviderRequest(
                instructions,
                question,
                [{"role": "user", "content": question}],
                str(config.get("provider.model")),
                0.1,
                700,
            )
        )
    except HarnessError as exc:
        raise HandoverError(
            f"The model could not be asked: {exc}. Open Start here, or run: harness doctor"
        ) from exc
    text = str(getattr(answer, "text", "") or "").strip()
    if not text:
        raise HandoverError("The model sent nothing back")
    return Explanation(case_id=str(case["id"]), answer=text[:MAX_ANSWER_CHARS], asked=question)


def failure_from_run(result: Mapping[str, Any], case_id: str = "") -> tuple[dict[str, Any], str]:
    """Pick one failed check out of a run, with what it saw."""

    cases = result.get("cases") if isinstance(result, Mapping) else None
    if not isinstance(cases, Sequence):
        raise HandoverError("That is not a run report this tool understands")
    failed = [
        item for item in cases
        if isinstance(item, Mapping) and item.get("status") in ("failed", "flaky")
    ]
    if not failed:
        raise HandoverError("Nothing failed in that run, so there is nothing to explain")
    if case_id:
        wanted = [item for item in failed if item.get("id") == case_id]
        if not wanted:
            names = ", ".join(str(item.get("id")) for item in failed)
            raise HandoverError(f"{case_id} did not fail in that run. These did: {names}")
        chosen = wanted[0]
    else:
        chosen = failed[0]
    attempts = chosen.get("attempts") or []
    evidence = ""
    if isinstance(attempts, Sequence) and attempts:
        last = attempts[-1]
        if isinstance(last, Mapping):
            evidence = str(last.get("evidence") or "")
    return dict(chosen), evidence


def read_run(config: LoadedConfig, path: str = "") -> dict[str, Any]:
    """The run report to explain: a named one, or the most recent."""

    if path:
        wanted = confined_path(config.project_root, path, allow_missing=True, allow_control=True)
        if not wanted.is_file():
            raise HandoverError(f"There is no run report at {path}")
        try:
            return json.loads(wanted.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HandoverError(f"Cannot read {path}: {exc}") from exc
    base = confined_path(
        config.project_root,
        str(config.get("qa.artifacts_dir", ".harness/qa/runs")),
        allow_missing=True,
        allow_control=True,
    )
    folders = sorted((item for item in base.iterdir() if item.is_dir()), key=lambda item: item.name) if base.is_dir() else []
    for folder in reversed(folders):
        report = folder / "result.json"
        if report.is_file():
            try:
                return json.loads(report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    raise HandoverError("There is no kept run to look at yet. Run the checks first.")
