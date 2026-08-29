"""Running your automations on a timer, with nobody watching.

"Run the whole suite every night at two, and leave the report where I can find
it in the morning." That is the whole idea, and until now the harness could not
do it: everything it ran, somebody had to start.

Watch mode has an `--every`, and it only counts while you sit there watching -
close the window and nothing happens. That is not the same thing, and this
module is the difference.

How it really runs
------------------

The harness does not sit in the background waiting for two in the morning. A
program that must stay running is a program that is not running when you need
it: somebody closes the window, the machine restarts, and the night's run
quietly never happened.

Instead the machine's own scheduler is asked to run one short command - `harness
timer run` - every so often. That command looks at what is due, runs it, writes
down what happened, and stops. The machine handles being asleep, being
restarted, and starting up again on its own, because that is what it is for and
it is better at it than we would be.

Nothing here changes a machine setting on its own. `harness timer install`
writes out the exact line to give your machine, and it is yours to run.

What it will not do
-------------------

  - Run two at once. A run that takes longer than the gap between two firings
    would otherwise pile up on itself.
  - Catch up on everything missed. A machine off for a week comes back to one
    run, not a hundred and sixty-eight, and it says how many it skipped.
  - Run an automation that stops to ask a person. There is nobody there at two
    in the morning; this says so when you set it up rather than at two in the
    morning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .models import HarnessError
from .pipeline_runs import PipelineRunConflict, PipelineRunStore
from .safety import confined_path, take_the_file_away

WHERE_THEY_LIVE = ".harness/timers"
# The one file that says when anything last ran, so a machine that was off
# knows what it missed rather than guessing.
# Named with a dot in front, which no timer's name can produce: a name has to
# start with a letter or number. Called "what-happened.json", a timer called
# "What Happened" wrote straight over it, and every timer in the project
# quietly stopped firing with nothing to say why.
WHAT_HAPPENED = ".harness/timers/.what-happened.json"
# What it used to be called, so a project that already has one keeps its
# history rather than starting the timers again from nothing.
WHAT_HAPPENED_WAS = ".harness/timers/what-happened.json"
# How long one run may take before it is called off. Long enough for a real
# suite on a slow morning; short enough that a stuck run does not block
# tonight's as well.
LONGEST_RUN_SECONDS = 3600.0
# How many runs are remembered per timer.
HOW_MANY_KEPT = 20
RUN_HISTORY_SAID_CHARACTERS = 400
# How far the missed ones are counted before it stops counting. Past this the
# number is said as "more than", because it would not be the real one.
MOST_COUNTED = 1000
NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")

# How often something runs, in plain words. Every one of these is a thing
# somebody says out loud; none of them is a line of five numbers and a star.
HOW_OFTEN: tuple[tuple[str, str, str], ...] = (
    (
        "every-hour",
        "Every hour",
        "On the hour, all day and all night. For something quick.",
    ),
    (
        "every-day",
        "Every day",
        "Once a day, at the time you pick. Weekends as well.",
    ),
    (
        "every-weekday",
        "Every weekday",
        "Monday to Friday, at the time you pick. Not at the weekend.",
    ),
    (
        "every-week",
        "Once a week",
        "On the day and at the time you pick.",
    ),
)
HOW_OFTEN_NAMES = {key for key, _label, _means in HOW_OFTEN}
DAYS: tuple[str, ...] = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)


class TimerError(HarnessError):
    """A timer that could not be read, saved, or run."""


@dataclass
class Timer:
    """One automation, and when it runs."""

    name: str
    automation: str
    how_often: str = "every-day"
    at: str = "02:00"
    on: str = "monday"
    turned_on: bool = True
    runs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "automation": self.automation,
            "how_often": self.how_often,
            "at": self.at,
            "on": self.on,
            "turned_on": self.turned_on,
            "runs": self.runs[-HOW_MANY_KEPT:],
        }


def check_the_name(name: Any) -> str:
    said = str(name or "").strip()
    if not NAME_SHAPE.fullmatch(said):
        raise TimerError(
            "A timer's name can hold letters, numbers, spaces, dashes and "
            "underscores, and has to start with a letter or number."
        )
    return said


def _check_the_time(at: Any) -> str:
    said = str(at or "").strip()
    found = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", said)
    if not found:
        raise TimerError(
            f"{said!r} is not a time of day. Write it as hours and minutes, "
            "like 02:00 or 17:30."
        )
    return f"{int(found.group(1)):02d}:{found.group(2)}"


def read_it(said: Any) -> Timer:
    """Check one timer, and hand back a tidy one."""

    if not isinstance(said, dict):
        raise TimerError("A timer is a set of settings, and this is not one.")
    how_often = str(said.get("how_often") or "every-day")
    if how_often not in HOW_OFTEN_NAMES:
        raise TimerError(
            f"{how_often!r} is not one of the ways this can run. Pick one of: "
            + ", ".join(sorted(HOW_OFTEN_NAMES))
        )
    on = str(said.get("on") or "monday").strip().lower()
    if on not in DAYS:
        raise TimerError(f"{on!r} is not a day of the week.")
    return Timer(
        name=check_the_name(said.get("name")),
        automation=str(said.get("automation") or "").strip(),
        how_often=how_often,
        # An hourly timer has no time of day, so whatever is there is kept and
        # simply not used, rather than refused.
        at=_check_the_time(said.get("at") or "02:00"),
        on=on,
        turned_on=bool(said.get("turned_on", True)),
        runs=[one for one in (said.get("runs") or []) if isinstance(one, dict)][
            -HOW_MANY_KEPT:
        ],
    )


def in_plain_words(timer: Timer) -> str:
    """When this runs, said the way a person would say it."""

    if timer.how_often == "every-hour":
        return "Every hour, on the hour"
    if timer.how_often == "every-day":
        return f"Every day at {timer.at}"
    if timer.how_often == "every-weekday":
        return f"Every weekday at {timer.at}"
    return f"Every {timer.on.title()} at {timer.at}"


def folder(config: LoadedConfig) -> Path:
    return confined_path(
        config.project_root, WHERE_THEY_LIVE, allow_missing=True, allow_control=True
    )


def _where_it_lives(config: LoadedConfig, name: str) -> Path:
    safe = check_the_name(name).replace(" ", "-").lower()
    return confined_path(
        config.project_root,
        f"{WHERE_THEY_LIVE}/{safe}.json",
        allow_missing=True,
        allow_control=True,
    )


def _write_it_whole(path: Path, written: str) -> None:
    """Write beside, then move into place, so no reader sees half of one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    beside = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    beside.write_text(written, encoding="utf-8")
    for wait in (0.02, 0.05, 0.1, 0.2, 0.4):
        try:
            os.replace(beside, path)
            return
        except PermissionError:
            time.sleep(wait)
    os.replace(beside, path)


def every_one(config: LoadedConfig) -> list[Timer]:
    """Every timer this project has, by name."""

    where = folder(config)
    if not where.is_dir():
        return []
    found: list[Timer] = []
    for path in sorted(where.glob("*.json")):
        if path.name == Path(WHAT_HAPPENED).name:
            continue
        try:
            found.append(read_it(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, TimerError):
            # One timer nobody can read is one timer, not the end of the list.
            continue
    return found


def load(config: LoadedConfig, name: str) -> Timer:
    path = _where_it_lives(config, name)
    if not path.is_file():
        raise TimerError(f"There is no timer called {name}.")
    try:
        return read_it(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise TimerError(f"{path.name} cannot be read: {exc}") from exc


def save(config: LoadedConfig, said: Any, *, they_meant_it: bool = False) -> Timer:
    """Keep one timer. Whatever it ran before is kept with it.

    An automation that stops to ask a person is refused here unless they said to
    do it anyway. Here, and not at each of the places that call this, because
    this is the one function that really writes a timer down. Guarded at the
    callers, the guard was missed by the next caller somebody added - which is
    how the panel came to be the only place that asked.
    """

    timer = read_it(said)
    if not timer.automation:
        raise TimerError("Say which automation this runs.")
    if timer.turned_on and not they_meant_it:
        # Turning one off is never refused. Only leaving it on is - and only for
        # the one reason nobody can do anything about at two in the morning.
        why_not = does_it_stop_to_ask_a_person(config, timer.automation)
        if why_not:
            raise TimerError(f"{why_not} Say to do it anyway if you mean it.")
    path = _where_it_lives(config, timer.name)
    if path.is_file():
        try:
            already = read_it(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TimerError):
            already = None
        if already is not None and already.name != timer.name:
            # Two names that come to the same file. Saving would take the other
            # one away without a word, along with everything it had run.
            raise TimerError(
                f"There is already a timer called {already.name}, and "
                f"{timer.name} would write over it - both are kept in "
                f"{path.name}, because capitals, spaces and dashes are not "
                "told apart. Pick a name that differs by more than those."
            )
        timer.runs = already.runs if already is not None else []
    _write_it_whole(path, json.dumps(timer.to_dict(), indent=2) + "\n")
    return timer


def remove(config: LoadedConfig, name: str) -> str:
    path = _where_it_lives(config, name)
    if not path.is_file():
        raise TimerError(f"There is no timer called {name}.")
    take_the_file_away(path)
    return f"{name} was taken off the timer."


def _at_as_numbers(timer: Timer) -> tuple[int, int]:
    hours, minutes = timer.at.split(":")
    return int(hours), int(minutes)


def when_it_runs_next(timer: Timer, after: datetime) -> datetime:
    """The first time this timer is due, at or after the moment given."""

    if timer.how_often == "every-hour":
        on_the_hour = after.replace(minute=0, second=0, microsecond=0)
        return on_the_hour if on_the_hour >= after else on_the_hour + timedelta(hours=1)

    hours, minutes = _at_as_numbers(timer)
    when = after.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if when < after:
        when += timedelta(days=1)
    if timer.how_often == "every-day":
        return when
    if timer.how_often == "every-weekday":
        while when.weekday() >= 5:  # Saturday and Sunday
            when += timedelta(days=1)
        return when
    wanted = DAYS.index(timer.on)
    while when.weekday() != wanted:
        when += timedelta(days=1)
    return when


def _what_happened(config: LoadedConfig) -> dict[str, Any]:
    where = confined_path(
        config.project_root, WHAT_HAPPENED, allow_missing=True, allow_control=True
    )
    if not where.is_file():
        # The one this used to be kept in, read once so a project that already
        # has timers does not start them all again from nothing.
        older = confined_path(
            config.project_root, WHAT_HAPPENED_WAS, allow_missing=True,
            allow_control=True,
        )
        if older.is_file():
            try:
                held = json.loads(older.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            # Only if it really is a record and not a timer somebody made
            # under that name back when that was possible.
            if isinstance(held, dict) and "automation" not in held:
                return held
        return {}
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        held = None
    if not isinstance(held, dict):
        # Read as nothing, every timer looks brand new and the week the machine
        # was off is lost without a word. Put the bad one aside instead, so
        # there is something to look at and something to say.
        _put_the_broken_one_aside(where)
        return {}
    return held


def _put_the_broken_one_aside(where: Path) -> None:
    beside = where.with_name(f"{where.name}.could-not-be-read")
    try:
        os.replace(where, beside)
    except OSError:
        pass


def what_could_not_be_read(config: LoadedConfig) -> str:
    """Something to say when the record of what ran had to be put aside."""

    beside = confined_path(
        config.project_root,
        f"{WHAT_HAPPENED}.could-not-be-read",
        allow_missing=True,
        allow_control=True,
    )
    if not beside.is_file():
        return ""
    return (
        f"{beside.name} could not be read, so it was put aside and the record "
        "started again. Nothing is broken, but a run that was due while it was "
        "unreadable was not counted as missed."
    )


def _keep_what_happened(config: LoadedConfig, held: dict[str, Any]) -> None:
    where = confined_path(
        config.project_root, WHAT_HAPPENED, allow_missing=True, allow_control=True
    )
    _write_it_whole(where, json.dumps(held, indent=2) + "\n")


def what_is_due(
    config: LoadedConfig, now: datetime | None = None
) -> list[tuple[Timer, int]]:
    """Every timer that should have run by now, and how many it missed.

    A machine that was off for a week comes back to one run of each, not a
    hundred and sixty-eight, and the number it skipped is handed back so
    somebody can be told rather than left to wonder.
    """

    now = now or datetime.now()
    seen = _what_happened(config)
    due: list[tuple[Timer, int]] = []
    for timer in every_one(config):
        if not timer.turned_on or not timer.automation:
            continue
        last = seen.get(timer.name, {}).get("last_looked", "")
        since = _from_words(last) if last else None
        if since is None:
            # Never looked at before, so nothing is owed yet. This is what
            # stops a timer added at noon running the night's job the moment
            # it is saved.
            continue
        when = when_it_runs_next(timer, since + timedelta(seconds=1))
        if when > now:
            continue
        missed = 0
        after = when
        while True:
            after = when_it_runs_next(timer, after + timedelta(seconds=1))
            if after > now:
                break
            missed += 1
            if missed >= MOST_COUNTED:
                # Counted no further. A machine off for years is still one run,
                # and whoever reads this is told "more than", not a number that
                # would be wrong.
                break
        due.append((timer, missed))
    return due


def how_many_missed_in_words(missed: int) -> str:
    """How many were missed, said honestly when it stopped counting."""

    if not missed:
        return ""
    if missed >= MOST_COUNTED:
        return f"more than {MOST_COUNTED} missed while the machine was off"
    return f"{missed} missed while the machine was off"


def _in_words(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def _from_words(said: str) -> datetime | None:
    try:
        return datetime.strptime(said, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def looked_just_now(config: LoadedConfig, now: datetime | None = None) -> None:
    """Write down that the timers were looked at, so nothing fires twice.

    Called the first time as well, which is what stops a timer added at noon
    running the night's job the moment it is saved.
    """

    now = now or datetime.now()
    held = _what_happened(config)
    for timer in every_one(config):
        one = held.setdefault(timer.name, {})
        one.setdefault("last_looked", _in_words(now))
    _keep_what_happened(config, held)


def write_down_a_run(
    config: LoadedConfig, timer: Timer, said: str, passed: bool, missed: int = 0,
    when: datetime | None = None, *, by_hand: bool = False, run_id: str = "",
) -> None:
    """Keep what one run did.

    A run somebody started by hand does not move the timer. Pressing "run it
    now" at noon should not push tonight's run to tomorrow: they asked for one
    extra, not for one instead.
    """

    when = when or datetime.now()
    if not by_hand:
        held = _what_happened(config)
        held.setdefault(timer.name, {})["last_looked"] = _in_words(when)
        _keep_what_happened(config, held)
    # What a step printed goes into a file people are told to commit. A key in
    # a failing command's output would be committed along with it, for good.
    # Cleaned again here even though the runner cleans it too: this is called
    # from the panel as well, and the file is the one thing that keeps it.
    said = in_safe_words(config, said)
    original_characters = len(said)
    said_truncated = original_characters > RUN_HISTORY_SAID_CHARACTERS and bool(run_id)
    full_result_reference = f"pipeline-run:{run_id}" if said_truncated else ""
    if said_truncated:
        marker = (
            f"… [shortened from {original_characters:,} characters; "
            f"full result: {full_result_reference}]"
        )
        kept = max(0, RUN_HISTORY_SAID_CHARACTERS - len(marker))
        said_for_history = said[:kept] + marker
    else:
        # Without a durable run ID there is nowhere honest to point for the
        # omitted tail. Keep it all rather than silently losing it.
        said_for_history = said
    ran = {
        "at": _in_words(when),
        "run_id": run_id,
        "passed": passed,
        "said": said_for_history,
        "said_truncated": said_truncated,
        "said_original_characters": original_characters,
        "full_result_reference": full_result_reference,
        "missed": missed,
    }
    path = _where_it_lives(config, timer.name)
    # Written onto whatever is on disk now, not onto the copy we started with.
    # A run may take the best part of an hour; somebody who turned the timer
    # off while it was going would have found it turned back on afterwards, by
    # a run that had not heard.
    try:
        now_on_disk = read_it(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TimerError):
        now_on_disk = None
    if now_on_disk is None or now_on_disk.name != timer.name:
        # Taken off while it was running, or the name now belongs to a
        # different timer. Either way there is nothing here to write onto, and
        # writing the copy we started with would bring back a timer somebody
        # deleted, or put it over somebody else's new one. The run happened and
        # is still handed back to whoever asked for it; it is only not kept.
        timer.runs = _the_last_of(list(timer.runs) + [ran])
        return
    now_on_disk.runs = _the_last_of(list(now_on_disk.runs) + [ran])
    _write_it_whole(path, json.dumps(now_on_disk.to_dict(), indent=2) + "\n")
    timer.runs = list(now_on_disk.runs)


def in_safe_words(config: LoadedConfig, said: str) -> str:
    """Whatever a step printed, with anything that looks like a key taken out.

    Called where the words are made, not only where they are written down. Kept
    on the file alone, the same text still reached the screen, the terminal and
    the log, which is where somebody reads it out loud over a call.
    """

    from .redaction import CredentialRedactor

    return CredentialRedactor(config).text(said)


def _the_last_of(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest few. A timer left running for a year is not a growing file."""

    return runs[-HOW_MANY_KEPT:]


def does_it_stop_to_ask_a_person(config: LoadedConfig, automation: str) -> str:
    """The one reason a timer is refused, and nothing else.

    What_stops_it_running_alone also hands back "there is no automation called
    that", which is worth showing somebody but is not a reason to refuse: an
    automation nobody has drawn yet is somebody's next job, and the timer will
    simply say so when it gets there.
    """

    from . import pipelines as pipeline_lab
    try:
        pipeline_lab.load(config, automation)
    except HarnessError:
        return ""
    return what_stops_it_running_alone(config, automation)


def what_stops_it_running_alone(config: LoadedConfig, automation: str) -> str:
    """Why this automation should not be put on a timer, if it should not.

    A step that stops to ask a person has nobody to ask at two in the morning.
    Saying so now beats finding out from a run that sat there all night.
    """

    from . import pipelines as pipeline_lab

    try:
        pipeline_lab.load(config, automation)
    except HarnessError as exc:
        # Worth saying, and shown - but it is not the reason this refuses. An
        # automation somebody has not drawn yet is somebody's next job, not a
        # timer that will sit there all night with nobody to answer it.
        return str(exc)
    waiting = [
        step if where == automation else f"{step} (in {where})"
        for step, where in _where_it_stops_to_ask(config, automation)
    ]
    if waiting:
        return (
            f"{automation} stops to ask a person at: {', '.join(waiting[:3])}. "
            "Nobody is there when this runs, so it would wait and then give up. "
            "Take that step out, or set it to run only when you are watching."
        )
    return ""


def _where_it_stops_to_ask(
    config: LoadedConfig, automation: str, seen: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """Every step that stops to ask a person, and which automation it is on.

    The automation comes back with each step because that is what somebody
    needs to go and look at it. Handed the step's name alone, they went looking
    on the wrong drawing.

    Following the ones it runs inside itself as well. One automation can run
    another as a single step, and looking only at the steps drawn on this one
    missed the ask completely: the timer went on with no warning at all, and at
    two in the morning the outer one started the inner one, which sat there
    waiting for somebody who had gone home.

    Followed exactly as deep as a run itself follows them, and never the same
    one twice, so two automations that run each other cannot go round for ever.

    Exactly as deep matters, and getting it nearly right is worse than not
    trying. Counted one step short, the last automation a run really does reach
    was never opened, and an ask sitting in it was invisible: no warning, and a
    night spent waiting for somebody who had gone home.
    """

    from . import pipelines as pipeline_lab

    if automation in seen:
        return []
    try:
        held = pipeline_lab.load(config, automation)
    except HarnessError:
        return []
    found: list[tuple[str, str]] = []
    for node in held.get("nodes", []):
        if node.get("kind") == "wait_for_a_person":
            found.append((str(node.get("label") or node.get("id")), automation))
        elif node.get("kind") == "another_pipeline":
            inside = str((node.get("settings") or {}).get("pipeline") or "").strip()
            # The same sum a run does before it takes a step into another one.
            if not inside or len(seen) + 1 > pipeline_lab.DEEPEST_NESTING:
                continue
            # Handed back as it came: the step keeps the automation it is really
            # on, rather than picking up the name of every one on the way down.
            found.extend(_where_it_stops_to_ask(config, inside, seen | {automation}))
    # One automation can be run by two others in the same drawing, and saying
    # the same step twice reads like two problems.
    return list(dict.fromkeys(found))


# How often a run that is going touches its lock, and how long a lock has to
# sit untouched before it is treated as left behind. Judged on the touching
# rather than on how long the run has taken: a run really can take hours, and
# taking the lock off one that is still working is the very thing the lock is
# there to stop.
TOUCH_THE_LOCK_EVERY = 30.0
LEFT_BEHIND_AFTER = 300.0
# And however sure we are that the run holding it is still going, a lock is
# never believed for longer than this. A process number is only a number: the
# machine hands the same one out again once the first is gone, and a run that
# crashed can leave its number to somebody else's program. Without this the
# lock stops being a lock and becomes a project that never runs anything again.
NEVER_HELD_LONGER_THAN = 24 * 3600.0


def _only_one_at_a_time(config: LoadedConfig):
    """A lock on disk, so two runs of the timer never overlap.

    The machine's scheduler will happily start another while the last one is
    still going. A suite that takes eleven minutes on a ten minute timer would
    otherwise pile up on itself until nothing finishes.
    """

    where = confined_path(
        config.project_root,
        f"{WHERE_THEY_LIVE}/running.lock",
        allow_missing=True,
        allow_control=True,
    )
    where.parent.mkdir(parents=True, exist_ok=True)
    try:
        holding = os.open(where, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Unless whoever left it is long gone. A machine that lost power mid
        # run would otherwise never run anything again.
        #
        # How long it has been touched, not how long the run has taken. A run
        # that is still working touches this every half minute; one whose
        # machine went down stops. Judged on the run's own length, a genuinely
        # slow suite had its lock taken away and was run a second time
        # alongside itself - the one thing this is here to stop.
        #
        # And on top of that: only if whoever left it is really gone. Both the
        # clock and the file's own time are the machine's wall clock, and that
        # can jump - somebody fixes a wrong clock, or the machine asks the
        # internet what the time is and is told an hour later. A jump forward
        # made a run that was working look long dead.
        try:
            untouched_for = time.time() - where.stat().st_mtime
        except OSError:
            untouched_for = NEVER_HELD_LONGER_THAN + 1
        if untouched_for <= LEFT_BEHIND_AFTER:
            return None
        if (
            untouched_for < NEVER_HELD_LONGER_THAN
            and _is_it_still_the_same_run(_who_left_it(where))
        ):
            return None
        take_the_file_away(where, missing_ok=True)
        try:
            holding = os.open(where, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None
    os.write(holding, json.dumps(_who_is_holding_it()).encode("utf-8"))
    os.close(holding)
    return where


def _who_is_holding_it() -> dict[str, Any]:
    """Enough about this run that nothing else can be mistaken for it."""

    import platform

    return {
        "process": os.getpid(),
        "started": _when_that_process_started(os.getpid()),
        "machine": platform.node(),
    }


def run_what_is_due(
    config: LoadedConfig, now: datetime | None = None, *, check_kinds: Any = None
) -> dict[str, Any]:
    """Run everything that should have run by now, and write down what happened.

    This is the whole of what the machine's scheduler calls. It looks, it runs,
    it writes down, and it stops. Nothing stays running between one firing and
    the next.
    """

    from . import pipelines as pipeline_lab

    now = now or datetime.now()
    due = what_is_due(config, now)
    if not due:
        looked_just_now(config, now)
        return {"ran": [], "note": "Nothing was due."}

    held = _only_one_at_a_time(config)
    if held is None:  # noqa: SIM108 - the two ways out read better apart
        return {
            "ran": [],
            "note": (
                "The last run is still going, so this one was left. Nothing is "
                "lost: it will be due again next time."
            ),
        }
    ran: list[dict[str, Any]] = []
    run_store = PipelineRunStore(config)
    still_going = threading.Event()
    keep_touching = threading.Thread(
        target=_keep_saying_it_is_alive, args=(held, still_going), daemon=True
    )
    keep_touching.start()
    try:
        for timer, missed in due:
            began = time.monotonic()
            run_id = ""
            attempt_id = ""
            try:
                automation = pipeline_lab.load(config, timer.automation)
                frozen = pipeline_lab.freeze_definition(config, automation)
                history = _what_happened(config).get(timer.name, {})
                since = _from_words(history.get("last_looked", ""))
                occurrence = (
                    when_it_runs_next(timer, since + timedelta(seconds=1))
                    if since is not None else now
                )
                timer_digest = hashlib.sha256(
                    json.dumps(timer.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:16]
                timer_name_digest = hashlib.sha256(timer.name.encode("utf-8")).hexdigest()[:16]
                try:
                    accepted, created = run_store.accept(
                        frozen,
                        source=f"timer:{timer.name}",
                        request_id=(
                            f"timer:{timer_name_digest}:{timer_digest}:"
                            f"{occurrence.isoformat(timespec='seconds')}"
                        ),
                    )
                except PipelineRunConflict as exc:
                    ran.append({
                        "timer": timer.name,
                        "automation": timer.automation,
                        "run_id": "",
                        "passed": False,
                        "said": in_safe_words(config, str(exc)),
                        "missed": missed,
                        "deferred": True,
                        "told": [],
                    })
                    continue
                run_id = accepted["run_id"]
                attempt_id = accepted["attempt_id"]
                if not created:
                    prior = accepted.get("result")
                    if not isinstance(prior, dict):
                        ran.append({
                            "timer": timer.name, "automation": timer.automation,
                            "run_id": run_id, "passed": False,
                            "said": f"Occurrence is already {accepted['state']}; it was not duplicated.",
                            "missed": missed, "deferred": True, "told": [],
                        })
                        continue
                    passed = bool(prior.get("passed"))
                    said = str(prior.get("said") or "")
                else:
                    run_store.start(run_id, attempt_id)
                    run = pipeline_lab.run_it(
                        config,
                        automation,
                        check_kinds=check_kinds,
                        stopping=lambda: (
                            run_store.should_stop(run_id)
                            or time.monotonic() - began > LONGEST_RUN_SECONDS
                        ),
                        run_id=run_id,
                        frozen=frozen,
                        decision_nonce=attempt_id,
                    )
                    run_result = (
                        run.to_dict() if callable(getattr(run, "to_dict", None))
                        else {
                            "passed": bool(getattr(run, "passed", False)),
                            "outcome": "passed" if getattr(run, "passed", False) else "failed",
                            "said": str(getattr(run, "said", "")),
                            "nodes": [],
                        }
                    )
                    finished = run_store.finish(run_id, attempt_id, run_result)
                    prior = finished.get("result") or run_result
                    passed, said = bool(prior.get("passed")), str(prior.get("said") or "")
            except HarnessError as exc:
                passed, said = False, str(exc)
                if run_id:
                    try:
                        run_store.fail(run_id, attempt_id, said)
                    except HarnessError:
                        pass
            except BaseException as exc:  # one occurrence must be closed even on thread death
                passed, said = False, f"The timer run stopped unexpectedly: {exc}"
                if run_id:
                    try:
                        run_store.fail(run_id, attempt_id, said)
                    except HarnessError:
                        pass
                # Process/thread control exceptions retain their semantics,
                # but only after the durable run has been terminalized.
                if not isinstance(exc, Exception):
                    raise
            # Cleaned here, before it goes anywhere: this same text is written
            # down, printed in a terminal, and put on the panel.
            said = in_safe_words(config, said)
            write_down_a_run(config, timer, said, passed, missed, now, run_id=run_id)
            ran.append({
                "timer": timer.name,
                "automation": timer.automation,
                "run_id": run_id,
                "passed": passed,
                "said": said,
                "missed": missed,
                "told": _tell_somebody_about_it(
                    config, timer, passed, said, run_id=run_id
                ),
            })
    finally:
        still_going.set()
        keep_touching.join(timeout=5)
        take_the_file_away(held, missing_ok=True)
    deferred = sum(1 for item in ran if item.get("deferred"))
    completed = len(ran) - deferred
    note = f"{completed} ran."
    if deferred:
        note += f" {deferred} deferred without advancing its occurrence."
    return {"ran": ran, "note": note}


def _who_left_it(where: Path) -> dict[str, Any]:
    """Who this lock says is holding it.

    Older locks held nothing but a number. Those are still read, and simply do
    not say when the run started or which machine it was on.
    """

    try:
        said = where.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if said.isdigit():
        return {"process": int(said)}
    try:
        held = json.loads(said)
    except json.JSONDecodeError:
        return {}
    return held if isinstance(held, dict) else {}


def _is_it_still_the_same_run(held: dict[str, Any]) -> bool:
    """Is the run that made this lock the one going under that number now?

    A number on its own is not enough. The machine hands the same number out
    again once the first program is gone, and a lock file that came from
    somebody else's machine names a number that means nothing here. Both were
    read as "still going", and the timers stopped for good.

    So the lock says when its run started as well, and that is compared. A
    number handed out again belongs to something that started later, and the
    two never match.
    """

    import platform

    if not held:
        return False
    on = held.get("machine")
    if on and on != platform.node():
        # It came from somewhere else - a shared folder, or a repository. There
        # is nothing here it could be talking about.
        return False
    process = held.get("process") or 0
    if not isinstance(process, int) or not _is_it_still_going(process):
        return False
    began = held.get("started")
    going_since = _when_that_process_started(process)
    if began is None or going_since is None:
        # This machine will not say. Then it is only the number, which is worth
        # something but not everything - and NEVER_HELD_LONGER_THAN is what
        # stops that being for ever.
        return True
    return began == going_since


def _when_that_process_started(process: int):
    """When that process started, as this machine counts it, or None.

    Only ever compared with another answer from this same machine, so what the
    number means does not matter - only that the same process gives the same
    answer and a different one does not.
    """

    if process <= 0:
        return None
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(QUERY, False, process)
        if not handle:
            return None
        try:
            made = ctypes.wintypes.FILETIME()
            ended = ctypes.wintypes.FILETIME()
            in_the_machine = ctypes.wintypes.FILETIME()
            for_us = ctypes.wintypes.FILETIME()
            got = kernel.GetProcessTimes(
                handle,
                ctypes.byref(made),
                ctypes.byref(ended),
                ctypes.byref(in_the_machine),
                ctypes.byref(for_us),
            )
            if not got:
                return None
            return (made.dwHighDateTime << 32) | made.dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    # Linux keeps it in the twenty-second thing on this line. Machines that do
    # not have it say nothing, and the never-held-longer-than cap covers them.
    try:
        said = Path(f"/proc/{process}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The program's own name is in brackets and may hold spaces, so everything
    # up to the last bracket is put aside first.
    after = said.rpartition(")")[2].split()
    return int(after[19]) if len(after) > 19 and after[19].isdigit() else None


def _is_it_still_going(process: int) -> bool:
    """Is that process still on this machine?

    Being wrong one way is much worse than the other. Thinking a dead run is
    alive costs one skipped firing, and it is due again next time. Thinking a
    live run is dead starts a second copy of your whole suite alongside the
    first, which is the one thing the lock is here to stop. So anything we
    cannot answer is answered "still going".
    """

    if process <= 0:
        return False
    if os.name == "nt":
        # Not os.kill: on Windows that ends the process, whatever number you
        # hand it. We only want to look.
        import ctypes

        QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        NO_SUCH_PROCESS = 87  # ERROR_INVALID_PARAMETER
        STILL_GOING = 259  # STILL_ACTIVE
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(QUERY, False, process)
        if not handle:
            return ctypes.get_last_error() != NO_SUCH_PROCESS
        try:
            how_it_ended = ctypes.c_ulong(0)
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(how_it_ended)):
                return True
            return how_it_ended.value == STILL_GOING
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(process, 0)  # Asks after it; does not touch it.
    except ProcessLookupError:
        return False
    except OSError:
        # Somebody else's process, which we are not allowed to ask about. It is
        # there, which is what we wanted to know.
        return True
    return True


def _tell_somebody_about_it(
    config: LoadedConfig, timer: Timer, passed: bool, said: str, *, run_id: str = ""
) -> list[dict[str, Any]]:
    """Say what happened, wherever somebody asked to be told.

    Only when it did not pass. A run at two in the morning that went fine is
    not news, and something that tells you every night is something you stop
    reading by the end of the week.

    Nothing here is allowed to stop the run. Whoever set this up wanted to hear
    about a failing suite; being unable to reach Slack is not a reason to lose
    the record of what the suite did.
    """

    from . import tell_somebody as telling

    if passed:
        return []
    try:
        return [
            one.to_dict()
            for one in telling.tell_everybody(
                config,
                f"{timer.name} did not pass",
                f"{timer.automation}, run by the timer.\n\n{said}",
                passed=passed,
                only_when_it_fails=True,
                full_result_reference=(
                    "Nexus → Visual test automation → run "
                    f"{run_id} (pipeline-run:{run_id})"
                    if run_id else ""
                ),
            )
        ]
    except HarnessError as exc:
        return [{"name": "", "sent": False, "note": str(exc)}]


def _keep_saying_it_is_alive(where: Path, stop: threading.Event) -> None:
    """Touch the lock while the run goes on, so nobody thinks it was left."""

    while not stop.wait(TOUCH_THE_LOCK_EVERY):
        try:
            os.utime(where, None)
        except OSError:
            return


def how_to_ask_this_machine(config: LoadedConfig, every_minutes: int = 10) -> dict[str, str]:
    """The line to give this machine's own scheduler, and how to take it off.

    Written out rather than run. Asking a machine to start something on its own
    is a change to that machine, and that is somebody's own decision to make -
    so this hands over the exact words and stays out of it.
    """

    import shlex
    import shutil
    import sys

    where = config.project_root
    name = f"harness-timer-{project_short_name(where)}"
    from .starting import how_to_start_the_harness

    found = shutil.which("harness")
    # Asked in one place. Written out here as "python -m our_harness", the line
    # handed to the scheduler was right for anybody who had installed the
    # harness and wrong for everybody who had only downloaded it - and it went
    # wrong at two in the morning, months later, with nobody watching.
    starting = how_to_start_the_harness()
    if os.name == "nt":
        # Every path gets quotes. This project's own folder has a space in its
        # name, and so does the usual place Python is installed for everybody:
        # left bare, the line looks right, is accepted, and never runs.
        start = (
            f'"{found}"' if found
            else " ".join(f'"{one}"' if " " in one else one for one in starting)
        )
        inside = f'cmd /c cd /d "{where}" && {start} timer run'
        return {
            "name": name,
            "what": (
                f'schtasks /create /tn "{name}" /sc minute /mo {every_minutes} '
                f"/tr {_as_one_windows_argument(inside)} /f"
            ),
            "to_take_it_off": f'schtasks /delete /tn "{name}" /f',
            "to_see_it": f'schtasks /query /tn "{name}"',
            "machine": "this machine's Task Scheduler",
        }
    start = (
        shlex.quote(found) if found
        else " ".join(shlex.quote(one) for one in starting)
    )
    line = f"*/{every_minutes} * * * * cd {shlex.quote(str(where))} && {start} timer run"
    return {
        "name": name,
        "what": f"(crontab -l 2>/dev/null; echo {shlex.quote(line)}) | crontab -",
        "to_take_it_off": "crontab -e   # and take that line out",
        "to_see_it": "crontab -l",
        "machine": "this machine's cron",
    }


def _as_one_windows_argument(said: str) -> str:
    """One argument for schtasks, with the quotes inside it kept.

    The command to run is itself full of quoted paths, and it has to arrive as
    a single argument with those quotes intact. Windows wants each one doubled
    inside the outer pair.
    """

    return '"' + said.replace('"', '\\"') + '"' 


def project_short_name(where: Path) -> str:
    """A name for this project that is safe in a scheduled task's name."""

    said = re.sub(r"[^A-Za-z0-9-]+", "-", where.name).strip("-").lower()
    return said or "project"
