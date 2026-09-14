from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from our_harness import chat, goal_chat_projection, goal_provider_activity, long_horizon
from our_harness.models import HarnessError, ProviderResponse
from our_harness.provider_activity import PublicStream, FINGERPRINT
from our_harness.redaction import CredentialRedactor
from tests import test_codex_cli_provider as codex_fixture
from tests import test_subscription_cli as claude_fixture
from tests import test_long_horizon_dialogue as dialogue_fixture


def encoded(event):
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


def codex_item(kind, identity="item-1", **fields):
    return {"type": "item.completed", "item": {"type": kind, "id": identity, **fields}}


class PublicStreamTests(unittest.TestCase):
    def test_chunked_unicode_redaction_private_exclusion_and_final_dedup(self):
        rows = []
        redactor = mock.Mock()
        redactor.text.side_effect = lambda text: text.replace("secret-value", "[REDACTED]")
        redactor.value.side_effect = lambda value: json.loads(json.dumps(value).replace("secret-value", "[REDACTED]"))
        stream = PublicStream("codex", rows.append, redactor, {"required": ["answer"]})
        events = [codex_item("raw_reasoning", text="PRIVATE_SENTINEL"),
                  codex_item("agent_message", "public", text="Reading 🙂 secret-value"),
                  codex_item("agent_message", "final", text='{"answer":"Done"}'),
                  codex_item("command_execution", "tool", command="arbitrary check", exit_code=1, aggregated_output="failed")]
        data = b"".join(encoded(e) for e in events + [events[1]])
        for start in range(0, len(data), 7):
            stream.feed(data[start:start + 7])
        stream.finish()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "Reading 🙂 [REDACTED]")
        self.assertEqual(rows[1]["status"], "failed")
        self.assertNotIn("PRIVATE_SENTINEL", repr(rows))

    def test_claude_tools_and_public_text_exclude_thinking_and_images(self):
        rows = []
        stream = PublicStream("claude", rows.append, CredentialRedactor())
        stream.feed(encoded({"type": "assistant", "message": {"id": "a", "content": [
            {"type": "thinking", "thinking": "PRIVATE"}, {"type": "text", "text": "Reading files"},
            {"type": "tool_use", "id": "tool-a", "name": "Read", "input": {"file_path": "other/root.txt"}}]}}))
        stream.feed(encoded({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-a", "content": [
                {"type": "text", "text": "file contents"}, {"type": "image", "source": "PRIVATE_IMAGE"}]}]}}))
        stream.finish()
        self.assertEqual([r.get("status") for r in rows], [None, "requested", "finished"])
        self.assertEqual(rows[-1]["name"], "Read")
        self.assertNotIn("PRIVATE", repr(rows))

    def test_bounded_output_discloses_truncation_and_callback_failure_surfaces(self):
        rows = []
        stream = PublicStream("codex", rows.append, CredentialRedactor())
        stream.feed(encoded(codex_item("command_execution", aggregated_output="x" * 100000)))
        stream.finish()
        self.assertTrue(rows[0]["truncated"])
        self.assertLess(len(json.dumps(rows)), 24000)
        failed = PublicStream("codex", mock.Mock(side_effect=RuntimeError("storage failed")), CredentialRedactor())
        failed.feed(encoded(codex_item("agent_message", text="public")))
        with self.assertRaisesRegex(HarnessError, "could not be recorded"):
            failed.finish()

    def test_real_redactor_and_malformed_or_oversized_stream_fail_closed(self):
        rows = []
        stream = PublicStream("codex", rows.append, CredentialRedactor())
        stream.feed(encoded(codex_item("command_execution", command="inspect", aggregated_output="password=never-display-this")))
        stream.finish()
        self.assertNotIn("never-display-this", repr(rows))
        for data in (b"invalid json\n", b"x" * 100):
            broken = PublicStream("codex", rows.append, CredentialRedactor(), line_limit=32)
            broken.feed(data)
            with self.assertRaises(HarnessError):
                broken.finish()

    def test_live_capture_keeps_complete_events_on_timeout_with_partial_tail(self):
        from our_harness.providers.codex_cli import _run_bounded
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "stream.py"
            script.write_text('import json,time\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","id":"one","text":"Public before timeout"}}),flush=True)\nprint("{",end="",flush=True)\ntime.sleep(5)\n')
            rows = []
            result = _run_bounded([sys.executable, str(script)], cwd=root, stdin_text=None,
                timeout_seconds=0.5, max_output_bytes=10000,
                public_stream=PublicStream("codex", rows.append, CredentialRedactor()))
            self.assertTrue(result.timed_out)
            self.assertEqual(rows[0]["text"], "Public before timeout")

    def test_live_codex_child_public_activity_precedes_final_and_survives_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                gate = root / "activity-was-delivered"
                injection = '\n'.join([
                    'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","id":"public","text":"Still working"}}), flush=True)',
                    'import time',
                    f'gate = pathlib.Path({str(gate)!r})',
                    'until = time.monotonic() + 5',
                    'while not gate.exists() and time.monotonic() < until: time.sleep(0.01)',
                    'assert gate.exists(), "Activity was buffered until final reply"',
                    'raise SystemExit(17)' if fail else '',
                    'prompt = sys.stdin.read()',
                ])
                script = codex_fixture.FAKE_CODEX.replace('prompt = sys.stdin.read()', injection)
                rows = []
                def sink(value):
                    rows.append(value)
                    gate.write_text("received")
                with mock.patch.object(codex_fixture, 'FAKE_CODEX', script):
                    _, provider, _ = codex_fixture.CodexCLIProviderTests().make_provider(root)
                    request = replace(codex_fixture.CodexCLIProviderTests.request(), on_public_activity=sink)
                    if fail:
                        with self.assertRaises(HarnessError):
                            provider.complete(request)
                    else:
                        self.assertEqual(json.loads(provider.complete(request).text), {"answer": "ok"})
                self.assertEqual(rows[0]["text"], "Still working")

    def test_standard_claude_child_streams_while_preserving_terminal_usage(self):
        fixture = claude_fixture.RunningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        script = claude_fixture.CLAUDE_LIKE.replace('prompt = sys.stdin.read()', '''
assert arguments[arguments.index("--output-format") + 1] == "stream-json"
assert "--verbose" in arguments
print(json.dumps({"type":"assistant","message":{"id":"public","content":[{"type":"text","text":"Inspecting"}]}}), flush=True)
prompt = sys.stdin.read()
''')
        tool = claude_fixture.fake_tool(fixture.folder, "stream-fixture", script)
        rows = []
        response = fixture.provider("claude-cli", tool).complete(fixture.request(on_public_activity=rows.append))
        self.assertEqual(response.text, "Say hello")
        self.assertEqual(response.input_tokens, 11)
        self.assertEqual(rows[0]["text"], "Inspecting")

    def test_custom_claude_command_reports_coverage_without_changing_arguments(self):
        fixture = claude_fixture.RunningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        tool = claude_fixture.fake_tool(fixture.folder, "custom-fixture", claude_fixture.CLAUDE_LIKE)
        rows = []
        response = fixture.provider("claude-cli", tool, arguments=["-p", "--output-format", "json"]).complete(
            fixture.request(on_public_activity=rows.append))
        self.assertEqual(response.text, "Say hello")
        self.assertIn("unavailable for this custom Claude command", rows[0]["text"])
        self.assertEqual(rows[0]["kind"], "notice")


class DurableActivityTests(unittest.TestCase):
    def test_engine_to_chat_live_reopen_order_and_stale_dispatch(self):
        fixture = dialogue_fixture.LongHorizonDialogueTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        goal = fixture.create(request="public-activity")
        store = fixture.runtime.store
        route, filed = "builder-route", "activity-chat"
        chat.keep_long_horizon_prompt(fixture.config, route, goal["objective"], filed_as=filed,
            request_id=goal["request_id"], chat_id=goal["conversation_id"], project_id="game", lead_id="builder",
            intent_sha256=chat.long_horizon_intent_sha256(goal["conversation_id"], "game", "builder", goal["objective"], []))
        snapshots, callbacks, old_pages = [], [], []
        def project():
            current = store.get(goal["goal_id"])
            goal_chat_projection.keep_page(fixture.config, route, store.public(current),
                store.dialogue_history(goal["goal_id"]), filed_as=filed)
            rows = chat.read_it(fixture.config, route, filed)
            snapshots.append(rows)
            return rows
        def complete(request):
            sink = request.on_public_activity
            self.assertTrue(callable(sink))
            callbacks.append(sink)
            stream = PublicStream("codex", sink, CredentialRedactor(), request.response_format.schema)
            def feed(event):
                reader = threading.Thread(target=stream.feed, args=(encoded(event),))
                reader.start()
                reader.join(5)
                self.assertFalse(reader.is_alive(), "Provider reader could not persist activity")
                stream.finish()
            feed(codex_item("agent_message", "public", text="I am inspecting the files."))
            start = {"type": "item.started", "item": {"type": "command_execution", "id": "cmd", "command": "portable-test"}}
            feed(start)
            old_pages.append(store.dialogue_history(goal["goal_id"]))
            live = project()
            self.assertTrue(any(one.text == "I am inspecting the files." for one in live))
            self.assertEqual(json.loads([one for one in live if one.phase == "agent_tool"][-1].text)["status"], "requested")
            feed(codex_item("command_execution", "cmd", command="portable-test", exit_code=0, aggregated_output="ok"))
            stream.finish()
            # Replayed identical event must remain one archive entry.
            sink({"schema_version": 1, "contract_fingerprint": FINGERPRINT, "provider": "codex", "id": "public", "kind": "message", "text": "I am inspecting the files."})
            with self.assertRaisesRegex(HarnessError, "different content"):
                sink({"schema_version": 1, "contract_fingerprint": FINGERPRINT, "provider": "codex", "id": "public", "kind": "message", "text": "Changed content"})
            return ProviderResponse(text=json.dumps(dialogue_fixture.reply("blocked", "Need a project decision.")), finish_reason="stop")
        with mock.patch.object(chat, "create_provider", return_value=mock.Mock(complete=complete)):
            fixture.runtime.run(goal["goal_id"])
        rows = project()
        tools = [one for one in rows if one.phase == "agent_tool"]
        self.assertEqual(len(tools), len(callbacks))
        self.assertEqual(json.loads(tools[0].text)["status"], "finished")
        self.assertFalse(any(one.correlation.get("delivery") for one in rows if one.text == "I am inspecting the files."))
        self.assertEqual([one.to_dict() for one in project()], [one.to_dict() for one in rows])
        # A delayed old page must never downgrade a completed tool row.
        goal_chat_projection.keep_page(fixture.config, route, store.public(store.get(goal["goal_id"])),
                                       old_pages[0], filed_as=filed)
        self.assertEqual([one.to_dict() for one in chat.read_it(fixture.config, route, filed)], [one.to_dict() for one in rows])
        reopened = long_horizon.GoalStore(fixture.config)
        page = reopened.dialogue_history(goal["goal_id"])
        self.assertEqual(len([m for m in page["messages"] if m.get("provider_activity")]), 3 * len(callbacks))
        agent_page = reopened.dialogue_history(goal["goal_id"], viewer_agent_id="builder")
        self.assertFalse(any(m.get("provider_activity") for m in agent_page["messages"]))
        with self.assertRaisesRegex(HarnessError, "Unsupported"):
            callbacks[0]({"schema_version": 999})
        # A late callback may not cross a changed dispatch identity after restart.
        def change(document, _db):
            document["tasks"][0]["provider_effect_id"] = "different-effect"
        store._mutate(goal["goal_id"], change)
        with self.assertRaisesRegex(HarnessError, "Stale"):
            callbacks[0]({"schema_version": 1, "contract_fingerprint": FINGERPRINT, "provider": "codex", "id": "late", "kind": "message", "text": "late"})
