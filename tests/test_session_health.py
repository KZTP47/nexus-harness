"""Background sign-in health: detect, recover by itself, announce, allow manual control."""
from __future__ import annotations

import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import session_health
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError, ProviderResponse
from our_harness.session_health import SessionHealthMonitor, looks_like_sign_in_problem, session_key


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portable-session-health-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "writer": {"kind": "claude-cli", "model": "any-model", "command": ["arbitrary/bin/claude.exe"]},
            "second-writer": {"kind": "claude-cli", "model": "other", "command": ["claude"]},
            "reviewer": {"kind": "codex-cli", "model": "m"},
            "api": {"kind": "openai", "model": "x"},
        }
        self.config = LoadedConfig(data, self.root, [], {})
        self.status = {"authentication": "signed-in"}
        self.live_error = None
        self.opened = []
        self.live_calls = []
        self.clock = Clock()
        self.recovered = []
        self.monitor = self.make()

    def make(self):
        monitor = SessionHealthMonitor(
            lambda: self.config, settings_path=self.root / "state" / "session-health.json",
            status_check=lambda config, route: dict(self.status),
            live_check=self.live, open_sign_in=self.open, clock=self.clock,
            dispatch=lambda work: work(),
        )
        monitor.on_recovered(self.recovered.append)
        return monitor

    def live(self, config, route):
        self.live_calls.append(route)
        if self.live_error:
            raise HarnessError(self.live_error)

    def open(self, config, route):
        self.opened.append(route)
        return {"opened": True, "note": "Sign-in window opened."}

    def claude(self):
        return session_key("claude-cli", ["claude"])

    def test_routes_sharing_one_cli_share_one_session_and_api_routes_are_ignored(self):
        self.monitor.tick()
        keys = {one["key"]: one for one in self.monitor.snapshot()["sessions"]}
        self.assertEqual(set(keys), {"claude-cli:claude", "codex-cli:codex-cli"})
        self.assertEqual(keys["claude-cli:claude"]["routes"], ["second-writer", "writer"])

    def test_sign_in_failure_holds_work_opens_sign_in_once_and_success_resumes(self):
        self.monitor.tick()
        message = "Failed to authenticate: OAuth session expired and could not be refreshed"
        self.monitor.provider_outcome(self.claude(), "claude-cli", False, message)
        self.monitor.provider_outcome(self.claude(), "claude-cli", False, message)
        self.assertEqual(len(self.opened), 1, "the sign-in window opens once per incident")
        held = self.monitor.blocked("writer")
        self.assertIsNotNone(held)
        self.assertEqual(held["label"], "Claude")
        self.assertIsNotNone(self.monitor.blocked("second-writer"))
        self.assertIsNone(self.monitor.blocked("reviewer"))
        kinds = [item["kind"] for item in self.monitor.snapshot()["feed"]]
        self.assertEqual(kinds, ["signed_out", "sign_in_opened"])
        self.monitor.provider_outcome(self.claude(), "claude-cli", True, "")
        self.assertIsNone(self.monitor.blocked("writer"))
        self.assertEqual(self.recovered, [["second-writer", "writer"]])
        self.assertEqual(self.monitor.snapshot()["feed"][-1]["kind"], "recovered")

    def test_outages_and_model_errors_never_open_a_sign_in_window(self):
        self.monitor.tick()
        for message in ("Rate limit reached; try again in 20 seconds", "model not found: x",
                        "The provider did not answer in 180 seconds", "HTTP 500 from service"):
            self.monitor.provider_outcome(self.claude(), "claude-cli", False, message)
        self.assertEqual(self.opened, [])
        self.assertIsNone(self.monitor.blocked("writer"))

    def test_status_said_signed_in_is_not_enough_to_close_an_incident(self):
        self.monitor.tick()
        self.monitor.provider_outcome(self.claude(), "claude-cli", False, "Please run claude auth login")
        self.live_error = "Failed to authenticate: token expired"
        self.clock.now += 25
        self.monitor.tick()
        self.assertEqual(self.live_calls, ["second-writer"])
        self.assertIsNotNone(self.monitor.blocked("writer"))
        # Back-off: the next verification waits longer, not one per second.
        self.clock.now += 25
        self.monitor.tick()
        self.assertEqual(len(self.live_calls), 1)
        self.live_error = None
        self.clock.now += 60
        self.monitor.tick()
        self.assertIsNone(self.monitor.blocked("writer"))
        self.assertEqual(len(self.recovered), 1)

    def test_status_command_saying_signed_out_opens_an_incident(self):
        self.status = {"authentication": "signed-out", "note": "Not logged in."}
        self.monitor.tick()
        self.assertIsNotNone(self.monitor.blocked("writer"))
        self.assertIsNotNone(self.monitor.blocked("reviewer"))
        self.assertEqual(sorted(self.opened), ["reviewer", "second-writer"])

    def test_user_can_take_charge_per_session_and_globally_and_it_survives_restart(self):
        self.monitor.tick()
        self.monitor.manual(self.claude(), True)
        self.monitor.provider_outcome(self.claude(), "claude-cli", False, "not logged in")
        self.assertEqual(self.opened, [])
        session = next(one for one in self.monitor.snapshot()["sessions"] if one["key"] == self.claude())
        self.assertFalse(session["auto_sign_in"])
        self.assertTrue(self.monitor.open_sign_in(self.claude())["opened"])
        self.assertEqual(self.opened, ["second-writer"])

        self.monitor.save_settings({"auto_sign_in": False})
        restarted = self.make()
        self.assertFalse(restarted.settings()["auto_sign_in"])
        restarted.tick()
        restarted.provider_outcome(session_key("codex-cli"), "codex-cli", False, "401 Unauthorized")
        self.assertEqual(self.opened, ["second-writer"], "no automatic window when the user turned it off")
        self.assertIsNotNone(restarted.blocked("reviewer"))

    def test_unreadable_or_future_settings_fall_back_to_automatic(self):
        path = self.root / "state" / "session-health.json"
        path.parent.mkdir(parents=True)
        for text in ("not json", '{"schema_version": 99, "auto_sign_in": false}'):
            path.write_text(text, encoding="utf-8")
            self.assertTrue(self.make().settings()["auto_sign_in"])

    def test_removed_route_forgets_its_session(self):
        self.monitor.tick()
        del self.config.data["providers"]["reviewer"]
        self.monitor.tick()
        self.assertNotIn("codex-cli:codex-cli", {one["key"] for one in self.monitor.snapshot()["sessions"]})

    def test_mailbox_incident_opens_its_own_sign_in_and_resolves(self):
        opened, checked = [], []
        self.monitor.external_incident("mailbox:a", "owner@example.test mailbox", "Signed out",
                                       opener=lambda: opened.append(1) or {"opened": True},
                                       checker=lambda: checked.append(1))
        self.monitor.external_incident("mailbox:a", "owner@example.test mailbox", "Signed out",
                                       opener=lambda: opened.append(1) or {"opened": True})
        self.assertEqual(opened, [1])
        self.monitor.tick()  # config-driven pruning must keep owned sessions
        self.monitor.check_now("mailbox:a")
        self.assertEqual(checked, [1])
        state = {one["key"]: one for one in self.monitor.snapshot()["sessions"]}
        self.assertEqual(state["mailbox:a"]["state"], "signed_out")
        self.monitor.external_resolved("mailbox:a")
        state = {one["key"]: one for one in self.monitor.snapshot()["sessions"]}
        self.assertEqual(state["mailbox:a"]["state"], "ok")
        self.monitor.external_incident("mailbox:b", "oauth mailbox", "Reconnect required")
        self.assertFalse(self.monitor.open_sign_in("mailbox:b")["opened"])

    def test_failed_automatic_sign_in_is_explained_not_raised(self):
        self.monitor._open_sign_in = lambda config, route: (_ for _ in ()).throw(HarnessError("not installed"))
        self.monitor.tick()
        self.monitor.provider_outcome(self.claude(), "claude-cli", False, "not logged in")
        session = next(one for one in self.monitor.snapshot()["sessions"] if one["key"] == self.claude())
        self.assertIn("not installed", session["sign_in_note"])

    def test_sign_in_wording(self):
        for text in ("Failed to authenticate: OAuth session expired", "You are not logged in",
                     "Please sign in again", "HTTP 401 Unauthorized", "Your session has expired",
                     "Run: claude auth login", "disabled claude subscription access"):
            self.assertTrue(looks_like_sign_in_problem(text), text)
        for text in ("Request timed out", "Rate limit exceeded", "context window exceeded", ""):
            self.assertFalse(looks_like_sign_in_problem(text), text)


class ProviderObservationTests(unittest.TestCase):
    def test_every_created_cli_provider_reports_its_outcome(self):
        seen = []
        listener = lambda key, kind, ok, message: seen.append((key, ok, message))
        session_health.add_listener(listener)
        self.addCleanup(session_health.remove_listener, listener)

        class Fake:
            settings = {"command": ["C:/tools/codex.exe"]}

            def __init__(self):
                self.fail = False

            def complete(self, request):
                if self.fail:
                    raise HarnessError("401 Unauthorized")
                return ProviderResponse(text="OK")

        provider = session_health.observe_provider(Fake(), "codex-cli")
        provider.complete(object())
        provider.fail = True
        with self.assertRaises(HarnessError):
            provider.complete(object())
        self.assertEqual(seen, [("codex-cli:codex", True, ""), ("codex-cli:codex", False, "401 Unauthorized")])
        untouched = Fake()
        self.assertIs(session_health.observe_provider(untouched, "openai"), untouched)
        self.assertNotIn("complete", vars(untouched))


class EmailAndServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portable-session-server-")
        self.addCleanup(temporary.cleanup)
        runtime = mock.patch.dict(os.environ, {"OUR_HARNESS_SWARM_RUN_DIR": str(Path(temporary.name) / "runtime")})
        runtime.start()
        self.addCleanup(runtime.stop)
        self.harness = _panel()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.panel = self.harness.panel

    def test_http_snapshot_and_actions(self):
        status, state = self.harness.ask("/api/session-health")
        self.assertEqual(status, 200)
        self.assertEqual(state["contract"], session_health.CONTRACT)
        self.assertTrue(state["settings"]["auto_sign_in"])
        status, state = self.harness.ask("/api/session-health", {"action": "settings", "auto_sign_in": False})
        self.assertEqual(status, 200)
        self.assertFalse(state["settings"]["auto_sign_in"])
        self.assertEqual(self.harness.ask("/api/session-health", {"action": "bogus"})[0], 400)
        self.assertEqual(self.harness.ask("/api/session-health", {"action": "open_sign_in", "session": "nope"})[0], 400)

    def test_drafts_wait_during_a_sign_in_incident_and_retry_after_it(self):
        from tests.test_email_studio import Secrets
        service = self.panel.email
        studio = service.studio
        studio.secrets = Secrets()
        answers = {"fail": True}

        def provider(route, task, context):
            if answers["fail"]:
                raise HarnessError("Failed to authenticate: OAuth session expired")
            return "Synthetic reply."
        studio.provider_call = provider
        account = studio.dispatch("account_save", {"email": "owner@example.test", "provider_route": "writer"})["account"]
        message = studio.dispatch("import", {"account_id": account["id"], "sender": "a@example.test",
                                             "subject": "Hello", "body": "Please answer."})["message"]
        failed = studio.dispatch("create_draft", {"account_id": account["id"], "message_id": message["id"]})["draft"]
        with self.assertRaises(HarnessError):
            studio.process_draft(failed["id"])
        monitor = self.panel.session_health
        monitor._open_sign_in = lambda config, route: {"opened": True}
        monitor._dispatch = lambda work: work()
        with monitor._lock:
            session = monitor._session("claude-cli:claude", "claude-cli", ["writer"])
        monitor._routes = lambda: {"claude-cli:claude": {"kind": "claude-cli", "routes": ["writer"]}}
        monitor.provider_outcome("claude-cli:claude", "claude-cli", False, "OAuth session expired")
        second = studio.dispatch("import", {"account_id": account["id"], "sender": "b@example.test",
                                            "subject": "Again", "body": "Another question."})["message"]
        waiting = studio.dispatch("create_draft", {"account_id": account["id"], "message_id": second["id"]})["draft"]
        with mock.patch.object(service, "_background") as background:
            service._start_draft(waiting)
        background.assert_not_called()
        holds = service.snapshot()["sign_in_holds"]
        self.assertEqual([(one["label"], one["drafts"]) for one in holds], [("Claude", 1)])

        answers["fail"] = False
        started = []
        with mock.patch.object(service, "_start_draft", side_effect=started.append):
            monitor.provider_outcome("claude-cli:claude", "claude-cli", True, "")
        # The failed draft is retried; the waiting one is started by the mail
        # loop's own maintenance pass now that nothing holds it.
        self.assertIn(failed["id"], [one["id"] for one in started])
        self.assertEqual(studio._get("draft", failed["id"])["status"], "queued")
        self.assertIsNone(monitor.blocked("writer"))
        self.assertEqual(service.snapshot()["sign_in_holds"], [])
        self.assertEqual(session["state"], "ok")

    def test_recovery_resumes_only_goals_paused_by_that_provider(self):
        resumed = []
        goals = [
            {"goal_id": "g-provider", "status": "paused", "note": "Required provider work failed. Completed contributions are retained.",
             "agents": [{"id": "a1", "who": "writer"}], "project": {"id": "p", "path": str(self.harness.root)}},
            {"goal_id": "g-user-pause", "status": "paused", "note": "Paused by the user.",
             "agents": [{"id": "a1", "who": "writer"}], "project": {"id": "p", "path": str(self.harness.root)}},
            {"goal_id": "g-other-route", "status": "paused", "note": "Required provider work failed.",
             "agents": [{"id": "a2", "who": "reviewer"}], "project": {"id": "p", "path": str(self.harness.root)}},
            {"goal_id": "g-running", "status": "running", "note": "Required provider work failed.",
             "agents": [{"id": "a1", "who": "writer"}], "project": {"id": "p", "path": str(self.harness.root)}},
        ]
        runtime = mock.Mock()
        runtime.store.list.return_value = goals
        runtime.resume.side_effect = lambda goal_id, **kw: resumed.append(goal_id) or {}
        with mock.patch.object(type(self.panel), "long_horizon", new_callable=mock.PropertyMock, return_value=runtime), \
                mock.patch.object(self.panel, "require_project_execution_authority"):
            self.assertEqual(self.panel._resume_provider_paused_goals(["writer"]), ["g-provider"])
        self.assertEqual(resumed, ["g-provider"])

    def test_signed_out_browser_mailbox_is_recorded_backs_off_and_resolves(self):
        service = self.panel.email
        studio = service.studio
        account = studio.dispatch("account_save", {"email": "owner@example.test", "provider_route": "writer"})["account"]
        stored = studio._get("account", account["id"])
        stored.update(kind="browser_outlook", connector_id="c1", connector_fingerprint="f", connection_state="connected",
                      poll_enabled=True, poll_seconds=5)
        studio._put("account", stored)
        with mock.patch.object(studio.local_mail, "status", return_value={"state": "sign_in_required"}):
            with self.assertRaises(HarnessError) as raised:
                studio.dispatch("sync", {"account_id": account["id"]})
        self.assertIn("signed out", str(raised.exception))
        after = studio._get("account", account["id"])
        self.assertEqual(after["session_state"], "sign_in_required")
        self.assertEqual(after["connection_state"], "connected")
        opened = []
        with mock.patch.object(studio.local_mail, "open", side_effect=lambda *a, **k: opened.append(a) or {}):
            service._mailbox_session(account["id"], True)
        self.assertEqual(opened, [("browser_outlook", "c1")])
        sessions = {one["key"]: one for one in self.panel.session_health.snapshot()["sessions"]}
        self.assertEqual(sessions["mailbox:" + account["id"]]["state"], "signed_out")
        with mock.patch.object(service, "_background") as background:
            service._schedule_polls([studio._public_account(after)])
        background.assert_called_once()
        due = service._poll_due[account["id"]] - time.monotonic()
        self.assertGreater(due, 60, "a signed-out mailbox is checked calmly, not every 5 seconds")
        service._mailbox_session(account["id"], False)
        sessions = {one["key"]: one for one in self.panel.session_health.snapshot()["sessions"]}
        self.assertEqual(sessions["mailbox:" + account["id"]]["state"], "ok")


def _panel():
    """The shared HTTP panel harness, used here without collecting its tests."""
    from tests.test_team_server import PanelTestCase

    class Panel(PanelTestCase):
        def runTest(self):  # pragma: no cover - harness only
            pass
    return Panel()


if __name__ == "__main__":
    unittest.main()
