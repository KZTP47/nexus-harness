"""The board of agents: who works on what, and who may talk to whom.

The harness could already do each of these things in its own tab - build a
team, talk to an assistant, pick a project - but never all of them at once, and
never across more than one project. The board is that view, and these tests are
about the two things it must never get wrong.

**Two agents must not share a conversation.** An agent's conversation is filed
under its own name, not under the assistant it uses. Two agents both on Claude
would otherwise each read the other's half of it, which is worse than useless:
it is one assistant answering as though it were two.

**Nobody talks to anybody unless somebody drew the line.** Off is the answer
when nothing was said. Two agents that should not know about each other are two
agents that will not hear from each other, and that is the safer way round to
be wrong.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import chat, server, swarm
from our_harness.config import DEFAULT_CONFIG, LoadedConfig, load_config


class BoardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # The board is kept beside somebody's own settings, because it spans
        # projects and belongs to none of them. Sent somewhere throwaway, so a
        # test never touches the real one.
        self.somewhere_else = self.root / "settings"
        self.somewhere_else.mkdir()
        patched = mock.patch.dict(os.environ, {
            "APPDATA": str(self.somewhere_else),
            "XDG_CONFIG_HOME": str(self.somewhere_else),
        })
        patched.start()
        self.addCleanup(patched.stop)

    def a_project(self, name: str = "alpha") -> Path:
        where = self.root / name
        (where / ".harness").mkdir(parents=True)
        return where

    def a_board(self, **kept) -> swarm.Board:
        return swarm.save({
            "agents": kept.get("agents", []),
            "projects": kept.get("projects", []),
            "works_on": kept.get("works_on", []),
            "talks_to": kept.get("talks_to", []),
        })


class WhatABoardIs(BoardTestCase):
    def test_an_empty_one_when_nothing_was_ever_written(self) -> None:
        board = swarm.load()
        self.assertEqual(board.agents, [])
        self.assertEqual(board.projects, [])

    def test_agents_and_projects_are_kept(self) -> None:
        board = self.a_board(
            agents=[{"name": "The reviewer", "who": "claude", "job": "Reads it"}],
            projects=[{"path": str(self.a_project()), "tasks": ["Make it pass"]}],
        )
        self.assertEqual([one.name for one in board.agents], ["The reviewer"])
        self.assertEqual(board.projects[0].tasks, ["Make it pass"])

    def test_it_is_kept_beside_your_own_settings(self) -> None:
        """Not inside a project. A board is above every project, and putting it
        in one would mean the others could not see it."""

        self.a_board(agents=[{"name": "One"}])
        self.assertIn(str(self.somewhere_else), str(swarm.where_it_lives()))
        self.assertTrue(swarm.where_it_lives().is_file())

    def test_it_is_read_back_the_same(self) -> None:
        self.a_board(
            agents=[{"name": "One", "who": "claude"}, {"name": "Two", "who": "copilot"}],
            projects=[{"path": str(self.a_project()), "tasks": ["Do it"]}],
        )
        # Ids are handed out while it is being read, so they have to be looked
        # up rather than guessed at.
        board = swarm.load()
        again = swarm.save(json.loads(json.dumps(board.to_dict())))
        self.assertEqual(
            [one.id for one in again.agents], [one.id for one in board.agents])
        self.assertEqual(again.projects[0].tasks, ["Do it"])

    def test_jobs_are_lines_of_text_and_are_kept(self) -> None:
        """Read as a list of objects, every job was quietly dropped and the
        board said the project had nothing to do in it."""

        board = self.a_board(projects=[
            {"path": str(self.a_project()), "tasks": ["First", "Second"]},
        ])
        self.assertEqual(board.projects[0].tasks, ["First", "Second"])
        self.assertEqual(swarm.load().projects[0].tasks, ["First", "Second"])

    def test_something_that_is_not_a_job_is_left_out(self) -> None:
        board = self.a_board(projects=[
            {"path": str(self.a_project()), "tasks": ["First", {"nonsense": 1}, None]},
        ])
        self.assertEqual(board.projects[0].tasks, ["First"])

    def test_a_file_nobody_can_read_starts_empty(self) -> None:
        """Rather than a panel that will not open at all."""

        swarm.where_it_lives().parent.mkdir(parents=True, exist_ok=True)
        swarm.where_it_lives().write_text("{ not json", encoding="utf-8")
        self.assertEqual(swarm.load().agents, [])

    def test_a_board_that_makes_no_sense_starts_empty(self) -> None:
        swarm.where_it_lives().parent.mkdir(parents=True, exist_ok=True)
        swarm.where_it_lives().write_text(
            json.dumps({"agents": [{"name": "!!! no"}]}), encoding="utf-8")
        self.assertEqual(swarm.load().agents, [])


class TwoWindowsOnOneBoard(BoardTestCase):
    """A whole board at a time has one trap, and the version is the way out.

    Both windows send a whole board built from what each read. Without a
    version, the second write quietly throws the first one's change away and
    neither person is told anything happened.
    """

    def test_a_board_says_how_many_times_it_has_been_written(self) -> None:
        self.assertEqual(swarm.load().version, 0)
        self.assertEqual(self.a_board(agents=[{"name": "One"}]).version, 1)
        self.assertEqual(self.a_board(agents=[{"name": "Two"}]).version, 2)

    def test_a_save_built_from_the_board_that_is_there_goes_through(self) -> None:
        board = self.a_board(agents=[{"name": "One"}])
        again = swarm.save({"version": board.version, "agents": [{"name": "Two"}]})
        self.assertEqual([one.name for one in again.agents], ["Two"])

    def test_a_save_built_from_an_older_board_is_refused(self) -> None:
        self.a_board(agents=[{"name": "One"}])
        self.a_board(agents=[{"name": "Two"}])
        with self.assertRaises(swarm.SwarmError) as caught:
            swarm.save({"version": 1, "agents": [{"name": "Three"}]})
        self.assertIn("another window", str(caught.exception))

    def test_the_refused_one_changed_nothing(self) -> None:
        self.a_board(agents=[{"name": "One"}])
        self.a_board(agents=[{"name": "Two"}])
        with self.assertRaises(swarm.SwarmError):
            swarm.save({"version": 1, "agents": [{"name": "Three"}]})
        self.assertEqual([one.name for one in swarm.load().agents], ["Two"])

    def test_a_board_that_says_nothing_about_versions_is_taken_as_is(self) -> None:
        """A test, or the very first write, or anything that never read one."""

        self.a_board(agents=[{"name": "One"}])
        self.assertEqual(swarm.save({"agents": [{"name": "Two"}]}).version, 2)

    def test_something_that_only_looks_like_a_version_is_refused(self) -> None:
        """Taken as "said nothing", a version written as 3.0 or as "3" would
        slip past the check and quietly put it back the way it was."""

        self.a_board(agents=[{"name": "One"}])
        for said in (1.0, "1", True, -1, [1]):
            with self.subTest(said=said), self.assertRaises(swarm.SwarmError) as caught:
                swarm.save({"version": said, "agents": [{"name": "Two"}]})
            self.assertIn("not a version", str(caught.exception))
        self.assertEqual([one.name for one in swarm.load().agents], ["One"])

    def test_the_count_is_not_capped_the_way_a_place_on_the_board_is(self) -> None:
        """Read with the helper that keeps a box on the board, it would stop at
        four thousand and the check above would quietly stop working."""

        board = swarm.read_it({"version": 99999, "agents": []})
        self.assertEqual(board.version, 99999)


class NamesAreWhereConversationsAreKept(BoardTestCase):
    def test_two_agents_on_one_assistant_keep_their_own_conversations(self) -> None:
        """The whole reason an agent has a name of its own."""

        where = self.a_project()
        config = load_config(where)
        first = chat.where_it_is_kept(config, "claude", swarm.filed_as("The reviewer"))
        second = chat.where_it_is_kept(config, "claude", swarm.filed_as("The writer"))
        self.assertNotEqual(first, second)

    def test_two_agents_may_not_share_a_name(self) -> None:
        with self.assertRaises(swarm.SwarmError) as caught:
            self.a_board(agents=[{"name": "One"}, {"name": " one "}])
        self.assertIn("already an agent", str(caught.exception))

    def test_an_agent_needs_a_name(self) -> None:
        with self.assertRaises(swarm.SwarmError):
            self.a_board(agents=[{"name": "   "}])

    def test_a_name_that_would_be_a_path_is_refused(self) -> None:
        """The name becomes a file name, so this is the one that matters."""

        for said in ("../out", "a/b", "a\\b", "a:b"):
            with self.subTest(said=said), self.assertRaises(swarm.SwarmError):
                self.a_board(agents=[{"name": said}])

    def test_a_name_written_as_an_essay_is_cut_short(self) -> None:
        board = self.a_board(agents=[{"name": "a" * 500}])
        self.assertEqual(len(board.agents[0].name), swarm.LONGEST_NAME)

    def test_what_it_is_filed_under_follows_the_name(self) -> None:
        """Worked out from the name rather than written down, so renaming an
        agent renames its conversation and leaves nothing behind."""

        board = self.a_board(agents=[{"name": " The  reviewer "}])
        self.assertEqual(board.agents[0].to_dict()["filed_as"], "The reviewer")


class NobodyTalksUnlessSomebodyDrewTheLine(BoardTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.board = self.a_board(
            agents=[{"name": "One"}, {"name": "Two"}, {"name": "Three"}],
            talks_to=[{"one": "agent-1", "other": "agent-2"}],
        )

    def test_a_pair_with_a_line_may_talk(self) -> None:
        self.assertTrue(swarm.may_they_talk(self.board, "agent-1", "agent-2"))

    def test_it_reads_the_same_both_ways_round(self) -> None:
        """Held smallest first, so "A talks to B" and "B talks to A" are one
        line and cannot disagree with each other."""

        self.assertTrue(swarm.may_they_talk(self.board, "agent-2", "agent-1"))

    def test_a_pair_with_no_line_may_not(self) -> None:
        self.assertFalse(swarm.may_they_talk(self.board, "agent-1", "agent-3"))

    def test_nobody_talks_to_themselves(self) -> None:
        self.assertFalse(swarm.may_they_talk(self.board, "agent-1", "agent-1"))

    def test_the_same_pair_twice_is_one_line(self) -> None:
        board = self.a_board(
            agents=[{"name": "One"}, {"name": "Two"}],
            talks_to=[
                {"one": "agent-1", "other": "agent-2"},
                {"one": "agent-2", "other": "agent-1"},
            ],
        )
        self.assertEqual(len(board.talks_to), 1)

    def test_a_line_to_somebody_who_is_gone_is_dropped(self) -> None:
        """Not refused. The board somebody can see is the truth, and a line to
        nothing is not something they can point at to fix."""

        board = self.a_board(
            agents=[{"name": "One"}],
            talks_to=[{"one": "agent-1", "other": "agent-9"}],
        )
        self.assertEqual(board.talks_to, [])


class WhoWorksOnWhat(BoardTestCase):
    def test_an_agent_can_be_on_more_than_one_project(self) -> None:
        board = self.a_board(
            agents=[{"name": "One"}],
            projects=[
                {"path": str(self.a_project("alpha"))},
                {"path": str(self.a_project("beta"))},
            ],
            works_on=[
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-1", "project": "project-2"},
            ],
        )
        self.assertEqual(len(board.works_on), 2)

    def test_a_project_can_have_more_than_one_agent(self) -> None:
        board = self.a_board(
            agents=[{"name": "One"}, {"name": "Two"}],
            projects=[{"path": str(self.a_project())}],
            works_on=[
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
        )
        self.assertEqual(
            [one.name for one in swarm.who_works_on(board, "project-1")], ["One", "Two"])

    def test_the_same_line_twice_is_one_line(self) -> None:
        board = self.a_board(
            agents=[{"name": "One"}],
            projects=[{"path": str(self.a_project())}],
            works_on=[
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-1", "project": "project-1"},
            ],
        )
        self.assertEqual(len(board.works_on), 1)

    def test_a_line_to_a_project_that_is_gone_is_dropped(self) -> None:
        board = self.a_board(
            agents=[{"name": "One"}],
            works_on=[{"agent": "agent-1", "project": "project-9"}],
        )
        self.assertEqual(board.works_on, [])

    def test_one_folder_may_only_be_on_the_board_once(self) -> None:
        where = str(self.a_project())
        with self.assertRaises(swarm.SwarmError) as caught:
            self.a_board(projects=[{"path": where}, {"path": where}])
        self.assertIn("twice", str(caught.exception))


class HowMuchFitsOnIt(BoardTestCase):
    def test_more_agents_than_fit_are_left_off(self) -> None:
        board = self.a_board(
            agents=[{"name": f"Agent {n}"} for n in range(swarm.MOST_AGENTS + 6)])
        self.assertEqual(len(board.agents), swarm.MOST_AGENTS)

    def test_more_jobs_than_fit_are_left_off(self) -> None:
        board = self.a_board(projects=[{
            "path": str(self.a_project()),
            "tasks": [f"Job {n}" for n in range(swarm.MOST_TASKS + 6)],
        }])
        self.assertEqual(len(board.projects[0].tasks), swarm.MOST_TASKS)

    def test_a_box_cannot_be_dragged_off_the_board(self) -> None:
        """A box at minus four thousand is a box nobody can find again."""

        board = self.a_board(agents=[{"name": "One", "at": {"x": -9000, "y": 99999}}])
        self.assertEqual(board.agents[0].at, {"x": 0, "y": 4000})

    def test_a_place_that_is_not_a_number_falls_back(self) -> None:
        board = self.a_board(agents=[{"name": "One", "at": {"x": "over there"}}])
        self.assertEqual(board.agents[0].at, {"x": 40, "y": 40})

    def test_boxes_with_no_place_do_not_land_on_top_of_each_other(self) -> None:
        """Given one spot, a board written anywhere but the panel stacked every
        box exactly on top of the last, and what you saw was one box with the
        others hidden underneath it."""

        board = self.a_board(agents=[{"name": f"Agent {n}"} for n in range(6)])
        places = [(one.at["x"], one.at["y"]) for one in board.agents]
        self.assertEqual(len(set(places)), len(places), places)

    def test_projects_with_no_place_sit_below_the_agents(self) -> None:
        board = self.a_board(
            agents=[{"name": "One"}],
            projects=[
                {"path": str(self.a_project("alpha"))},
                {"path": str(self.a_project("beta"))},
            ],
        )
        self.assertEqual(board.agents[0].at["y"], 40)
        self.assertEqual([one.at["y"] for one in board.projects], [320, 320])
        self.assertNotEqual(board.projects[0].at["x"], board.projects[1].at["x"])


class WhatIsNotReady(BoardTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.where = self.a_project()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.where, [], {})

    def test_an_empty_board_says_to_add_an_agent(self) -> None:
        said = swarm.what_is_not_ready(self.config)
        self.assertIn("no agents yet", " ".join(said))

    def test_a_machine_with_no_named_assistant_is_told_where_to_go(self) -> None:
        """The one route with no name of its own is not offered on a board, so
        somebody who has only that one has to be sent somewhere useful."""

        said = " ".join(swarm.what_is_not_ready(self.config))
        self.assertIn("Open Your team", said)

    def test_an_agent_pointed_at_nobody_is_not_called_ready(self) -> None:
        """An empty choice used to match the route with no name, so an agent
        nobody had set up read as ready and quietly used whatever this project
        happened to use."""

        self.a_board(agents=[{"name": "Nobody home"}])
        agent = swarm.how_it_stands(self.config)["board"]["agents"][0]
        self.assertFalse(agent["ready"])
        self.assertIn("Nothing is chosen", agent["why_not"])

    def test_an_agent_with_no_assistant_is_named(self) -> None:
        self.a_board(agents=[{"name": "Nobody home"}])
        said = " ".join(swarm.what_is_not_ready(self.config))
        self.assertIn("Nobody home", said)

    def test_a_project_nobody_works_on_is_named(self) -> None:
        self.a_board(
            agents=[{"name": "One"}],
            projects=[{"path": str(self.where), "tasks": ["Do it"]}],
        )
        said = " ".join(swarm.what_is_not_ready(self.config))
        self.assertIn("Nobody works on", said)

    def test_a_project_with_nothing_to_do_is_named(self) -> None:
        self.a_board(
            agents=[{"name": "One"}],
            projects=[{"path": str(self.where)}],
            works_on=[{"agent": "agent-1", "project": "project-1"}],
        )
        said = " ".join(swarm.what_is_not_ready(self.config))
        self.assertIn("no jobs written down", said)

    def test_a_folder_that_is_gone_is_named(self) -> None:
        self.a_board(projects=[{"path": str(self.root / "nowhere")}])
        said = " ".join(swarm.what_is_not_ready(self.config))
        self.assertIn("not a folder on this machine", said)

    def test_it_can_be_told_what_was_already_read(self) -> None:
        """So one look at the board does not read the whole thing twice."""

        self.a_board(agents=[{"name": "One"}])
        stands = swarm.how_it_stands(self.config)
        self.assertEqual(
            swarm.what_is_not_ready(self.config, stands),
            swarm.what_is_not_ready(self.config),
        )


class AskingForOneAgent(BoardTestCase):
    def test_one_that_is_there(self) -> None:
        board = self.a_board(agents=[{"name": "One"}, {"name": "Two"}])
        self.assertEqual(swarm.the_agent(board, "agent-2").name, "Two")

    def test_one_that_is_gone_says_so_plainly(self) -> None:
        """A panel left open while the board changed in another window."""

        board = self.a_board(agents=[{"name": "One"}])
        with self.assertRaises(swarm.SwarmError) as caught:
            swarm.the_agent(board, "agent-9")
        self.assertIn("Refresh the board", str(caught.exception))


class SettingThemGoing(BoardTestCase):
    """The part that acts on the board.

    No assistant is really asked here. What is being held down is the shape of
    it: who gets asked, in what order, who is shown whose notes, and that a run
    which cannot finish still finishes.
    """

    def setUp(self) -> None:
        super().setUp()
        self.where = self.a_project()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.where, [], {})
        self.asked: list[tuple[str, str, str]] = []

        def instead(config, route, text, filed_as=""):
            self.asked.append((route, filed_as, text))
            return {"answer": {"who": "them", "text": f"{filed_as} says so", "at": ""}}

        patched = mock.patch.object(chat, "say", instead)
        patched.start()
        self.addCleanup(patched.stop)
        # One assistant that can really be reached, so the run has something to
        # do on any machine, including one with nothing installed.
        ready = mock.patch.object(chat, "who_can_talk", lambda config: [
            {"route": "claude", "label": "Claude", "ready": True,
             "why_not": "", "how_to_fix_it": ""},
        ])
        ready.start()
        self.addCleanup(ready.stop)

    def a_working_board(self, talks: bool = True) -> None:
        self.a_board(
            agents=[
                {"name": "The reviewer", "who": "claude", "job": "Reads it"},
                {"name": "The writer", "who": "claude"},
            ],
            projects=[{"path": str(self.where), "tasks": ["Make it pass"]}],
            works_on=[
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
            talks_to=[{"one": "agent-1", "other": "agent-2"}] if talks else [],
        )

    def a_run(self) -> dict:
        running = swarm.Running()
        running.start(self.config)
        running.wait(20)
        return running.how_it_is_going()

    def test_nothing_to_do_is_refused_at_the_press(self) -> None:
        """Rather than after the first assistant has already been asked."""

        with self.assertRaises(swarm.SwarmError) as caught:
            swarm.Running().start(self.config)
        self.assertIn("nothing to set going", str(caught.exception))

    def test_everybody_is_asked_on_their_own_first(self) -> None:
        """The whole point of two assistants. One that read the other first is
        not a second opinion."""

        self.a_working_board()
        doing = self.a_run()
        rounds = [one["round"] for one in doing["turns"]]
        self.assertEqual(rounds[:2], [swarm.ON_ITS_OWN, swarm.ON_ITS_OWN])
        self.assertEqual(rounds[2:], [swarm.AFTER_THE_OTHERS, swarm.AFTER_THE_OTHERS])

    def test_each_agent_is_asked_under_its_own_name(self) -> None:
        self.a_working_board()
        self.a_run()
        self.assertEqual(
            [filed for _route, filed, _text in self.asked][:2],
            ["The reviewer", "The writer"],
        )

    def test_the_jobs_and_the_folder_go_with_the_asking(self) -> None:
        self.a_working_board()
        self.a_run()
        first = self.asked[0][2]
        self.assertIn("Make it pass", first)
        self.assertIn(str(self.where), first)
        self.assertIn("Reads it", first)

    def test_the_second_round_shows_what_the_other_said(self) -> None:
        self.a_working_board()
        self.a_run()
        later = self.asked[2][2]
        self.assertIn("The writer says so", later)
        self.assertNotIn("The reviewer says so", later)

    def test_nobody_is_shown_anything_when_no_line_was_drawn(self) -> None:
        """Two agents that should not know about each other are two agents that
        will not hear from each other."""

        self.a_working_board(talks=False)
        doing = self.a_run()
        self.assertEqual(len(doing["turns"]), 2)
        self.assertEqual(len(self.asked), 2)

    def test_a_project_with_no_jobs_is_left_alone(self) -> None:
        self.a_board(
            agents=[{"name": "The reviewer", "who": "claude"}],
            projects=[{"path": str(self.where)}],
            works_on=[{"agent": "agent-1", "project": "project-1"}],
        )
        with self.assertRaises(swarm.SwarmError):
            swarm.Running().start(self.config)

    def test_an_agent_nobody_set_up_is_never_asked(self) -> None:
        self.a_board(
            agents=[
                {"name": "The reviewer", "who": "claude"},
                {"name": "Nobody home"},
            ],
            projects=[{"path": str(self.where), "tasks": ["Do it"]}],
            works_on=[
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
        )
        doing = self.a_run()
        self.assertEqual([one["name"] for one in doing["turns"]], ["The reviewer"])

    def test_one_that_will_not_answer_does_not_stop_the_rest(self) -> None:
        self.a_working_board(talks=False)
        answers = iter([
            swarm.SwarmError("claude was asked and did not answer"),
            {"answer": {"text": "the second one still ran"}},
        ])

        def instead(config, route, text, filed_as=""):
            said = next(answers)
            if isinstance(said, Exception):
                raise said
            return said

        with mock.patch.object(chat, "say", instead):
            doing = self.a_run()
        self.assertEqual(
            [one["state"] for one in doing["turns"]], ["went wrong", "done"])
        self.assertIn("did not answer", doing["turns"][0]["why_not"])

    def test_a_run_that_falls_over_still_stops_going(self) -> None:
        """One that says it is still going refuses every later press, and the
        only way out would be restarting the panel."""

        self.a_working_board()
        with mock.patch.object(
            swarm.Running, "_do_it", side_effect=RuntimeError("nobody expected this")
        ):
            running = swarm.Running()
            running.start(self.config)
            running.wait(20)
        self.assertFalse(running.busy)
        self.assertIn("nobody expected", running.how_it_is_going()["note"])

    def test_two_runs_at_once_are_refused(self) -> None:
        self.a_working_board()
        held = threading.Event()

        def instead(config, route, text, filed_as=""):
            held.wait(10)
            return {"answer": {"text": "at last"}}

        with mock.patch.object(chat, "say", instead):
            running = swarm.Running()
            running.start(self.config)
            try:
                with self.assertRaises(swarm.SwarmError) as caught:
                    running.start(self.config)
                self.assertIn("already going", str(caught.exception))
            finally:
                held.set()
                running.wait(20)

    def test_stopping_leaves_the_rest_unasked_and_says_so(self) -> None:
        self.a_working_board()
        running = swarm.Running()

        def instead(config, route, text, filed_as=""):
            running.stop()
            return {"answer": {"text": "the only one asked"}}

        with mock.patch.object(chat, "say", instead):
            running.start(self.config)
            running.wait(20)
        doing = running.how_it_is_going()
        self.assertTrue(doing["stopped"])
        self.assertEqual(doing["turns"][0]["state"], "done")
        self.assertEqual(
            [one["state"] for one in doing["turns"][1:]], ["not done"] * 3)
        self.assertIn("Stopped", doing["note"])

    def test_a_board_that_is_going_may_not_be_changed(self) -> None:
        """An agent renamed halfway through would have what it says land in a
        conversation nothing points at any more."""

        self.a_working_board()
        held = threading.Event()

        def instead(config, route, text, filed_as=""):
            held.wait(10)
            return {"answer": {"text": "at last"}}

        with mock.patch.object(chat, "say", instead):
            running = swarm.Running()
            running.start(self.config)
            try:
                self.assertIn("cannot be changed", running.why_it_cannot_be_changed())
            finally:
                held.set()
                running.wait(20)
        self.assertEqual(running.why_it_cannot_be_changed(), "")

    def test_nothing_stops_a_board_being_changed_when_nothing_is_going(self) -> None:
        self.assertEqual(swarm.Running().why_it_cannot_be_changed(), "")

    def test_stopping_when_nothing_is_going_says_so_plainly(self) -> None:
        self.assertIn("Nothing is going", swarm.Running().stop())

    def test_nothing_has_been_run_yet_is_an_answer_too(self) -> None:
        self.assertIsNone(swarm.Running().how_it_is_going())


class WhatThePanelIsTold(BoardTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.where = self.a_project()
        config = load_config(self.where)
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        thread = threading.Thread(target=self.panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.panel.shutdown)

    def ask(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        asked = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": self.panel.token,
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(asked, timeout=15) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_an_empty_board_is_still_an_answer(self) -> None:
        status, said = self.ask("/api/swarm")
        self.assertEqual(status, 200)
        self.assertEqual(said["board"]["agents"], [])
        self.assertIn("agents", said["most"])
        self.assertIn("no agents yet", " ".join(said["what_is_not_ready"]))

    def test_saving_a_board_writes_it_down(self) -> None:
        status, said = self.ask("/api/swarm/save", {"board": {
            "agents": [{"name": "The reviewer", "who": "claude"}],
            "projects": [{"path": str(self.where), "tasks": ["Do it"]}],
            "works_on": [],
            "talks_to": [],
        }})
        self.assertEqual(status, 200)
        self.assertEqual(said["board"]["agents"][0]["name"], "The reviewer")
        self.assertEqual(swarm.load().agents[0].name, "The reviewer")

    def test_a_board_that_makes_no_sense_is_refused_and_says_why(self) -> None:
        status, said = self.ask(
            "/api/swarm/save", {"board": {"agents": [{"name": "a/b"}]}})
        self.assertEqual(status, 400)
        self.assertIn("not a name", said["error"])

    def test_the_board_is_left_as_it_was_when_a_save_is_refused(self) -> None:
        self.ask("/api/swarm/save", {"board": {"agents": [{"name": "Kept"}]}})
        self.ask("/api/swarm/save", {"board": {"agents": [{"name": "a/b"}]}})
        self.assertEqual([one.name for one in swarm.load().agents], ["Kept"])

    def test_reading_one_agents_own_conversation(self) -> None:
        self.ask("/api/swarm/save", {"board": {
            "agents": [{"name": "The reviewer", "who": "claude"}],
        }})
        status, said = self.ask("/api/swarm/said?agent=agent-1")
        self.assertEqual(status, 200)
        self.assertEqual(said["agent"]["name"], "The reviewer")
        self.assertEqual(said["said"], [])

    def test_asking_about_an_agent_that_is_gone(self) -> None:
        status, said = self.ask("/api/swarm/said?agent=agent-9")
        self.assertEqual(status, 400)
        self.assertIn("not on the board", said["error"])

    def test_talking_to_one_with_no_assistant_says_which_button_to_press(self) -> None:
        self.ask("/api/swarm/save", {"board": {"agents": [{"name": "Nobody home"}]}})
        status, said = self.ask(
            "/api/swarm/say", {"agent": "agent-1", "text": "hello"})
        self.assertEqual(status, 400)
        self.assertIn("no assistant chosen", said["error"])

    def test_talking_goes_to_the_agents_own_conversation(self) -> None:
        """Two agents on one assistant, and what one is told is not what the
        other reads back."""

        self.ask("/api/swarm/save", {"board": {"agents": [
            {"name": "The reviewer", "who": "claude"},
            {"name": "The writer", "who": "claude"},
        ]}})
        said = []

        def instead(config, route, text, filed_as=""):
            said.append((route, filed_as, text))
            return {"said": [{"who": "them", "text": "ok", "at": ""}]}

        with mock.patch.object(chat, "say", instead):
            status, _ = self.ask(
                "/api/swarm/say", {"agent": "agent-2", "text": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(said, [("claude", "The writer", "hello")])

    def test_starting_again_clears_only_that_agents_conversation(self) -> None:
        self.ask("/api/swarm/save", {"board": {"agents": [
            {"name": "The reviewer", "who": "claude"},
            {"name": "The writer", "who": "claude"},
        ]}})
        config = self.panel.config
        for name in ("The reviewer", "The writer"):
            kept = chat.where_it_is_kept(config, "claude", name)
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_text(json.dumps(
                [{"who": "you", "text": name, "at": ""}]), encoding="utf-8")
        status, _ = self.ask("/api/swarm/start-again", {"agent": "agent-1"})
        self.assertEqual(status, 200)
        self.assertEqual(chat.read_it(config, "claude", "The reviewer"), [])
        self.assertEqual(
            [one.text for one in chat.read_it(config, "claude", "The writer")],
            ["The writer"],
        )

    def test_nothing_has_been_set_going_yet(self) -> None:
        status, said = self.ask("/api/swarm/how-it-is-going")
        self.assertEqual(status, 200)
        self.assertIsNone(said["doing"])

    def test_setting_an_empty_board_going_is_refused_and_says_why(self) -> None:
        """No assistant is reached: the run is turned down before any of it
        starts, which is the only safe thing for a check to press."""

        status, said = self.ask("/api/swarm/start", {})
        self.assertEqual(status, 400)
        self.assertIn("nothing to set going", said["error"])

    def test_stopping_when_nothing_is_going_says_so(self) -> None:
        status, said = self.ask("/api/swarm/stop", {})
        self.assertEqual(status, 200)
        self.assertIn("Nothing is going", said["note"])

    def test_a_save_from_a_window_that_is_behind_is_refused(self) -> None:
        status, said = self.ask("/api/swarm/save", {"board": {
            "version": 0, "agents": [{"name": "First"}],
        }})
        self.assertEqual(status, 200)
        behind = said["board"]["version"] - 1
        status, said = self.ask("/api/swarm/save", {"board": {
            "version": behind, "agents": [{"name": "Second"}],
        }})
        self.assertEqual(status, 400)
        self.assertIn("another window", said["error"])
        self.assertEqual([one.name for one in swarm.load().agents], ["First"])

    def test_the_panel_is_told_when_the_board_cannot_be_changed(self) -> None:
        _status, said = self.ask("/api/swarm")
        self.assertEqual(said["cannot_be_changed"], "")

    def test_a_save_is_turned_down_while_the_board_is_going(self) -> None:
        """The panel greys the buttons out for the same reason. Both have to
        agree, so the server says no as well."""

        with mock.patch.object(
            type(self.panel.swarm_runner), "why_it_cannot_be_changed",
            lambda self: "The board is going, so it cannot be changed.",
        ):
            status, said = self.ask("/api/swarm/save", {"board": {"agents": []}})
        self.assertEqual(status, 400)
        self.assertIn("cannot be changed", said["error"])

    def test_the_board_needs_the_token_like_everything_else(self) -> None:
        """Nothing on the board is readable without it, the same as everything
        else the panel serves."""

        asked = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/swarm", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(asked, timeout=15)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("token", json.loads(caught.exception.read())["error"])


if __name__ == "__main__":
    unittest.main()
