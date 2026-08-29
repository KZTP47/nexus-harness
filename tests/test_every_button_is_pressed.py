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
    "authorityRepairButton": (
        "This deliberately replaces the selected project's local authority descriptor "
        "and registry binding after an exact confirmation. Browser QA runs against the "
        "real checkout and must not re-register it; the repair endpoint, rollback, race, "
        "and UI wiring are exercised against isolated temporary projects in the authority tests."
    ),
    "pipelineImport": (
        "This opens the operating system file picker. Browser QA cannot choose a "
        "portable user file safely; pipeline server, UI contract, and real Electron "
        "smoke tests exercise validation, selection, persistence, and restart restore."
    ),
    "pipelineExport": (
        "This opens Electron's native Save dialog or leaves a browser download. "
        "Browser QA must not write an arbitrary file; desktop and pipeline tests "
        "exercise the native bridge, versioned document, and round trip."
    ),
    "swarmImport": (
        "This opens the operating system file picker. Browser QA cannot select a "
        "personal board file; board API and UI contract tests exercise strict import "
        "validation and persistence without touching user data."
    ),
    "teamSetUp": (
        "It asks the server to do exactly what #setUpSeats asks, and that one is "
        "already pressed and put back again by a check on the first view. Pressing "
        "it here would write the settings of the project being checked a second "
        "time for no more coverage. That it really is the same request is held "
        "down by a test in tests/test_team_server.py."
    ),
    "teamCustomWho": (
        "The same list as teamNodeWho: the assistants found on this machine, "
        "which differ from machine to machine, so there is no value a check "
        "could choose everywhere. A check does open the window and read it, and "
        "makes sure only somebody really here can be picked."
    ),
    "teamNodeWho": (
        "It is filled in with the assistants found on this machine, which differ "
        "from machine to machine, so there is no value a check could choose "
        "everywhere. A check does choose from it: it takes the first one marked "
        "ready and makes sure the list only ever offers somebody really here."
    ),
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
    "talkStop": (
        "This is enabled only while a real assistant or signed-in web provider "
        "is answering. The browser suite must not spend a real account turn merely "
        "to make Stop clickable; cancellation is exercised with bounded fakes in "
        "tests/test_talking_to_them.py and the desktop web-chat tests."
    ),
    "theBigChatStop": (
        "This is enabled only during a live pair-chat request. Starting an external "
        "model turn just so a browser check can cancel it would make CI depend on a "
        "private account; tests/test_the_board_of_agents.py and the desktop tests "
        "exercise the server and provider cancellation paths instead."
    ),
    "webChatDialogClose": (
        "This dialog exists only when the Electron web-chat bridge is available. "
        "The panel browser checks deliberately have no signed-in provider session; "
        "desktop tests cover the bridge and closing lifecycle."
    ),
    "webChatOpenWindow": (
        "This asks Electron to move a signed-in provider page into its own window. "
        "A headless browser runner has neither that bridge nor a provider account, "
        "so desktop tests cover the bounded IPC action."
    ),
    "webChatViewerClose": (
        "This closes an Electron-hosted provider view which cannot exist in the "
        "headless panel runner. Desktop tests cover hiding the embedded provider "
        "view and preserving its isolated session."
    ),
    "swarmAgentPictureBrowse": (
        "This opens the operating system file picker. Browser automation supplies "
        "files through the hidden input instead; the picture validation and saved "
        "appearance are covered by the board tests."
    ),
    "swarmAgentPictureClear": (
        "This is enabled only after a local profile image has been selected. The "
        "image lifecycle is exercised with synthetic bounded image data in the board "
        "tests rather than opening a developer's personal file picker in CI."
    ),
    "agentRunAutomation": (
        "Its choices are the saved automations in the developer's current project, "
        "so neither their names nor a harmless choice exist on every machine. The "
        "pipeline server tests exercise loading an exact saved automation contract."
    ),
    "agentRunCopyContract": (
        "This writes the selected automation contract into the developer's real "
        "system clipboard. Browser QA must not replace personal clipboard contents; "
        "the contract generation and exact automation identity are server-tested."
    ),
    "agentRunNow": (
        "This executes the selected saved automation, whose steps may run commands or "
        "change external state. Browser QA must not run an arbitrary developer-owned "
        "automation; the bounded agent-run endpoint is exercised with test pipelines."
    ),
    "pipelineOpenActiveRun": (
        "This exists only after the server acknowledges an exact durable run. Creating a "
        "real run in browser QA could execute arbitrary commands from the developer's saved "
        "automation; focused UI tests exercise exact-run bootstrap and immutable snapshot "
        "adoption with bounded responses instead."
    ),
    "pipelineStopActive": (
        "This is enabled only for a server-confirmed live durable run. Browser QA must not "
        "start and cancel arbitrary developer-owned work for button coverage; focused UI and "
        "pipeline server tests exercise its exact run-id stop path with bounded fakes."
    ),
    "swarmAgentCheckLogin": (
        "This deliberately probes the selected provider's real account session. A "
        "browser check must not spend an authenticated provider request merely to "
        "exercise the button; provider readiness is covered with bounded fakes."
    ),
    "swarmAgentManualLogin": (
        "This deliberately opens the selected provider's real interactive login or "
        "signed-in web-chat manager. Browser QA must not alter a developer account; "
        "the login process and web-chat bridge are exercised with isolated fakes."
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
            # Some stateful checks choose a generated option and then dispatch
            # its change event inside one atomic step. Count that real choice;
            # requiring a separate `choose` step would make the save race the
            # assertion it belongs to.
            chosen.update(re.findall(
                r"getElementById\('([A-Za-z0-9_]+)'\)\.value\s*=", script
            ))
            chosen.update(re.findall(r'\$\("([A-Za-z0-9_]+)"\)\.value\s*=', script))
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


# The same thing for the buttons the panel builds while it runs, keyed by the
# words on them rather than by a name in the page.
BUILT_ALLOWED_TO_BE_UNPRESSED: dict[str, str] = {
    "Load 20 older parts": (
        "This appears only after a shared page contains more than twenty durable "
        "parts. The bounded latest/older HTTP windows and the button's exact click "
        "handler are exercised against isolated pages in the board and page tests; "
        "making the release browser workflow manufacture twenty-one model turns "
        "would add minutes and external-account dependence without more UI coverage."
    ),
    "Export": (
        "This per-board action opens a native Save dialog or leaves a browser download. "
        "Browser QA must not write arbitrary user files; board API, UI contract, and "
        "desktop bridge tests exercise the exact exported document and safe write path."
    ),
    "Put it back": (
        "Pressing it takes a setting out of this project's own settings file. "
        "On a project whose settings file writes everything out - like this one "
        "- there is no setting where that changes nothing, so a check pressing "
        "it would change the settings of the project it is checking. What it "
        "really does, both when there is something to put back and when there "
        "is not, is proven in tests/test_settings.py, on a throwaway project "
        "where breaking one costs nothing."
    ),
    "Open sign-in": (
        "This deliberately opens a real provider's interactive OAuth terminal. "
        "A browser check must not start or alter a developer's account session. "
        "The fixed command, visible console, and uncaptured output are exercised "
        "with a mocked process in tests/test_agent_mailbox.py."
    ),
    "Open its sign-in": (
        "This is the same explicit provider OAuth action from an agent chat. A "
        "browser check must not start or alter a developer's real account session; "
        "tests/test_agent_mailbox.py verifies the safe process boundary instead."
    ),
    "Repair Claude access": (
        "This deliberately updates and resets the real Claude command-line OAuth "
        "session after an explicit confirmation. A browser check must not alter a "
        "developer's account; tests/test_agent_mailbox.py proves the fixed visible "
        "command and uncaptured process boundary, and tests/test_bug_fixes_two.py "
        "proves the authenticated panel endpoint."
    ),
    "Find Cloud project ID": (
        "This opens the signed-in person's Google Cloud Console in the system "
        "browser. A browser check must not inspect or alter a developer's Google "
        "account; the fixed official URL and click wiring are checked as source."
    ),
    "View full web AI chat": (
        "This is created only for an Electron-connected, signed-in provider chat. "
        "The headless panel suite has no provider session; desktop tests exercise "
        "the exact embedded-view action and its isolated conversation binding."
    ),
    "Open web AI in a window": (
        "This is created only for an Electron-connected provider chat and opens a "
        "native window. Desktop tests cover that IPC path without requiring CI to "
        "sign in to a private web account."
    ),
    "Open window": (
        "This native provider-window action is present only with the Electron web "
        "chat bridge. Its IPC and URL boundaries are covered by desktop tests."
    ),
    "Disconnect": (
        "This would remove a real signed-in provider connection after confirmation. "
        "CI must not alter a developer account session; desktop tests exercise the "
        "connection removal lifecycle with isolated fakes."
    ),
    "Open sign-in or choose a chat": (
        "This deliberately starts an interactive provider sign-in or selects a real "
        "account conversation. CI has no account and must not manufacture one; the "
        "desktop transport and provider boundary are tested independently."
    ),
}


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
            if label not in said
            and _first_words(label) not in said
            and label not in BUILT_ALLOWED_TO_BE_UNPRESSED
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

    def test_gemini_help_opens_the_official_cloud_welcome_page(self) -> None:
        script = (PANEL / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'GOOGLE_CLOUD_PROJECT_WELCOME = "https://console.cloud.google.com/welcome"',
            script,
        )
        self.assertIn('window.open(GOOGLE_CLOUD_PROJECT_WELCOME, "_blank"', script)
        self.assertIn("copy Project ID — not Project name or Project number", script)

    def test_claude_repair_requires_confirmation_before_the_endpoint(self) -> None:
        script = (PANEL / "app.js").read_text(encoding="utf-8")
        function = script.split("async function repairClaudeAccess", 1)[1].split(
            "async function signInThisAssistant", 1
        )[0]
        self.assertLess(function.index("window.confirm"), function.index("/api/team/repair-claude"))
        self.assertIn("Nexus will not see your account or credentials", function)

    def test_starter_checks_answer_the_real_unsaved_drawing_guard(self) -> None:
        flows = json.loads(FLOWS.read_text(encoding="utf-8"))
        selecting_starters: list[tuple[str, str]] = []
        for case in flows.get("cases", []):
            for step in case.get("steps") or []:
                script = str(step.get("script") or "")
                if "starter.click()" in script:
                    selecting_starters.append((str(case.get("id") or ""), script))

        self.assertGreaterEqual(len(selecting_starters), 5, selecting_starters)
        for case_id, script in selecting_starters:
            with self.subTest(case=case_id):
                self.assertIn("pipelineUnsavedDialog", script)
                self.assertIn("pipelineUnsavedDiscard", script)
                self.assertIn("pipelineName", script)
                self.assertNotIn("window.confirm", script)

    def test_shared_page_check_is_an_in_memory_transaction(self) -> None:
        flows = json.loads(FLOWS.read_text(encoding="utf-8"))
        case = next(
            one for one in flows["cases"]
            if one["id"] == "the-page-the-agents-share-is-on-the-board"
        )
        scripts = "\n".join(str(step.get("script") or "") for step in case["steps"])
        self.assertIn("window.__pageRequestWas = window.request", scripts)
        self.assertIn("window.__pageFake = structuredClone(thePage)", scripts)
        self.assertIn("the page update did not name the exact page version", scripts)
        self.assertIn("window.request = window.__pageRequestWas", scripts)
        self.assertIn("window.__boardSnapshotCaptured = true", scripts)
        self.assertIn("if (!window.__boardSnapshotCaptured || !window.__boardWas)", scripts)
        self.assertTrue(case["steps"][-1].get("always"))


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
