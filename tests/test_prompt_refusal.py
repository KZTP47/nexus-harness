"""A provider refusing one prompt is not a broken connection, and is said so."""

from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, provider_repair
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.prompt_refusal import prompt_was_refused
from our_harness.safety import SHOWN_PATH_CHARACTERS, validate_portable_relative_path

OPENAI = ("Codex CLI exited 1: {\"type\":\"error\",\"message\":\"Invalid prompt: your prompt was flagged as "
          "potentially violating our usage policy. Please try again with a different prompt: "
          "https://platform.openai.com/docs/guides/reasoning#advice-on-prompting\"}")
ANTHROPIC = "API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy."


class RecognisingTests(unittest.TestCase):
    def test_known_refusals_are_recognised_and_ordinary_failures_are_not(self):
        self.assertTrue(prompt_was_refused(OPENAI))
        self.assertTrue(prompt_was_refused(ANTHROPIC))
        for other in ("Codex CLI exited 1: stream disconnected", "HTTP 401 Unauthorized",
                      "Invalid prompt format: expected a string", "rate limit reached", "", None):
            with self.subTest(other=other):
                self.assertFalse(prompt_was_refused(other))

    def test_classified_as_a_refused_prompt_not_protocol_or_auth(self):
        diagnosis = provider_repair.classify_prior_failure(OPENAI)
        self.assertEqual(diagnosis["category"], "prompt-refused")
        self.assertIs(diagnosis["retryable"], True)


class SavedNoteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="prompt-refusal-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"codex": {"kind": "codex-cli", "model": "gpt-test", "endpoint": "",
                                       "command": ["codex"]}}
        self.config = LoadedConfig(data, root, [], {})

    def test_a_refusal_is_not_written_down_against_the_route(self):
        chat._write_down_that_it_would_not(self.config, "codex", OPENAI)
        self.assertNotIn("codex", chat.what_would_not_answer(self.config))

    def test_a_refusal_leaves_an_earlier_real_failure_in_place(self):
        chat._write_down_that_it_would_not(self.config, "codex", "Provider timed out.")
        chat._write_down_that_it_would_not(self.config, "codex", OPENAI)
        self.assertEqual(chat.what_would_not_answer(self.config)["codex"]["why"], "Provider timed out.")

    def test_a_refusal_saved_by_an_earlier_build_is_dropped_when_read(self):
        where = chat._where_the_noes_are(self.config)
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps({"codex": {"why": "The last time: " + OPENAI, "when": time.time(), "at": "x"}}),
                         encoding="utf-8")
        self.assertNotIn("codex", chat.what_would_not_answer(self.config))
        self.assertNotIn("codex", json.loads(where.read_text(encoding="utf-8")))  # Removed from disk too.

    def test_the_repair_panel_says_the_connection_is_fine(self):
        status = {"route": "codex", "kind": "codex-cli", "installed": True,
                  "state": "authenticated", "authentication": "signed-in"}
        with mock.patch.object(provider_repair, "connection_status", return_value=status), \
             mock.patch.object(provider_repair.chat, "what_would_not_answer",
                               return_value={"codex": {"why": OPENAI, "when": time.time(), "at": "x"}}):
            plan = provider_repair.repair_plan(self.config, "codex")
        repair = plan.get("repair", plan)
        self.assertEqual(repair["state"], "prompt-refused")
        self.assertIn("not the connection", repair["title"])
        self.assertNotIn("login", [one.get("id") for one in repair["actions"]])


class EchoTests(unittest.TestCase):
    def test_a_rejected_path_is_echoed_only_briefly(self):
        rambling = "unicorn.html " + "Oops no, I must output valid JSON and fix the tool. " * 20 + "done. "
        with self.assertRaises(HarnessError) as caught:
            validate_portable_relative_path(rambling)
        message = str(caught.exception)
        self.assertLess(len(message), SHOWN_PATH_CHARACTERS + 120)
        self.assertIn("more characters", message)

    def test_short_paths_are_echoed_unchanged(self):
        with self.assertRaisesRegex(HarnessError, r"end with a space or dot: bad\.$"):
            validate_portable_relative_path("bad.")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
