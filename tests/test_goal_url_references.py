import tempfile
import unittest
from pathlib import Path

from our_harness import swarm_work


class GoalURLReferenceTests(unittest.TestCase):
    def test_url_inside_action_prose_does_not_break_project_tool_context(self):
        goal = (
            "Create a browser game. Use Three.js loaded from "
            "https://cdn.example/npm/three@0.180.0/build/three.module.js in index.html. "
            "Create game-core.cjs and game.test.js."
        )
        paths = swarm_work._goal_named_paths(goal)
        self.assertIn("index.html", paths)
        self.assertIn("game-core.cjs", paths)
        self.assertFalse(any("cdn.example" in path or "three.module.js" in path for path in paths))
        with tempfile.TemporaryDirectory() as temporary:
            contract = swarm_work._derive_requirement_contract(Path(temporary), goal)
            self.assertTrue(contract["requirements"])

    def test_website_goals_never_grant_local_authority_from_url_components(self):
        for goal in (
            "Create unit, API and E2E Playwright tests for this repo and website https://example.test/app/index.html",
            'Write tests for "the website https://example.test/app/index.html" in test_web.py',
            "Create checks for https://example.test/path/../private.py?name=secret.json and write test_web.py",
        ):
            with self.subTest(goal=goal):
                paths = swarm_work._goal_named_paths(goal)
                self.assertFalse(any("index.html" in path or "private.py" in path or "secret.json" in path for path in paths))

    def test_url_does_not_hide_unsafe_local_paths(self):
        # An unsafe spelling next to a URL is reported to the agents and never
        # grants anything, not even the part after "//" as a top-level file.
        for path in ("../private.py", "safe/../private.py", "tests/file.py:stream", "tests//unsafe.py"):
            for quote in ("", '"', "`"):
                with self.subTest(path=path, quote=quote):
                    goal = f"Use https://example.test/docs and create {quote}{path}{quote}"
                    self.assertEqual(swarm_work._goal_named_paths(goal), [])
                    with tempfile.TemporaryDirectory() as temporary:
                        spec = swarm_work._compile_goal_spec(Path(temporary), goal)
                    self.assertIn(path, spec["ignored_path_tokens"])
                    self.assertEqual(spec["write_policy"]["grants"], [])


if __name__ == "__main__":
    unittest.main()
