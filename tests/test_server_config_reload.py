"""One runtime config revision for settings, projects, and route caches."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from our_harness import server
from our_harness.config import load_config
from our_harness.providers import subscription_cli


class ServerConfigReloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "arbitrary-project"
        (self.project / ".harness").mkdir(parents=True)
        self.machine_state = self.root / "machine-state"
        self.machine_state.mkdir()
        environment = mock.patch.dict(os.environ, {
            "APPDATA": str(self.machine_state),
            "XDG_CONFIG_HOME": str(self.machine_state),
            "OUR_HARNESS_PIPELINE_RUN_DIR": str(self.root / "pipeline-runtime"),
            "OUR_HARNESS_SWARM_RUN_DIR": str(self.root / "swarm-runtime"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.panel = server.HarnessHTTPServer(
            ("127.0.0.1", 0), load_config(self.project)
        )
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_port
        threading.Thread(target=self.panel.serve_forever, daemon=True).start()
        self.addCleanup(self.panel.shutdown)

    def ask(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": self.panel.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    @staticmethod
    def route(kind: str = "claude-cli") -> dict:
        return {"kind": kind, "model": "test-model", "endpoint": ""}

    def test_generic_change_reloads_all_derived_state_and_invalidates_routes(self) -> None:
        old_config = self.panel.config
        old_redactor = self.panel.events.redactor
        old_revision = self.panel._config_revision
        self.panel.swarm_known_routes = [
            {"route": "obsolete", "label": "Old route", "ready": True}
        ]

        status, changed = self.ask("/api/settings/change", {
            "key": "providers", "value": {"fresh": self.route()},
        })

        self.assertEqual(status, 200, changed)
        self.assertIn("fresh", self.panel.config.get("providers"))
        self.assertIsNot(self.panel.config, old_config)
        self.assertIsNot(self.panel.events.redactor, old_redactor)
        self.assertEqual(self.panel._config_revision, old_revision + 1)
        self.assertIsNone(self.panel.swarm_known_routes)

        configured = [{"route": "fresh", "label": "Fresh", "ready": False}]
        local_standing = {
            "board": {"agents": [], "projects": []}, "who_can_be_used": [],
        }
        with mock.patch.object(
            server.chat_lab, "already_set_up", return_value=configured,
        ) as already, mock.patch.object(
            server.swarm_lab, "how_it_stands", return_value=local_standing,
        ) as standing:
            answer = self.panel.swarm_standing()
        already.assert_called_once_with(self.panel.config)
        self.assertEqual(standing.call_args.kwargs["known_routes"], configured)
        self.assertTrue(answer["provider_status_stale"])

    def test_generic_reset_reloads_instead_of_leaving_the_changed_snapshot(self) -> None:
        status, changed = self.ask("/api/settings/change", {
            "key": "providers", "value": {"fresh": self.route()},
        })
        self.assertEqual(status, 200, changed)
        self.panel.swarm_known_routes = [
            {"route": "fresh", "label": "Fresh", "ready": True}
        ]
        before = self.panel._config_revision

        status, reset = self.ask("/api/settings/reset", {"key": "providers"})

        self.assertEqual(status, 200, reset)
        self.assertEqual(self.panel.config.get("providers"), {})
        self.assertEqual(self.panel._config_revision, before + 1)
        self.assertIsNone(self.panel.swarm_known_routes)

    def test_project_move_cannot_reuse_the_previous_projects_route_cache(self) -> None:
        other = self.root / "a-different-project"
        (other / ".harness").mkdir(parents=True)
        self.panel.swarm_known_routes = [
            {"route": "only-old-project", "label": "Old", "ready": True}
        ]
        before = self.panel._config_revision

        self.panel.move_to(str(other))

        self.assertEqual(self.panel.config.project_root, other.resolve())
        self.assertEqual(self.panel._config_revision, before + 1)
        self.assertIsNone(self.panel.swarm_known_routes)

    def test_workspace_run_thread_receives_the_config_accepted_under_admission(self) -> None:
        other = self.root / "a-different-project"
        (other / ".harness").mkdir(parents=True)
        entered = threading.Event()
        release = threading.Event()
        observed: list[Path] = []

        def capture(
            handler, _task, _dry_run, _graph=None, _bootstrap=False,
            accepted_config=None,
        ) -> None:
            entered.set()
            self.assertTrue(release.wait(5))
            observed.append(accepted_config.project_root)
            handler.server.release_run()

        with mock.patch.object(server.HarnessHandler, "_run_task", new=capture):
            status, accepted = self.ask("/api/run", {"task": "Use the admitted project"})
            self.assertEqual(status, 202, accepted)
            self.assertTrue(entered.wait(5))
            # Simulate a hostile late config replacement after the admission
            # boundary. The worker must still use the immutable accepted value.
            self.panel.config = load_config(other)
            release.set()
            limit = threading.Event()
            for _one in range(100):
                if observed:
                    break
                limit.wait(0.01)

        self.assertEqual(observed, [self.project.resolve()])

    def test_slow_discovery_from_an_old_revision_cannot_republish_after_reload(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        result: list[bool] = []

        def delayed(_config):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release provider discovery")
            return [
                {"route": "obsolete", "label": "Old", "ready": True}
            ]

        with mock.patch.object(
            server.swarm_lab, "discover_who_can_be_used", side_effect=delayed,
        ):
            worker = threading.Thread(
                target=lambda: result.append(
                    self.panel.refresh_swarm_provider_status()
                ),
                daemon=True,
            )
            worker.start()
            self.assertTrue(entered.wait(5))
            (self.project / ".harness" / "config.json").write_text(
                json.dumps({"memory": {"enabled": False}}), encoding="utf-8",
            )
            self.panel.reload_config()
            release.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [False])
        self.assertIsNone(self.panel.swarm_known_routes)
        self.assertTrue(self.panel.swarm_standing()["provider_status_stale"])

    def test_live_long_horizon_effect_refuses_change_before_writing(self) -> None:
        class LiveWorker:
            @staticmethod
            def is_alive() -> bool:
                return True

        runtime = SimpleNamespace(
            workers={"goal": LiveWorker()},
            close=lambda: None,
        )
        self.panel._long_horizon = runtime

        status, refused = self.ask("/api/settings/change", {
            "key": "memory.enabled", "value": False,
        })

        self.assertEqual(status, 400, refused)
        self.assertIn("middle of a provider or apply step", refused["error"])
        self.assertFalse((self.project / ".harness" / "config.json").exists())
        self.panel._long_horizon = None

    def test_idle_long_horizon_runtime_is_reopened_on_the_new_revision(self) -> None:
        closed: list[bool] = []
        runtime = SimpleNamespace(
            workers={},
            close=lambda: closed.append(True),
        )
        self.panel._long_horizon = runtime

        status, changed = self.ask("/api/settings/change", {
            "key": "memory.enabled", "value": False,
        })

        self.assertEqual(status, 200, changed)
        self.assertEqual(closed, [True])
        self.assertIsNone(self.panel._long_horizon)
        self.assertIs(self.panel.config.get("memory.enabled"), False)

    def test_connect_route_is_effective_immediately_and_clears_old_health(self) -> None:
        self.panel.swarm_known_routes = [
            {"route": "obsolete", "label": "Old", "ready": True}
        ]
        with mock.patch.object(subscription_cli, "connection_status", return_value={
            "state": "authenticated", "installed": True,
        }):
            status, connected = self.ask("/api/team/connect", {"kind": "claude-cli"})

        self.assertEqual(status, 200, connected)
        self.assertIn("claude", self.panel.config.get("providers"))
        self.assertIsNone(self.panel.swarm_known_routes)

    def test_exact_gemini_repair_is_effective_immediately(self) -> None:
        status, changed = self.ask("/api/settings/change", {
            "key": "providers",
            "value": {"work-gemini": self.route("gemini-cli")},
        })
        self.assertEqual(status, 200, changed)
        self.panel.swarm_known_routes = [
            {"route": "work-gemini", "label": "Before", "ready": False}
        ]

        status, repaired = self.ask("/api/team/set-google-project", {
            "route": "work-gemini", "google_project": "portable-project-id",
        })

        self.assertEqual(status, 200, repaired)
        self.assertEqual(
            self.panel.config.get("providers")["work-gemini"]["google_project"],
            "portable-project-id",
        )
        self.assertIsNone(self.panel.swarm_known_routes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
