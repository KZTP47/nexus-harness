"""Running a pipeline through the panel, not through the library.

The pipeline tests drive the engine directly. That leaves the part between the
button and the engine untested: the lock that allows one run at a time, the
thread it runs on, and what happens when a run falls over. A stuck lock there
means every later press is refused until the panel is restarted, and nobody is
ever told why, so it is worth testing at the door rather than only inside.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import pipelines, server
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class PanelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_temporary.cleanup)
        prior_runtime = os.environ.get("OUR_HARNESS_PIPELINE_RUN_DIR")
        os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = self.runtime_temporary.name
        self.addCleanup(
            lambda: os.environ.pop("OUR_HARNESS_PIPELINE_RUN_DIR", None)
            if prior_runtime is None
            else os.environ.__setitem__("OUR_HARNESS_PIPELINE_RUN_DIR", prior_runtime)
        )
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        thread = threading.Thread(target=self.panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.panel.shutdown)

    def ask(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": self.panel.token,
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def wait_until_free(self, seconds: float = 10.0) -> None:
        until = time.monotonic() + seconds
        while time.monotonic() < until and self.panel.pipeline_running:
            time.sleep(0.05)


class RunningOneThroughThePanelTests(PanelTestCase):
    def test_a_run_starts_and_the_panel_says_what_happened(self) -> None:
        drawn = {
            "name": "Small", "edges": [],
            "nodes": [{"id": "start", "kind": "start", "label": "Start", "settings": {}}],
        }
        status, said = self.ask("/api/pipelines/run", {"pipeline": drawn})
        self.assertEqual(status, 202)
        self.assertEqual(said["name"], "Small")
        self.wait_until_free()
        self.assertTrue(self.panel.pipeline_run["passed"], self.panel.pipeline_run)

    def test_agent_follows_the_exact_accepted_run_and_its_event_cursor(self) -> None:
        drawn = {
            "name": "Agent saved", "edges": [],
            "nodes": [{"id": "start", "kind": "start", "label": "Start", "settings": {}}],
        }
        self.assertEqual(self.ask("/api/pipelines/save", {"pipeline": drawn})[0], 200)
        status, accepted = self.ask(
            "/api/pipelines/agent-run",
            {"automation": "Agent saved", "request_id": "agent-request-1"},
        )
        self.assertEqual(status, 202)
        run_id = accepted["run_id"]
        self.assertTrue(run_id)
        self.wait_until_free()
        status, exact = self.ask(f"/api/pipeline-runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(exact["run_id"], run_id)
        self.assertFalse(exact["running"])
        self.assertTrue(exact["result"]["passed"])
        status, events = self.ask(f"/api/pipeline-runs/{run_id}/events?after=0")
        self.assertEqual(status, 200)
        self.assertTrue(events["events"])
        self.assertTrue(all(one["run_id"] == run_id for one in events["events"]))
        stale, _ = self.ask("/api/pipelines/stop", {"run_id": "not-the-agent-run"})
        self.assertEqual(stale, 400)

    def test_only_one_runs_at_a_time(self) -> None:
        holding = threading.Event()

        def slow(config, pipeline, **kwargs):
            holding.wait(5)
            return pipelines.Run(name="Slow", passed=True, said="done")

        drawn = {
            "name": "Slow", "edges": [],
            "nodes": [{"id": "start", "kind": "start", "label": "Start", "settings": {}}],
        }
        with mock.patch.object(pipelines, "run_it", slow):
            first, _ = self.ask("/api/pipelines/run", {"pipeline": drawn})
            self.assertEqual(first, 202)
            second, said = self.ask("/api/pipelines/run", {"pipeline": drawn})
            self.assertEqual(second, 400)
            self.assertIn("running already", said["error"])
            holding.set()
            self.wait_until_free()
        # And once it is over, the next press works.
        again, _ = self.ask("/api/pipelines/run", {"pipeline": drawn})
        self.assertEqual(again, 202)
        self.wait_until_free()

    def test_same_request_replays_while_its_worker_is_still_running(self) -> None:
        holding = threading.Event()

        def slow(config, pipeline, **kwargs):
            holding.wait(5)
            return pipelines.Run(name="Replay", passed=True, said="done")

        drawn = {
            "name": "Replay", "edges": [],
            "nodes": [{"id": "start", "kind": "start", "label": "Start", "settings": {}}],
        }
        with mock.patch.object(pipelines, "run_it", slow):
            first_status, first = self.ask(
                "/api/pipelines/run", {"pipeline": drawn, "request_id": "same-panel-request"}
            )
            replay_status, replay = self.ask(
                "/api/pipelines/run", {"pipeline": drawn, "request_id": "same-panel-request"}
            )
            self.assertEqual((first_status, replay_status), (202, 202))
            self.assertEqual(replay["run_id"], first["run_id"])
            self.assertTrue(replay["replayed"])
            holding.set()
            self.wait_until_free()

    def test_completed_latest_run_reconciles_an_ambiguous_post_by_request_id(self) -> None:
        drawn = {
            "name": "Reconcile", "edges": [],
            "nodes": [{"id": "start", "kind": "start", "label": "Start", "settings": {}}],
        }
        request_id = "ambiguous-post-1"
        status, accepted = self.ask(
            "/api/pipelines/run", {"pipeline": drawn, "request_id": request_id}
        )
        self.assertEqual(status, 202)
        self.wait_until_free()
        status, projection = self.ask("/api/pipelines")
        self.assertEqual(status, 200)
        latest = projection["latest_run"]
        self.assertEqual(latest["request_id"], request_id)
        self.assertEqual(latest["run_id"], accepted["run_id"])
        self.assertFalse(latest["running"])
        self.assertIn("definition", latest)
        self.assertNotIn("attempt_id", latest)
        status, found = self.ask(
            f"/api/pipeline-runs/by-request?request_id={request_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(found["run_id"], accepted["run_id"])
        self.assertEqual(found["project_authority_id"], projection["project_authority_id"])
        status, exact = self.ask(f"/api/pipeline-runs/{found['run_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(exact["project_authority_id"], found["project_authority_id"])

    def test_request_lookup_cannot_cross_project_authorities(self) -> None:
        drawn = {
            "name": "Scoped", "edges": [],
            "nodes": [{"id": "start", "kind": "start", "label": "Start", "settings": {}}],
        }
        request_id = "same-words-different-authority"
        status, accepted = self.ask(
            "/api/pipelines/run", {"pipeline": drawn, "request_id": request_id}
        )
        self.assertEqual(status, 202)
        self.wait_until_free()

        other_root = self.root / "other-project"
        (other_root / ".harness").mkdir(parents=True)
        other = server.HarnessHTTPServer(
            ("127.0.0.1", 0), LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), other_root, [], {})
        )
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{other.server_address[1]}/api/pipeline-runs/by-request"
                f"?request_id={request_id}",
                headers={"X-Harness-Token": other.token},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(caught.exception.code, 400)
            status, found = self.ask(
                f"/api/pipeline-runs/by-request?request_id={request_id}"
            )
            self.assertEqual(status, 200)
            self.assertEqual(found["run_id"], accepted["run_id"])
            self.assertNotEqual(found["project_authority_id"], other.pipeline_store.authority_id)
        finally:
            other.shutdown()
            other.server_close()

    def test_a_run_that_falls_over_lets_go_of_the_lock(self) -> None:
        # The one that would hurt most: a run that throws leaving the panel
        # convinced a pipeline is still going, for ever.
        def explodes(config, pipeline, **kwargs):
            raise RuntimeError("something nobody expected")

        drawn = {
            "name": "Doomed", "edges": [],
            "nodes": [{"id": "start", "kind": "start", "label": "Start", "settings": {}}],
        }
        with mock.patch.object(pipelines, "run_it", explodes):
            status, _ = self.ask("/api/pipelines/run", {"pipeline": drawn})
            self.assertEqual(status, 202)
            self.wait_until_free()
        self.assertFalse(self.panel.pipeline_running, "it let go of the lock")
        self.assertFalse(self.panel.pipeline_run["passed"])
        self.assertIn("something nobody expected", self.panel.pipeline_run["said"])
        status, _ = self.ask("/api/pipelines/run", {"pipeline": drawn})
        self.assertEqual(status, 202, "the next press still works")
        self.wait_until_free()

    def test_a_drawing_it_refuses_never_takes_the_lock(self) -> None:
        status, said = self.ask("/api/pipelines/run", {"pipeline": {"nodes": "none"}})
        self.assertEqual(status, 400)
        self.assertTrue(said["error"])
        self.assertFalse(self.panel.pipeline_running)

    def test_stopping_asks_the_run_to_stop(self) -> None:
        status, said = self.ask("/api/pipelines/stop", {})
        self.assertEqual(status, 200)
        self.assertIn("stop", said["note"])
        self.assertTrue(self.panel.pipeline_stop)


class KeepingThemThroughThePanelTests(PanelTestCase):
    def test_creating_one_saves_and_lists_a_blank_automation_immediately(self) -> None:
        status, said = self.ask("/api/pipelines/create", {"name": "Fresh automation"})
        self.assertEqual(status, 200)
        self.assertEqual(said["pipeline"], {
            "name": "Fresh automation", "nodes": [], "edges": [],
        })
        self.assertIn("Fresh automation", said["saved"])

        status, duplicate = self.ask("/api/pipelines/create", {"name": "Fresh automation"})
        self.assertEqual(status, 400)
        self.assertIn("already an automation", duplicate["error"])

    def test_saving_listing_loading_and_removing(self) -> None:
        drawn = pipelines.a_starting_pipeline()
        status, said = self.ask("/api/pipelines/save", {"pipeline": drawn})
        self.assertEqual(status, 200)
        self.assertEqual(said["pipeline"]["name"], "First pipeline")

        status, listed = self.ask("/api/pipelines")
        self.assertEqual(status, 200)
        self.assertIn("First pipeline", listed["saved"])
        self.assertTrue(listed["kinds"], "the panel is told which steps exist")

        status, opened = self.ask("/api/pipelines?name=First%20pipeline")
        self.assertEqual(status, 200)
        self.assertEqual(len(opened["pipeline"]["nodes"]), len(drawn["nodes"]))

        status, gone = self.ask("/api/pipelines/delete", {"name": "First pipeline"})
        self.assertEqual(status, 200)
        self.assertEqual(gone["saved"], [])

    def test_asking_for_one_that_is_not_there_is_refused_plainly(self) -> None:
        status, said = self.ask("/api/pipelines?name=Nothing")
        self.assertEqual(status, 400)
        self.assertIn("no pipeline called", said["error"])

    def test_a_name_that_would_climb_out_of_the_folder_is_refused(self) -> None:
        for bad in ("../escape", "/etc/passwd", "a/b"):
            with self.subTest(bad=bad):
                status, _said = self.ask("/api/pipelines/delete", {"name": bad})
                self.assertEqual(status, 400)

    def test_checking_a_drawing_runs_none_of_it(self) -> None:
        ran: list[str] = []
        with mock.patch.object(pipelines, "run_it", lambda *a, **k: ran.append("ran")):
            status, said = self.ask(
                "/api/pipelines/check", {"pipeline": pipelines.a_starting_pipeline()}
            )
        self.assertEqual(status, 200)
        self.assertEqual(ran, [])
        self.assertEqual(len(said["pipeline"]["nodes"]), 6)

    def test_everything_here_needs_the_token(self) -> None:
        for path, body in (
            ("/api/pipelines", None),
            ("/api/pipelines/create", {"name": "Fresh automation"}),
            ("/api/pipelines/save", {"pipeline": pipelines.a_starting_pipeline()}),
            ("/api/pipelines/run", {"pipeline": pipelines.a_starting_pipeline()}),
            ("/api/pipelines/delete", {"name": "First pipeline"}),
        ):
            with self.subTest(path=path):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}{path}",
                    data=json.dumps(body).encode("utf-8") if body is not None else None,
                    headers={"Content-Type": "application/json"},
                    method="POST" if body is not None else "GET",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(caught.exception.code, 400)
        self.assertFalse(self.panel.pipeline_running)


if __name__ == "__main__":
    unittest.main()
