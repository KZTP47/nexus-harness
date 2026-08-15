from __future__ import annotations

import copy
import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from our_harness import workflows
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def built_in_graph() -> dict:
    return json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8"))


class NameTests(unittest.TestCase):
    def test_a_readable_name_is_kept_and_tidied(self) -> None:
        self.assertEqual(workflows.clean_name("  Careful   review "), "Careful review")

    def test_names_that_could_escape_the_folder_are_refused(self) -> None:
        for name in ("../escape", "a/b", "a\\b", "", "   ", ".", "-leading"):
            with self.subTest(name=name), self.assertRaises(HarnessError):
                workflows.clean_name(name)

    def test_a_very_long_name_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            workflows.clean_name("x" * 65)

    def test_a_name_that_is_not_text_is_refused(self) -> None:
        for value in (None, 5, ["a"]):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                workflows.clean_name(value)

    def test_two_names_that_look_alike_land_on_one_file(self) -> None:
        self.assertEqual(workflows.file_name("Quick fix"), "quick-fix.json")
        self.assertEqual(workflows.file_name("quick   FIX"), "quick-fix.json")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.graph = built_in_graph()
        self.addCleanup(self.temporary.cleanup)

    def test_nothing_is_saved_to_begin_with(self) -> None:
        self.assertEqual(workflows.listed(self.config), [])

    def test_saving_then_loading_gives_the_same_workflow_back(self) -> None:
        saved = workflows.save(self.config, "Careful review", self.graph)
        self.assertEqual(saved.name, "Careful review")
        self.assertTrue(saved.valid)
        self.assertGreater(saved.nodes, 0)
        loaded = workflows.load(self.config, "Careful review")
        self.assertEqual(loaded.graph, saved.graph)

    def test_several_workflows_are_listed_in_name_order(self) -> None:
        for name in ("Quick fix", "Careful review", "another one"):
            workflows.save(self.config, name, self.graph)
        self.assertEqual(
            [item.name for item in workflows.listed(self.config)],
            ["another one", "Careful review", "Quick fix"],
        )

    def test_saving_twice_with_one_name_replaces_rather_than_duplicates(self) -> None:
        workflows.save(self.config, "One", self.graph)
        workflows.save(self.config, "One", self.graph)
        self.assertEqual(len(workflows.listed(self.config)), 1)

    def test_a_workflow_the_harness_could_not_run_is_never_saved(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            workflows.save(self.config, "Broken", {"nodes": [], "edges": []})
        self.assertIn("could not run it", str(caught.exception))
        self.assertEqual(workflows.listed(self.config), [])

    def test_something_that_is_not_a_workflow_is_refused(self) -> None:
        for value in ("a string", 5, None, ["nodes"]):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                workflows.save(self.config, "X", value)

    def test_loading_one_that_is_not_there_names_the_ones_that_are(self) -> None:
        workflows.save(self.config, "Quick fix", self.graph)
        with self.assertRaises(HarnessError) as caught:
            workflows.load(self.config, "Missing one")
        message = str(caught.exception)
        self.assertIn("no workflow named Missing one", message)
        self.assertIn("Quick fix", message)

    def test_deleting_removes_it_and_says_so_when_it_is_not_there(self) -> None:
        workflows.save(self.config, "Quick fix", self.graph)
        self.assertEqual(workflows.delete(self.config, "Quick fix"), "Quick fix")
        self.assertEqual(workflows.listed(self.config), [])
        with self.assertRaises(HarnessError):
            workflows.delete(self.config, "Quick fix")

    def test_renaming_keeps_the_workflow_and_drops_the_old_name(self) -> None:
        workflows.save(self.config, "Old name", self.graph)
        renamed = workflows.rename(self.config, "Old name", "New name")
        self.assertEqual(renamed.name, "New name")
        self.assertEqual([item.name for item in workflows.listed(self.config)], ["New name"])
        self.assertEqual(workflows.load(self.config, "New name").nodes, renamed.nodes)

    def test_renaming_onto_a_name_already_taken_is_refused(self) -> None:
        workflows.save(self.config, "One", self.graph)
        workflows.save(self.config, "Two", self.graph)
        with self.assertRaises(HarnessError):
            workflows.rename(self.config, "One", "Two")
        self.assertEqual(len(workflows.listed(self.config)), 2)

    def test_a_damaged_file_is_reported_and_does_not_hide_the_others(self) -> None:
        workflows.save(self.config, "Good one", self.graph)
        (self.root / ".harness" / "workflows" / "broken.json").write_text("not json", encoding="utf-8")
        found = {item.name: item for item in workflows.listed(self.config)}
        self.assertIn("Good one", found)
        self.assertTrue(found["Good one"].valid)
        self.assertIn("broken", found)
        self.assertFalse(found["broken"].valid)
        self.assertTrue(found["broken"].issues)

    def test_a_file_larger_than_the_limit_is_refused(self) -> None:
        path = self.root / ".harness" / "workflows"
        path.mkdir(parents=True, exist_ok=True)
        (path / "huge.json").write_text("x" * (workflows.MAX_WORKFLOW_BYTES + 1), encoding="utf-8")
        found = {item.name: item for item in workflows.listed(self.config)}
        self.assertFalse(found["huge"].valid)
        self.assertIn("larger than", found["huge"].issues[0])

    def test_the_number_of_workflows_is_capped(self) -> None:
        config = self.config
        for index in range(workflows.MAX_WORKFLOWS):
            workflows.save(config, f"Flow {index}", self.graph)
        with self.assertRaises(HarnessError) as caught:
            workflows.save(config, "One too many", self.graph)
        self.assertIn("which is the limit", str(caught.exception))

    def test_a_saved_workflow_holds_its_name_and_when_it_was_saved(self) -> None:
        workflows.save(self.config, "Quick fix", self.graph)
        body = json.loads((self.root / ".harness" / "workflows" / "quick-fix.json").read_text(encoding="utf-8"))
        self.assertEqual(body["name"], "Quick fix")
        self.assertTrue(body["saved_at"].endswith("Z"))
        self.assertIn("nodes", body["graph"])

    def test_the_listing_leaves_the_graph_out_until_it_is_asked_for(self) -> None:
        saved = workflows.save(self.config, "Quick fix", self.graph)
        self.assertNotIn("graph", saved.to_dict())
        self.assertIn("graph", saved.to_dict(include_graph=True))


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        self.config = LoadedConfig(data, self.root, [], {})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, body: dict | None = None, token: bool = True):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json"}
        if token:
            headers["X-Harness-Token"] = self.server.token
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            answer = connection.getresponse()
            return answer.status, json.loads(answer.read() or b"{}")
        finally:
            connection.close()

    def test_the_panel_can_save_list_open_rename_and_delete(self) -> None:
        graph = built_in_graph()
        status, body = self.call("GET", "/api/workflows")
        self.assertEqual((status, body["workflows"]), (200, []))

        status, body = self.call("POST", "/api/workflows/save", {"name": "Careful review", "graph": graph})
        self.assertEqual(status, 200)
        self.assertEqual(body["saved"]["name"], "Careful review")

        status, body = self.call("GET", "/api/workflows")
        self.assertEqual([item["name"] for item in body["workflows"]], ["Careful review"])

        status, body = self.call("GET", "/api/workflows?name=Careful%20review")
        self.assertEqual(status, 200)
        self.assertIn("nodes", body["workflow"]["graph"])

        status, body = self.call("POST", "/api/workflows/rename", {"name": "Careful review", "new_name": "Slow and sure"})
        self.assertEqual(body["saved"]["name"], "Slow and sure")

        status, body = self.call("POST", "/api/workflows/delete", {"name": "Slow and sure"})
        self.assertEqual(body["deleted"], "Slow and sure")
        status, body = self.call("GET", "/api/workflows")
        self.assertEqual(body["workflows"], [])

    def test_a_workflow_that_would_not_run_is_refused_by_the_panel(self) -> None:
        status, body = self.call("POST", "/api/workflows/save", {"name": "Broken", "graph": {"nodes": []}})
        self.assertEqual(status, 400)
        self.assertIn("could not run it", body["error"])

    def test_a_name_that_could_escape_the_folder_is_refused(self) -> None:
        status, body = self.call("POST", "/api/workflows/save", {"name": "../escape", "graph": built_in_graph()})
        self.assertEqual(status, 400)
        self.assertIn("workflow name", body["error"])

    def test_every_workflow_call_needs_the_session_token(self) -> None:
        for method, path in (
            ("GET", "/api/workflows"),
            ("POST", "/api/workflows/save"),
            ("POST", "/api/workflows/delete"),
            ("POST", "/api/workflows/rename"),
        ):
            with self.subTest(path=path):
                status, _body = self.call(method, path, {} if method == "POST" else None, token=False)
                self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()


class RenameSafetyTests(unittest.TestCase):
    """A rename must leave one copy, never two and never none."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        workflows.save(self.config, "Old name", built_in_graph())
        self.addCleanup(self.temporary.cleanup)

    def test_a_rename_that_cannot_remove_the_old_file_changes_nothing(self) -> None:
        from unittest import mock

        with mock.patch.object(workflows, "delete", side_effect=OSError("in use")):
            with self.assertRaises(HarnessError) as caught:
                workflows.rename(self.config, "Old name", "New name")
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual([item.name for item in workflows.listed(self.config)], ["Old name"])

    def test_changing_only_the_spelling_keeps_one_file(self) -> None:
        renamed = workflows.rename(self.config, "Old name", "OLD NAME")
        self.assertEqual(renamed.name, "OLD NAME")
        self.assertEqual([item.name for item in workflows.listed(self.config)], ["OLD NAME"])
        self.assertEqual(len(list((self.root / ".harness" / "workflows").glob("*.json"))), 1)
