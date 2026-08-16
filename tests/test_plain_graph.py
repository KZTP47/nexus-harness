"""The workflow said in plain words.

The point of this piece is that the explanation cannot drift away from the
thing it explains, so most of what is checked here is that every part of the
story comes from the graph handed in, and nothing is invented.
"""

from __future__ import annotations

import json
import unittest
from importlib.resources import files

from our_harness import plain_graph


def shipped_workflow() -> dict:
    return json.loads(
        files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8")
    )


class TheStoryTests(unittest.TestCase):
    def test_every_node_gets_exactly_one_step(self) -> None:
        graph = shipped_workflow()
        told = plain_graph.story(graph)
        self.assertEqual(
            [stage.id for stage in told],
            sorted([stage.id for stage in told], key=lambda name: [s.id for s in told].index(name)),
        )
        self.assertEqual(len(told), len(graph["nodes"]))
        self.assertEqual({stage.id for stage in told}, {node["id"] for node in graph["nodes"]})

    def test_it_starts_where_the_work_starts_and_ends_where_it_ends(self) -> None:
        told = plain_graph.story(shipped_workflow())
        self.assertEqual(told[0].id, "start")
        self.assertEqual(told[-1].id, "end")

    def test_the_order_follows_the_arrows(self) -> None:
        told = plain_graph.story(shipped_workflow())
        order = [stage.id for stage in told]
        self.assertLess(order.index("planner"), order.index("coder"))
        self.assertLess(order.index("coder"), order.index("review"))

    def test_an_arrow_back_to_an_earlier_step_is_called_out(self) -> None:
        told = {stage.id: stage for stage in plain_graph.story(shipped_workflow())}
        self.assertEqual(told["unit"].goes_back_to, ["coder"])
        self.assertEqual(told["planner"].goes_back_to, [], "nothing goes back from the plan")

    def test_a_check_nobody_has_named_still_shows_up(self) -> None:
        # A word-for-word explanation would quietly drop a check it did not
        # recognise, which is the one case where being quiet is worst.
        graph = shipped_workflow()
        graph["nodes"].append({
            "id": "licences", "type": "tool", "label": "Licence Check",
            "config": {"role": "something_new"}, "position": {"x": 0, "y": 0},
        })
        graph["edges"].append({"source": "unit", "target": "licences"})
        told = {stage.id: stage for stage in plain_graph.story(graph)}
        self.assertIn("licences", told)
        self.assertEqual(told["licences"].title, "Licence Check")

    def test_a_node_nothing_points_at_is_still_told(self) -> None:
        graph = shipped_workflow()
        graph["nodes"].append({"id": "lonely", "type": "tool", "label": "On its own",
                               "position": {"x": 0, "y": 0}})
        told = [stage.id for stage in plain_graph.story(graph)]
        self.assertIn("lonely", told)

    def test_a_workflow_that_loops_does_not_go_round_for_ever(self) -> None:
        graph = {
            "nodes": [{"id": "a", "type": "start"}, {"id": "b", "type": "coder"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        }
        self.assertEqual([stage.id for stage in plain_graph.story(graph)], ["a", "b"])

    def test_rubbish_is_refused_quietly_rather_than_thrown(self) -> None:
        for value in (None, "workflow", 7, {"nodes": "none"}):
            with self.subTest(value=value):
                self.assertEqual(plain_graph.story(value), [])


class WhatTheScreenGetsTests(unittest.TestCase):
    def test_it_hands_over_everything_the_page_draws(self) -> None:
        said = plain_graph.in_plain_words(shipped_workflow())
        self.assertTrue(said["headline"])
        self.assertEqual(len(said["stages"]), 9)
        self.assertTrue(said["loops"])
        for line in said["loops"]:
            self.assertIn("goes back to", line)

    def test_the_lines_about_going_back_name_real_steps(self) -> None:
        said = plain_graph.in_plain_words(shipped_workflow())
        titles = set(said["titles"].values())
        for line in said["loops"]:
            after = line.split("goes back to: ", 1)[1]
            self.assertIn(after, titles)

    def test_nothing_to_show_says_so(self) -> None:
        said = plain_graph.in_plain_words({"nodes": [], "edges": []})
        self.assertEqual(said["stages"], [])
        self.assertIn("no workflow", said["headline"])


if __name__ == "__main__":
    unittest.main()
