"""Your team: who is here, what they do, and who hands work to whom.

The rule this holds down above every other: a job is only ever given to an
assistant this machine really has. A team naming a tool nobody installed is a
run that fails an hour later for a reason nobody can see now.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import seats as seats_lab
from our_harness import team
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


def a_seat(kind: str, route: str, label: str, *, ready: bool, why_not: str = "") -> seats_lab.Seat:
    return seats_lab.Seat(
        kind=kind,
        label=label,
        route=route,
        command=route,
        found_at=f"C:/tools/{route}.exe" if ready else "",
        version="1.2.3" if ready else "",
        ready=ready,
        why_not=why_not,
    )


class TeamTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def pretend(self, *seats: seats_lab.Seat):
        look = seats_lab.Look(seats=list(seats), settings_file=".harness/config.local.json")
        return mock.patch.object(seats_lab, "look", return_value=look)

    def both(self):
        return self.pretend(
            a_seat("claude-cli", "claude", "Claude command line", ready=True),
            a_seat("copilot-cli", "copilot", "GitHub Copilot command line", ready=True),
        )

    def only_one(self):
        return self.pretend(
            a_seat("claude-cli", "claude", "Claude command line", ready=True),
            a_seat(
                "copilot-cli", "copilot", "GitHub Copilot command line",
                ready=False, why_not="The copilot command is not on this machine.",
            ),
        )


class WhoIsHereTests(TeamTestCase):
    def test_it_says_who_is_ready_and_who_is_not(self) -> None:
        with self.only_one():
            who = team.who_is_here(self.config)
        by_route = {one["route"]: one for one in who["members"]}
        self.assertTrue(by_route["claude"]["ready"])
        self.assertFalse(by_route["copilot"]["ready"])
        self.assertIn("not on this machine", by_route["copilot"]["why_not"])
        self.assertEqual(who["how_many_ready"], 1)

    def test_team_data_does_not_expose_install_paths(self) -> None:
        private = a_seat("claude-cli", "claude", "Claude", ready=True)
        private.found_at = "C:/Users/private-name/AppData/Local/Claude/claude.exe"
        with self.pretend(private):
            who = team.who_is_here(self.config)
        shown = who["members"][0]
        self.assertEqual(shown["found_at"], "")
        self.assertNotIn("private-name", str(shown))

    def test_with_two_it_says_they_can_check_each_other(self) -> None:
        with self.both():
            who = team.who_is_here(self.config)
        self.assertEqual(who["how_many_ready"], 2)
        self.assertIn("check each other", who["note"])

    def test_with_one_it_says_a_second_is_worth_having(self) -> None:
        with self.only_one():
            who = team.who_is_here(self.config)
        self.assertIn("A second one is worth having", who["note"])

    def test_with_none_it_says_so_plainly(self) -> None:
        with self.pretend():
            who = team.who_is_here(self.config)
        self.assertEqual(who["members"], [])
        self.assertIn("No assistant was found", who["note"])

    def test_it_never_invents_somebody(self) -> None:
        # Everything the team view offers comes from the seat search, so the
        # two views can never disagree about what is installed.
        with self.only_one():
            who = team.who_is_here(self.config)
        self.assertEqual(sorted(one["route"] for one in who["members"]), ["claude", "copilot"])


class TheReadyMadeTeamTests(TeamTestCase):
    def test_two_assistants_means_the_writer_is_not_the_reviewer(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
        who = {node["id"]: node["config"]["provider_route"]
               for node in graph["nodes"] if node.get("config")}
        self.assertEqual(who["planner"], "claude")
        self.assertEqual(who["coder"], "copilot")
        self.assertEqual(who["reviewer"], "claude")
        self.assertNotEqual(who["coder"], who["reviewer"], "it would be reading back its own work")

    def test_one_assistant_still_makes_a_team_that_runs(self) -> None:
        with self.only_one():
            graph = team.a_starting_team(self.config)
            self.assertEqual(team.check_it(self.config, graph), [])
        routes = {node["config"]["provider_route"] for node in graph["nodes"] if node.get("config")}
        self.assertEqual(routes, {"claude"})

    def test_it_says_plainly_when_one_assistant_checks_its_own_work(self) -> None:
        with self.only_one():
            said = team.in_plain_words(team.a_starting_team(self.config))
        self.assertIn("read back its own work", said["note"])

    def test_the_ready_made_team_is_one_the_harness_could_really_run(self) -> None:
        with self.both():
            self.assertEqual(team.check_it(self.config, team.a_starting_team(self.config)), [])

    def test_the_work_goes_back_when_the_review_says_no(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
        back = [edge for edge in graph["edges"]
                if edge["source"] == "reviewer" and edge["target"] == "coder"]
        self.assertEqual(len(back), 1)
        self.assertIn("max_iterations", back[0]["loop"])
        self.assertGreater(back[0]["loop"]["max_iterations"], 0, "it could argue forever")


class InPlainWordsTests(TeamTestCase):
    def test_every_arrow_is_one_sentence(self) -> None:
        with self.both():
            said = team.in_plain_words(team.a_starting_team(self.config))
        self.assertEqual(len(said["hand_overs"]), 5)
        first = said["hand_overs"][0]
        self.assertEqual(first["who"], "The task")
        self.assertEqual(first["what"], "task")

    def test_an_arrow_with_a_condition_says_when(self) -> None:
        with self.both():
            said = team.in_plain_words(team.a_starting_team(self.config))
        only_when = [one for one in said["hand_overs"] if one["only_when"]]
        self.assertEqual(len(only_when), 1)
        self.assertIn("review_passed", only_when[0]["only_when"])

    def test_nonsense_does_not_take_it_down(self) -> None:
        for bad in (None, "team", 12, [], {"nodes": "no"}):
            with self.subTest(bad=bad):
                said = team.in_plain_words(bad)
                self.assertEqual(said.get("hand_overs", []), [])


class WhatIsInTheWayTests(TeamTestCase):
    def test_a_custom_prompt_beyond_the_old_limit_is_kept_exactly(self) -> None:
        prompt = "  " + ("work carefully 🧭\n" * 3_000) + "  "
        self.assertGreater(len(prompt), 4_000)
        checked = team.check_a_custom_member({
            "label": "Planner", "job": "planner", "asking": "set-prompt",
            "prompt": prompt,
        })
        self.assertEqual(checked["prompt"], prompt)

    def test_an_over_limit_custom_prompt_is_rejected_without_mutation(self) -> None:
        prompt = "x" * 100_001
        given = {
            "label": "Planner", "job": "planner", "asking": "set-prompt",
            "prompt": prompt,
        }
        with self.assertRaises(team.TeamError) as caught:
            team.check_a_custom_member(given)
        self.assertIn("100,001", str(caught.exception))
        self.assertIn("100,000", str(caught.exception))
        self.assertIn("did not truncate", str(caught.exception))
        self.assertEqual(given["prompt"], prompt)

    def test_a_job_given_to_nobody_is_named(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
            graph["nodes"][1]["config"] = {}
            problems = team.check_it(self.config, graph)
        self.assertTrue(any("nobody doing it" in one for one in problems), problems)

    def test_a_job_given_to_a_tool_nobody_has_is_named(self) -> None:
        with self.only_one():
            graph = team.a_starting_team(self.config)
            graph["nodes"][2]["config"] = {"provider_route": "copilot"}
            problems = team.check_it(self.config, graph)
        self.assertTrue(any("not ready yet" in one for one in problems), problems)

    def test_a_job_given_to_something_that_does_not_exist_is_named(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
            graph["nodes"][2]["config"] = {"provider_route": "made-up"}
            problems = team.check_it(self.config, graph)
        self.assertTrue(any("made-up" in one for one in problems), problems)

    def test_too_many_on_one_team_is_refused(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
            for number in range(team.MOST_MEMBERS + 1):
                graph["nodes"].append({
                    "id": f"extra-{number}", "type": "coder", "label": f"Extra {number}",
                    "config": {"provider_route": "claude"},
                })
            problems = team.check_it(self.config, graph)
        self.assertTrue(any("stops being a picture" in one for one in problems), problems)

    def test_something_that_is_not_a_team_at_all(self) -> None:
        with self.both():
            self.assertTrue(team.check_it(self.config, "a team"))


class KeepingThemTests(TeamTestCase):
    def test_a_team_is_saved_read_back_and_removed(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
            saved = team.save_team(self.config, "Two seats", graph)
            self.assertEqual(saved["name"], "Two seats")
            self.assertTrue(saved["valid"])
            self.assertEqual([one["name"] for one in team.teams(self.config)], ["Two seats"])
            read = team.load_team(self.config, "Two seats")
            self.assertEqual(len(read["plain"]["hand_overs"]), 5)
            team.remove_team(self.config, "Two seats")
            self.assertEqual(team.teams(self.config), [])

    def test_a_team_that_could_not_run_is_never_saved(self) -> None:
        with self.only_one():
            graph = team.a_starting_team(self.config)
            graph["nodes"][2]["config"] = {"provider_route": "copilot"}
            with self.assertRaises(team.TeamError) as caught:
                team.save_team(self.config, "Broken", graph)
        self.assertIn("cannot be saved", str(caught.exception))
        self.assertEqual(team.teams(self.config), [])

    def test_renaming_moves_a_team_rather_than_copying_it(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
            team.save_team(self.config, "First name", graph)
            team.save_team(self.config, "Second name", graph, was="First name")
            self.assertEqual([one["name"] for one in team.teams(self.config)], ["Second name"])

    def test_a_name_nothing_could_be_made_of_is_refused(self) -> None:
        with self.both():
            graph = team.a_starting_team(self.config)
            for bad in ("", "   ", "../escape", "a" * 80, "con", "team/name"):
                with self.subTest(bad=bad):
                    with self.assertRaises(Exception):
                        team.save_team(self.config, bad, graph)

    def test_a_rename_onto_another_team_is_refused(self) -> None:
        # Open one team, type the name of another, press Save. It used to write
        # over that other team and then take this one away: two teams became
        # one, and nobody was told.
        with self.both():
            graph = team.a_starting_team(self.config)
            other = team.a_starting_team(self.config)
            other["nodes"].append({
                "id": "theirs", "type": "coder", "label": "Something of theirs",
                "config": {"provider_route": "claude"},
            })
            other["edges"].append({
                "id": "e6", "source": "planner", "target": "theirs", "variables": ["plan"],
            })
            team.save_team(self.config, "Alpha", graph)
            team.save_team(self.config, "Beta", other)
            with self.assertRaises(team.TeamError) as caught:
                team.save_team(self.config, "Beta", graph, was="Alpha")
        self.assertIn("already a team called Beta", str(caught.exception))
        self.assertEqual(
            sorted(one["name"] for one in team.teams(self.config)), ["Alpha", "Beta"]
        )
        kept = team.load_team(self.config, "Beta")
        self.assertTrue(any(node["id"] == "theirs" for node in kept["graph"]["nodes"]))

    def test_changing_only_the_capital_letters_still_works(self) -> None:
        # "Alpha" and "ALPHA" share one file, so this is one team moving rather
        # than two teams existing. It used to be refused with a message telling
        # the person to do the thing they were already doing.
        with self.both():
            graph = team.a_starting_team(self.config)
            team.save_team(self.config, "Alpha", graph)
            saved = team.save_team(self.config, "ALPHA", graph, was="Alpha")
        self.assertEqual(saved["name"], "ALPHA")
        self.assertEqual([one["name"] for one in team.teams(self.config)], ["ALPHA"])

    def test_a_saved_team_that_can_no_longer_run_says_so_in_the_list(self) -> None:
        # Saved on a machine with two assistants, opened on a machine with one.
        # The picture is still well formed; the team still cannot run.
        with self.both():
            team.save_team(self.config, "Two seats", team.a_starting_team(self.config))
        with self.only_one():
            listed = team.teams(self.config)
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["valid"], "the list said it would run, and it would not")
        self.assertTrue(listed[0]["issues"])

    def test_settings_that_are_not_settings_do_not_take_the_view_down(self) -> None:
        # Everything here can arrive from a request, so nothing may assume its
        # own shape.
        for nonsense in (["a list"], "some words", 12, True):
            with self.subTest(nonsense=nonsense):
                graph = {
                    "schema_version": 2, "name": "Odd", "entry": "start",
                    "nodes": [
                        {"id": "start", "type": "start", "label": "Start"},
                        {"id": "coder", "type": "coder", "label": "Writes", "config": nonsense},
                    ],
                    "edges": [{"id": "e1", "source": "start", "target": "coder"}],
                }
                with self.both():
                    said = team.in_plain_words(graph)
                    problems = team.check_it(self.config, graph)
                self.assertTrue(said["members"])
                self.assertTrue(problems, "it should say the job has nobody doing it")

    def test_listing_many_teams_looks_at_the_machine_once(self) -> None:
        # Finding out who is here runs each assistant's own tool, and one of
        # those waiting on a sign-in can sit there for the best part of a
        # minute. Doing it once per saved team is how a view stops opening.
        with self.both():
            graph = team.a_starting_team(self.config)
            for number in range(6):
                team.save_team(self.config, f"Team {number}", graph)
        with self.both() as looked:
            listed = team.teams(self.config)
        self.assertEqual(len(listed), 6)
        self.assertEqual(looked.call_count, 1, "it looked once per team")

    def test_everything_the_view_needs_comes_in_one_answer(self) -> None:
        with self.both():
            all_of_it = team.everything(self.config)
        self.assertEqual(all_of_it["who"]["how_many_ready"], 2)
        self.assertEqual(len(all_of_it["jobs"]), len(team.JOBS))
        self.assertTrue(all_of_it["starting_team"]["nodes"])
        self.assertTrue(all_of_it["starting_plain"]["hand_overs"])
        self.assertEqual(all_of_it["teams"], [])


class TeamAccessibilityTests(unittest.TestCase):
    def test_light_sidebar_overrides_late_dark_local_model_styles(self) -> None:
        styles = (
            Path(__file__).parents[1] / "src" / "our_harness" / "ui" / "styles.css"
        ).read_text(encoding="utf-8")
        scoped = styles.index(".team-side .local-model-one {")
        generic = styles.index(".local-model-one {")
        self.assertGreater(scoped, generic, "the light-panel override must win the cascade")
        accessible = styles[scoped:]
        self.assertIn("color: #0f172a", accessible)
        self.assertIn("background: #fff", accessible)
        self.assertIn(".team-side .local-model-names button", styles)
        self.assertIn(".team-side .local-model-one.not-running { opacity: 1; }", styles)


if __name__ == "__main__":
    unittest.main()
