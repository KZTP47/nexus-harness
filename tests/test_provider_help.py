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
from our_harness.provider_help import NEEDS_SETUP, READY, provider_options, setup_advice


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
        self.which = mock.patch.object(provider_help.shutil, "which", return_value=None)
        self.which.start()
        self.addCleanup(self.which.stop)
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
            {"ollama", "openai", "anthropic", "gemini", "codex-cli"},
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

    def test_an_installed_codex_command_is_ready(self) -> None:
        with mock.patch.object(provider_help.shutil, "which", return_value="/usr/bin/codex"):
            options = self.by_id()
        self.assertEqual(options["codex-cli"].state, READY)
        self.assertIn("/usr/bin/codex", options["codex-cli"].reason)

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
