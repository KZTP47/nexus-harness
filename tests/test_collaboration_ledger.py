from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from our_harness import chat, collaboration_ledger as collaboration_ledger_module
from our_harness.collaboration_ledger import CollaborationLedger, ledger_paths
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class SharedCollaborationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        runtime_environment = mock.patch.dict(
            os.environ,
            {"OUR_HARNESS_SWARM_RUN_DIR": str(self.root / ".runtime")},
        )
        runtime_environment.start()
        self.addCleanup(runtime_environment.stop)
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

    def test_200k_goal_is_canonical_and_projection_is_chunked_not_corrupted(self) -> None:
        goal = "long-goal:" + ("g" * 200_000)
        ledger = CollaborationLedger(
            self.config, "claude", "pair-chat-long", session_id="session-long"
        ).begin(goal, self.participants, mode="project_work")
        events = self.events(ledger)
        self.assertEqual(events[0]["text"], goal)
        projection = ledger.projection_for("agent-1")
        self.assertIn("chunk_total", projection)
        self.assertLessEqual(len(projection), 123_000)

    def test_recipient_privacy_uses_a_nonleaking_cursor_tombstone(self) -> None:
        ledger = self.ledger()
        ledger.record_contribution({
            "speaker_id": "agent-2", "phase": "private",
            "recipient_id": "agent-1", "recipient_name": "Claude",
            "text": "SECRET-FOR-CLAUDE-ONLY",
        }, state={"private_marker": "STATE-FOR-CLAUDE-ONLY"})

        for_codex = ledger.projection_for("agent-2")
        self.assertNotIn("SECRET-FOR-CLAUDE-ONLY", for_codex)
        self.assertNotIn("STATE-FOR-CLAUDE-ONLY", for_codex)
        self.assertNotIn('"recipient', for_codex)
        self.assertIn("not_addressed_to_this_agent", for_codex)
        ledger.acknowledge("agent-2")
        self.assertIn("[No new entries.", ledger.projection_for("agent-2"))

        for_claude = ledger.projection_for("agent-1")
        self.assertIn("SECRET-FOR-CLAUDE-ONLY", for_claude)
        self.assertIn("STATE-FOR-CLAUDE-ONLY", for_claude)

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
                "speaker_id": "agent-1", "speaker_name": f"Untrusted name {number}",
                "phase": "agent_reply", "text": f"turn {number}",
            })

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(1, 33)))
        events = self.events(ledger)
        self.assertEqual([one["seq"] for one in events], list(range(1, 34)))
        self.assertEqual(len({one["hash"] for one in events}), 33)

    def test_contribution_read_waits_until_jsonl_and_integrity_anchor_are_both_published(self) -> None:
        ledger = self.ledger()
        first_reached_anchor = threading.Event()
        allow_first_anchor = threading.Event()
        real_write_anchor = collaboration_ledger_module._write_ledger_anchor
        calls = 0
        calls_lock = threading.Lock()

        def delayed_write_anchor(path: Path, events: list[dict]) -> None:
            nonlocal calls
            with calls_lock:
                calls += 1
                this_call = calls
            if this_call == 1:
                first_reached_anchor.set()
                self.assertTrue(allow_first_anchor.wait(10))
            real_write_anchor(path, events)

        def append(number: int) -> dict:
            return ledger.record_contribution({
                "speaker_id": "agent-1", "phase": "agent_reply",
                "text": f"turn {number}",
            })

        with mock.patch(
            "our_harness.collaboration_ledger._write_ledger_anchor",
            side_effect=delayed_write_anchor,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(append, 1)
                self.assertTrue(first_reached_anchor.wait(10))
                second = pool.submit(append, 2)
                # The second contribution must wait for the atomic ledger
                # transaction instead of validating JSONL against an old anchor.
                self.assertFalse(second.done())
                allow_first_anchor.set()
                self.assertEqual(first.result(timeout=10)["seq"], 2)
                self.assertEqual(second.result(timeout=10)["seq"], 3)

        self.assertEqual([one["seq"] for one in self.events(ledger)], [1, 2, 3])

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
        with self.assertRaisesRegex(HarnessError, "keyed integrity|quarantined"):
            ledger.record_state("round", {"round": 2})

    def test_a_crash_after_event_fsync_recovers_only_the_valid_authenticated_extension(self) -> None:
        ledger = self.ledger()
        anchor_path = collaboration_ledger_module._ledger_anchor_path(ledger.paths.jsonl)
        prefix_anchor = anchor_path.read_text(encoding="utf-8")
        real_write_anchor = collaboration_ledger_module._write_ledger_anchor

        def crash_before_anchor(path: Path, events: list[dict]) -> None:
            if len(events) == 2:
                raise OSError("simulated process loss before anchor publication")
            real_write_anchor(path, events)

        with mock.patch(
            "our_harness.collaboration_ledger._write_ledger_anchor",
            side_effect=crash_before_anchor,
        ):
            with self.assertRaisesRegex(OSError, "simulated process loss"):
                ledger.record_state("durable-before-crash", {"round": 1})

        self.assertEqual(2, len(self.events(ledger)))
        self.assertEqual(prefix_anchor, anchor_path.read_text(encoding="utf-8"))
        reopened = CollaborationLedger(
            self.config, "claude", "pair-chat-1", session_id="session-one"
        )
        recovered = reopened._read()
        self.assertEqual([1, 2], [one["seq"] for one in recovered])
        repaired_anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        self.assertEqual(2, repaired_anchor["value"]["count"])
        self.assertEqual(recovered[-1]["integrity_mac"], repaired_anchor["value"]["head"])

    def test_the_first_fsynced_event_also_recovers_from_an_authenticated_empty_prefix(self) -> None:
        ledger = CollaborationLedger(
            self.config, "claude", "first-event-crash", session_id="session-one"
        )
        ledger._generation = collaboration_ledger_module._rotate_generation(ledger.paths)
        real_write_anchor = collaboration_ledger_module._write_ledger_anchor

        def crash_after_first_fsync(path: Path, events: list[dict]) -> None:
            if len(events) == 1:
                raise OSError("simulated first-event anchor loss")
            real_write_anchor(path, events)

        with mock.patch(
            "our_harness.collaboration_ledger._write_ledger_anchor",
            side_effect=crash_after_first_fsync,
        ):
            with self.assertRaisesRegex(OSError, "first-event anchor loss"):
                ledger.append(kind="user_goal", phase="user_goal", text="Keep this")

        reopened = CollaborationLedger(
            self.config, "claude", "first-event-crash", session_id="session-one"
        )
        recovered = reopened._read()
        self.assertEqual(1, len(recovered))
        self.assertEqual("Keep this", recovered[0]["text"])

    def test_a_missing_jsonl_cannot_erase_an_authenticated_nonempty_history(self) -> None:
        ledger = self.ledger()
        anchor_path = collaboration_ledger_module._ledger_anchor_path(
            ledger.paths.jsonl
        )
        anchor_before = anchor_path.read_text(encoding="utf-8")
        ledger.paths.jsonl.unlink()

        reopened = CollaborationLedger(
            self.config, "claude", "pair-chat-1", session_id="session-one"
        )
        with self.assertRaisesRegex(HarnessError, "keyed integrity|quarantined"):
            reopened._read()
        self.assertEqual(anchor_before, anchor_path.read_text(encoding="utf-8"))

    def test_an_authenticated_empty_anchor_is_the_only_recoverable_missing_jsonl(self) -> None:
        ledger = CollaborationLedger(
            self.config, "claude", "empty-prefix", session_id="session-one"
        )
        ledger._generation = collaboration_ledger_module._rotate_generation(
            ledger.paths
        )
        collaboration_ledger_module._write_ledger_anchor(ledger.paths.jsonl, [])

        self.assertEqual([], ledger._read())
        appended = ledger.append(
            kind="user_goal", phase="user_goal", text="First durable event"
        )
        self.assertEqual(1, appended["seq"])

    def test_stripping_every_mac_and_the_anchor_is_not_blessed_as_legacy(self) -> None:
        ledger = self.ledger()
        ledger.record_state("durable-event", {"round": 1})
        events = self.events(ledger)
        for event in events:
            event.pop("previous_mac", None)
            event.pop("integrity_mac", None)
        ledger.paths.jsonl.write_text(
            "".join(
                collaboration_ledger_module._canonical(one) + "\n"
                for one in events
            ),
            encoding="utf-8",
        )
        anchor_path = collaboration_ledger_module._ledger_anchor_path(
            ledger.paths.jsonl
        )
        anchor_path.unlink()

        reopened = CollaborationLedger(
            self.config, "claude", "pair-chat-1", session_id="session-one"
        )
        with self.assertRaisesRegex(HarnessError, "keyed integrity|quarantined"):
            reopened._read()
        self.assertFalse(anchor_path.exists())

    def test_an_empty_jsonl_without_an_authenticated_anchor_is_not_no_state(self) -> None:
        ledger = CollaborationLedger(
            self.config, "claude", "unanchored-empty", session_id="session-one"
        )
        ledger.paths.jsonl.parent.mkdir(parents=True, exist_ok=True)
        ledger.paths.jsonl.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(HarnessError, "keyed integrity|quarantined"):
            ledger._read()

    def test_a_valid_anchor_never_blesses_an_invalid_appended_suffix(self) -> None:
        for damage in ("event_hash", "event_mac", "mac_link"):
            with self.subTest(damage=damage):
                filed_as = "damaged-" + damage
                ledger = CollaborationLedger(
                    self.config, "claude", filed_as, session_id="session-one"
                ).begin("Build safely", self.participants, mode="project_work")
                anchor_path = collaboration_ledger_module._ledger_anchor_path(
                    ledger.paths.jsonl
                )
                prefix_anchor = anchor_path.read_text(encoding="utf-8")
                ledger.record_state("durable-event", {"round": 1})
                events = self.events(ledger)
                anchor_path.write_text(prefix_anchor, encoding="utf-8")
                if damage == "event_hash":
                    events[1]["text"] = "rewritten without its public hash"
                elif damage == "event_mac":
                    events[1]["integrity_mac"] = "0" * 64
                else:
                    events[1]["previous_mac"] = "0" * 64
                    events[1]["integrity_mac"] = (
                        collaboration_ledger_module._event_integrity(events[1])
                    )
                ledger.paths.jsonl.write_text(
                    "".join(
                        collaboration_ledger_module._canonical(one) + "\n"
                        for one in events
                    ),
                    encoding="utf-8",
                )

                reopened = CollaborationLedger(
                    self.config, "claude", filed_as, session_id="session-one"
                )
                with self.assertRaisesRegex(HarnessError, "keyed integrity|quarantined"):
                    reopened._read()
                self.assertEqual(prefix_anchor, anchor_path.read_text(encoding="utf-8"))

    def test_missing_invalid_divergent_or_ahead_anchor_is_quarantined(self) -> None:
        cases = ("missing", "invalid", "divergent", "ahead")
        for damage in cases:
            with self.subTest(damage=damage):
                filed_as = "anchor-" + damage
                ledger = CollaborationLedger(
                    self.config, "claude", filed_as, session_id="session-one"
                ).begin("Build safely", self.participants, mode="project_work")
                anchor_path = collaboration_ledger_module._ledger_anchor_path(
                    ledger.paths.jsonl
                )
                if damage == "missing":
                    anchor_path.unlink()
                elif damage == "invalid":
                    anchor_path.write_text("{}\n", encoding="utf-8")
                elif damage == "divergent":
                    alternate = CollaborationLedger(
                        self.config, "claude", filed_as + "-other", session_id="other"
                    ).begin("A different history", self.participants, mode="project_work")
                    ledger.paths.jsonl.write_bytes(alternate.paths.jsonl.read_bytes())
                else:
                    ledger.record_state("second", {"round": 2})
                    lines = ledger.paths.jsonl.read_text(encoding="utf-8").splitlines()
                    ledger.paths.jsonl.write_text(lines[0] + "\n", encoding="utf-8")

                reopened = CollaborationLedger(
                    self.config, "claude", filed_as, session_id="session-one"
                )
                with self.assertRaisesRegex(HarnessError, "keyed integrity|quarantined"):
                    reopened._read()

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
            "speaker_id": "agent-1", "speaker_name": "Agent <script>", "phase": "reply",
            "text": "<script>alert('no')</script>",
        })
        mirror = ledger.paths.markdown.read_text(encoding="utf-8")
        self.assertIn("Claude (claude)", mirror)
        self.assertNotIn("Agent &lt;script&gt;", mirror)
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

    def test_projection_acknowledges_only_the_contiguous_chunk_actually_delivered(self) -> None:
        ledger = self.ledger()
        first_marker = "FIRST-ENTRY-START"
        second_marker = "SECOND-ENTRY-END"
        ledger.record_contribution({
            "speaker_id": "agent-1", "phase": "reply",
            "text": first_marker + ("a" * 79_000),
        })
        ledger.record_contribution({
            "speaker_id": "agent-2", "phase": "reply",
            "text": ("b" * 79_000) + second_marker,
        })
        seen = []
        for _ in range(8):
            projection = ledger.projection_for("agent-1")
            seen.append(projection)
            ledger.acknowledge("agent-1")
            if "[No new entries." in ledger.projection_for("agent-1"):
                break
        combined = "\n".join(seen)
        self.assertIn(first_marker, combined)
        self.assertIn(second_marker, combined)
        self.assertLess(combined.index(first_marker), combined.index(second_marker))

    def test_one_oversized_event_is_resent_then_delivered_in_ordered_fragments(self) -> None:
        ledger = self.ledger()
        text = "START-OF-LARGE-ENTRY" + ("0123456789" * 15_000) + "END-OF-LARGE-ENTRY"
        ledger.record_contribution({
            "speaker_id": "agent-1", "phase": "reply", "text": text,
        })
        first = ledger.projection_for("agent-2")
        self.assertEqual(first, ledger.projection_for("agent-2"))
        offsets: list[int] = []
        for _ in range(8):
            projection = ledger.projection_for("agent-2")
            match = re.search(r'"chunk_offset": (\d+)', projection)
            if match:
                offsets.append(int(match.group(1)))
            ledger.acknowledge("agent-2")
            if "[No new entries." in ledger.projection_for("agent-2"):
                break
        self.assertEqual(offsets, sorted(set(offsets)))
        self.assertGreaterEqual(len(offsets), 2)

    def test_new_objective_and_reset_fence_late_writers(self) -> None:
        stale = self.ledger()
        fresh = CollaborationLedger(
            self.config, "claude", "pair-chat-1", session_id="session-two"
        ).begin("A newer goal", self.participants, mode="discussion")
        with self.assertRaisesRegex(HarnessError, "no longer current"):
            stale.record_state("late", {"status": "wrong"})
        chat.start_again(self.config, "claude", "pair-chat-1")
        with self.assertRaisesRegex(HarnessError, "no longer current"):
            fresh.record_state("late", {"status": "wrong"})

    def test_contribution_author_and_goal_provenance_are_immutable(self) -> None:
        ledger = self.ledger()
        event = ledger.record_contribution({
            "speaker_id": "agent-1", "speaker_name": "Mallory",
            "speaker_route": "other", "phase": "reply", "text": "hello",
        })
        self.assertEqual(event["speaker_name"], "Claude")
        self.assertEqual(event["speaker_route"], "claude")
        self.assertEqual(event["state"]["goal_id"], "session-one")
        self.assertEqual(event["state"]["author_snapshot"]["id"], "agent-1")

    def test_cross_process_appends_share_one_authority_lock_and_hash_chain(self) -> None:
        ledger = self.ledger()
        script = r'''import copy, sys
from pathlib import Path
from our_harness.collaboration_ledger import CollaborationLedger
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
root = Path(sys.argv[1]).resolve()
config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
ledger = CollaborationLedger(config, "claude", "pair-chat-1", session_id="session-one")
for n in range(12): ledger.record_state("child", {"worker": sys.argv[2], "n": n})
'''
        env = dict(os.environ, PYTHONPATH=str(Path.cwd() / "src"))
        processes = [
            subprocess.Popen([sys.executable, "-c", script, str(self.root), str(index)], env=env)
            for index in range(3)
        ]
        self.assertTrue(all(process.wait(timeout=30) == 0 for process in processes))
        events = self.events(ledger)
        self.assertEqual(len(events), 37)
        self.assertEqual([one["seq"] for one in events], list(range(1, 38)))
