"""What the harness has learned, kept as notes anybody can read.

A harness that runs against the same project every day learns things: how this
person likes to be answered, which command really runs the tests here, what
went wrong last time and what fixed it. Kept inside a database, all of that is
the harness's private business. Kept as notes, it is yours: you can read it,
correct it, delete the wrong parts, and hand it to somebody else.

So this is a vault of small notes, in the shape people already use for exactly
this: a markdown file each, a few lines of frontmatter at the top, and links
written [[like this]]. Nothing here needs the harness to read it. Open the
folder in any editor and it is a set of notes about your project.

Four kinds of note
  - about you: how you like to be worked with.
  - how to: something that worked, written down so it can be done again. These
    keep a tally of how often they were used and how often that went well.
  - about this project: what the harness has worked out about the code.
  - lesson: something that went wrong once, and what fixed it.

Two ideas borrowed from other self-improving harnesses, because they are the
right ideas: a note that is used and goes well earns its place, and a note that
nothing has touched for a long time is quietly marked as going stale rather
than believed for ever.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .models import HarnessError
from .safety import confined_path

WHERE_THEY_LIVE = ".harness/vault"
# A note is a small thing on purpose. Anything longer belongs in the project.
MOST_LETTERS = 20000
MOST_NOTES = 2000
# After this long with nothing touching it, a note is called stale: still
# there, still readable, no longer taken as certainly true.
GOES_STALE_AFTER_DAYS = 90

KINDS: dict[str, tuple[str, str]] = {
    "about-you": ("About you", "How you like to be worked with."),
    "how-to": ("How to", "Something that worked, written down so it can be done again."),
    "about-this-project": ("About this project", "What the harness has worked out about the code."),
    "lesson": ("Lesson", "Something that went wrong once, and what fixed it."),
}
LINK = re.compile(r"\[\[([^\]|]{1,120})(?:\|[^\]]{0,120})?\]\]")
NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,._'()-]{0,79}$")


class VaultError(HarnessError):
    """A note that cannot be read, written, or found."""


@dataclass
class Note:
    """One thing the harness has learned."""

    name: str
    title: str
    kind: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    sure: float = 0.5
    learned: str = ""
    touched: str = ""
    came_from: str = ""
    uses: int = 0
    worked: int = 0
    links: list[str] = field(default_factory=list)

    @property
    def stale(self) -> bool:
        touched = _read_when(self.touched or self.learned)
        if touched is None:
            return False
        return (time.time() - touched) > GOES_STALE_AFTER_DAYS * 86400

    @property
    def how_it_goes(self) -> float:
        """Of the times this was used, how often it went well."""

        return round(self.worked / self.uses, 2) if self.uses else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "body": self.body,
            "tags": self.tags,
            "sure": self.sure,
            "learned": self.learned,
            "touched": self.touched,
            "came_from": self.came_from,
            "uses": self.uses,
            "worked": self.worked,
            "links": self.links,
            "stale": self.stale,
            "how_it_goes": self.how_it_goes,
        }


def _read_when(said: str) -> float | None:
    if not said:
        return None
    try:
        parts = [int(part) for part in said.split("-")]
        if len(parts) != 3:
            return None
        import calendar

        return calendar.timegm((parts[0], parts[1], parts[2], 0, 0, 0, 0, 0, 0))
    except (TypeError, ValueError):
        return None


def today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def as_a_name(title: str) -> str:
    """The file name a note with this title lives under."""

    tidy = re.sub(r"[^A-Za-z0-9]+", "-", str(title or "").strip().lower()).strip("-")
    if not tidy:
        raise VaultError("A note needs a title")
    return tidy[:60]


def check_the_title(title: str) -> str:
    title = str(title or "").strip()
    if not NAME_SHAPE.match(title):
        raise VaultError(
            "A title can hold letters, numbers, spaces and ordinary punctuation, "
            "and has to start with a letter or number"
        )
    return title


def folder(config: LoadedConfig) -> Path:
    return confined_path(
        config.project_root, WHERE_THEY_LIVE, allow_missing=True, allow_control=True
    )


def file_for(config: LoadedConfig, name: str) -> Path:
    safe = as_a_name(name)
    return confined_path(
        config.project_root, f"{WHERE_THEY_LIVE}/{safe}.md", allow_missing=True, allow_control=True
    )


# ---- reading and writing one note ------------------------------------------


def _read_front(text: str) -> tuple[dict[str, Any], str]:
    """The few lines at the top, and everything after them."""

    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    front: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            inside = value[1:-1].strip()
            front[key] = [part.strip().strip("'\"") for part in inside.split(",") if part.strip()]
        else:
            front[key] = value.strip("'\"")
    return front, text[end + 4:].lstrip("\n")


def _write_front(note: Note) -> str:
    tags = ", ".join(note.tags)
    return (
        "---\n"
        f"title: {note.title}\n"
        f"kind: {note.kind}\n"
        f"tags: [{tags}]\n"
        f"sure: {note.sure}\n"
        f"learned: {note.learned}\n"
        f"touched: {note.touched}\n"
        f"from: {note.came_from}\n"
        f"uses: {note.uses}\n"
        f"worked: {note.worked}\n"
        "---\n\n"
        f"{note.body.strip()}\n"
    )


def _as_a_number(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def read_one(config: LoadedConfig, name: str) -> Note:
    where = file_for(config, name)
    if not where.is_file():
        raise VaultError(f"There is no note called {name}")
    try:
        text = where.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise VaultError(f"{where.name} cannot be read: {exc}") from exc
    return from_text(where.stem, text)


def from_text(name: str, text: str) -> Note:
    """One note, read out of the file it lives in."""

    front, body = _read_front(text)
    kind = str(front.get("kind") or "about-this-project")
    return Note(
        name=name,
        title=str(front.get("title") or name.replace("-", " ")),
        kind=kind if kind in KINDS else "about-this-project",
        body=body.strip(),
        tags=[
            re.sub(r"[^A-Za-z0-9_-]+", "", str(tag))[:32]
            for tag in (front.get("tags") or [])
            if re.sub(r"[^A-Za-z0-9_-]+", "", str(tag))
        ][:12],
        sure=max(0.0, min(1.0, _as_a_number(front.get("sure"), 0.5))),
        learned=str(front.get("learned") or ""),
        touched=str(front.get("touched") or front.get("learned") or ""),
        came_from=str(front.get("from") or ""),
        uses=int(_as_a_number(front.get("uses"), 0)),
        worked=int(_as_a_number(front.get("worked"), 0)),
        links=sorted({as_a_name(found) for found in LINK.findall(body)}),
    )


def write_one(config: LoadedConfig, note: Note, *, was: str = "") -> Note:
    """Write a note, checking it over first.

    Hand in `was` when changing a note that already exists. Two things depend
    on it:

      - Changing a title changes the file name. Without knowing which note this
        was, the old file is left behind and the vault quietly holds two.
      - Two different titles can turn into the same file name - "Payment Notes"
        and "Payment  Notes" both become payment-notes. Writing anyway would
        destroy somebody's note without a word, so it is refused unless this is
        that same note.
    """

    note.title = check_the_title(note.title)
    if note.kind not in KINDS:
        raise VaultError(f"{note.kind} is not a kind of note this keeps")
    if len(note.body) > MOST_LETTERS:
        raise VaultError(
            f"A note is a small thing: this one is longer than {MOST_LETTERS} letters. "
            "Anything that long belongs in the project, with a note pointing at it."
        )
    if any(ord(letter) < 32 and letter not in "\t\n\r" for letter in note.body):
        raise VaultError("That note holds a control character")
    note.tags = [re.sub(r"[^A-Za-z0-9_-]+", "", str(tag))[:32] for tag in note.tags]
    note.tags = sorted({tag for tag in note.tags if tag})[:12]
    note.name = as_a_name(note.title)
    note.learned = note.learned or today()
    note.touched = today()
    note.links = sorted({as_a_name(found) for found in LINK.findall(note.body)})
    where = file_for(config, note.name)
    was = as_a_name(was) if was else ""
    if where.is_file() and note.name != was:
        there = read_one(config, note.name)
        if there.title != note.title:
            raise VaultError(
                f"There is already a note called {there.title}, and it lives in the "
                f"same file as {note.title} would. Give this one a name that is "
                "different by more than spaces, capitals or punctuation."
            )
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(_write_front(note), encoding="utf-8")
    if was and was != note.name:
        # The title changed, so the file name changed with it. This is one note
        # moving, not two notes existing.
        file_for(config, was).unlink(missing_ok=True)
    return note


def remove(config: LoadedConfig, name: str) -> str:
    where = file_for(config, name)
    if not where.is_file():
        raise VaultError(f"There is no note called {name}")
    where.unlink()
    return f"{name} was removed."


# ---- the whole vault --------------------------------------------------------


def all_notes(config: LoadedConfig) -> list[Note]:
    where = folder(config)
    if not where.is_dir():
        return []
    found: list[Note] = []
    for path in sorted(where.glob("*.md"))[:MOST_NOTES]:
        try:
            found.append(from_text(path.stem, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, VaultError):
            # A file that is not text at all is somebody else's, or a mistake.
            # Either way one bad file must not hide every other note.
            continue
    return found


def everything(config: LoadedConfig) -> dict[str, Any]:
    """The whole vault, ready to draw: notes, links, tags, and what is missing."""

    notes = all_notes(config)
    by_name = {note.name: note for note in notes}
    links: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for note in notes:
        for onto in note.links:
            if onto == note.name:
                continue
            if onto in by_name:
                links.append({"from": note.name, "to": onto})
            else:
                # A link to a note nobody has written yet. Obsidian draws these
                # too, and they are the most useful thing in the picture: they
                # are what somebody meant to write down and has not yet.
                missing.append({"from": note.name, "to": onto})
    tags: dict[str, int] = {}
    for note in notes:
        for tag in note.tags:
            tags[tag] = tags.get(tag, 0) + 1
    return {
        "notes": [note.to_dict() for note in notes],
        "links": links,
        "not_written_yet": missing,
        "tags": [{"tag": tag, "notes": count} for tag, count in sorted(tags.items())],
        "kinds": [
            {"kind": kind, "name": name, "means": means}
            for kind, (name, means) in KINDS.items()
        ],
        "counts": {
            "notes": len(notes),
            "links": len(links),
            "stale": len([note for note in notes if note.stale]),
            "not_written_yet": len({item["to"] for item in missing}),
        },
    }


def search(config: LoadedConfig, words: str) -> list[Note]:
    """Every note holding these words, in the title, the body, or a tag.

    The panel sifts the notes it already has, which is quicker than asking.
    This is for a vault too large to hand over whole, where the asking has to
    happen where the notes are.
    """

    looking = str(words or "").strip().lower()
    if not looking:
        return all_notes(config)
    wanted = [part for part in looking.split() if part]
    found: list[Note] = []
    for note in all_notes(config):
        haystack = f"{note.title} {note.body} {' '.join(note.tags)} {note.kind}".lower()
        if all(part in haystack for part in wanted):
            found.append(note)
    return found


def neighbours(config: LoadedConfig, name: str) -> dict[str, Any]:
    """The notes around one note: what it points at, and what points at it."""

    notes = all_notes(config)
    by_name = {note.name: note for note in notes}
    if name not in by_name:
        raise VaultError(f"There is no note called {name}")
    points_at = [by_name[onto].to_dict() for onto in by_name[name].links if onto in by_name]
    points_here = [
        note.to_dict() for note in notes if name in note.links and note.name != name
    ]
    return {"note": by_name[name].to_dict(), "points_at": points_at, "points_here": points_here}


# ---- how a note earns its place --------------------------------------------


def used(config: LoadedConfig, name: str, *, went_well: bool) -> Note:
    """Say that a note was used, and whether that went well.

    This is what turns a pile of notes into something that improves. A note
    used ten times that went well ten times is worth more than one written once
    and never touched, and the picture shows the difference.
    """

    note = read_one(config, name)
    note.uses += 1
    if went_well:
        note.worked += 1
    # What it is worth moves towards how it actually goes, rather than jumping,
    # so one bad afternoon does not throw away everything a note has earned.
    note.sure = round(max(0.05, min(0.99, note.sure + (0.1 if went_well else -0.15))), 2)
    return write_one(config, note)


def going_stale(config: LoadedConfig) -> list[Note]:
    """Notes nothing has touched for a long time.

    Not deleted, and not disbelieved: shown, so somebody can say whether they
    are still true. A harness that keeps believing everything it ever learned
    ends up confidently wrong.
    """

    return [note for note in all_notes(config) if note.stale]


def lately(config: LoadedConfig, days: int = 14) -> list[Note]:
    """What has been learned or touched lately."""

    since = time.time() - max(1, min(3650, int(days))) * 86400
    fresh = []
    for note in all_notes(config):
        when = _read_when(note.touched or note.learned)
        if when is not None and when >= since:
            fresh.append(note)
    return sorted(fresh, key=lambda note: note.touched or note.learned, reverse=True)


# ---- learning from what the harness already has -----------------------------


def learn_from_memory(config: LoadedConfig, *, most: int = 40) -> dict[str, Any]:
    """Turn what the harness remembers into notes, without writing over yours.

    The harness already keeps what happened in every run. That is a record, not
    knowledge: this reads it and writes down the parts worth keeping as notes,
    leaving anything a person has edited exactly as it is.
    """

    from .memory import MemoryStore

    made: list[str] = []
    already: list[str] = []
    with MemoryStore(config) as memory:
        found = memory.memory_graph(limit=max(1, min(200, most)))
    passed_over: list[str] = []
    for record in found.get("records", []):
        title = str(record.get("title") or "").strip()
        body = str(record.get("summary") or record.get("body") or "").strip()
        if not title or not body:
            continue
        try:
            # A run can call something "Bug: fixed a race", and a colon is not
            # something a file name may hold. One record nobody can turn into a
            # note must not stop the others, and must not throw away the ones
            # already written.
            tidy_title = check_the_title(_as_a_title(title)[:80])
            name = as_a_name(tidy_title)
        except VaultError:
            passed_over.append(title[:60])
            continue
        where = file_for(config, name)
        if where.is_file():
            already.append(name)
            continue
        note = Note(
            name=name,
            title=tidy_title,
            kind="about-this-project",
            body=body[:2000],
            tags=["from-a-run"],
            sure=float(record.get("trust") or 0.5),
            came_from=str(record.get("run_id") or "a run"),
        )
        try:
            write_one(config, note)
        except VaultError:
            passed_over.append(title[:60])
            continue
        made.append(name)
    said = (
        f"{len(made)} note(s) written from what the harness remembers."
        if made
        else "Nothing new: everything worth keeping is already a note."
    )
    if passed_over:
        said += f" {len(passed_over)} could not be turned into a note."
    return {
        "made": made,
        "already_here": already,
        "passed_over": passed_over,
        "note": said,
    }


def _as_a_title(said: str) -> str:
    """Something a run said, tidied into something that can be a title."""

    tidy = re.sub(r"[^A-Za-z0-9 ,._'()-]+", " ", str(said or ""))
    return re.sub(r"\s+", " ", tidy).strip()


def a_starting_vault() -> list[Note]:
    """The few notes a fresh vault begins with, so it is never a blank page."""

    return [
        Note(
            name="how-this-vault-works",
            title="How this vault works",
            kind="about-this-project",
            body=(
                "Everything the harness learns about you and this project is kept here "
                "as a note. One markdown file each, in .harness/vault, which you can "
                "open in any editor.\n\n"
                "Notes point at each other with double brackets, and the picture draws "
                "a line for every link. See [[what-a-note-is-for]].\n\n"
                "Nothing here is secret and nothing here is fixed. Correct a note that "
                "is wrong, and delete one that is no longer true."
            ),
            tags=["start-here"],
            sure=1.0,
        ),
        Note(
            name="what-a-note-is-for",
            title="What a note is for",
            kind="about-this-project",
            body=(
                "A note holds one thing worth remembering. There are four kinds:\n\n"
                "- About you: how you like to be worked with.\n"
                "- How to: something that worked, so it can be done again.\n"
                "- About this project: what the harness worked out about the code.\n"
                "- Lesson: something that went wrong once, and what fixed it.\n\n"
                "A how-to note keeps count of how often it was used and how often that "
                "went well, so the ones that earn their place stand out. "
                "See [[how-this-vault-works]]."
            ),
            tags=["start-here"],
            sure=1.0,
        ),
    ]


def start_it_off(config: LoadedConfig) -> list[Note]:
    """Write the starting notes, if the vault is empty."""

    if all_notes(config):
        return []
    return [write_one(config, note) for note in a_starting_vault()]
