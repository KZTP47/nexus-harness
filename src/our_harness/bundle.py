"""One zip file holding everything somebody needs to help you.

When a check fails on your machine and nobody else can see it, the useful thing
to send is not a screenshot of a terminal. It is the checks themselves, the last
few runs with their evidence, the settings, and what this machine is. This
builds exactly that, with credentials taken out first.

The older tool this replaces had a quiet fault worth naming, because it is easy
to repeat. The screen that asked for the file name saved it under one name and
the part that built the zip read a different one, so the name a person typed was
never used. Nothing failed, it just did the wrong thing. Here there is one list
of parts, one name for each, and the same list is used to ask, to build, and to
check. A name that does not exist is refused with the list of ones that do.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import __version__
from .config import LoadedConfig
from .models import HarnessError
from .redaction import CredentialRedactor
from .safety import confined_path

# The one list. Everything else in this file, and the command line, reads it.
PARTS: dict[str, str] = {
    "checks": "The checks themselves, so somebody else can run them",
    "runs": "The last few runs, with what each check saw",
    "history": "How the checks have behaved over time",
    "settings": "The project settings, with credentials taken out",
    "machine": "What this computer is: system, Python version, harness version",
}
DEFAULT_RUNS = 5
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
_TEXT_SUFFIXES = (".json", ".txt", ".md", ".log", ".xml", ".html", ".csv", ".yml", ".yaml")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,80}$")
# Files that sit beside the checks but belong to a part of their own.
_OWNED_BY_ANOTHER_PART = {"history.json"}


class BundleError(HarnessError):
    """A problem building the zip that the user can fix."""


@dataclass
class Result:
    """What was built, and what was deliberately left out."""

    path: Path
    parts: tuple[str, ...]
    files: tuple[str, ...] = ()
    total_bytes: int = 0
    left_out: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "parts": list(self.parts),
            "files": list(self.files),
            "total_bytes": self.total_bytes,
            "left_out": list(self.left_out),
        }


def chosen_parts(names: Iterable[str] | None) -> tuple[str, ...]:
    """Read the asked-for parts, refusing any name this tool does not have."""

    if not names:
        return tuple(PARTS)
    wanted: list[str] = []
    for name in names:
        for piece in str(name).split(","):
            clean = piece.strip().lower()
            if not clean:
                continue
            if clean == "all":
                return tuple(PARTS)
            if clean not in PARTS:
                raise BundleError(
                    f"There is no part called {clean}. Choose from: {', '.join(PARTS)}, or all."
                )
            if clean not in wanted:
                wanted.append(clean)
    if not wanted:
        raise BundleError(f"Name at least one part: {', '.join(PARTS)}, or all.")
    return tuple(wanted)


def default_name(when: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(when))
    return f"harness-bundle-{stamp}.zip"


def output_path(config: LoadedConfig, wanted: str = "") -> Path:
    """Where the zip goes. Inside the project unless a full path is given."""

    if not wanted:
        # Two bundles made in the same second would land on one name, and the
        # second was refused with advice about choosing another name that the
        # panel gives no way to follow. The next free name is used instead.
        base = default_name()
        spot = confined_path(
            config.project_root, f".harness/bundles/{base}",
            allow_missing=True, allow_control=True,
        )
        for number in range(2, 100):
            if not spot.exists():
                return spot
            spot = confined_path(
                config.project_root,
                f".harness/bundles/{base[:-4]}-{number}.zip",
                allow_missing=True, allow_control=True,
            )
        return spot
    candidate = Path(wanted)
    if candidate.is_absolute():
        if candidate.is_dir():
            candidate = candidate / default_name()
        if candidate.suffix.lower() != ".zip":
            raise BundleError("The bundle file name must end with .zip")
        return candidate
    if candidate.suffix.lower() != ".zip":
        raise BundleError("The bundle file name must end with .zip")
    return confined_path(config.project_root, wanted, allow_missing=True, allow_control=True)


def _run_folders(config: LoadedConfig, keep: int) -> list[Path]:
    base = confined_path(
        config.project_root,
        str(config.get("qa.artifacts_dir", ".harness/qa/runs")),
        allow_missing=True,
        allow_control=True,
    )
    if not base.is_dir():
        return []
    folders = sorted((item for item in base.iterdir() if item.is_dir()), key=lambda item: item.name)
    return folders[-keep:] if keep > 0 else []


def _machine_notes() -> dict[str, Any]:
    return {
        "harness_version": __version__,
        "python_version": sys.version.split()[0],
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _hide_home(text: str) -> str:
    """Take the person's own folder name out of anything written down."""

    home = str(Path.home())
    if not home:
        return text
    return text.replace(home, "~").replace(home.replace("\\", "/"), "~")


class _Writer:
    """Puts files in the zip, keeping to the size caps and saying what it skipped."""

    def __init__(self, archive: zipfile.ZipFile, redactor: CredentialRedactor) -> None:
        self.archive = archive
        self.redactor = redactor
        self.files: list[str] = []
        self.left_out: list[str] = []
        self.total = 0

    def text(self, name: str, body: str) -> None:
        self.blob(name, _hide_home(self.redactor.text(body)).encode("utf-8"))

    def blob(self, name: str, body: bytes) -> None:
        if len(body) > MAX_FILE_BYTES:
            self.left_out.append(f"{name} is larger than {MAX_FILE_BYTES} bytes")
            return
        if self.total + len(body) > MAX_TOTAL_BYTES:
            self.left_out.append(f"{name} did not fit, the bundle is already {self.total} bytes")
            return
        self.archive.writestr(name, body)
        self.files.append(name)
        self.total += len(body)

    def file(self, name: str, path: Path) -> None:
        try:
            if path.is_symlink() or not path.is_file():
                self.left_out.append(f"{name} is not a plain file")
                return
            # How big it is, before reading it. A run folder can hold a browser
            # trace of several hundred megabytes, and reading one into memory
            # only to say it is too big spends all of that for nothing.
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                self.left_out.append(f"{name} is larger than {MAX_FILE_BYTES} bytes")
                return
            if self.total + size > MAX_TOTAL_BYTES:
                self.left_out.append(
                    f"{name} did not fit, the bundle is already {self.total} bytes"
                )
                return
            raw = path.read_bytes()
        except OSError as exc:
            self.left_out.append(f"{name} could not be read: {exc}")
            return
        if path.suffix.lower() in _TEXT_SUFFIXES:
            self.text(name, raw.decode("utf-8", errors="replace"))
        else:
            self.blob(name, raw)


def build(
    config: LoadedConfig,
    *,
    parts: Sequence[str] | None = None,
    runs: int = DEFAULT_RUNS,
    output: str = "",
    replace: bool = False,
) -> Result:
    """Build the zip and say what went in and what was left out.

    A file that is already there is never written over unless `replace` says
    so, because a mistyped name should not cost somebody their work.
    """

    wanted = chosen_parts(parts)
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 0 or runs > 100:
        raise BundleError("The number of runs must be a whole number from 0 to 100")
    destination = output_path(config, output)
    if destination.exists() and not replace:
        raise BundleError(
            f"{destination} is already there. Choose another name, or say replace to write over it."
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BundleError(f"Cannot make the folder for {destination}: {exc}") from exc
    redactor = CredentialRedactor(config)
    root = config.project_root
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        writer = _Writer(archive, redactor)
        if "checks" in wanted:
            folder = confined_path(root, ".harness/qa", allow_missing=True, allow_control=True)
            if folder.is_dir():
                for item in sorted(folder.glob("*.json")):
                    # One file belongs to one part, so nothing is packed twice
                    # and asking for the checks does not quietly bring the
                    # run history along with them.
                    if item.name in _OWNED_BY_ANOTHER_PART:
                        continue
                    writer.file(f"checks/{item.name}", item)
        if "runs" in wanted:
            for run in _run_folders(config, runs):
                for item in sorted(run.rglob("*")):
                    if item.is_dir():
                        continue
                    inside = item.relative_to(run).as_posix()
                    writer.file(f"runs/{run.name}/{inside}", item)
        if "history" in wanted:
            history = confined_path(
                root, ".harness/qa/history.json", allow_missing=True, allow_control=True
            )
            if history.is_file():
                writer.file("history.json", history)
        if "settings" in wanted:
            for name in ("config.json", "config.local.json"):
                item = confined_path(root, f".harness/{name}", allow_missing=True, allow_control=True)
                if item.is_file():
                    writer.file(f"settings/{name}", item)
        if "machine" in wanted:
            writer.text("machine.json", json.dumps(_machine_notes(), indent=2, sort_keys=True))
        manifest = {
            "schema_version": 1,
            "made_by": f"Our Harness {__version__}",
            "made_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "project": root.name,
            "parts": list(wanted),
            "runs_kept": runs if "runs" in wanted else 0,
            "files": sorted(writer.files),
            "left_out": writer.left_out,
            "note": (
                "Credentials were taken out before this was written. "
                "Read it before sending it on."
            ),
        }
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return Result(
        path=destination,
        parts=wanted,
        # The list of contents is not itself contents, so both counts agree.
        files=tuple(sorted(writer.files)),
        total_bytes=writer.total,
        left_out=tuple(writer.left_out),
    )


def read_manifest(path: Path) -> dict[str, Any]:
    """Read the list of contents back out of a bundle."""

    if not path.is_file():
        raise BundleError(f"There is no bundle at {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("manifest.json") as handle:
                body = json.loads(handle.read().decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise BundleError(f"{path.name} is not a bundle this tool made: {exc}") from exc
    if not isinstance(body, dict) or "parts" not in body:
        raise BundleError(f"{path.name} has no list of contents")
    unknown = [name for name in body.get("parts", []) if name not in PARTS]
    if unknown:
        raise BundleError(
            f"{path.name} says it holds a part this version does not know: {unknown[0]}"
        )
    return body


def describe(result: Result) -> list[str]:
    """Plain lines about what was built, for printing."""

    lines = [
        f"Wrote {result.path}",
        f"It holds {len(result.files)} files and a list of contents, "
        f"{result.total_bytes} bytes before packing.",
    ]
    for part in result.parts:
        lines.append(f"  {part}: {PARTS[part]}")
    if result.left_out:
        lines.append("Left out:")
        lines.extend(f"  {item}" for item in result.left_out)
    lines.append("Credentials were taken out. Read it before sending it on.")
    return lines
