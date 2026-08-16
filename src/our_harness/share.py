"""One file you can send to anyone.

A run leaves a folder of results, evidence and pictures. That is right for a
machine and wrong for a person: you cannot email a folder to your manager, and
the person you send it to should not have to install anything to read it.

So this makes a single web page. The screenshots are inside the file itself, so
it still shows them on a machine that has never seen this project. Credentials
are taken out before anything is written, and the person's own folder name is
taken out with them.
"""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import comparison
from .config import LoadedConfig
from .models import HarnessError
from .redaction import CredentialRedactor
from .safety import confined_path

# One picture this big is already more than anybody wants to look at, and the
# whole page has to stay small enough to send.
MAX_PICTURE_BYTES = 3_000_000
MAX_TOTAL_PICTURE_BYTES = 20_000_000
MAX_PICTURES = 60
# How much of the evidence text is worth putting in front of a person.
MAX_EVIDENCE_CHARS = 4000

PICTURE_KINDS = {".png": "image/png"}


class ShareError(HarnessError):
    """A problem making the page."""


@dataclass
class Page:
    """The finished page and what had to be left out of it."""

    run_id: str
    html: str
    pictures: int
    left_out: tuple[str, ...] = ()
    # The folder the run really sits in. The name inside the report file is
    # whatever was written there, so it is fine to show and wrong to build a
    # path from.
    folder: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pictures": self.pictures,
            "left_out": list(self.left_out),
            "bytes": len(self.html.encode("utf-8")),
        }


def _hide_home(text: str) -> str:
    """Take the person's own folder name out of anything written down."""

    home = str(Path.home())
    if not home:
        return text
    return text.replace(home, "~").replace(home.replace("\\", "/"), "~")


def runs_folder(config: LoadedConfig) -> Path:
    return confined_path(
        config.project_root,
        str(config.get("qa.artifacts_dir", ".harness/qa/runs")),
        allow_missing=True,
        allow_control=True,
    )


def chosen_run(config: LoadedConfig, run_id: str = "") -> Path:
    """The run folder asked for, or the most recent one that was kept."""

    if run_id:
        folder = comparison.kept_run_folder(config, run_id)
        if not (folder / "result.json").is_file():
            raise ShareError(f"There is no kept run called {run_id}")
        return folder
    kept = [item for item in comparison.kept_runs(config) if (item / "result.json").is_file()]
    if not kept:
        raise ShareError("There are no kept runs yet. Run the checks first with: harness qa run")
    return kept[-1]


def _pictures_in(folder: Path) -> list[Path]:
    """Every picture in a run folder, in a settled order."""

    found: list[Path] = []
    for item in sorted(folder.rglob("*")):
        if item.is_file() and item.suffix.lower() in PICTURE_KINDS:
            found.append(item)
    return found


def _picture_label(folder: Path, picture: Path) -> str:
    try:
        return picture.relative_to(folder).as_posix()
    except ValueError:  # pragma: no cover - rglob always gives a child
        return picture.name


def _as_data_address(picture: Path) -> str:
    kind = PICTURE_KINDS[picture.suffix.lower()]
    packed = base64.b64encode(picture.read_bytes()).decode("ascii")
    return f"data:{kind};base64,{packed}"


# Terminal colour codes. A browser writes them into its own messages, and on a
# page they read as rubbish: "Call log: [2m - waiting for ...". They turn up
# two ways: as the real character, and spelled out in letters once the evidence
# has been through a JSON file on the way here.
_COLOUR_CODES = re.compile(r"(?:\x1b|\\u001[bB]|\\x1[bB]|\\e)\[[0-9;]{0,20}[A-Za-z]")


def _clean(redactor: CredentialRedactor, text: str) -> str:
    return _COLOUR_CODES.sub("", _hide_home(redactor.text(text)))


def _safe(redactor: CredentialRedactor, value: object) -> str:
    """Every piece of text that goes on the page goes through here.

    Two things have to happen to all of it, in this order: take out anything
    that looks like a credential, then turn any markup into ordinary words. A
    name or a title is free text somebody typed, so a token pasted into a check
    title is just as real as one in the evidence, and both must go.
    """

    return html.escape(_clean(redactor, str(value if value is not None else "")))


_STYLE = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 24px; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.5; }
h1 { margin: 0 0 4px; font-size: 1.5rem; }
.when { color: #666; margin: 0 0 18px; }
.headline { padding: 14px 16px; border-radius: 10px; font-weight: 700; margin-bottom: 18px; }
.headline.good { background: #e6f7ea; color: #12492a; }
.headline.bad { background: #fdeaec; color: #6d1220; }
table { border-collapse: collapse; width: 100%; margin-bottom: 24px; }
caption { text-align: left; font-weight: 700; padding-bottom: 8px; }
th, td { border-bottom: 1px solid #d8d8d8; padding: 8px 10px; text-align: left; vertical-align: top; }
th { background: #f2f4f6; }
.status { font-weight: 700; text-transform: capitalize; }
.status.passed { color: #157a3c; }
.status.failed { color: #b3202f; }
.status.flaky { color: #9a6800; }
.status.skipped { color: #666; }
details { margin-bottom: 10px; border: 1px solid #d8d8d8; border-radius: 8px; padding: 10px 12px; }
summary { cursor: pointer; font-weight: 650; }
pre { overflow-x: auto; background: #f6f7f8; padding: 10px; border-radius: 8px; font-size: .82rem; }
figure { margin: 14px 0; }
figure img { max-width: 100%; height: auto; border: 1px solid #d8d8d8; border-radius: 8px; }
figcaption { color: #666; font-size: .82rem; margin-top: 4px; }
.left-out { color: #666; font-size: .85rem; }
@media (prefers-color-scheme: dark) {
  body { background: #14181c; color: #e9eef2; }
  th { background: #202830; }
  th, td, details, figure img { border-color: #33404b; }
  pre { background: #1b2229; }
  .headline.good { background: #133224; color: #b7f0cb; }
  .headline.bad { background: #3a161c; color: #ffc7cf; }
  .when, figcaption, .left-out { color: #a7b4bf; }
}
"""


def _reasons_of(item: Mapping[str, Any]) -> list[str]:
    """Why a check failed, as a list of sentences.

    A run report is a file on disk and can hold anything. A reasons field that
    is a number used to make this page fail to build at all; one that is a
    piece of text would be read letter by letter.
    """

    reasons = item.get("reasons")
    if isinstance(reasons, str) or not isinstance(reasons, Sequence):
        return []
    return [str(reason).strip() for reason in reasons if reason is not None and str(reason).strip()]


def build(
    config: LoadedConfig,
    run_id: str = "",
    *,
    with_pictures: bool = True,
) -> Page:
    """Make the page for one run."""

    folder = chosen_run(config, run_id)
    report = comparison.read_report(folder)
    cases = report.get("cases")
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
        raise ShareError("That run report holds no list of checks")
    redactor = CredentialRedactor(config)
    identifier = str(report.get("run_id") or folder.name)
    left_out: list[str] = []

    rows: list[str] = []
    passed = failed = flaky = skipped = 0
    for item in cases:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "")
        passed += status == "passed"
        failed += status == "failed"
        flaky += status == "flaky"
        skipped += status == "skipped"
        reasons = _reasons_of(item)
        why = "; ".join(reasons) if reasons else "As expected"
        rows.append(
            "<tr>"
            f"<td><code>{_safe(redactor, item.get('id'))}</code><br>"
            f"{_safe(redactor, item.get('title'))}</td>"
            f'<td class="status {_safe(redactor, status)}">{_safe(redactor, status or "unknown")}</td>'
            f"<td>{_safe(redactor, item.get('duration_ms') or 0)} ms</td>"
            f"<td>{_safe(redactor, why)}</td>"
            "</tr>"
        )

    details: list[str] = []
    for item in cases:
        if not isinstance(item, Mapping) or str(item.get("status") or "") == "passed":
            continue
        attempts = item.get("attempts") or []
        evidence = ""
        if isinstance(attempts, Sequence) and attempts and isinstance(attempts[-1], Mapping):
            evidence = str(attempts[-1].get("evidence") or "")
        if len(evidence) > MAX_EVIDENCE_CHARS:
            evidence = evidence[:MAX_EVIDENCE_CHARS] + "\n... the rest was left out to keep this short"
        reasons = _reasons_of(item)
        details.append(
            "<details open><summary>"
            f"{_safe(redactor, item.get('id'))}: {_safe(redactor, item.get('title'))}"
            "</summary>"
            + "".join(f"<p>{_safe(redactor, reason)}</p>" for reason in reasons)
            + (f"<pre>{_safe(redactor, evidence)}</pre>" if evidence else "")
            + "</details>"
        )

    figures: list[str] = []
    kept_pictures = 0
    if with_pictures:
        spent = 0
        for picture in _pictures_in(folder):
            # The note is cleaned as it is written down, not as it is shown.
            # The list itself is handed back by to_dict(), which is what the
            # panel and `--json` read, so cleaning it only on the way to the
            # page would leave a credential in the answer nobody looked at.
            label = _clean(redactor, _picture_label(folder, picture))
            if kept_pictures >= MAX_PICTURES:
                left_out.append(f"{label} (there were more pictures than fit on one page)")
                continue
            size = picture.stat().st_size
            if size > MAX_PICTURE_BYTES:
                left_out.append(f"{label} (too big to put in the file)")
                continue
            if spent + size > MAX_TOTAL_PICTURE_BYTES:
                left_out.append(f"{label} (the file was already as large as it should be)")
                continue
            try:
                address = _as_data_address(picture)
            except OSError as exc:
                left_out.append(f"{label} (could not be read: {_clean(redactor, str(exc))})")
                continue
            spent += size
            kept_pictures += 1
            figures.append(
                f'<figure><img src="{address}" alt="{_safe(redactor, label)}">'
                f"<figcaption>{_safe(redactor, label)}</figcaption></figure>"
            )

    headline = "All checks passed" if failed == 0 else "Some checks failed"
    tone = "good" if failed == 0 else "bad"
    page = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Test run {_safe(redactor, identifier)}</title>\n<style>{_STYLE}</style>\n"
        "</head>\n<body>\n"
        f"<h1>Test run {_safe(redactor, identifier)}</h1>\n"
        f'<p class="when">Checks: {_safe(redactor, report.get("suite_name") or "default")}. '
        f'Started {_safe(redactor, report.get("started_at") or "at an unknown time")}.</p>\n'
        f'<div class="headline {tone}" role="status">{headline}. '
        f"{passed} passed, {failed} failed, {flaky} flaky, {skipped} skipped.</div>\n"
        "<table>\n<caption>Every check in this run</caption>\n<thead><tr>"
        '<th scope="col">Check</th><th scope="col">Result</th><th scope="col">Time</th>'
        '<th scope="col">What happened</th></tr></thead>\n<tbody>\n'
        + "\n".join(rows)
        + "\n</tbody>\n</table>\n"
        + ("<h2>What went wrong</h2>\n" + "\n".join(details) + "\n" if details else "")
        + ("<h2>Pictures</h2>\n" + "\n".join(figures) + "\n" if figures else "")
        + (
            '<p class="left-out">Left out of this file: '
            + _safe(redactor, "; ".join(left_out))
            + "</p>\n"
            if left_out
            else ""
        )
        + "</body>\n</html>\n"
    )
    return Page(
        run_id=identifier,
        html=page,
        pictures=kept_pictures,
        left_out=tuple(left_out),
        folder=folder.name,
    )


def write(
    config: LoadedConfig,
    run_id: str = "",
    *,
    output: str = "",
    with_pictures: bool = True,
) -> tuple[Path, Page]:
    """Make the page and save it, and say where it went."""

    page = build(config, run_id, with_pictures=with_pictures)
    base = str(config.get("qa.artifacts_dir", ".harness/qa/runs")).rstrip("/")
    where = output or f"{base}/{page.folder}/report.html"
    path = confined_path(config.project_root, where, allow_missing=True, allow_control=True)
    if path.is_dir():
        raise ShareError(f"{where} is a folder, so the page cannot be written there")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page.html, encoding="utf-8")
    return path, page


def summary(path: Path, page: Page) -> list[str]:
    """What to tell somebody who just made one."""

    one = page.pictures == 1
    out = [
        f"Wrote {path}",
        "It is one file. Send it to anyone; they do not need this project to read it.",
        f"{page.pictures} picture{'' if one else 's'} {'is' if one else 'are'} inside the file itself."
        if page.pictures
        else "There were no pictures in this run to put inside it.",
    ]
    if page.left_out:
        out.append("Left out: " + "; ".join(page.left_out[:5]))
    return out


def as_json(path: Path, page: Page) -> str:
    shape = page.to_dict()
    shape["path"] = path.as_posix()
    return json.dumps(shape, indent=2, sort_keys=True) + "\n"
