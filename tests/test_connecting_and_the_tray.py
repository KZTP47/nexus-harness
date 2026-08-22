"""Connecting an assistant in one press, and what that broke on the way.

Somebody had Claude installed and signed in, an agent on the board set to use
it, and the board still said not ready. There was nothing on screen to press:
the only way to point the settings at an assistant by name was a settings file
or a terminal, so they had to ask somebody else for help with their own machine.

The first attempt at the button did three things wrong, and every one of them
is a test here. It moved their default assistant onto whatever they had just
connected. It left the panel reading settings from before the press, so the
board went on saying not ready. And it wrote a route that would not load, which
stopped the whole settings file loading and took every other assistant down with
it - connecting Codex turned Claude off.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import seats, swarm
from our_harness.config import DEFAULT_CONFIG, LoadedConfig, load_isolated_config, validate_config
from our_harness.models import HarnessError


class ConnectingOneAssistantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = load_isolated_config(self.root)

    def test_every_assistant_it_offers_has_a_model_to_write_down(self) -> None:
        """Left out, the route written for it holds an empty model, an empty
        model is refused, and the settings file stops loading at all - so
        connecting one assistant took every other route down with it."""

        for kind in seats.KNOWN_SEATS:
            with self.subTest(kind=kind):
                self.assertTrue(
                    seats._default_model(self.config, kind),
                    f"{kind} has no model to write down")

    def test_a_route_it_would_write_is_a_route_that_loads(self) -> None:
        """The whole check, in one line: nothing this offers to write may stop
        the settings loading."""

        for kind in seats.KNOWN_SEATS:
            with self.subTest(kind=kind):
                try:
                    routes = seats.routes_for(self.config, [kind])
                except seats.SeatError:
                    continue      # not on this machine, so nothing is offered
                data = copy.deepcopy(DEFAULT_CONFIG)
                data["providers"] = routes
                validate_config(data)

    def test_writing_one_does_not_move_the_default_onto_it(self) -> None:
        """Connecting Gemini so one agent can use it should not quietly move
        everything else onto Gemini."""

        # A machine that already has somebody's choice written down, which is
        # the case this is about.
        local = self.root / ".harness" / "config.local.json"
        local.write_text(json.dumps({
            "provider": {"name": "claude-cli", "model": "claude-sonnet-4-5",
                         "endpoint": "", "api_key_env": ""},
            "providers": {"claude": {"kind": "claude-cli", "model": "claude-sonnet-4-5",
                                     "endpoint": ""}},
        }), encoding="utf-8")
        held = load_isolated_config(self.root)
        seats.write_one_route(held, "gemini", {
            "kind": "gemini-cli", "model": "gemini-2.5-pro", "endpoint": ""})
        written = json.loads(local.read_text(encoding="utf-8"))
        self.assertEqual(written["providers"]["gemini"]["kind"], "gemini-cli")
        self.assertEqual(written["providers"]["claude"]["kind"], "claude-cli")
        self.assertEqual(
            written["provider"]["name"], "claude-cli",
            "connecting one assistant moved the default onto it")

    def test_a_route_that_would_not_load_is_refused_before_it_is_written(self) -> None:
        """Written and then found wanting, the settings file is already broken
        and every other assistant with it."""

        with self.assertRaises(seats.SeatError) as caught:
            seats.write_one_route(self.config, "broken", {
                "kind": "gemini-cli", "model": "", "endpoint": "https://not-allowed"})
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertFalse((self.root / ".harness" / "config.local.json").is_file())

    def test_a_name_that_is_not_a_name_is_refused(self) -> None:
        for bad in ("", "has a space", "../escape", "1starts-with-a-number"):
            with self.subTest(name=bad), self.assertRaises(seats.SeatError):
                seats.write_one_route(self.config, bad, {
                    "kind": "gemini-cli", "model": "m", "endpoint": ""})

    def test_codex_is_told_where_it_is(self) -> None:
        """Codex is installed by its own desktop app, into a folder of its own,
        and nothing ever puts it on the path. A route without the full path is a
        route that cannot find it."""

        with mock.patch.object(seats, "available", lambda kind: r"C:\somewhere\codex.exe"), \
             mock.patch.object(seats, "_a_model_codex_really_has", lambda where: "gpt-5.5"):
            route = seats.routes_for(self.config, ["codex-cli"])["codex"]
        self.assertEqual(route["command"], [r"C:\somewhere\codex.exe"])
        self.assertEqual(route["model"], "gpt-5.5")

    def test_codex_that_is_nowhere_is_said_plainly(self) -> None:
        with mock.patch.object(seats, "available", lambda kind: ""), \
             self.assertRaises(seats.SeatError) as caught:
            seats.routes_for(self.config, ["codex-cli"])
        self.assertIn("not on this machine", str(caught.exception))

    def test_which_model_codex_has_is_asked_rather_than_guessed(self) -> None:
        """A model name written down in here is right until the day it is not,
        and then it is a route that fails with a message about a catalog."""

        class Answered:
            returncode = 0
            stdout = json.dumps({"models": [{"slug": "gpt-5.5"}, {"slug": "older"}]})

        with mock.patch.object(seats.subprocess, "run", lambda *a, **k: Answered()):
            self.assertEqual(seats._a_model_codex_really_has("codex.exe"), "gpt-5.5")

    def test_codex_saying_nothing_useful_is_not_a_crash(self) -> None:
        class Refused:
            returncode = 1
            stdout = ""

        with mock.patch.object(seats.subprocess, "run", lambda *a, **k: Refused()):
            self.assertEqual(seats._a_model_codex_really_has("codex.exe"), "")


class WhatTheBoardOffersToConnectTests(unittest.TestCase):
    """A button that offers to connect something that is not installed is a
    button that fails, and a button that fails is worse than no button."""

    def test_it_offers_only_what_is_really_here(self) -> None:
        with mock.patch(
                "our_harness.providers.subscription_cli.available",
                lambda kind: r"C:\somewhere\gemini.cmd" if kind == "gemini-cli" else ""):
            self.assertEqual(swarm._which_one_to_connect("gemini", {"ready": False}), "gemini-cli")
            self.assertEqual(swarm._which_one_to_connect("codex", {"ready": False}), "")

    def test_nothing_is_offered_for_one_that_already_works(self) -> None:
        self.assertEqual(swarm._which_one_to_connect("claude", {"ready": True}), "")

    def test_nothing_is_offered_for_an_agent_with_no_assistant_chosen(self) -> None:
        """There is nothing to connect. What that one needs is somebody to pick
        an assistant for it, which is a different sentence and a different fix."""

        self.assertEqual(swarm._which_one_to_connect("", None), "")

    def test_a_route_name_nobody_knows_offers_nothing(self) -> None:
        self.assertEqual(swarm._which_one_to_connect("something-made-up", {"ready": False}), "")


class BoardWorkIsApartFromYourChatTests(unittest.TestCase):
    def test_the_two_names_are_different(self) -> None:
        """A run used to be filed under the plain agent name, which is the very
        file the person's own conversation with that agent lives in."""

        self.assertNotEqual(
            swarm.filed_as_on_the_board("The reviewer"), "The reviewer")

    def test_it_still_says_which_agent_it_belongs_to(self) -> None:
        """Apart is not the same as unrecognisable. Somebody looking at the list
        of chats has to be able to tell whose this is."""

        self.assertTrue(swarm.filed_as_on_the_board("The reviewer").startswith("The reviewer"))

    def test_two_agents_do_not_share_a_board_conversation(self) -> None:
        self.assertNotEqual(
            swarm.filed_as_on_the_board("The reviewer"),
            swarm.filed_as_on_the_board("The writer"))


class ASettingsFileThatIsAlreadyBrokenTests(unittest.TestCase):
    """The check that stops a bad route being written was checking a different
    thing from the one being written. It validated the settings held in memory;
    the write re-reads the file and merges onto whatever is really there. And
    when that file is already broken, the settings in memory are quietly the
    defaults - so the check saw a clean slate, passed, and the file stayed
    exactly as unloadable as before while the tool said it had worked."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.local = self.root / ".harness" / "config.local.json"

    def a_broken_file(self) -> None:
        self.local.write_text(json.dumps({
            "providers": {
                "good": {"kind": "claude-cli", "model": "claude-sonnet-4-5", "endpoint": ""},
                "broken": {"kind": "openai", "model": "", "endpoint": "https://x"},
            },
        }), encoding="utf-8")

    def test_it_says_the_file_was_already_broken_rather_than_reporting_success(self) -> None:
        self.a_broken_file()
        held = load_isolated_config(self.root)
        with self.assertRaises(seats.SeatError) as caught:
            seats.write_one_route(held, "gemini", {
                "kind": "gemini-cli", "model": "gemini-2.5-pro", "endpoint": ""})
        self.assertIn("does not load as it is", str(caught.exception))
        self.assertIn("nothing was changed", str(caught.exception).lower())

    def test_the_broken_file_is_left_exactly_as_it_was(self) -> None:
        self.a_broken_file()
        was = self.local.read_text(encoding="utf-8")
        held = load_isolated_config(self.root)
        with self.assertRaises(seats.SeatError):
            seats.write_one_route(held, "gemini", {
                "kind": "gemini-cli", "model": "gemini-2.5-pro", "endpoint": ""})
        self.assertEqual(self.local.read_text(encoding="utf-8"), was)

    def test_it_checks_the_file_on_disk_and_not_only_what_is_in_memory(self) -> None:
        """A route that looks fine beside the settings in memory can still stop
        the file loading once it is merged onto what is really there."""

        self.local.write_text(json.dumps({
            "providers": {"gemini": {"kind": "gemini-cli", "model": "one", "endpoint": ""}},
        }), encoding="utf-8")
        held = load_isolated_config(self.root)
        # A route that is fine on its own and refused for this kind: an address
        # for something that signs in.
        with self.assertRaises(seats.SeatError):
            seats.write_one_route(held, "second", {
                "kind": "gemini-cli", "model": "two", "endpoint": "https://not-allowed"})
        written = json.loads(self.local.read_text(encoding="utf-8"))
        self.assertNotIn("second", written["providers"])

    def test_a_settings_file_that_is_not_json_is_said_plainly(self) -> None:
        self.local.write_text("this is not settings", encoding="utf-8")
        held = load_isolated_config(self.root)
        with self.assertRaises(seats.SeatError) as caught:
            seats.write_one_route(held, "gemini", {
                "kind": "gemini-cli", "model": "gemini-2.5-pro", "endpoint": ""})
        self.assertIn("nothing was changed", str(caught.exception).lower())

    def test_a_good_file_still_takes_a_new_route(self) -> None:
        """The check has to let the ordinary case through, or it is just a wall."""

        self.local.write_text(json.dumps({
            "providers": {"claude": {"kind": "claude-cli", "model": "claude-sonnet-4-5",
                                     "endpoint": ""}},
        }), encoding="utf-8")
        held = load_isolated_config(self.root)
        seats.write_one_route(held, "gemini", {
            "kind": "gemini-cli", "model": "gemini-2.5-pro", "endpoint": ""})
        written = json.loads(self.local.read_text(encoding="utf-8"))
        self.assertEqual(written["providers"]["gemini"]["kind"], "gemini-cli")
        self.assertEqual(written["providers"]["claude"]["kind"], "claude-cli")


class _APretendPanel:
    """A panel that says it is ready and then says nothing more."""

    def __init__(self, *args, **rest) -> None:
        import io

        ready = json.dumps({"url": "http://127.0.0.1:1/", "port": 1})
        self.stdout = io.StringIO("harness-ui-ready " + ready + "\n")
        self.returncode = None

    def poll(self):
        return None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


class TheAppDoesNotWedgeTests(unittest.TestCase):
    """The panel writes a line for every request it answers. Nothing read that
    after the window opened, so the pipe filled, the print inside the panel
    blocked, and with it the thread answering that request - and then the whole
    app. Two hundred or so clicks in, which is a few minutes of use."""

    def test_starting_the_panel_leaves_something_reading_it(self) -> None:
        """Reached through the thing that is supposed to call it. Tested by
        calling the helper directly, taking the call out of start_the_panel was
        not noticed at all - and that call is the whole fix."""

        import sys as sys_lab
        from unittest import mock as mock_lab

        sys_lab.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import open_the_app

        kept = []
        with mock_lab.patch.object(
                open_the_app, "_keep_reading_what_it_prints",
                lambda started: kept.append(started)),              mock_lab.patch.object(open_the_app.subprocess, "Popen", _APretendPanel):
            open_the_app.start_the_panel(Path("."), 0)
        self.assertEqual(len(kept), 1, "nothing was left reading what the panel prints")

    def test_what_the_panel_prints_goes_on_being_read(self) -> None:
        import subprocess
        import sys as sys_lab
        import time as time_lab

        sys_lab.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import open_the_app

        # A stand-in that prints far more than a pipe will hold, the way the
        # panel does when somebody clicks around for a few minutes.
        started = subprocess.Popen(
            [sys_lab.executable, "-c",
             "import sys\n"
             "for i in range(20000): print('a line of log output ' * 4)\n"
             "print('STILL ALIVE', flush=True)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
        try:
            open_the_app._keep_reading_what_it_prints(started)
            gives_up_at = time_lab.monotonic() + 60
            while started.poll() is None and time_lab.monotonic() < gives_up_at:
                time_lab.sleep(0.1)
            self.assertIsNotNone(
                started.poll(),
                "it filled the pipe and stopped, which is the app wedging")
        finally:
            if started.poll() is None:
                started.kill()
            started.wait(timeout=20)


if __name__ == "__main__":
    unittest.main()
