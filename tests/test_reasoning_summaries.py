import json
import threading
import time
import unittest
from unittest import mock

from our_harness import chat, goal_chat_projection, goal_dialogue, summary_observer
from our_harness.models import ProviderResponse
from our_harness.provider_activity import PublicStream, SUMMARY_CONTRACT
from our_harness.redaction import CredentialRedactor
from tests.test_provider_activity import encoded, codex_item
from tests import test_long_horizon_dialogue as fixture_module


class ReasoningSummaryTests(unittest.TestCase):
    def test_public_summary_allowlist_excludes_private_content_and_requires_no_extra_call(self):
        rows = []
        with mock.patch.object(summary_observer, "offer", side_effect=lambda sink, value: sink(value)):
            stream = PublicStream("codex", rows.append, CredentialRedactor())
            stream.feed(encoded(codex_item("reasoning", text="I will check the existing API first.", raw_reasoning="PRIVATE")))
            stream.feed(encoded(codex_item("raw_reasoning", "raw", text="PRIVATE")))
            stream.finish()
        self.assertEqual(len(rows), 1)

        self.assertEqual(rows[0]["kind"], "reasoning_summary")
        self.assertEqual(rows[0]["summary_contract"], SUMMARY_CONTRACT)
        self.assertNotIn("PRIVATE", repr(rows))
        claude = PublicStream("claude", rows.append, CredentialRedactor())
        claude.feed(encoded({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "PRIVATE"}, {"type": "redacted_thinking", "data": "PRIVATE"}]}}))
        claude.finish()
        self.assertEqual(len(rows), 1)

    def test_summary_is_not_included_in_ordinary_chat_history(self):
        messages = chat._project_chat_history([
            chat.Said("them", "Public operator summary", "", phase="reasoning_summary"),
            chat.Said("you", "Actual user request", ""),
        ], speaker=None, filed_as="portable-chat", route="arbitrary-route")
        self.assertEqual(messages, [{"role": "user", "content": "Actual user request"}])

    def test_stalled_full_or_failed_summary_sink_cannot_block_or_fail_public_replies(self):
        observer = summary_observer.SummaryObserver(capacity=1)
        started, release = threading.Event(), threading.Event()
        rows = []
        def sink(value):
            if value["kind"] == "reasoning_summary":
                started.set()
                release.wait(5)
                raise RuntimeError("Optional summary storage unavailable")
            rows.append(value)
        try:
            with mock.patch.object(summary_observer, "offer", side_effect=observer.offer):
                stream = PublicStream("codex", sink, CredentialRedactor())
                stream.feed(encoded(codex_item("reasoning", "one", text="First summary")))
                self.assertTrue(started.wait(1))
                before = time.monotonic()
                for identity in ("two", "three"):
                    stream.feed(encoded(codex_item("reasoning", identity, text="Optional summary")))
                stream.feed(encoded(codex_item("agent_message", "reply", text="Work continues")))
                stream.finish()
                self.assertLess(time.monotonic() - before, 1)
                self.assertEqual(rows[0]["text"], "Work continues")
                self.assertLessEqual(observer.pending.qsize(), 1)
        finally:
            release.set()

    def test_summary_does_not_change_agent_context_counts_freshness_or_goal_calls(self):
        fixture = fixture_module.LongHorizonDialogueTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        goal = fixture.create(request="reasoning-summary")
        store, runtime = fixture.runtime.store, fixture.runtime
        goal_id = goal["goal_id"]
        route, filed = "builder-route", "summary-chat"
        chat.keep_long_horizon_prompt(fixture.config, route, goal["objective"], filed_as=filed,
            request_id=goal["request_id"], chat_id=goal["conversation_id"], project_id="game", lead_id="builder",
            intent_sha256=chat.long_horizon_intent_sha256(goal["conversation_id"], "game", "builder", goal["objective"], []))
        calls = []
        def complete(request):
            calls.append(request)
            before = store.get(goal_id)
            task = next(t for t in before["tasks"] if t["state"] == "running")
            context = runtime._agent_context(before, task)
            conversation = store.dialogue_history(goal_id, viewer_agent_id=task["assigned_agent_id"])
            stream = PublicStream("codex", request.on_public_activity, CredentialRedactor())
            for index in range(3):
                event = codex_item("reasoning", str(index), text="I will inspect the public contract first.")
                stream.feed(encoded(event))
                stream.feed(encoded(event))
            stream.finish()
            after = store.get(goal_id)
            self.assertEqual(runtime._agent_context(after, task), context)
            self.assertEqual(after["dialogue"], before["dialogue"])
            self.assertEqual(after["budget"]["provider_calls"], before["budget"]["provider_calls"])
            page = store.dialogue_history(goal_id, viewer_agent_id=task["assigned_agent_id"], limit=1)
            self.assertEqual(page["latest_sequence"], conversation["latest_sequence"])
            self.assertEqual(page["total_messages"], conversation["total_messages"])
            self.assertFalse(any(m.get("provider_activity") for m in page["messages"]))
            return ProviderResponse(text=json.dumps(fixture_module.reply("blocked", "Need a project decision.")), finish_reason="stop")
        with mock.patch.object(summary_observer, "offer", side_effect=lambda sink, value: sink(value)), \
                mock.patch.object(chat, "create_provider", return_value=mock.Mock(complete=complete)):
            runtime.run(goal_id)
        self.assertEqual(len(calls), 2)
        page = store.dialogue_history(goal_id)
        goal_chat_projection.keep_page(fixture.config, route, store.public(store.get(goal_id)), page, filed_as=filed)
        rows = chat.read_it(fixture.config, route, filed)
        self.assertEqual(len([r for r in rows if r.phase == "reasoning_summary"]), 6)
        goal_chat_projection.keep_page(fixture.config, route, store.public(store.get(goal_id)), page, filed_as=filed)
        self.assertEqual([r.to_dict() for r in chat.read_it(fixture.config, route, filed)], [r.to_dict() for r in rows])
        participant = store.dialogue_history(goal_id, viewer_agent_id="builder", limit=2)
        self.assertEqual(len(participant["messages"]), 2)
        self.assertFalse(participant["has_more"])
        # Existing archives migrate without counting operator rows as dialogue.
        expected = goal_dialogue.conversation_projection(store.get(goal_id)["dialogue_archive"]).copy()
        store._mutate(goal_id, lambda doc, db: doc["dialogue_archive"].pop("conversation_projection"))
        self.assertEqual(goal_dialogue.conversation_projection(store.get(goal_id)["dialogue_archive"]), expected)
