"""The connection-repair UI is driven by one provider-neutral engine contract."""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness import provider_repair


class RepairPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "codex": {"kind": "codex-cli", "model": "gpt", "command": ["codex"]},
        }
        self.config = LoadedConfig(data, self.root, [], {})

    def plan(self, status: dict, setup: dict | None = None) -> dict:
        with mock.patch.object(provider_repair, "connection_status", return_value=status), \
             mock.patch.object(provider_repair, "_route_setup", return_value=setup or {}):
            return provider_repair.repair_plan(self.config, "codex")

    def test_isolated_codex_offers_a_live_test_and_never_a_fake_login_fix(self) -> None:
        found = self.plan({
            "route": "codex", "kind": "codex-cli", "installed": True,
            "authentication": "unknown", "state": "isolated-ready",
            "can_login": False, "note": "Protected mode is available.",
        })
        actions = [one["id"] for one in found["repair"]["actions"]]
        self.assertEqual(found["repair"]["state"], "needs-verification")
        self.assertIn("live-test", actions)
        self.assertNotIn("login", actions)
        self.assertFalse(found["repair"]["diagnosis_costs_model_request"])

    def test_configuration_errors_open_settings_and_do_not_offer_sign_in(self) -> None:
        found = self.plan({
            "route": "codex", "kind": "codex-cli", "installed": True,
            "authentication": "unknown", "state": "configuration-error",
            "can_login": True, "note": "Fix reasoning_effort.",
        })
        actions = [one["id"] for one in found["repair"]["actions"]]
        self.assertEqual(actions, ["settings", "check"])
        self.assertIn("signing in", " ".join(found["repair"]["steps"]))

    def test_gemini_project_prerequisite_wins_over_generic_login_advice(self) -> None:
        found = self.plan({
            "route": "codex", "kind": "gemini-cli", "installed": True,
            "authentication": "signed-out", "state": "needs-login",
            "can_login": True, "note": "signed out",
        }, {
            "ready": False, "why_not": "Gemini needs a Google Cloud project id.",
            "trouble_last_time": "project required", "kind": "gemini-cli",
        })
        self.assertEqual(found["repair"]["state"], "needs-cloud-project")
        self.assertEqual(found["repair"]["actions"][0]["id"], "google-project")

    def test_a_real_claude_failure_gets_repair_then_live_verification(self) -> None:
        found = self.plan({
            "route": "codex", "kind": "claude-cli", "installed": True,
            "authentication": "signed-in", "state": "authenticated",
            "can_login": True, "note": "status is signed in",
        }, {
            "ready": True, "trouble_last_time": "Authentication required: not signed in.",
            "kind": "claude-cli",
        })
        self.assertEqual(found["repair"]["diagnosis"]["category"], "auth")
        self.assertEqual(
            [one["id"] for one in found["repair"]["actions"]],
            ["repair-claude", "live-test", "check"],
        )

    def test_missing_route_returns_a_typed_repair_plan_without_probing(self) -> None:
        with mock.patch.object(provider_repair, "connection_status") as checked, \
             mock.patch.object(provider_repair, "_route_setup") as setup:
            found = provider_repair.repair_plan(self.config, "deleted-route")

        checked.assert_not_called()
        setup.assert_not_called()
        self.assertEqual(found["state"], "route-missing")
        self.assertEqual(found["repair"]["state"], "route-missing")
        self.assertEqual(found["repair"]["diagnosis"]["category"], "config")
        self.assertEqual(
            [one["id"] for one in found["repair"]["actions"]],
            ["choose-route", "settings", "check"],
        )

    def test_saved_failures_are_typed_and_only_auth_can_offer_login(self) -> None:
        cases = {
            "auth": "Authentication required: not signed in.",
            "config": "Error loading configuration: unknown option reasoning_effort.",
            "model": "The selected model is not available on this account.",
            "capacity": "Quota exhausted for this billing account.",
            "rate-limit": "Provider HTTP 429: too many requests.",
            "network": "Connection refused while contacting the provider endpoint.",
            "timeout": "The provider request timed out.",
            "protocol": "The provider returned invalid JSON.",
            "outcome-unknown": "The outcome is unknown; the request may have been sent.",
            "unknown": "The purple subsystem declined the request.",
        }
        login_actions = {"login", "repair-claude", "web-chat"}
        for category, failure in cases.items():
            with self.subTest(category=category):
                found = self.plan({
                    "route": "codex", "kind": "claude-cli", "installed": True,
                    "authentication": "signed-in", "state": "authenticated",
                    "can_login": True, "note": "status is signed in",
                }, {
                    "ready": True, "trouble_last_time": failure,
                    "kind": "claude-cli",
                })
                repair = found["repair"]
                self.assertEqual(repair["diagnosis"]["category"], category)
                self.assertRegex(repair["diagnosis_fingerprint"], r"^[0-9a-f]{64}$")
                ids = {one["id"] for one in repair["actions"]}
                if category == "auth":
                    self.assertIn("repair-claude", ids)
                else:
                    self.assertTrue(ids.isdisjoint(login_actions))
                if category == "outcome-unknown":
                    self.assertNotIn("live-test", ids)
                for action in repair["actions"]:
                    self.assertEqual(action["route"], "codex")
                    self.assertEqual(
                        action["diagnosis_fingerprint"],
                        repair["diagnosis_fingerprint"],
                    )
                    self.assertIn(action["cost"], {"none", "model-request"})

    def test_diagnosis_fingerprint_changes_with_failure_evidence(self) -> None:
        status = {
            "route": "codex", "kind": "codex-cli", "installed": True,
            "authentication": "signed-in", "state": "authenticated",
            "can_login": True, "note": "signed in",
        }
        first = self.plan(status, {
            "ready": True, "trouble_last_time": "Provider request timed out.",
            "kind": "codex-cli",
        })
        second = self.plan(status, {
            "ready": True, "trouble_last_time": "Provider returned invalid JSON.",
            "kind": "codex-cli",
        })
        self.assertNotEqual(
            first["repair"]["diagnosis_fingerprint"],
            second["repair"]["diagnosis_fingerprint"],
        )

    def test_uncertain_prior_delivery_wording_never_offers_a_live_retry(self) -> None:
        diagnosis = provider_repair.classify_prior_failure(
            "This web chat has an uncertain prior delivery; Nexus will not "
            "resend it until the provider conversation is inspected."
        )

        self.assertEqual(diagnosis["category"], "outcome-unknown")
        self.assertFalse(diagnosis["retryable"])

    def test_uncertain_web_turn_offers_safe_exact_conversation_inspection(self) -> None:
        route = "web:gemini-session-17"
        status = {
            "route": route, "kind": "web-chat", "installed": True,
            "authentication": "signed-in", "state": "authenticated",
            "can_login": True, "note": "connected",
        }
        setup = {
            "ready": True,
            "trouble_last_time": "Provider outcome is unknown; the turn may have been sent.",
            "kind": "web-chat",
        }
        with mock.patch.object(provider_repair, "connection_status", return_value=status), \
             mock.patch.object(provider_repair, "_route_setup", return_value=setup):
            found = provider_repair.repair_plan(self.config, route)

        repair = found["repair"]
        self.assertEqual(repair["state"], "outcome-unknown")
        self.assertEqual(
            [one["id"] for one in repair["actions"]],
            ["inspect-provider-turn", "check"],
        )
        inspect = repair["actions"][0]
        self.assertEqual(inspect["route"], route)
        self.assertEqual(inspect["cost"], "none")
        self.assertEqual(
            inspect["diagnosis_fingerprint"],
            repair["diagnosis_fingerprint"],
        )

    def test_saved_web_transport_failure_reaches_repair_without_a_static_route(self) -> None:
        route = "web:chatgpt-session-42"
        with mock.patch.object(provider_repair.chat, "already_set_up", return_value=[]), \
             mock.patch.object(provider_repair.chat, "what_would_not_answer", return_value={
                 route: {"why": "ChatGPT has an unreconciled provider turn; the outcome is unknown."},
             }), mock.patch.object(provider_repair, "connection_status", return_value={
                 "route": route, "kind": "web-chat", "installed": True,
                 "authentication": "signed-in", "state": "authenticated",
                 "can_login": True, "note": "connected",
             }):
            found = provider_repair.repair_plan(self.config, route)

        self.assertEqual(found["repair"]["state"], "outcome-unknown")
        self.assertEqual(
            [one["id"] for one in found["repair"]["actions"]],
            ["inspect-provider-turn", "check"],
        )

    def test_missing_web_connection_outranks_an_unknown_stale_failure(self) -> None:
        route = "web:chatgpt-abcdef123456"
        with mock.patch.object(provider_repair.chat, "already_set_up", return_value=[]), \
             mock.patch.object(provider_repair.chat, "what_would_not_answer", return_value={
                 route: {"why": "The purple subsystem declined the request."},
             }), mock.patch.object(provider_repair, "connection_status", return_value={
                 "route": route, "kind": "web-chat", "installed": True,
                 "authentication": "signed-out", "state": "needs-login",
                 "can_login": True,
                 "note": "This exact web-chat route is not connected to Electron.",
             }):
            found = provider_repair.repair_plan(self.config, route)

        repair = found["repair"]
        self.assertEqual(repair["state"], "needs-login")
        self.assertEqual(repair["diagnosis"]["source"], "non-billing-status")
        self.assertEqual(
            [one["id"] for one in repair["actions"]],
            ["web-chat", "check"],
        )
        reconnect = repair["actions"][0]
        self.assertEqual(reconnect["connection_id"], "chatgpt-abcdef123456")
        self.assertEqual(reconnect["provider"], "chatgpt")
        self.assertNotIn("settings", {one["id"] for one in repair["actions"]})
        self.assertNotIn("live-test", {one["id"] for one in repair["actions"]})

    def test_unknown_web_failure_can_never_send_a_dynamic_route_to_settings(self) -> None:
        route = "web:chatgpt-portable-17"
        with mock.patch.object(provider_repair, "connection_status", return_value={
                 "route": route, "kind": "web-chat", "installed": True,
                 "authentication": "signed-in", "state": "authenticated",
                 "can_login": True, "note": "connected",
             }), mock.patch.object(provider_repair, "_route_setup", return_value={
                 "ready": True, "kind": "web-chat",
                 "trouble_last_time": "The purple subsystem declined the request.",
             }):
            found = provider_repair.repair_plan(self.config, route)

        actions = found["repair"]["actions"]
        self.assertEqual([one["id"] for one in actions], ["live-test", "web-chat", "check"])
        self.assertNotIn("settings", {one["id"] for one in actions})
        reconnect = next(one for one in actions if one["id"] == "web-chat")
        self.assertEqual(reconnect["connection_id"], "chatgpt-portable-17")
        self.assertEqual(reconnect["provider"], "chatgpt")

    def test_live_success_becomes_an_explicit_verified_state(self) -> None:
        base = self.plan({
            "route": "codex", "kind": "codex-cli", "installed": True,
            "authentication": "signed-in", "state": "authenticated",
            "can_login": True, "note": "signed in",
        })
        found = provider_repair.verified_plan(base, 1250)
        self.assertEqual(found["repair"]["state"], "verified")
        self.assertIn("1.2 seconds", found["repair"]["summary"])
        self.assertEqual(found["repair"]["diagnosis"]["source"], "live-model-answer")
        for action in found["repair"]["actions"]:
            self.assertEqual(
                action["diagnosis_fingerprint"],
                found["repair"]["diagnosis_fingerprint"],
            )


class RepairEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        (root / ".harness").mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        data["providers"] = {
            "codex": {"kind": "codex-cli", "model": "gpt", "command": ["codex"]},
        }
        self.config = LoadedConfig(data, root, [], {})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path: str, body: dict) -> tuple[int, dict]:
        import http.client

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=15,
        )
        try:
            connection.request("POST", path, json.dumps(body), {
                "Host": f"127.0.0.1:{self.server.server_port}",
                "Content-Type": "application/json",
                "X-Harness-Token": self.server.token,
            })
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    @staticmethod
    def allowed_plan() -> dict:
        return {
            "route": "codex", "kind": "codex-cli", "installed": True,
            "state": "isolated-ready", "authentication": "unknown",
            "repair": {
                "state": "needs-verification", "tone": "attention",
                "title": "Ready", "summary": "Ready", "steps": [],
                "actions": [{"id": "live-test", "label": "Run live test", "note": "one request"}],
            },
        }

    def test_diagnosis_endpoint_never_calls_a_model(self) -> None:
        with mock.patch.object(
            provider_repair, "repair_plan", return_value=self.allowed_plan(),
        ) as diagnosed, mock.patch(
            "our_harness.chat.ask_once",
        ) as asked:
            status, body = self.call("/api/team/repair-plan", {"route": "codex"})
        self.assertEqual(status, 200)
        self.assertEqual(body["repair"]["state"], "needs-verification")
        diagnosed.assert_called_once()
        asked.assert_not_called()

    def test_live_test_is_explicit_isolated_and_does_not_return_model_text(self) -> None:
        seen: dict[str, str | bool] = {}

        def answer(_config, _route, _text, **kwargs):  # type: ignore[no-untyped-def]
            folder = Path(kwargs["working_directory"])
            seen["exists_during_call"] = folder.is_dir()
            seen["folder"] = str(folder)
            seen["conversation_key"] = str(kwargs["conversation_key"])
            return {"text": "READY and private provider output", "milliseconds": 321, "model": "gpt"}

        plan = self.allowed_plan()
        with mock.patch.object(provider_repair, "repair_plan", return_value=plan), \
             mock.patch("our_harness.chat.ask_once", side_effect=answer):
            status, body = self.call("/api/team/test-route", {"route": "codex"})
        self.assertEqual(status, 200)
        self.assertTrue(body["answered"])
        self.assertEqual(body["plan"]["repair"]["state"], "verified")
        self.assertNotIn("text", body)
        self.assertTrue(seen["exists_during_call"])
        self.assertFalse(Path(str(seen["folder"])).exists())
        self.assertTrue(str(seen["conversation_key"]).startswith("connection-test-"))

    def test_server_refuses_a_live_test_while_a_required_repair_is_unfinished(self) -> None:
        blocked = self.allowed_plan()
        blocked["repair"] = {
            **blocked["repair"], "state": "needs-login",
            "actions": [{"id": "login", "label": "Open sign-in", "note": "sign in"}],
        }
        with mock.patch.object(provider_repair, "repair_plan", return_value=blocked), \
             mock.patch("our_harness.chat.ask_once") as asked:
            status, body = self.call("/api/team/test-route", {"route": "codex"})
        self.assertEqual(status, 400)
        self.assertIn("Finish the repair step", body["error"])
        asked.assert_not_called()

    def test_stop_targets_only_the_exact_route_test(self) -> None:
        token = self.server.chat_cancellations.begin("connection-test:codex")
        self.addCleanup(
            self.server.chat_cancellations.finish, "connection-test:codex", token,
        )
        status, body = self.call("/api/team/stop-route-test", {"route": "codex"})
        self.assertEqual(status, 200)
        self.assertTrue(body["stopped"])
        self.assertTrue(token.cancelled)


class RepairPanelContractTests(unittest.TestCase):
    def test_panel_exposes_one_guided_flow_and_discloses_live_request_cost(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "our_harness" / "ui"
        markup = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="swarmAgentRepairStart"', markup)
        self.assertIn("Diagnosis is free and never sends a model prompt", markup)
        self.assertIn("uses one model request in an empty temporary folder", markup)
        self.assertIn('request("/api/team/repair-plan"', script)
        self.assertIn('request("/api/team/test-route"', script)
        self.assertIn('request("/api/team/stop-route-test"', script)
        self.assertIn('request("/api/team/set-google-project"', script)


if __name__ == "__main__":
    unittest.main()
