"""No check leans on something the panel can wipe underneath it.

Opening a view makes the panel ask the harness for fresh data. That answer
arrives whenever it arrives. A check that puts its own data in and then relies
on it in a later step is racing that answer: most of the time it wins, and once
in a while it does not, and then it fails for a reason that has nothing to do
with what it was checking.

This has now happened three times, to three different checks, so it is a rule
rather than a habit: a check that stages data on a view that refreshes itself
has to turn that refresh off first, in the same step, before it puts anything
in.

The list of what each view reloads is worked out from the panel's own code, not
written by hand. The first version of this test had a hand-written list, and it
was missing three of the panel's own globals, which left the rule blind in
exactly the places the person writing it had not thought of. Reading the code
means adding a view, or a piece of data to an existing one, is covered on the
day it is written rather than the day somebody remembers.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "src" / "our_harness" / "ui" / "app.js"
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"

_SWITCH_VIEW = re.compile(r"^function switchView\(name\) \{.*$", re.MULTILINE)
# The panel listens for news on its own timer, whatever view is open, and what
# it does when news arrives can wipe the page's data just as a view change can.
_THE_POLLER = "pollEvents"
# if (name === "checks") refreshChecks();  and the { ... } form
_VIEW_BRANCH = re.compile(r'if \(name === "(\w+)"\)\s*(\{[^}]*\}|[^;]+;)')
_A_CALL = re.compile(r"\b(\w+)\(")
_TOP_LEVEL_NAME = re.compile(r"^let (\w+)", re.MULTILINE)
_TURNS_IT_OFF = re.compile(r"refresh\w+\s*=\s*async")


def panel_script() -> str:
    return PANEL.read_text(encoding="utf-8")


def globals_of_the_page() -> set[str]:
    return set(_TOP_LEVEL_NAME.findall(panel_script()))


def body_of(name: str) -> str:
    """The whole of a function, however many lines it is written over.

    Some of this panel is written one function to a line and some over many, so
    reading only the first line quietly finds nothing in half of them. Braces
    are counted instead.
    """

    text = panel_script()
    start = re.search(rf"^(?:async )?function {re.escape(name)}\s*\(", text, re.MULTILINE)
    if not start:
        return ""
    opened = text.find("{", start.end())
    if opened < 0:
        return ""
    depth = 0
    for index in range(opened, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start.start():index + 1]
    return text[start.start():]


def what_a_view_reloads() -> dict[str, set[str]]:
    """Read the panel: which of its own data each view refreshes for itself."""

    switch = _SWITCH_VIEW.search(panel_script())
    if not switch:
        raise AssertionError(
            "Could not find switchView in the panel. If it was rewritten, this "
            "test has to be rewritten with it."
        )
    names = globals_of_the_page()
    found: dict[str, set[str]] = {}
    for view, branch in _VIEW_BRANCH.findall(switch.group(0)):
        wiped: set[str] = set()
        for called in _A_CALL.findall(branch):
            body = body_of(called)
            if not body:
                continue
            for name in names:
                if _replaced_with_what_the_harness_sent(body, name):
                    wiped.add(name)
        found.setdefault(view, set()).update(wiped)
    return found


def _replaced_with_what_the_harness_sent(body: str, name: str) -> bool:
    """Does this function put something the harness sent into that name?

    A name the panel only uses to keep track of itself - which box has the
    keyboard, how far through the events it is, its own timer - is not data a
    check can have replaced underneath it. Data fetched from the harness, or
    taken out of an event, is.

    The question is asked of the whole function rather than of the line the
    name is on, because the fetching is usually a line or two earlier:

        const answer = await request("/api/qa/history");
        historyRuns = answer.runs || [];
    """

    if not re.search(rf"\b{re.escape(name)}\s*=[^=]", body):
        return False
    if re.search(r"await (?:request|collectRecordPages)\(", body):
        return True
    return bool(re.search(rf"\b{re.escape(name)}\s*=[^;\n]{{0,200}}payload", body))


def what_the_poller_reloads() -> set[str]:
    """What the panel's own listening can wipe, whatever view you are on.

    Opening a view is not the only thing that reloads data. The panel asks for
    news every fraction of a second, and what it does with that news assigns to
    the same globals. That window is shorter and always open, so it is the
    worse race of the two, and reading only switchView cannot see it.
    """

    body = body_of(_THE_POLLER)
    if not body:
        raise AssertionError(
            "Could not find the panel's event listening. If it was renamed, "
            "this test has to be renamed with it."
        )
    names = globals_of_the_page()
    wiped: set[str] = set()
    for called in _A_CALL.findall(body):
        inside = body_of(called)
        if not inside:
            continue
        for name in names:
            if _replaced_with_what_the_harness_sent(inside, name):
                wiped.add(name)
    return wiped


def cases() -> list[dict]:
    return json.loads(FLOWS.read_text(encoding="utf-8")).get("cases", [])


def view_of(case: dict) -> str:
    for step in case.get("steps") or []:
        found = re.search(r'\[data-view="(\w+)"\]', str(step.get("target") or ""))
        if found:
            return found.group(1)
    return ""


def racing_steps(case: dict, table: dict[str, set[str]]) -> list[str]:
    """Steps that stage data a view refresh could wipe before a later step."""

    steps = case.get("steps") or []
    # Whatever this view reloads, plus whatever the listening reloads, which is
    # everywhere and always.
    at_risk = table.get(view_of(case), set()) | what_the_poller_reloads()
    found: list[str] = []
    for index, step in enumerate(steps):
        script = str(step.get("script") or "")
        if _TURNS_IT_OFF.search(script):
            continue
        if not steps[index + 1:]:
            continue
        for name in sorted(at_risk):
            # Two ways to stage something, and both are lost the same way when
            # the view reloads: putting a new value in the name, and changing
            # what is already in it. Reading only for the first left the second
            # wide open, and a check staging an arrow by pushing onto a list
            # failed now and then for a reason that looked like nothing.
            put_in = re.search(
                rf"\b{re.escape(name)}\s*=[^=]"
                rf"|\b{re.escape(name)}\.\w+\s*=[^=]"
                rf"|\b{re.escape(name)}(?:\.\w+)*\.(?:push|pop|shift|unshift|splice|sort|reverse|fill)\(",
                script,
            )
            if not put_in:
                continue
            # Read back in the same step means the step got its answer before
            # anything else could run. Only data left for later is at risk.
            # The name has to be read as code, not merely appear: these checks
            # say what they are doing in plain words, and "a pipeline that goes
            # round" was being taken for a read of the data called pipeline.
            used_as_code = rf"\b{re.escape(name)}\s*(?:[.\[(),;=]|$)"
            if re.search(used_as_code, script[put_in.end():]):
                continue
            found.append(f"step {index + 1} sets {name}")
    return found


class ReadingThePanelTests(unittest.TestCase):
    def test_the_reading_finds_every_view(self) -> None:
        table = what_a_view_reloads()
        for view in ("checks", "prompts", "start", "workflow", "history", "memory"):
            with self.subTest(view=view):
                self.assertIn(view, table)

    def test_the_reading_finds_the_data_each_view_reloads(self) -> None:
        table = what_a_view_reloads()
        # Every one of these was found by hand at some point, three of them only
        # after the hand-written list was caught missing them.
        expected = {
            "checks": {"qaSuite", "qaResult"},
            "prompts": {"promptRecords"},
            "start": {"checkup"},
            "history": {"historyRuns"},
            "workflow": {"teamNotes", "savedWorkflows"},
        }
        for view, names in expected.items():
            with self.subTest(view=view):
                self.assertTrue(
                    names <= table.get(view, set()),
                    f"{view} reloads {sorted(table.get(view, set()))}, "
                    f"which does not cover {sorted(names)}",
                )

    def test_the_listening_is_read_as_well_as_the_views(self) -> None:
        # Opening a view is not the only thing that replaces data. The panel
        # asks for news on its own timer, whatever view is open, and what it
        # does with that news replaces data too. Reading only switchView left
        # this whole second door unwatched.
        wiped = what_the_poller_reloads()
        for name in ("usageRecords", "qaResult", "promptRecords"):
            with self.subTest(name=name):
                self.assertIn(name, wiped)

    def test_what_the_listening_replaces_counts_on_every_view(self) -> None:
        # The timer does not care which view is open, so what it replaces is at
        # risk everywhere, not only on one view.
        case = {
            "steps": [
                {"do": "click", "target": '[data-view="checks"]'},
                {"do": "run", "script": "usageRecords = [{model: 'x'}]; return 'staged'"},
                {"do": "expect_visible", "target": "body"},
            ]
        }
        self.assertTrue(racing_steps(case, what_a_view_reloads()))

    def test_a_view_that_reloads_nothing_is_said_so_plainly(self) -> None:
        # The memory view builds its data as it goes and hands it straight to
        # the drawing, so there is nothing there to race.
        self.assertEqual(what_a_view_reloads().get("memory"), set())


class NoCheckRacesThePanelTests(unittest.TestCase):
    def test_there_are_checks_to_read(self) -> None:
        # Without this, a broken read would pass by finding nothing.
        self.assertGreater(len(cases()), 40)

    def test_the_reading_really_finds_views_in_the_checks(self) -> None:
        seen = {view_of(case) for case in cases()} - {""}
        self.assertGreaterEqual(len(seen), 4, seen)

    def test_staging_and_reading_back_in_one_step_is_not_a_race(self) -> None:
        # Nothing else runs inside a step, so a check that puts something in
        # and reads it back before the step ends has its answer already.
        case = {
            "steps": [
                {"do": "click", "target": '[data-view="prompts"]'},
                {"do": "run",
                 "script": "promptRecords = [{id: 'x'}]; return String(promptRecords.length)"},
                {"do": "expect_visible", "target": "body"},
            ]
        }
        self.assertEqual(racing_steps(case, what_a_view_reloads()), [])

    def test_no_check_stages_data_a_view_can_wipe_under_it(self) -> None:
        table = what_a_view_reloads()
        guilty = {
            case["id"]: racing_steps(case, table)
            for case in cases()
            if racing_steps(case, table)
        }
        self.assertEqual(
            guilty,
            {},
            "These checks put data in and then rely on it in a later step, on a view "
            "that reloads itself: "
            + "; ".join(f"{name} ({', '.join(where)})" for name, where in guilty.items())
            + ". Turn the view's refresh off in the same step first.",
        )


if __name__ == "__main__":
    unittest.main()
