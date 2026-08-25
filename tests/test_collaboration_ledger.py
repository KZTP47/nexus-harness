from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from our_harness import chat
from our_harness.collaboration_ledger import CollaborationLedger, ledger_paths
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class SharedCollaborationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.participants = [
            {"id": "agent-1", "name": "Claude", "who": "claude"},
            {"id": "agent-2", "name": "Codex", "who": "codex"},
        ]

    def ledger(self) -> CollaborationLedger:
        return CollaborationLedger(
            self.config, "claude", "pair-chat-1", session_id="session-one"
        ).begin("Build the parser together", self.participants, mode="project_work")

    def events(self, ledger: CollaborationLedger) -> list[dict]:
        return [
            json.loads(line)
            for line in ledger.paths.jsonl.read_text(encoding="utf-8").splitlines()
        ]

    def test_canonical_file_is_append_only_hash_chained_and_has_a_readable_mirror(self) -> None:
        ledger = self.ledger()
        ledger.record_contribution({
            "speaker_id": "agent-1", "speaker_name": "Claude",
            "speaker_route": "claude", "recipient_name": "Codex",
            "phase": "agent_discussion", "text": "I propose design A.",
        })
        ledger.finish("Design A was accepted.", complete=True, stopped_because="complete")

        events = self.events(ledger)
        self.assertEqual([one["seq"] for one in events], [1, 2, 3])
        self.assertEqual(events[0]["previous_hash"], "")
        self.assertEqual(events[1]["previous_hash"], events[0]["hash"])
        self.assertEqual(events[2]["previous_hash"], events[1]["hash"])
        mirror = ledger.paths.markdown.read_text(encoding="utf-8")
        self.assertIn("Nexus shared collaboration ledger", mirror)
        self.assertIn("I propose design A.", mirror)
        self.assertIn("Design A was accepted.", mirror)

    def test_each_agent_gets_the_goal_and_only_the_delta_after_its_cursor(self) -> None:
        ledger = self.ledger()
        first = ledger.projection_for("agent-1")
        self.assertIn("CURRENT USER GOAL\nBuild the parser together", first)
        self.assertIn('"seq": 1', first)
        self.assertIn('"speaker": "User"', first)
        ledger.acknowledge("agent-1")
        ledger.record_contribution({
            "speaker_id": "agent-2", "speaker_name": "Codex",
            "speaker_route": "codex", "phase": "agent_reply",
            "text": "The new tokenizer tests pass.",
        })
        second = ledger.projection_for("agent-1")
        self.assertIn("The new tokenizer tests pass.", second)
        self.assertNotIn('"speaker": "User"', second)
        ledger.acknowledge("agent-1")
        third = ledger.projection_for("agent-1")
        self.assertIn("[No new entries.", third)
        # Codex has its own cursor and still receives both current-session events.
        codex = ledger.projection_for("agent-2")
        self.assertIn('"speaker": "User"', codex)
        self.assertIn("The new tokenizer tests pass.", codex)

    def test_stale_session_cursors_are_bounded(self) -> None:
        ledger = self.ledger()
        for number in range(300):
            ledger.projection_for(f"agent-{number}")
            ledger.acknowledge(f"agent-{number}")
        saved = json.loads(ledger.paths.cursors.read_text(encoding="utf-8"))
        self.assertLessEqual(len(saved["agents"]), 256)

    def test_projection_names_relative_full_chat_files_not_a_personal_absolute_path(self) -> None:
        projection = self.ledger().projection_for("agent-1")
        self.assertIn(".harness/chats/", projection)
        self.assertIn("collaboration.md", projection)
        self.assertNotIn(str(self.root), projection)

    def test_a_new_process_instance_can_resume_the_same_session_and_cursors(self) -> None:
        first = self.ledger()
        first.projection_for("agent-1")
        first.acknowledge("agent-1")
        first.record_state("round", {"round": 1, "remaining": ["Run tests"]})
        resumed = CollaborationLedger(
            self.config, "claude", "pair-chat-1", session_id="session-one"
        )
        projection = resumed.projection_for("agent-1")
        self.assertIn("\"round\": 1", projection)
        self.assertNotIn('"speaker": "User"', projection)

    def test_prepared_context_is_resent_until_the_provider_turn_is_acknowledged(self) -> None:
        ledger = self.ledger()
        prepared = ledger.projection_for("agent-1")
        retried = ledger.projection_for("agent-1")
        self.assertIn('"speaker": "User"', prepared)
        self.assertIn('"speaker": "User"', retried)
        ledger.acknowledge("agent-1")
        after_answer = ledger.projection_for("agent-1")
        self.assertIn("[No new entries.", after_answer)

    def test_concurrent_completed_turns_receive_unique_monotonic_sequence_numbers(self) -> None:
        ledger = self.ledger()

        def append(number: int) -> None:
            ledger.record_contribution({
                "speaker_id": f"agent-{number}", "speaker_name": f"Agent {number}",
                "phase": "agent_reply", "text": f"turn {number}",
            })

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(1, 33)))
        events = self.events(ledger)
        self.assertEqual([one["seq"] for one in events], list(range(1, 34)))
        self.assertEqual(len({one["hash"] for one in events}), 33)

    def test_credentials_are_redacted_before_they_reach_disk_or_a_projection(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "claude": {
                "kind": "claude-cli", "model": "claude",
                "api_key_env": "LEDGER_TEST_SECRET",
            },
        }
        config = LoadedConfig(data, self.root, [], {})
        secret = "credential-that-must-not-survive"
        with mock.patch.dict(os.environ, {"LEDGER_TEST_SECRET": secret}):
            ledger = CollaborationLedger(
                config, "claude", "private", session_id="secret-session"
            ).begin(f"Use {secret}", self.participants, mode="discussion")
            ledger.record_state("state", {"token": secret})
            projection = ledger.projection_for("agent-1")
        all_text = (
            ledger.paths.jsonl.read_text(encoding="utf-8")
            + ledger.paths.markdown.read_text(encoding="utf-8")
            + projection
        )
        self.assertNotIn(secret, all_text)
        self.assertIn("[REDACTED]", all_text)

    def test_a_damaged_or_externally_modified_suffix_is_not_extended(self) -> None:
        ledger = self.ledger()
        with ledger.paths.jsonl.open("a", encoding="utf-8") as stream:
            stream.write('{"seq": 999, "text": "forged"}\n')
        with self.assertRaisesRegex(HarnessError, "damaged|modified"):
            ledger.record_state("round", {"round": 2})

    def test_starting_chat_again_removes_transcript_mirror_ledger_and_cursors(self) -> None:
        ledger = self.ledger()
        ledger.projection_for("agent-1")
        transcript = chat.where_it_is_kept(self.config, "claude", "pair-chat-1")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("[]\n", encoding="utf-8")
        paths = ledger_paths(self.config, "claude", "pair-chat-1")
        chat.start_again(self.config, "claude", "pair-chat-1")
        self.assertFalse(transcript.exists())
        self.assertFalse(paths.jsonl.exists())
        self.assertFalse(paths.markdown.exists())
        self.assertFalse(paths.cursors.exists())

    def test_markdown_mirror_does_not_render_agent_supplied_html(self) -> None:
        ledger = self.ledger()
        ledger.record_contribution({
            "speaker_name": "Agent <script>", "phase": "reply",
            "text": "<script>alert('no')</script>",
        })
        mirror = ledger.paths.markdown.read_text(encoding="utf-8")
        self.assertIn("Agent &lt;script&gt;", mirror)
        self.assertIn("    <script>alert('no')</script>", mirror)
        self.assertNotIn("\n<script>alert", mirror)

    def test_partial_readable_mirror_is_rebuilt_from_the_canonical_chain(self) -> None:
        ledger = self.ledger()
        ledger.paths.markdown.write_text("partial write", encoding="utf-8")
        ledger.record_state("round", {"round": 2})
        mirror = ledger.paths.markdown.read_text(encoding="utf-8")
        self.assertIn("Nexus shared collaboration ledger", mirror)
        self.assertIn("nexus-ledger-seq:1", mirror)
        self.assertIn("nexus-ledger-seq:2", mirror)
        self.assertNotIn("partial write", mirror)
