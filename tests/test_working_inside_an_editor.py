"""The harness answering an editor, instead of only its own panel.

The panel is a good place to watch a run and a bad place to be in the middle of
writing code. This is the other half of the conversation `mcp.py` already
speaks: there, the harness calls somebody else's tools; here, somebody else's
editor calls ours.

Everything here is about the ways that goes wrong - a message that wants no
answer being answered, a tool that runs commands being offered to an editor
nobody said could run commands, a stray line on the pipe, and an editor left
waiting because something fell over in a way nobody expected.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import editor, mcp, pipelines
from our_harness.mcp import MCPClient
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class EditorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        (self.root / "README.md").write_text("# A project\n", encoding="utf-8")
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def talking(self, *, may_run_things: bool = False) -> editor.Conversation:
        return editor.Conversation(self.config, may_run_things=may_run_things)

    def ask(self, method: str, given: dict | None = None, number: int = 1):
        return self.talking().answer({
            "jsonrpc": "2.0", "id": number, "method": method, "params": given or {},
        })

    def call_it(self, name: str, arguments: dict | None = None, *, may_run_things=False):
        said = self.talking(may_run_things=may_run_things).answer({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        return said["result"]

    def the_words(self, result: dict) -> str:
        return result["content"][0]["text"]


class SayingHello(EditorTestCase):
    def test_it_agrees_to_a_version_the_editor_knows(self) -> None:
        for wanted in editor.ONES_WE_KNOW:
            with self.subTest(wanted=wanted):
                said = self.ask("initialize", {"protocolVersion": wanted})
                self.assertEqual(said["result"]["protocolVersion"], wanted)

    def test_a_version_nobody_here_knows_gets_ours_rather_than_a_refusal(self) -> None:
        """Refused outright, somebody is left looking at a tool that will not
        start with nothing at all to go on."""

        said = self.ask("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(said["result"]["protocolVersion"], editor.WE_SPEAK)

    def test_it_says_what_it_is_and_that_it_has_tools(self) -> None:
        said = self.ask("initialize", {"protocolVersion": editor.WE_SPEAK})["result"]
        self.assertEqual(said["serverInfo"]["name"], editor.WHAT_WE_ARE_CALLED)
        self.assertIn("tools", said["capabilities"])
        self.assertIn("already knows", said["instructions"])

    def test_a_message_that_wants_no_answer_gets_none(self) -> None:
        """Answering one is how an editor ends up waiting for a reply to
        something it never asked for."""

        self.assertIsNone(self.talking().answer({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }))

    def test_it_answers_a_ping(self) -> None:
        self.assertEqual(self.ask("ping")["result"], {})

    def test_something_it_does_not_do_is_said_plainly(self) -> None:
        said = self.ask("nonsense/method")
        self.assertIn("does not do", said["error"]["message"])
        self.assertEqual(said["id"], 1)

    def test_something_that_is_not_a_message_at_all(self) -> None:
        for bad in ("not a message", 12, [], {"jsonrpc": "1.0", "id": 1}):
            with self.subTest(bad=bad):
                said = self.talking().answer(bad)
                self.assertIn("error", said)


class WhatIsOffered(EditorTestCase):
    def test_reading_is_always_offered(self) -> None:
        offered = [one["name"] for one in self.ask("tools/list")["result"]["tools"]]
        self.assertEqual(offered, [one["name"] for one in editor.READING_ONLY])

    def test_running_things_is_not_offered_unless_somebody_said_so(self) -> None:
        """An editor is a place where a tool gets called without anybody
        deciding to call it."""

        offered = [one["name"] for one in editor.what_we_offer()]
        for one in editor.RUNS_THINGS:
            self.assertNotIn(one["name"], offered)

    def test_and_is_offered_when_they_did(self) -> None:
        offered = [one["name"] for one in editor.what_we_offer(may_run_things=True)]
        for one in editor.RUNS_THINGS:
            self.assertIn(one["name"], offered)

    def test_asking_for_one_that_is_turned_off_says_why(self) -> None:
        said = self.call_it("run_an_automation", {"name": "Nightly"})
        self.assertTrue(said["isError"])
        words = self.the_words(said)
        self.assertIn("turned off", words)
        self.assertIn("look_it_up", words, "and what there is instead")

    def test_every_tool_says_what_it_is_and_what_it_takes(self) -> None:
        for one in editor.what_we_offer(may_run_things=True):
            with self.subTest(tool=one["name"]):
                self.assertTrue(one["description"].endswith("."))
                self.assertEqual(one["inputSchema"]["type"], "object")
                self.assertTrue(callable(one["run"]))

    def test_a_tool_that_runs_things_says_that_it_runs_things(self) -> None:
        for one in editor.RUNS_THINGS:
            with self.subTest(tool=one["name"]):
                said = one["description"].lower()
                self.assertIn("command", said)
                self.assertIn("this machine", said)


class AnsweringTheQuestions(EditorTestCase):
    def test_it_says_which_automations_are_here(self) -> None:
        pipelines.save(self.config, {
            "name": "Nightly check",
            "nodes": [{"id": "start", "kind": "start", "label": "Start"}],
            "edges": [],
        })
        self.assertIn("Nightly check", self.the_words(self.call_it("list_the_automations")))

    def test_with_none_saved_it_says_where_to_make_one(self) -> None:
        said = self.the_words(self.call_it("list_the_automations"))
        self.assertIn("harness ui", said)

    def test_looking_something_up_says_whether_it_is_sure(self) -> None:
        (self.root / "thing.py").write_text("def a_thing():\n    return 1\n", encoding="utf-8")
        said = self.the_words(self.call_it("look_it_up", {
            "asking": "what-uses-it", "name": "a_thing",
        }))
        self.assertTrue(said.startswith(("Exactly:", "A guess:")), said[:80])

    def test_a_question_with_only_a_name_is_not_told_off_for_a_path(self) -> None:
        """Not given at all is not the same as given wrongly. Treated the same,
        asking by name - which is the whole point of asking by name - came back
        complaining about a path the editor never sent."""

        said = self.call_it("look_it_up", {"asking": "where-is-it", "name": "a_thing"})
        self.assertFalse(said["isError"], self.the_words(said))

    def test_a_question_nobody_offers_is_said_plainly(self) -> None:
        said = self.call_it("look_it_up", {"asking": "sideways", "name": "x"})
        self.assertTrue(said["isError"])
        self.assertIn("where is it", self.the_words(said))

    def test_asking_the_project_what_it_knows_with_nothing_to_ask(self) -> None:
        said = self.call_it("what_the_project_knows", {"about": "   "})
        self.assertTrue(said["isError"])
        self.assertIn("what to look for", self.the_words(said))

    def test_a_tool_nobody_has(self) -> None:
        said = self.call_it("make_me_a_sandwich")
        self.assertTrue(said["isError"])
        self.assertIn("no tool called", self.the_words(said))


class WhatItRefuses(EditorTestCase):
    def test_text_where_a_number_belongs(self) -> None:
        said = self.call_it("look_it_up", {"asking": "where-is-it", "line": "seven"})
        self.assertTrue(said["isError"])
        self.assertIn("whole number", self.the_words(said))

    def test_a_number_where_text_belongs(self) -> None:
        said = self.call_it("look_it_up", {"asking": "where-is-it", "name": 7})
        self.assertTrue(said["isError"])
        self.assertIn("has to be text", self.the_words(said))

    def test_something_far_too_long(self) -> None:
        said = self.call_it("look_it_up", {"asking": "where-is-it", "name": "x" * 900})
        self.assertTrue(said["isError"])
        self.assertIn("longer than", self.the_words(said))

    def test_a_very_long_answer_is_cut_short_and_says_so(self) -> None:
        """An editor puts this in front of a model, and a whole file of it is a
        whole file of somebody's budget."""

        long_one = "x" * (editor.MOST_TO_SEND * 2)
        with mock.patch.object(
            editor, "_list_the_automations", lambda config, given: long_one
        ):
            talking = self.talking()
            talking.offered["list_the_automations"]["run"] = lambda config, given: long_one
            said = talking.answer({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "list_the_automations", "arguments": {}},
            })["result"]
        words = self.the_words(said)
        self.assertLess(len(words), editor.MOST_TO_SEND + 500)
        self.assertIn("Cut short", words)

    def test_something_nobody_expected_is_still_an_answer(self) -> None:
        """An editor left waiting is worse than an editor told no."""

        talking = self.talking()
        def falls_over(config, given):
            raise ZeroDivisionError("a password is in here somewhere")

        talking.offered["list_the_automations"]["run"] = falls_over
        said = talking.answer({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_the_automations", "arguments": {}},
        })
        # It comes back as a failure, and what fell over is this project's own
        # business rather than another program's.
        self.assertEqual(said["id"], 3)
        words = json.dumps(said)
        self.assertIn("ZeroDivisionError", words)
        self.assertNotIn("password", words)


class OverThePipe(EditorTestCase):
    def talk_to_it(self, messages: list, *, may_run_things: bool = False) -> list[dict]:
        said = "\n".join(json.dumps(one) for one in messages) + "\n"
        writing = io.StringIO()
        editor.talk(
            self.config,
            may_run_things=may_run_things,
            reading=io.StringIO(said),
            writing=writing,
        )
        return [json.loads(line) for line in writing.getvalue().splitlines()]

    def test_one_message_on_each_line_and_one_answer_on_each_line(self) -> None:
        answers = self.talk_to_it([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        self.assertEqual([one["id"] for one in answers], [1, 2],
                         "the one that wanted no answer got none")

    def test_a_blank_line_is_not_an_error(self) -> None:
        writing = io.StringIO()
        editor.talk(
            self.config,
            reading=io.StringIO('\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
            writing=writing,
        )
        answers = [json.loads(line) for line in writing.getvalue().splitlines()]
        self.assertEqual(len(answers), 1)

    def test_a_line_that_is_not_json_is_said_plainly(self) -> None:
        writing = io.StringIO()
        editor.talk(
            self.config,
            reading=io.StringIO("this is not a message at all\n"),
            writing=writing,
        )
        answers = [json.loads(line) for line in writing.getvalue().splitlines()]
        self.assertIn("not JSON", answers[0]["error"]["message"])

    def test_json_that_is_not_a_message_is_said_differently(self) -> None:
        answers = self.talk_to_it(["a string is JSON, and is not a message"])
        self.assertIn("not a message", answers[0]["error"]["message"])

    def test_a_line_far_too_long_is_refused_without_reading_it(self) -> None:
        writing = io.StringIO()
        editor.talk(
            self.config,
            reading=io.StringIO("x" * (editor.LONGEST_LINE + 10) + "\n"),
            writing=writing,
        )
        self.assertIn("far too long", writing.getvalue())

    def test_several_messages_in_one_go(self) -> None:
        answers = self.talk_to_it([[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]])
        self.assertEqual([one["id"] for one in answers], [1, 2])

    def test_it_stops_when_the_editor_goes_away(self) -> None:
        writing = io.StringIO()
        self.assertEqual(
            editor.talk(self.config, reading=io.StringIO(""), writing=writing), 0
        )
        self.assertEqual(writing.getvalue(), "")


class WhatToPasteIntoYourEditor(EditorTestCase):
    def test_it_writes_it_out_and_never_writes_it_in(self) -> None:
        how = editor.how_to_tell_your_editor(self.config)
        settings = json.loads(how["settings"])
        one = settings["mcpServers"][editor.WHAT_WE_ARE_CALLED]
        self.assertIn("editor", one["args"])
        self.assertIn("serve", one["args"])
        self.assertIn(str(self.root), one["args"])
        self.assertNotIn("--let-it-run-things", one["args"])

    def test_the_line_that_lets_it_run_things_says_so(self) -> None:
        how = editor.how_to_tell_your_editor(self.config, may_run_things=True)
        one = json.loads(how["settings"])["mcpServers"][editor.WHAT_WE_ARE_CALLED]
        self.assertIn("--let-it-run-things", one["args"])
        self.assertTrue(how["may_run_things"])

    def test_it_says_where_the_thing_goes(self) -> None:
        how = editor.how_to_tell_your_editor(self.config)
        where = " ".join(how["where_it_goes"])
        for editor_name in ("VS Code", "Cursor", "Claude Desktop"):
            self.assertIn(editor_name, where)

    def test_a_project_folder_with_a_space_in_it_needs_no_quoting(self) -> None:
        """This project's own folder has a space in its name.

        The command and its parts are kept apart rather than joined into one
        line, so nothing needs quoting - and nothing here has to guess which
        terminal you use, because the two want different quoting.
        """

        with_a_space = self.root / "Some Folder"
        with_a_space.mkdir()
        (with_a_space / ".harness").mkdir()
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), with_a_space, [], {})
        how = editor.how_to_tell_your_editor(config)
        self.assertIn(str(with_a_space.resolve()), how["arguments"])
        self.assertTrue(how["command"])
        for one in how["arguments"]:
            self.assertNotIn('"', one)
            self.assertNotIn("'", one)
        args = json.loads(how["settings"])["mcpServers"][editor.WHAT_WE_ARE_CALLED]["args"]
        self.assertEqual(args, how["arguments"])




class SayingWhatEachToolReallyDoes(EditorTestCase):
    """Our own client refuses to call a tool that does not say it only reads.

    Left unsaid, the harness could not use its own server: every one of the
    three that only read was turned away by our own rule.
    """

    def test_the_reading_ones_say_they_only_read(self) -> None:
        for one in self.ask("tools/list")["result"]["tools"]:
            with self.subTest(tool=one["name"]):
                self.assertIs(one["annotations"]["readOnlyHint"], True)
                self.assertIs(one["annotations"]["destructiveHint"], False)

    def test_the_running_ones_do_not_claim_to_only_read(self) -> None:
        for one in editor.RUNS_THINGS:
            with self.subTest(tool=one["name"]):
                self.assertIs(one["annotations"]["readOnlyHint"], False)

    def test_every_one_that_runs_things_says_it_may_destroy_something(self) -> None:
        """A check of the plain command kind hands its command straight to the
        runner, so it can delete a file just as easily as an automation can.
        Marked soft, an editor that asks before a destructive thing would have
        asked about one of these two and not the other."""

        for one in editor.RUNS_THINGS:
            with self.subTest(tool=one["name"]):
                self.assertIs(one["annotations"]["destructiveHint"], True)

    def test_running_the_checks_says_what_a_check_can_be_without_over_claiming(self) -> None:
        """Said as "a check is a command", it was not true: most kinds only
        read a page or look through the files, and only the plain command kind
        runs anything. Over-claiming is the safe way to be wrong, and it is
        still wrong."""

        said = [one for one in editor.RUNS_THINGS if one["name"] == "run_the_checks"][0]
        self.assertIn("Most checks only read", said["description"])
        self.assertIn("can also be a plain command", said["description"])
        self.assertIn("deleting", said["description"])

    def test_the_kinds_of_check_that_only_read_really_only_read(self) -> None:
        """The wording above is only true while this is. A new kind of check
        that runs something would make the description an understatement, which
        is the direction that gets somebody hurt."""

        from our_harness import qa

        offered = set(qa._CASE_FIELDS_BY_KIND)
        self.assertIn("command", offered, "the kind this warns about")
        # Every other kind the harness ships. If a new one turns up here, look
        # at whether it runs anything before leaving the wording as it is.
        self.assertEqual(
            offered - {"command"},
            {"http", "browser", "file", "visual", "crawl", "secrets"},
            "a new kind of check: does it run anything? the wording says most "
            "of them do not",
        )

    def test_running_an_automation_says_it_may_write_and_reach_out(self) -> None:
        """It runs whatever the person who drew it put in it, and that can be
        a file written into the project or an assistant asked over the network.
        Somebody turning this on for "commands" has to be told the rest."""

        said = [one for one in editor.RUNS_THINGS if one["name"] == "run_an_automation"][0]
        self.assertIn("write files", said["description"])
        self.assertIn("network", said["description"])
        self.assertIs(said["annotations"]["destructiveHint"], True)


class TheNumbersEverybodyUses(EditorTestCase):
    def test_a_method_this_does_not_do_says_so_with_the_usual_number(self) -> None:
        """Our own client watches for this number to decide a server is an
        older one and try the older way of saying hello. Sent the wrong one, it
        gave up on our own server instead of trying again."""

        said = self.ask("server/discover")
        self.assertEqual(said["error"]["code"], editor.NOT_ONE_WE_DO)

    def test_a_method_it_does_do_asked_badly_says_so_differently(self) -> None:
        said = self.talking().answer({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "look_it_up", "arguments": {"asking": 7}},
        })
        # Asked badly comes back as a failed call, not a broken message, so the
        # editor shows the reason rather than a red light.
        self.assertIn("result", said)
        self.assertTrue(said["result"]["isError"])

    def test_something_that_is_not_a_message_uses_its_own_number(self) -> None:
        said = self.talking().answer("not a message")
        self.assertEqual(said["error"]["code"], editor.NOTHING_LIKE_A_MESSAGE)


class ItCallsItselfTheOneName(EditorTestCase):
    def test_the_editor_is_told_the_name_the_rest_of_it_uses(self) -> None:
        """Written out again here, a rename would leave this saying the old
        name to every editor that asks."""

        from our_harness import PRODUCT_NAME

        said = self.ask("initialize", {"protocolVersion": editor.WE_SPEAK})["result"]
        self.assertIn(PRODUCT_NAME, said["instructions"])




class OurOwnClientTalkingToOurOwnServer(EditorTestCase):
    """The strongest check there is: the half we already had, talking to the
    half we just wrote.

    Two things it caught. Our client watches for one particular number to
    decide a server is an older one and say hello the older way; ours sent a
    different number, and the client gave up rather than trying again. And a
    program our client starts was told so little about this machine that it
    could not work out where the home folder was, and fell over before saying a
    word - all our client ever saw was a closed pipe.
    """

    def a_real_server(self, *, may_run_things: bool = False) -> dict:
        return {
            "name": "our-own",
            "command": sys.executable,
            # Started through a little launcher rather than with PYTHONPATH:
            # a program our client starts is told only about paths this machine
            # has, and PYTHONPATH is not one of them - handing it on would push
            # our own code into somebody else's server.
            "args": [
                str(self.launcher),
                "--project", str(self.root), "editor", "serve",
            ] + (["--let-it-run-things"] if may_run_things else []),
            "allowed_tools": [
                "look_it_up", "what_the_project_knows", "list_the_automations",
            ],
        }

    def setUp(self) -> None:
        super().setUp()
        source = Path(__file__).resolve().parents[1] / "src"
        self.launcher = self.root / "start-it.py"
        self.launcher.write_text(
            "\n".join([
                "import sys",
                f"sys.path.insert(0, {str(source)!r})",
                "from our_harness.cli import main",
                "sys.exit(main())",
            ]) + "\n",
            encoding="utf-8",
        )

    def test_our_client_can_say_hello_to_our_server(self) -> None:
        with MCPClient(self.a_real_server(), timeout=120) as client:
            client.connect()
            offered = {one["name"] for one in client.list_tools()}
        self.assertIn("look_it_up", offered)
        self.assertNotIn("run_an_automation", offered, "not without being told to")

    def test_our_client_can_use_one_of_our_tools(self) -> None:
        with MCPClient(self.a_real_server(), timeout=120) as client:
            client.connect()
            said = client.call_tool("list_the_automations", {})
        self.assertIn("harness ui", json.dumps(said))

    def test_a_program_it_starts_can_find_its_own_home(self) -> None:
        """Told too little about this machine, ours fell over before it spoke."""

        for name in ("APPDATA", "USERPROFILE", "HOME"):
            if name in os.environ:
                self.assertIn(name, mcp.STARTS_A_PROGRAM)
        for name in mcp.STARTS_A_PROGRAM:
            self.assertNotIn("KEY", name, "these are paths, never secrets")
            self.assertNotIn("TOKEN", name)
            self.assertNotIn("SECRET", name)


class TheEndIsAtTheEnd(unittest.TestCase):
    """The line that runs this file goes last, and nowhere else.

    Put in the middle twice, everything after it was never defined by the time
    it ran, so running the file on its own silently dropped the very class it
    was sitting in front of - which both times was the most important one here.
    """

    def test_nothing_is_written_after_the_line_that_runs_it(self) -> None:
        said = Path(__file__).resolve().read_text(encoding="utf-8")
        # Built rather than written out, or this test would be the second one.
        line = "if __name__ == " + '"__main__":'
        self.assertEqual(said.count(line), 1)
        after = said.split(line, 1)[1]
        self.assertEqual(
            [one.strip() for one in after.splitlines() if one.strip()],
            ["# pragma: no cover", "unittest.main()"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
