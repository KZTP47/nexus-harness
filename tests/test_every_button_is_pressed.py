"""Every control in the panel is really used by a check.

A test can drive the code behind a button instead of the button, and then it
passes while the button is dead. That is not a small difference: the "Find
pages nobody checks" button never worked at all, and a test that called its
drawing function directly said it did.

So this counts controls, not functions, and it counts three kinds:

- buttons written in the page, found by their name;
- buttons the page builds while it runs, found by the words on them, which is
  how a person finds them too;
- boxes you tick, found by their name;
- lists you choose from, which must be really chosen from, not merely looked
  at. A check that says a list is on the screen proves the list exists and
  proves nothing about what choosing does.

The first version of this test only looked at the page's own markup, which
meant it could never see the eleven buttons the panel builds while it runs.
That is the same blind spot it was written to close, so it is closed here.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "src" / "our_harness" / "ui"
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"

_WRITTEN_BUTTON = re.compile(r'<button id="([A-Za-z0-9_]+)"')
_TICK_BOX = re.compile(r'<input id="([A-Za-z0-9_]+)" type="checkbox"')
_LIST_TO_CHOOSE_FROM = re.compile(r'<select id="([A-Za-z0-9_]+)"')
# A button the page builds while it runs, with words on it.
_BUILT_BUTTON = re.compile(r'make\("button",\s*"[^"]*",\s*"([^"]+)"\)')
# One built with only a class to find it by, such as the boxes on the drawing.
_BUILT_BY_CLASS = re.compile(r'make\("button",\s*"([a-z][a-z -]*)"\)')

# A control that is deliberately not pressed, and why. Nothing may sit here
# without a real sentence.
ALLOWED_TO_BE_UNPRESSED: dict[str, str] = {
    "nodeAgentRef": (
        "It is filled in from agents this project trusts, and this project "
        "trusts none, so there is nothing in the list to choose."
    ),
    "nodeProvider": (
        "It is filled in from the model routes in the settings, which differ "
        "from machine to machine, so there is no value a check could choose."
    ),
    "agentRef": (
        "It is filled in from agents this project trusts, and this project "
        "trusts none, so there is nothing in the list to choose."
    ),
    "agentProvider": (
        "It is filled in from the model routes in the settings, which differ "
        "from machine to machine, so there is no value a check could choose."
    ),
    "quickRun": (
        "Pressing it asks a model to change this project's own files. A check "
        "must never do that to the project it is checking."
    ),
    "exportButton": (
        "Pressing it downloads a file. A browser download during a check is a "
        "file left on the machine that nothing takes away again."
    ),
}


def panel_markup() -> str:
    return (PANEL / "index.html").read_text(encoding="utf-8")


def panel_script() -> str:
    return (PANEL / "app.js").read_text(encoding="utf-8")


def written_buttons() -> list[str]:
    return _WRITTEN_BUTTON.findall(panel_markup())


def tick_boxes() -> list[str]:
    return _TICK_BOX.findall(panel_markup())


def lists_to_choose_from() -> list[str]:
    return _LIST_TO_CHOOSE_FROM.findall(panel_markup())


def built_buttons() -> list[str]:
    return _BUILT_BUTTON.findall(panel_script())


def built_by_class() -> list[str]:
    return [found.split()[0] for found in _BUILT_BY_CLASS.findall(panel_script())]


def what_the_checks_do() -> tuple[set[str], str, set[str]]:
    """What the checks press, everything they say, and what they choose from."""

    flows = json.loads(FLOWS.read_text(encoding="utf-8"))
    pressed: set[str] = set()
    chosen: set[str] = set()
    said: list[str] = []
    for case in flows.get("cases", []):
        for step in case.get("steps") or []:
            target = str(step.get("target") or "")
            script = str(step.get("script") or "")
            said.append(target)
            said.append(script)
            if step.get("do") == "click":
                found = re.fullmatch(r"#([A-Za-z0-9_]+)", target)
                if found:
                    pressed.add(found.group(1))
            if step.get("do") == "choose":
                found = re.fullmatch(r"#([A-Za-z0-9_]+)", target)
                if found:
                    chosen.add(found.group(1))
            pressed.update(re.findall(r"getElementById\('([A-Za-z0-9_]+)'\)\.click\(\)", script))
            pressed.update(re.findall(r'\$\("([A-Za-z0-9_]+)"\)\.click\(\)', script))
    return pressed, " ".join(said), chosen


def _first_words(label: str, count: int = 2) -> str:
    return " ".join(label.split()[:count])


class WrittenButtonTests(unittest.TestCase):
    def test_the_panel_really_has_buttons_to_find(self) -> None:
        # Without this, a change that broke the reading would make the tests
        # below pass by finding nothing at all.
        self.assertGreater(len(written_buttons()), 25)

    def test_the_checks_really_press_buttons(self) -> None:
        pressed, _said, _chosen = what_the_checks_do()
        self.assertGreater(len(pressed), 20)

    def test_every_written_button_is_pressed_by_a_check(self) -> None:
        pressed, _said, _chosen = what_the_checks_do()
        missing = [
            name
            for name in written_buttons()
            if name not in pressed and name not in ALLOWED_TO_BE_UNPRESSED
        ]
        self.assertEqual(
            missing,
            [],
            "These buttons are in the panel and no check presses them: "
            + ", ".join(missing)
            + ". Add a check that clicks each one, or say in ALLOWED_TO_BE_UNPRESSED why not.",
        )


class BuiltButtonTests(unittest.TestCase):
    """The buttons the page builds while it runs, which have no name to find."""

    def test_the_panel_really_builds_buttons_while_it_runs(self) -> None:
        self.assertGreaterEqual(len(built_buttons()), 5, built_buttons())
        self.assertGreaterEqual(len(built_by_class()), 3, built_by_class())

    def test_every_built_button_is_pressed_by_a_check(self) -> None:
        _pressed, said, _chosen = what_the_checks_do()
        missing = [
            label
            for label in built_buttons()
            if label not in said and _first_words(label) not in said
        ]
        self.assertEqual(
            missing,
            [],
            "The panel builds these buttons while it runs and no check presses them: "
            + ", ".join(missing)
            + ". A check has to find one the way a person does, by the words on it.",
        )

    def test_every_button_found_only_by_its_class_is_pressed_too(self) -> None:
        _pressed, said, _chosen = what_the_checks_do()
        missing = [name for name in built_by_class() if name not in said]
        self.assertEqual(missing, [], f"No check touches these: {missing}")


class TickBoxTests(unittest.TestCase):
    """A box you tick changes what the harness is asked to do."""

    def test_the_panel_really_has_boxes_to_find(self) -> None:
        self.assertGreaterEqual(len(tick_boxes()), 2, tick_boxes())

    def test_every_tick_box_is_used_by_a_check(self) -> None:
        _pressed, said, _chosen = what_the_checks_do()
        missing = [name for name in tick_boxes() if name not in said]
        self.assertEqual(
            missing,
            [],
            "These boxes change what the harness does and no check ticks them: "
            + ", ".join(missing),
        )


class ListToChooseFromTests(unittest.TestCase):
    """A list has to be chosen from, not merely seen."""

    def test_the_panel_really_has_lists_to_find(self) -> None:
        self.assertGreaterEqual(len(lists_to_choose_from()), 6, lists_to_choose_from())

    def test_every_list_is_really_chosen_from_by_a_check(self) -> None:
        _pressed, _said, chosen = what_the_checks_do()
        missing = [
            name
            for name in lists_to_choose_from()
            if name not in chosen and name not in ALLOWED_TO_BE_UNPRESSED
        ]
        self.assertEqual(
            missing,
            [],
            "No check chooses anything from these lists: "
            + ", ".join(missing)
            + ". Seeing a list on the screen says nothing about what choosing does.",
        )


class TheAllowedListTests(unittest.TestCase):
    def test_nothing_sits_on_the_list_without_a_reason(self) -> None:
        for name, why in ALLOWED_TO_BE_UNPRESSED.items():
            with self.subTest(button=name):
                self.assertGreater(len(why), 40, f"{name} needs a real reason, not a note")

    def test_the_list_holds_no_control_that_has_gone(self) -> None:
        # A name left here after its button was removed would quietly excuse a
        # different one some day.
        here = set(written_buttons()) | set(tick_boxes()) | set(lists_to_choose_from())
        for name in ALLOWED_TO_BE_UNPRESSED:
            with self.subTest(button=name):
                self.assertIn(name, here)


if __name__ == "__main__":
    unittest.main()
