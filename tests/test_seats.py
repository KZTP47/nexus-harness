"""Setting up an assistant you already pay for.

Nothing here reaches for a real tool: a fake stands in for the machine, so the
same answers come back on a machine with both assistants and on one with none.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import seats
from our_harness.config import (
    DEFAULT_CONFIG,
    LoadedConfig,
    is_project_local_config_trusted,
    trust_project_local_config,
)
from our_harness.models import HarnessError


def pretend(installed: dict[str, str], versions: dict[str, str] | None = None):
    """A machine holding exactly these tools, saying exactly these versions."""

    said = versions or {}

    def available(kind, command=None):
        return installed.get(kind, "")

    def version(command, arguments):
        for kind, where in installed.items():
            if where == command:
                answer = said.get(kind, "9.9.9 (pretend)")
                return ("", answer) if answer.startswith("!") else (answer, "")
        return "", "not here"

    return (
        mock.patch.object(seats, "available", available),
        mock.patch.object(seats, "_ask_its_version", version),
    )


class SeatTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    @property
    def local(self) -> Path:
        return self.root / ".harness" / "config.local.json"

    def written(self) -> dict:
        return json.loads(self.local.read_text(encoding="utf-8"))


class LookingTests(SeatTestCase):
    def test_a_tool_that_is_there_and_answers_is_ready(self) -> None:
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"}, {"claude-cli": "2.1.101"})
        with finds, asks:
            found = seats.look(self.config)
        claude = [seat for seat in found.seats if seat.kind == "claude-cli"][0]
        self.assertTrue(claude.ready)
        self.assertEqual(claude.version, "2.1.101")
        self.assertEqual(claude.found_at, "/usr/bin/claude")
        self.assertEqual(claude.route, "claude")

    def test_a_tool_that_is_not_there_says_where_to_get_it(self) -> None:
        finds, asks = pretend({})
        with finds, asks:
            found = seats.look(self.config)
        for seat in found.seats:
            with self.subTest(seat=seat.kind):
                self.assertFalse(seat.ready)
                self.assertIn("not on this machine", seat.why_not)
                self.assertTrue(seat.install_hint)

    def test_a_tool_that_fails_while_printing_something_is_not_ready(self) -> None:
        # "Installed but not signed in" usually prints a line and still fails.
        # Reading that line as a version calls the seat ready, and the first
        # real run then fails for a reason nobody could see at setup time.
        def fails(command, arguments):
            return "", "It answered with code 1: Please run claude login first."

        finds, _asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, mock.patch.object(seats, "_ask_its_version", fails):
            found = seats.look(self.config)
        claude = [seat for seat in found.seats if seat.kind == "claude-cli"][0]
        self.assertFalse(claude.ready)
        self.assertIn("code 1", claude.why_not)

    def test_the_version_call_itself_refuses_a_failing_tool(self) -> None:
        # The line above stands in for the machine. This one drives the real
        # reader, so the two cannot drift apart.
        finished = mock.Mock(returncode=1, stdout="Please run claude login first.", stderr="")
        with mock.patch.object(seats.subprocess, "run", return_value=finished):
            version, trouble = seats._ask_its_version("/usr/bin/claude", ("--version",))
        self.assertEqual(version, "")
        self.assertIn("code 1", trouble)
        self.assertIn("claude login", trouble)

    def test_a_tool_that_will_not_answer_is_not_ready(self) -> None:
        # Installed, but sitting there waiting for somebody to sign in.
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"}, {"claude-cli": "!"})
        with finds, asks:
            found = seats.look(self.config)
        claude = [seat for seat in found.seats if seat.kind == "claude-cli"][0]
        self.assertFalse(claude.ready)
        self.assertEqual(found.ready, [])

    def test_it_says_whether_the_settings_are_trusted_yet(self) -> None:
        finds, asks = pretend({})
        with finds, asks:
            self.assertFalse(seats.look(self.config).trusted)

    def test_a_route_already_written_is_noticed(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"claude": {"kind": "claude-cli", "model": "x", "endpoint": ""}}
        config = LoadedConfig(data, self.root, [], {})
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            found = seats.look(config)
        claude = [seat for seat in found.seats if seat.kind == "claude-cli"][0]
        self.assertTrue(claude.already_set_up)
        self.assertEqual(claude.model, "x", "the model already chosen is kept")


class SettingUpTests(SeatTestCase):
    def test_it_writes_a_route_and_trusts_the_file(self) -> None:
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        self.assertEqual(done.routes, ["claude"])
        self.assertTrue(done.trusted)
        self.assertTrue(is_project_local_config_trusted(self.root, self.local))
        written = self.written()
        self.assertEqual(written["providers"]["claude"]["kind"], "claude-cli")
        self.assertEqual(written["providers"]["claude"]["endpoint"], "",
                         "a signed-in tool has no address to call")
        self.assertEqual(written["provider"]["name"], "claude-cli")

    def test_it_refuses_a_tool_that_is_not_on_this_machine(self) -> None:
        # A route to a tool nobody has is a run that fails later for a reason
        # nobody can see now.
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks, self.assertRaises(HarnessError) as caught:
            seats.set_up(self.config, ["copilot-cli"])
        self.assertIn("not ready", str(caught.exception))
        self.assertFalse(self.local.exists())

    def test_it_keeps_settings_that_were_already_there(self) -> None:
        self.local.write_text(json.dumps({
            "memory": {"enabled": True},
            "providers": {"mine": {"kind": "ollama", "model": "x",
                                   "endpoint": "http://127.0.0.1:11434"}},
        }), encoding="utf-8")
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        written = self.written()
        self.assertEqual(written["memory"], {"enabled": True})
        self.assertIn("mine", written["providers"], "somebody else's route stayed")
        self.assertIn("claude", written["providers"])
        self.assertEqual(done.kept, ["memory"])

    def test_a_file_somebody_else_left_behind_is_not_trusted_on_the_way_past(self) -> None:
        # Trusting a file nobody has read is not something to do quietly. It is
        # also not this tool's place to refuse: it writes the route, hands the
        # whole file back, says what trusting would allow, and leaves the
        # decision with the person whose machine it is.
        self.local.write_text(json.dumps({
            "providers": {"theirs": {"kind": "openai-compatible", "model": "x",
                                     "endpoint": "http://somewhere.example/v1"}},
            "execution": {"inherit_environment": True},
        }), encoding="utf-8")
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        self.assertFalse(done.trusted)
        self.assertFalse(is_project_local_config_trusted(self.root, self.local))
        self.assertTrue(done.needs_your_say, "the choice is handed over, not taken")
        self.assertIn("claude", self.written()["providers"], "the route was still written")
        self.assertTrue(done.contents, "the whole file is handed back to read")

    def test_it_names_what_trusting_would_allow(self) -> None:
        self.local.write_text(json.dumps({
            "providers": {"theirs": {"kind": "openai-compatible", "model": "x",
                                     "endpoint": "http://somewhere.example/v1",
                                     "command": ["curl", "http://elsewhere.example"]}},
            "execution": {"inherit_environment": True},
        }), encoding="utf-8")
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        said = " ".join(done.risky_parts)
        self.assertIn("starts this program", said)
        self.assertIn("curl", said)
        self.assertIn("somewhere.example", said)
        self.assertIn("every variable in your environment", said)

    def test_a_file_that_only_talks_to_this_machine_says_the_mild_thing(self) -> None:
        # Nothing here starts a program or calls anywhere else, so there is no
        # alarming line to write. The section is still named: a settings file
        # can only hold routes at all once it is trusted, and a section nobody
        # mentions is a section nobody reads.
        worrying = seats.what_makes_it_risky({
            "providers": {"mine": {"kind": "ollama", "model": "x",
                                   "endpoint": "http://127.0.0.1:11434"}},
        })
        self.assertEqual(len(worrying), 1)
        self.assertIn("It sets providers", worrying[0])
        for alarming in ("starts this program", "sends your code to"):
            self.assertNotIn(alarming, worrying[0])

    def test_the_person_can_trust_it_anyway(self) -> None:
        # The other half of handing over the choice: saying yes has to work.
        self.local.write_text(json.dumps({"memory": {"enabled": True}}), encoding="utf-8")
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        self.assertTrue(done.needs_your_say)
        said = seats.trust_it_anyway(self.config, done.mark)
        self.assertTrue(is_project_local_config_trusted(self.root, self.local))
        self.assertIn("trusted", said)

    def test_saying_you_have_read_it_is_checked_against_what_you_read(self) -> None:
        # "The panel shows the file first" is a promise the panel makes. This is
        # what checks it: trusting has to name the file that was read, and a
        # file that changed in between is not that file.
        self.local.write_text(json.dumps({"memory": {"enabled": True}}), encoding="utf-8")
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        with self.assertRaises(HarnessError) as caught:
            seats.trust_it_anyway(self.config, "")
        self.assertIn("read it", str(caught.exception))
        self.local.write_text(
            self.local.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        with self.assertRaises(HarnessError) as caught:
            seats.trust_it_anyway(self.config, done.mark)
        self.assertIn("has changed since it was shown", str(caught.exception))
        self.assertFalse(is_project_local_config_trusted(self.root, self.local))

    def test_trusting_a_file_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(HarnessError):
            seats.trust_it_anyway(self.config, "anything")

    def test_a_file_already_trusted_stays_trusted(self) -> None:
        # The other half: somebody who has already said this file is theirs
        # should not have to say it again for adding a seat.
        self.local.write_text(json.dumps({"memory": {"enabled": True}}), encoding="utf-8")
        trust_project_local_config(self.root, self.local)
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        self.assertTrue(done.trusted)
        self.assertTrue(is_project_local_config_trusted(self.root, self.local))

    def test_it_says_what_it_wrote_over(self) -> None:
        self.local.write_text(json.dumps({
            "providers": {"claude": {"kind": "claude-cli", "model": "old", "endpoint": ""}},
            "provider": {"name": "ollama", "model": "qwen2.5-coder:7b",
                         "endpoint": "http://127.0.0.1:11434", "api_key_env": ""},
        }), encoding="utf-8")
        trust_project_local_config(self.root, self.local)
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        self.assertIn("claude", done.replaced, "a route of the same name was replaced")
        self.assertTrue(any("ollama" in item for item in done.replaced),
                        "the assistant used by default was replaced too")

    def test_a_settings_file_that_cannot_be_read_is_left_alone(self) -> None:
        self.local.write_text("{ not json at all", encoding="utf-8")
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks, self.assertRaises(HarnessError) as caught:
            seats.set_up(self.config, ["claude-cli"])
        self.assertIn("cannot be read", str(caught.exception))
        self.assertEqual(self.local.read_text(encoding="utf-8"), "{ not json at all")

    def test_setting_up_twice_does_not_pile_routes_up(self) -> None:
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            seats.set_up(self.config, ["claude-cli"])
            seats.set_up(self.config, ["claude-cli"])
        self.assertEqual(list(self.written()["providers"]), ["claude"])

    def test_not_trusting_says_what_to_do_next(self) -> None:
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"], trust=False)
        self.assertFalse(done.trusted)
        self.assertIn("harness trust", done.note)

    def test_nothing_chosen_says_so(self) -> None:
        with self.assertRaises(HarnessError):
            seats.routes_for(self.config, [])

    def test_an_assistant_it_does_not_know_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            seats.routes_for(self.config, ["something-else"])


class PuttingItBackTests(SeatTestCase):
    def test_it_puts_back_exactly_what_was_there(self) -> None:
        before = json.dumps({"memory": {"enabled": True}}, indent=2) + "\n"
        self.local.write_text(before, encoding="utf-8")
        trust_project_local_config(self.root, self.local)
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        self.assertNotEqual(self.local.read_text(encoding="utf-8"), before)
        said = seats.put_it_back(self.config, done.previous)
        self.assertEqual(self.local.read_text(encoding="utf-8"), before)
        self.assertIn("put back", said)
        self.assertTrue(is_project_local_config_trusted(self.root, self.local),
                        "it was trusted before, so it is trusted after")

    def test_it_removes_a_file_that_was_not_there_before(self) -> None:
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        self.assertIsNone(done.previous.contents)
        self.assertTrue(self.local.exists())
        seats.put_it_back(self.config, done.previous)
        self.assertFalse(self.local.exists())

    def test_a_file_nobody_trusted_does_not_come_back_trusted(self) -> None:
        # Undo puts the trust mark back too. Coming back trusted would hand the
        # file authority it did not have a moment earlier.
        self.local.write_text(json.dumps({"memory": {"enabled": True}}), encoding="utf-8")
        finds, asks = pretend({"claude-cli": "/usr/bin/claude"})
        with finds, asks:
            done = seats.set_up(self.config, ["claude-cli"])
        said = seats.put_it_back(self.config, done.previous)
        self.assertFalse(is_project_local_config_trusted(self.root, self.local))
        self.assertIn("untrusted again", said)


class SharingTheWorkTests(SeatTestCase):
    def graph(self) -> dict:
        from importlib.resources import files

        return json.loads(
            files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8")
        )

    def routes_of(self, graph: dict) -> dict[str, str]:
        return {
            node["id"]: (node.get("config") or {}).get("provider_route")
            for node in graph["nodes"]
            if (node.get("config") or {}).get("provider_route")
        }

    def test_two_seats_put_the_reviewer_on_the_other_one(self) -> None:
        # The whole point of two assistants: they do not share a blind spot.
        shared = seats.share_the_work(self.graph(), ["claude", "copilot"])
        routes = self.routes_of(shared)
        self.assertEqual(routes["planner"], "claude")
        self.assertEqual(routes["coder"], "copilot")
        self.assertEqual(routes["review"], "claude")
        self.assertNotEqual(routes["coder"], routes["review"])

    def test_one_seat_is_used_for_everything(self) -> None:
        routes = self.routes_of(seats.share_the_work(self.graph(), ["claude"]))
        self.assertEqual(set(routes.values()), {"claude"})

    def test_every_agent_given_a_seat_can_send_notes(self) -> None:
        shared = seats.share_the_work(self.graph(), ["claude", "copilot"])
        for node in shared["nodes"]:
            config = node.get("config") or {}
            if config.get("provider_route"):
                with self.subTest(node=node["id"]):
                    self.assertIn("team.message", config["capabilities"])

    def test_the_one_writing_code_may_write_files(self) -> None:
        shared = seats.share_the_work(self.graph(), ["claude", "copilot"])
        coder = [node for node in shared["nodes"] if node["type"] == "coder"][0]
        self.assertIn("workspace.write", coder["config"]["capabilities"])

    def test_the_workflow_it_gives_back_is_one_the_harness_accepts(self) -> None:
        from our_harness.graphs import validate_graph

        shared = seats.share_the_work(self.graph(), ["claude", "copilot"])
        self.assertEqual(validate_graph(shared), [])

    def test_the_workflow_handed_in_is_not_changed(self) -> None:
        before = self.graph()
        untouched = json.dumps(before, sort_keys=True)
        seats.share_the_work(before, ["claude"])
        self.assertEqual(json.dumps(before, sort_keys=True), untouched)

    def test_no_routes_says_so(self) -> None:
        with self.assertRaises(HarnessError):
            seats.share_the_work(self.graph(), [])

    def test_something_that_is_not_a_workflow_is_refused(self) -> None:
        for value in ({}, {"nodes": "none"}, "workflow", None):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                seats.share_the_work(value, ["claude"])


class WhenTheSettingsAreBrokenTests(SeatTestCase):
    def test_it_still_works_when_the_settings_cannot_be_read(self) -> None:
        # This is the tool for fixing exactly that, so it has to start.
        self.local.write_text(json.dumps({
            "providers": {"mine": {"kind": "ollama", "model": "x",
                                   "endpoint": "http://127.0.0.1:11434"}}
        }), encoding="utf-8")
        config, trouble = seats.settings_to_work_from(self.root)
        self.assertTrue(trouble, "it should say plainly that it fell back")
        self.assertIn("could not be read", trouble)
        self.assertEqual(config.project_root, self.root)

    def test_it_says_nothing_when_the_settings_are_fine(self) -> None:
        _config, trouble = seats.settings_to_work_from(self.root)
        self.assertEqual(trouble, "")


if __name__ == "__main__":
    unittest.main()
