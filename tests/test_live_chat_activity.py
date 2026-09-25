"""Live agent activity (thinking, tools, edits, interim messages) in board chats."""
from __future__ import annotations

import contextvars
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from our_harness import cancellation, chat, summary_observer
from our_harness.provider_activity import CLAUDE_THINKING_CONTRACT, PublicStream
from our_harness.redaction import CredentialRedactor
from our_harness.server import ChatActivities, _public_activity_row


def encoded(event):
    return (json.dumps(event) + "\n").encode()


class ChatActivityFeedTests(unittest.TestCase):
    def test_feed_is_bounded_and_a_newer_state_replaces_the_older_one(self):
        feed = ChatActivities()
        feed.add_activity("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f", {"id": "cmd", "kind": "tool", "status": "requested", "name": "command_execution",
                                     "arguments": {"command": "npm test"}, "route": "codex"})
        feed.add_activity("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f", {"id": "msg", "kind": "message", "text": "Reading the files first."})
        feed.add_activity("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f", {"id": "cmd", "kind": "tool", "status": "finished", "name": "command_execution",
                                     "arguments": {"command": "npm test"}, "result": {"exit_code": 0}})
        rows = feed.read("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f")["provider_activity"]
        self.assertEqual([(one["id"], one["status"]) for one in rows], [("msg", ""), ("cmd", "finished")])
        self.assertEqual(json.loads(rows[-1]["arguments"])["command"], "npm test")
        for index in range(ChatActivities.ACTIVITY_KEEP + 20):
            feed.add_activity("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f", {"id": f"m{index}", "kind": "message", "text": "x" * 9000})
        rows = feed.read("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f")["provider_activity"]
        self.assertEqual(len(rows), ChatActivities.ACTIVITY_KEEP)
        self.assertLessEqual(max(len(one["text"]) for one in rows), 4000)
        # Turn rows and progress keep working alongside the live feed.
        feed.add_turn("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f", {"who": "them", "text": "Done."})
        self.assertEqual(feed.read("4f9c2a6e-1d2b-4c3a-9e8f-7a6b5c4d3e2f")["turns"][-1]["text"], "Done.")

    def test_row_projection_keeps_only_bounded_public_fields(self):
        row = _public_activity_row({"id": "x", "kind": "tool", "status": "failed", "name": "n",
                                    "arguments": {"command": ["a"] * 5000}, "private": "SECRET"})
        self.assertNotIn("private", row)
        self.assertLessEqual(len(row["arguments"]), 2000)


class AmbientActivityTests(unittest.TestCase):
    def test_every_call_in_a_chat_request_streams_to_it_including_worker_threads(self):
        seen = []
        requests = []

        class Provider:
            def complete(self, request):
                requests.append(request)
                if request.on_public_activity:
                    request.on_public_activity({"id": "a", "kind": "message", "text": "working"})
                from our_harness.models import ProviderResponse
                return ProviderResponse(text="ok")

        with mock.patch.object(chat, "create_provider", return_value=Provider()), \
                mock.patch.object(chat, "_known_route_setup_problem", return_value=""), \
                mock.patch.object(chat.ProviderRegistry, "provider_config", side_effect=lambda self_or_name, *a: a and a[0] or self_or_name, autospec=False):
            with chat.streaming_public_activity(seen.append):
                sink = chat._ambient_activity("codex")
                self.assertIsNotNone(sink)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [cancellation.submit(pool, chat._ambient_activity, route) for route in ("claude", "gemini")]
                    forwarded = [future.result() for future in futures]
                for route, one in zip(("claude", "gemini"), forwarded):
                    one({"id": route, "kind": "message", "text": "hi"})
                sink({"id": "c", "kind": "message", "text": "hi"})
        self.assertEqual([one["route"] for one in seen], ["claude", "gemini", "codex"])
        self.assertIsNone(chat._ambient_activity("codex"), "the sink ends with the request")

    def test_goal_recorder_takes_precedence_over_the_ambient_chat_sink(self):
        chosen = {}

        def fake_ask(config, route, text, **kwargs):
            chosen["activity"] = kwargs.get("on_public_activity")
        ambient = []
        with chat.streaming_public_activity(ambient.append):
            self.assertIsNotNone(chat._ambient_activity("codex"))
        # ask_once only falls back to the ambient sink when neither an explicit
        # sink nor a goal recorder factory was supplied.
        import inspect
        source = inspect.getsource(chat.ask_once)
        self.assertIn("if on_public_activity is None and public_activity_factory is None:", source)


class ProviderStreamTests(unittest.TestCase):
    def test_two_claude_text_blocks_of_one_message_do_not_collide(self):
        rows = []
        stream = PublicStream("claude", rows.append, CredentialRedactor())
        for text in ("First I read the file.", "Now I edit it."):
            stream.feed(encoded({"type": "assistant", "message": {"id": "same", "content": [
                {"type": "text", "text": text}]}}))
        stream.finish()
        self.assertIsNone(stream.error)
        self.assertEqual([one["text"] for one in rows], ["First I read the file.", "Now I edit it."])

    def test_claude_summarized_thinking_is_shown_as_thinking(self):
        rows = []
        with mock.patch.object(summary_observer, "offer", side_effect=lambda sink, value: sink(value)):
            stream = PublicStream("claude", rows.append, CredentialRedactor())
            stream.feed(encoded({"type": "assistant", "message": {"id": "m", "content": [
                {"type": "thinking", "thinking": "The API needs a new route."}]}}))
            stream.finish()
        self.assertEqual(rows[0]["kind"], "reasoning_summary")
        self.assertEqual(rows[0]["summary_contract"], CLAUDE_THINKING_CONTRACT)

    def test_codex_asks_for_reasoning_summaries_only_when_a_chat_watches(self):
        from tests.test_codex_cli_provider import CodexCLIProviderTests
        import tempfile
        from dataclasses import replace
        from pathlib import Path
        for watched in (False, True):
            with self.subTest(watched=watched), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _config, provider, record = CodexCLIProviderTests.make_provider(self, root)
                request = CodexCLIProviderTests.request(30)
                if watched:
                    request = replace(request, on_public_activity=lambda event: None)
                provider.complete(request)
                argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
                self.assertEqual('model_reasoning_summary="detailed"' in argv, watched)


if __name__ == "__main__":
    unittest.main()
