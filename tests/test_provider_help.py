from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness import provider_help
from our_harness.provider_help import ATTENTION, INSTALLED, NEEDS_SETUP, READY, provider_options, setup_advice
from our_harness.providers import subscription_cli


class ProviderHelpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        # Start from a machine with nothing set up, so each test adds one thing.
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            os.environ.pop(name, None)
        self.addCleanup(self.environment.stop)
        # Patch the network call itself, not the helper, so the real reachability
        # logic runs in every test rather than being replaced by a stub.
        import urllib.error

        self.reachable = mock.patch.object(
            provider_help.urllib.request, "urlopen", side_effect=urllib.error.URLError("nothing there")
        )
        self.reachable.start()
        self.addCleanup(self.reachable.stop)
        self.which = mock.patch.object(subscription_cli.shutil, "which", return_value=None)
        self.which.start()
        self.addCleanup(self.which.stop)
        self.desktop_builds = mock.patch.object(
            subscription_cli, "_every_build_of", return_value=[]
        )
        self.desktop_builds.start()
        self.addCleanup(self.desktop_builds.stop)
        self.other_locations = mock.patch.object(
            subscription_cli, "_where_else_it_might_be", return_value=[]
        )
        self.other_locations.start()
        self.addCleanup(self.other_locations.stop)
        provider_help.clear_cache()
        self.addCleanup(provider_help.clear_cache)

    def config(self, name: str = "ollama") -> LoadedConfig:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"]["name"] = name
        return LoadedConfig(data, self.root, [], {})

    def by_id(self, config: LoadedConfig | None = None) -> dict[str, dict]:
        return {item.id: item for item in provider_options(config or self.config())}

    def test_every_way_of_connecting_is_listed(self) -> None:
        self.assertEqual(
            set(self.by_id()),
            {
                "ollama", "openai", "anthropic", "gemini", "gemini-cli",
                "codex-cli", "claude-cli", "copilot-cli",
            },
        )

    def test_a_bare_machine_has_nothing_ready_and_says_what_to_do(self) -> None:
        advice = setup_advice(self.config())
        self.assertEqual(advice["ready_count"], 0)
        self.assertIn("No model is connected yet", advice["headline"])
        for option in advice["options"]:
            self.assertEqual(option["state"], NEEDS_SETUP)
            self.assertTrue(option["steps"], f"{option['id']} says nothing about what to do")
            self.assertTrue(option["reason"])

    def test_a_running_ollama_is_reported_as_ready(self) -> None:
        answer = mock.MagicMock()
        answer.__enter__.return_value.status = 200
        with mock.patch.object(provider_help.urllib.request, "urlopen", return_value=answer):
            options = self.by_id()
        self.assertEqual(options["ollama"].state, READY)
        self.assertIn("answered at", options["ollama"].reason)
        self.assertEqual(options["ollama"].steps, ())

    def test_a_set_key_is_reported_as_ready(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "test-value"
        options = self.by_id()
        self.assertEqual(options["anthropic"].state, READY)
        self.assertIn("ANTHROPIC_API_KEY is set", options["anthropic"].reason)
        self.assertEqual(options["openai"].state, NEEDS_SETUP)

    def test_the_key_value_itself_never_appears_anywhere(self) -> None:
        os.environ["OPENAI_API_KEY"] = "sk-secret-value-do-not-show"
        advice = setup_advice(self.config("openai"))
        self.assertNotIn("sk-secret-value-do-not-show", json.dumps(advice))

    def test_an_installed_codex_command_is_not_called_connected(self) -> None:
        with mock.patch.object(subscription_cli.shutil, "which", return_value="/usr/bin/codex"):
            options = self.by_id()
        self.assertEqual(options["codex-cli"].state, INSTALLED)
        self.assertIn("not connected", options["codex-cli"].reason)
        self.assertNotIn("/usr/bin/codex", options["codex-cli"].reason)

    def test_sign_in_diagnostics_use_the_exact_configured_cli_command(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "work": {
                "kind": "claude-cli", "model": "default",
                "command": ["C:/Company Tools/claude-wrapper.exe", "--work-seat"],
            }
        }
        config = LoadedConfig(data, self.root, [], {})
        with mock.patch.object(subscription_cli, "available", return_value=True) as available, \
             mock.patch.object(subscription_cli, "connection_status", return_value={
                 "authentication": "signed-in", "installed": True,
             }) as status, \
             mock.patch("our_harness.chat.what_would_not_answer", return_value={}):
            option = provider_help._signed_in_tool("claude-cli", config, True)
        self.assertEqual(option.state, READY)
        self.assertEqual(
            status.call_args.kwargs["command"],
            ["C:/Company Tools/claude-wrapper.exe", "--work-seat"],
        )
        available.assert_called_once_with(
            "claude-cli", ["C:/Company Tools/claude-wrapper.exe", "--work-seat"]
        )

    def test_codex_provider_help_preserves_isolated_ready_and_configuration_error(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "codex": {
                "kind": "codex-cli", "model": "gpt-5.5",
                "command": ["C:/Tools/codex.exe"], "auth_mode": "chatgpt",
            }
        }
        config = LoadedConfig(data, self.root, [], {})
        with mock.patch.object(subscription_cli, "available", return_value=True), \
             mock.patch("our_harness.chat.what_would_not_answer", return_value={}), \
             mock.patch.object(subscription_cli, "connection_status", return_value={
                 "authentication": "unknown", "installed": True,
                 "state": "isolated-ready", "problem": "newer config",
             }):
            isolated = provider_help._signed_in_tool("codex-cli", config, True)
        self.assertEqual(isolated.state, READY)
        self.assertIn("isolated command", isolated.reason)

        with mock.patch.object(subscription_cli, "available", return_value=True), \
             mock.patch("our_harness.chat.what_would_not_answer", return_value={}), \
             mock.patch.object(subscription_cli, "connection_status", return_value={
                 "authentication": "unknown", "installed": True,
                 "state": "configuration-error", "problem": "no isolation flag",
             }):
            broken = provider_help._signed_in_tool("codex-cli", config, True)
        self.assertEqual(broken.state, ATTENTION)
        self.assertIn("no isolation flag", broken.reason)

    def test_codex_provider_help_does_not_let_an_obsolete_config_refusal_outrank_isolation(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "codex": {
                "kind": "codex-cli", "model": "gpt-5.5",
                "command": ["C:/Tools/codex.exe"], "auth_mode": "chatgpt",
            }
        }
        config = LoadedConfig(data, self.root, [], {})
        old = {
            "codex": {
                "why": "Error loading configuration: config.toml: unknown variant ultra"
            }
        }
        isolated_status = {
            "authentication": "unknown", "installed": True,
            "state": "isolated-ready", "problem": old["codex"]["why"],
        }
        with mock.patch.object(subscription_cli, "available", return_value=True), \
             mock.patch("our_harness.chat.what_would_not_answer", return_value=old), \
             mock.patch.object(
                 subscription_cli, "connection_status", return_value=isolated_status,
             ):
            option = provider_help._signed_in_tool("codex-cli", config, True)

        self.assertEqual(option.state, READY)
        self.assertIn("isolated command", option.reason)

        old["codex"]["why"] = "ChatGPT authentication required"
        with mock.patch.object(subscription_cli, "available", return_value=True), \
             mock.patch("our_harness.chat.what_would_not_answer", return_value=old), \
             mock.patch.object(
                 subscription_cli, "connection_status", return_value=isolated_status,
             ):
            still_real = provider_help._signed_in_tool("codex-cli", config, True)
        self.assertEqual(still_real.state, ATTENTION)
        self.assertIn("authentication required", still_real.reason)

    def test_codex_isolated_ready_counts_as_effective_first_request_readiness(self) -> None:
        from our_harness import server as server_module

        status = {
            "route": "default", "kind": "codex-cli", "installed": True,
            "state": "isolated-ready", "authentication": "unknown",
            "note": "User config is newer than this binary.",
        }
        with mock.patch.object(server_module, "connection_status", return_value=status):
            found = server_module.effective_route_readiness(self.config("codex-cli"))[0]
        self.assertTrue(found["ready"])
        self.assertTrue(found["ready_for_first_request"])
        self.assertIn("first isolated request", found["note"])

    def test_unknown_gemini_auth_is_allowed_as_a_labelled_first_request(self) -> None:
        from our_harness import server as server_module

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": "gemini-cli", "model": "default", "command": ["C:/Tools/gemini.exe"]
        })
        config = LoadedConfig(data, self.root, [], {})
        status = {
            "route": "default", "kind": "gemini-cli", "installed": True,
            "state": "installed", "authentication": "unknown", "note": "No safe status command.",
        }
        with mock.patch.object(server_module, "connection_status", return_value=status):
            found = server_module.effective_route_readiness(config)[0]
        self.assertTrue(found["ready"])
        self.assertTrue(found["ready_for_first_request"])
        self.assertEqual(found["state"], "first-request-required")
        self.assertIn("first run", found["note"])

    def test_every_effective_agent_route_must_be_ready(self) -> None:
        from our_harness import server as server_module

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "coder_route": {"kind": "claude-cli", "model": "default"},
            "review_route": {"kind": "codex-cli", "model": "default"},
        }
        data["agents"] = {
            "coder": {"provider_ref": "coder_route", "role": "coder"},
            "reviewer": {"provider_ref": "review_route", "role": "evaluator"},
        }
        config = LoadedConfig(data, self.root, [], {})

        def state(_config, route, **_kwargs):
            ready = route != "review_route"
            return {
                "route": route, "kind": "fake", "installed": ready,
                "state": "ready" if ready else "needs-login",
                "authentication": "signed-in" if ready else "signed-out",
                "note": f"{route} {'ready' if ready else 'signed out'}",
            }

        with mock.patch.object(server_module, "connection_status", side_effect=state):
            routes = server_module.effective_route_readiness(config)
        self.assertEqual([item["route"] for item in routes], [
            "default", "coder_route", "review_route",
        ])
        self.assertTrue(routes[0]["ready"])
        self.assertTrue(routes[1]["ready"])
        self.assertFalse(routes[2]["ready"])

    def test_ready_ways_are_listed_before_the_rest(self) -> None:
        os.environ["GEMINI_API_KEY"] = "test-value"
        options = provider_options(self.config())
        self.assertEqual(options[0].id, "gemini")
        self.assertTrue(all(item.state == NEEDS_SETUP for item in options[1:]))

    def test_the_project_choice_is_marked_and_named_in_the_headline(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "test-value"
        advice = setup_advice(self.config("anthropic"))
        chosen = [item for item in advice["options"] if item["in_use"]]
        self.assertEqual([item["id"] for item in chosen], ["anthropic"])
        self.assertIn("Anthropic is set up and in use", advice["headline"])

    def test_a_chosen_provider_that_is_not_ready_is_called_out(self) -> None:
        os.environ["OPENAI_API_KEY"] = "test-value"
        advice = setup_advice(self.config("anthropic"))
        self.assertIn("which is not ready", advice["headline"])
        self.assertIn("OpenAI", advice["headline"])

    def test_the_advice_never_asks_for_a_key_on_the_page(self) -> None:
        advice = setup_advice(self.config())
        text = json.dumps(advice).lower()
        self.assertIn("never paste a key into this page", text)
        self.assertNotIn("enter your key here", text)
        self.assertNotIn("paste your key below", text)

    def test_a_server_that_answers_with_an_error_still_counts_as_listening(self) -> None:
        import urllib.error

        with mock.patch.object(
            provider_help.urllib.request, "urlopen",
            side_effect=urllib.error.HTTPError("u", 404, "no", None, None),
        ):
            self.assertTrue(provider_help._reachable("http://127.0.0.1:1/x"))

    def test_a_dead_address_is_not_reachable(self) -> None:
        import urllib.error

        with mock.patch.object(
            provider_help.urllib.request, "urlopen", side_effect=urllib.error.URLError("gone")
        ):
            self.assertFalse(provider_help._reachable("http://127.0.0.1:1/x"))

    def test_the_advice_survives_a_round_trip_through_json(self) -> None:
        advice = setup_advice(self.config())
        self.assertEqual(json.loads(json.dumps(advice)), advice)

    def test_a_password_in_the_address_is_never_shown_back(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"]["endpoint"] = "http://alice-user:hunter2@ollama.example:11434"
        advice = setup_advice(LoadedConfig(data, self.root, [], {}))
        text = json.dumps(advice)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("alice-user", text)
        self.assertIn("ollama.example:11434", text)

    def test_an_unreadable_address_falls_back_to_plain_words(self) -> None:
        self.assertEqual(provider_help._display_endpoint("::::"), "the configured address")
        self.assertEqual(provider_help._display_endpoint(""), "the configured address")

    def test_the_answer_is_kept_briefly_so_the_screen_opens_fast(self) -> None:
        clock = {"now": 100.0}
        config = self.config()
        with mock.patch.object(provider_help, "provider_options", wraps=provider_help.provider_options) as probe:
            setup_advice(config, clock=lambda: clock["now"])
            setup_advice(config, clock=lambda: clock["now"])
            self.assertEqual(probe.call_count, 1, "the second look should not probe again")
            clock["now"] += provider_help.CACHE_SECONDS + 1
            setup_advice(config, clock=lambda: clock["now"])
            self.assertEqual(probe.call_count, 2, "a stale answer should be replaced")

    def test_asking_for_a_fresh_answer_probes_again(self) -> None:
        config = self.config()
        with mock.patch.object(provider_help, "provider_options", wraps=provider_help.provider_options) as probe:
            setup_advice(config)
            setup_advice(config, refresh=True)
            self.assertEqual(probe.call_count, 2)

    def test_a_changed_endpoint_is_not_served_from_the_old_answer(self) -> None:
        first = copy.deepcopy(DEFAULT_CONFIG)
        first["provider"]["endpoint"] = "http://127.0.0.1:11434"
        second = copy.deepcopy(DEFAULT_CONFIG)
        second["provider"]["endpoint"] = "http://127.0.0.1:9999"
        with mock.patch.object(provider_help, "provider_options", wraps=provider_help.provider_options) as probe:
            setup_advice(LoadedConfig(first, self.root, [], {}))
            setup_advice(LoadedConfig(second, self.root, [], {}))
            self.assertEqual(probe.call_count, 2)

    def test_the_kept_answers_do_not_pile_up(self) -> None:
        for index in range(60):
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["provider"]["endpoint"] = f"http://127.0.0.1:{9000 + index}"
            setup_advice(LoadedConfig(data, self.root, [], {}))
        self.assertLessEqual(len(provider_help._cache), 32)

    def test_the_probe_does_not_wait_long(self) -> None:
        self.assertLessEqual(provider_help.PROBE_TIMEOUT_SECONDS, 2.0)
        seen: dict[str, float] = {}

        def record(url: str, timeout: float = 0.0, **_rest: object):
            seen["timeout"] = timeout
            raise OSError("nothing there")

        with mock.patch.object(provider_help.urllib.request, "urlopen", side_effect=record):
            provider_options(self.config())
        self.assertEqual(seen["timeout"], provider_help.PROBE_TIMEOUT_SECONDS)


class CheckupEndpointTests(unittest.TestCase):
    def test_the_first_screen_carries_the_model_advice(self) -> None:
        import http.client
        import threading

        from our_harness.server import HarnessHTTPServer

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".harness").mkdir()
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
            config = LoadedConfig(data, root, [], {})
            server = HarnessHTTPServer(("127.0.0.1", 0), config)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
            try:
                connection.request("GET", "/api/checkup", headers={
                    "Host": f"127.0.0.1:{server.server_port}", "X-Harness-Token": server.token,
                })
                answer = connection.getresponse()
                body = json.loads(answer.read())
                self.assertEqual(answer.status, 200)
                self.assertIn("model_setup", body)
                self.assertTrue(body["model_setup"]["options"])
                self.assertIn("headline", body["model_setup"])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
