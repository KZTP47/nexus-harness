"""Noticing a run going round in circles, and showing what it is doing.

Before this, a run that was getting nowhere found out by running out. The loop
stopped after a set number of calls, which is a stop and not a warning: by then
the budget was gone and nothing had been said while there was still something to
do about it. And whoever was watching saw a wall of tool calls with no plan
behind it, because the plan was only ever in the model's head.

So two things. The harness says something, once, when the same question has come
back with the same answer three times or the calls are nearly gone. And the
agent keeps a short list of what it is doing, which is the one thing a person
watching can actually read.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from our_harness import agent_tools
from our_harness.agent_tools import (
    THE_SAME_THING_THIS_OFTEN,
    THIS_FEW_CALLS_LEFT,
    AgentToolSession,
    tool_loop_instructions,
)
from our_harness.config import load_config
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.workflow import WorkflowDeadline


class StuckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        (self.root / "README.md").write_text("# A project\n", encoding="utf-8")
        self.config = load_config(self.root)
        self.memory = MemoryStore(self.config)
        self.addCleanup(self.memory.close)
        self.said: list[tuple[str, str, dict]] = []
        self.session = AgentToolSession(
            self.config,
            self.memory,
            WorkflowDeadline.start(30),
            lambda kind, node, payload: self.said.append((kind, node, payload)),
        )

    def read_it(self, call_id: str, path: str = "README.md") -> dict:
        return self.session.execute(
            "planner", call_id, "read_file",
            {"path": path, "start_line": 1, "end_line": 1, "max_bytes": 200},
        )

    def what_came_back(self, result: dict) -> dict:
        return json.loads(result["content"])

    def the_word(self, result: dict) -> str:
        """What the harness said on top of what the tool said.

        On the envelope, not inside the tool's own answer: inside, the same
        question came back different the second time, and the word got written
        into the copy kept for a restart to replay.
        """

        return str(result.get("notice") or "")

    def warnings(self) -> list[dict]:
        return [payload for kind, _node, payload in self.said if kind == "a_word_of_warning"]


class SayingTheSameThingOverAndOver(StuckTestCase):
    def test_nothing_is_said_the_first_two_times(self) -> None:
        for number in range(THE_SAME_THING_THIS_OFTEN - 1):
            said = self.the_word(self.read_it(f"read-{number}"))
            self.assertEqual(said, "", f"nothing on time {number + 1}")
        self.assertEqual(self.warnings(), [])

    def test_the_third_time_it_says_so(self) -> None:
        for number in range(THE_SAME_THING_THIS_OFTEN):
            result = self.read_it(f"read-{number}")
        said = self.the_word(result)
        self.assertIn("same thing", said)
        self.assertIn("read_file", said)
        self.assertIn("different", said, "and it says what to do instead")

    def test_what_the_tool_said_is_not_changed_by_the_word(self) -> None:
        """The same question must come back with the same answer, however many
        times it is asked. Put inside the answer, the word made the second one
        different from the first, and was kept in the copy a restart replays."""

        first = self.read_it("read-0")
        for number in range(1, THE_SAME_THING_THIS_OFTEN):
            again = self.read_it(f"read-{number}")
        self.assertEqual(first["content"], again["content"])
        self.assertNotIn("notice", self.what_came_back(again))
        self.assertTrue(self.the_word(again), "and the word is still said")

    def test_it_is_counted_on_what_was_asked_not_on_what_came_back(self) -> None:
        """The second and third are answered from the cache, and the count
        still goes up. Counted on the answer, the very repeats this is about
        would never have been counted at all."""

        for number in range(THE_SAME_THING_THIS_OFTEN):
            result = self.read_it(f"read-{number}")
        self.assertTrue(self.the_word(result))
        self.assertEqual(len(self.warnings()), 1)
        self.assertEqual(
            self.warnings()[0]["same_thing_times"], THE_SAME_THING_THIS_OFTEN
        )

    def test_a_different_question_starts_its_own_count(self) -> None:
        (self.root / "OTHER.md").write_text("# Another\n", encoding="utf-8")
        for number in range(THE_SAME_THING_THIS_OFTEN - 1):
            self.read_it(f"one-{number}")
        for number in range(THE_SAME_THING_THIS_OFTEN - 1):
            result = self.read_it(f"two-{number}", "OTHER.md")
        self.assertEqual(self.the_word(result), "")

    def test_the_person_watching_is_told_as_well(self) -> None:
        for number in range(THE_SAME_THING_THIS_OFTEN):
            self.read_it(f"read-{number}")
        warned = self.warnings()
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0]["name"], "read_file")
        self.assertIn("same thing", warned[0]["said"])


class RunningOutOfCalls(StuckTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session.max_calls = THIS_FEW_CALLS_LEFT + 2

    def test_nothing_is_said_while_there_is_room(self) -> None:
        self.assertEqual(self.the_word(self.read_it("read-0")), "")

    def test_it_says_so_before_the_calls_are_gone(self) -> None:
        (self.root / "OTHER.md").write_text("# Another\n", encoding="utf-8")
        self.read_it("read-0")
        said = self.the_word(self.read_it("read-1", "OTHER.md"))
        self.assertIn("left out of", said)
        self.assertIn("answer with what you have", said)

    def test_one_call_left_is_said_properly(self) -> None:
        self.assertEqual(agent_tools._how_many_calls(1), "1 tool call")
        self.assertEqual(agent_tools._how_many_calls(0), "0 tool calls")
        self.assertEqual(agent_tools._how_many_calls(3), "3 tool calls")

    def test_the_hard_stop_still_stops(self) -> None:
        """A warning is a warning. It does not buy anybody more calls."""

        (self.root / "OTHER.md").write_text("# Another\n", encoding="utf-8")
        for number in range(self.session.max_calls):
            self.session.execute(
                "planner", f"read-{number}", "read_file",
                {"path": "README.md", "start_line": 1, "end_line": 1,
                 "max_bytes": 200 + number},
            )
        with self.assertRaises(HarnessError) as caught:
            self.read_it("one-too-many")
        self.assertIn("limit reached", str(caught.exception))


class TheListOfWhatItIsDoing(StuckTestCase):
    def a_list(self, steps: list[dict], call_id: str = "list-1") -> dict:
        return self.what_came_back(
            self.session.execute("planner", call_id, "keep_a_list", {"steps": steps})
        )

    def test_it_is_kept_and_shown(self) -> None:
        said = self.a_list([
            {"what": "Read the file", "how_it_is_going": "done"},
            {"what": "Write the fix", "how_it_is_going": "going"},
            {"what": "Run the tests", "how_it_is_going": "waiting"},
        ])
        self.assertTrue(said["kept"])
        self.assertEqual(said["steps"], 3)
        self.assertEqual(said["done"], 1)
        self.assertIn("Write the fix", said["note"])
        self.assertEqual(
            [one["what"] for one in self.session.my_list],
            ["Read the file", "Write the fix", "Run the tests"],
        )
        shown = [payload for kind, _node, payload in self.said if kind == "the_list"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(len(shown[0]["steps"]), 3)

    def test_it_says_so_when_no_step_says_it_is_going(self) -> None:
        said = self.a_list([{"what": "Read the file", "how_it_is_going": "waiting"}])
        self.assertIn("Mark the one you are on", said["note"])

    def test_the_whole_list_is_sent_every_time(self) -> None:
        self.a_list([{"what": "First", "how_it_is_going": "going"}], "list-1")
        self.a_list([{"what": "Second", "how_it_is_going": "going"}], "list-2")
        self.assertEqual([one["what"] for one in self.session.my_list], ["Second"])

    def test_going_back_to_an_earlier_list_really_goes_back(self) -> None:
        """Answered from the cache, the third of these would have been the
        first's answer and the list would have been left saying the second."""

        first = [{"what": "First", "how_it_is_going": "going"}]
        self.a_list(first, "list-1")
        self.a_list([{"what": "Second", "how_it_is_going": "going"}], "list-2")
        self.a_list(first, "list-3")
        self.assertEqual([one["what"] for one in self.session.my_list], ["First"])

    def test_it_refuses_a_state_nobody_offers(self) -> None:
        said = self.a_list([{"what": "Read it", "how_it_is_going": "thinking"}])
        self.assertIn("error", said)
        self.assertIn("waiting, going, done or dropped", said["error"])

    def test_it_refuses_a_step_that_says_nothing(self) -> None:
        said = self.a_list([{"what": "   ", "how_it_is_going": "going"}])
        self.assertIn("error", said)

    def test_a_list_too_long_to_read_is_refused(self) -> None:
        said = self.a_list([
            {"what": f"Step {number}", "how_it_is_going": "waiting"}
            for number in range(agent_tools.MOST_STEPS + 1)
        ])
        self.assertIn("error", said)
        self.assertIn("read", said["error"])

    def test_a_step_written_as_an_essay_is_refused(self) -> None:
        """Refused, not quietly cut short. Cut short, the whole essay was still
        written out and hashed on the way in, and nobody was told that half
        their step had gone."""

        said = self.a_list([
            {"what": "x" * (agent_tools.MOST_LETTERS_IN_A_STEP + 1),
             "how_it_is_going": "going"},
        ])
        self.assertIn("error", said)
        self.assertIn("read at a glance", said["error"])
        self.assertEqual(self.session.my_list, [])

    def test_a_step_right_up_to_the_limit_is_kept(self) -> None:
        self.a_list([
            {"what": "x" * agent_tools.MOST_LETTERS_IN_A_STEP,
             "how_it_is_going": "going"},
        ])
        self.assertEqual(
            len(self.session.my_list[0]["what"]), agent_tools.MOST_LETTERS_IN_A_STEP
        )

    def test_an_empty_list_is_allowed(self) -> None:
        """Saying "there is nothing left" is a thing worth being able to say."""

        said = self.a_list([])
        self.assertTrue(said["kept"])
        self.assertEqual(self.session.my_list, [])


class WhatTheAgentIsToldBeforehand(StuckTestCase):
    def test_it_is_told_a_notice_may_arrive_and_what_it_means(self) -> None:
        told = tool_loop_instructions(self.session.definitions())
        self.assertIn("notice", told)
        self.assertIn("same thing over and over", told)
        self.assertIn("nearly out of calls", told)

    def test_it_is_told_to_keep_a_list_and_why(self) -> None:
        told = tool_loop_instructions(self.session.definitions())
        self.assertIn("keep_a_list", told)
        self.assertIn("watching", told)

    def test_anybody_with_tools_at_all_can_keep_a_list(self) -> None:
        offered = [one["name"] for one in self.session.definitions(
            node="coder", capabilities={"workspace.read"}
        )]
        self.assertIn("keep_a_list", offered)

    def test_a_node_allowed_nothing_still_gets_nothing(self) -> None:
        """This is a tool like the rest, not an exception to the permissions."""

        self.assertEqual(
            self.session.definitions(node="coder", capabilities=set()), []
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheCountIsOneAgentsOwn(StuckTestCase):
    """Put together across the whole run, it was not true of anybody.

    Two agents asking the same sensible question added up, and the second one
    was told on its very first go that it had asked three times. That is the
    harness telling an agent something about itself that did not happen.
    """

    def test_two_agents_asking_the_same_thing_are_counted_apart(self) -> None:
        for number in range(THE_SAME_THING_THIS_OFTEN - 1):
            self.session.execute(
                "planner", f"planner-{number}", "read_file",
                {"path": "README.md", "start_line": 1, "end_line": 1, "max_bytes": 200},
            )
        result = self.session.execute(
            "coder", "coder-0", "read_file",
            {"path": "README.md", "start_line": 1, "end_line": 1, "max_bytes": 200},
        )
        self.assertEqual(
            self.the_word(result), "", "the coder asked once, and is told nothing"
        )

    def test_one_agent_going_round_is_still_caught(self) -> None:
        for number in range(THE_SAME_THING_THIS_OFTEN):
            result = self.session.execute(
                "coder", f"coder-{number}", "read_file",
                {"path": "README.md", "start_line": 1, "end_line": 1, "max_bytes": 200},
            )
        self.assertIn("same thing", self.the_word(result))


class TheCountSurvivesARestart(StuckTestCase):
    """Left out of what is kept, a run picked up after an approval or a restart
    forgot it had been round in circles, and went round again with nobody
    saying anything."""

    def a_fresh_session(self, state: dict) -> AgentToolSession:
        fresh = AgentToolSession(
            self.config,
            self.memory,
            WorkflowDeadline.start(30),
            lambda kind, node, payload: self.said.append((kind, node, payload)),
        )
        fresh.restore_budget_state(state)
        return fresh

    def test_the_third_time_still_says_something_after_a_restart(self) -> None:
        for number in range(THE_SAME_THING_THIS_OFTEN - 1):
            self.read_it(f"read-{number}")
        carried = self.session.budget_state()
        self.assertIn("how_often", carried)

        fresh = self.a_fresh_session(carried)
        result = fresh.execute(
            "planner", "read-after", "read_file",
            {"path": "README.md", "start_line": 1, "end_line": 1, "max_bytes": 200},
        )
        self.assertIn("same thing", str(result.get("notice") or ""))

    def test_a_checkpoint_written_before_this_still_opens(self) -> None:
        """Nobody's saved run should stop working because we added a field."""

        carried = self.session.budget_state()
        carried.pop("how_often")
        fresh = self.a_fresh_session(carried)
        self.assertEqual(fresh.how_often, {})

    def test_a_made_up_count_is_refused(self) -> None:
        for bad in (
            {"not a digest": 1},
            {"a" * 64: -1},
            {"a" * 64: "three"},
            {"a" * 64: True},
            "not a list of counts",
        ):
            with self.subTest(how_often=bad):
                carried = self.session.budget_state()
                carried["how_often"] = bad
                with self.assertRaises(HarnessError) as caught:
                    self.a_fresh_session(carried)
                self.assertIn("repeat counts", str(caught.exception))

    def test_a_count_larger_than_the_whole_budget_is_refused(self) -> None:
        carried = self.session.budget_state()
        carried["how_often"] = {"a" * 64: self.session.max_calls + 1}
        with self.assertRaises(HarnessError):
            self.a_fresh_session(carried)


class EveryRouteIsToldWhatANoticeIs(unittest.TestCase):
    def test_the_route_that_uses_the_provider_s_own_tools_is_told_too(self) -> None:
        """The word reaches that route as well, so what it is has to be said on
        that route as well. Said only on the other one, the warning arrived
        looking like something the project had said."""

        from our_harness import workflow

        # The loop is run two ways: one where the harness writes the tool rules
        # into the prompt itself, and one where the provider offers the tools
        # and the rules go in as a plain sentence. Both use the same words, from
        # the one place they are written, so neither can quietly lose a sentence
        # the other still has.
        self.assertIs(workflow.WHAT_A_NOTICE_IS, agent_tools.WHAT_A_NOTICE_IS)
        self.assertIs(workflow.KEEP_A_LIST_EARLY, agent_tools.KEEP_A_LIST_EARLY)
        where = Path(__file__).resolve().parents[1] / "src" / "our_harness" / "workflow.py"
        said = where.read_text(encoding="utf-8")
        self.assertIn("+ WHAT_A_NOTICE_IS", said, "the native route says what a notice is")
        self.assertIn("KEEP_A_LIST_EARLY if offers_a_list", said,
                      "and asks for the list, when there is a list to keep")

    def test_the_words_really_go_out_on_the_route_that_writes_its_own(self) -> None:
        from our_harness.agent_tools import TOOL_DEFINITIONS

        told = tool_loop_instructions([dict(one) for one in TOOL_DEFINITIONS])
        self.assertIn(agent_tools.WHAT_A_NOTICE_IS, told)
        self.assertNotIn(agent_tools.KEEP_A_LIST_EARLY, told,
                         "and does not ask for a list nobody was offered")

    def test_the_list_is_asked_for_when_it_is_offered(self) -> None:
        from our_harness.agent_tools import MY_LIST_TOOL_DEFINITIONS, TOOL_DEFINITIONS

        told = tool_loop_instructions(
            [dict(one) for one in TOOL_DEFINITIONS + MY_LIST_TOOL_DEFINITIONS]
        )
        self.assertIn(agent_tools.KEEP_A_LIST_EARLY, told)

    def test_nobody_has_written_the_words_out_a_second_time(self) -> None:
        """Written twice, the two had already drifted apart the same afternoon:
        one said a notice comes back "in it" and the other "on it"."""

        source = Path(__file__).resolve().parents[1] / "src" / "our_harness"
        anchor = "not the project: it means you are asking the same thing"
        wrote_it = [
            one.name for one in source.rglob("*.py")
            if anchor in one.read_text(encoding="utf-8")
        ]
        self.assertEqual(wrote_it, ["agent_tools.py"], wrote_it)
