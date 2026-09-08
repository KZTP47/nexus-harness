"""Large histories stay freshly authenticated without per-event key-file I/O."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from our_harness import chat, runtime_integrity, swarm_chats
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from tests import test_swarm_chats as chat_fixtures


class ChatLoadingPerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "arbitrary-project"
        self.project.mkdir()
        environment = mock.patch.dict(os.environ, {
            "OUR_HARNESS_SWARM_RUN_DIR": str(self.root / "private-runtime"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.project, [], {})
        self.snapshot = chat.where_it_is_kept(self.config, "arbitrary-route", "saved-history")
        self.events = self.snapshot.with_suffix(".events.jsonl")
        self.events.parent.mkdir(parents=True)

    def seed(self, count=80):
        records, turns = [], []
        for index in range(count):
            turn = chat.Said("them", f"Turn {index}: " + "history " * 240, "2026-01-01T00:00:00Z")
            turns.append(turn)
            event = {
                "schema_version": 1, "seq": index + 1, "at": turn.at, "kind": "append",
                "turns": [turn.to_dict()], "previous_hash": records[-1]["hash"] if records else "",
            }
            event["hash"] = chat._transcript_event_hash(event)
            event["integrity_mac"] = chat._transcript_event_integrity(event)
            records.append(event)
        self.events.write_text("".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for event in records
        ), encoding="utf-8")
        chat._write_transcript_anchor(self.events, records)
        self.snapshot.write_text(json.dumps([one.to_dict() for one in turns], indent=2) + "\n", encoding="utf-8")
        return records, turns

    def read(self):
        return chat.read_it(self.config, "arbitrary-route", "saved-history")

    def test_large_history_uses_bounded_key_io_and_still_verifies_every_event(self):
        records, expected = self.seed(1200)
        timings = []
        for _ in range(2):
            with mock.patch.object(runtime_integrity, "integrity_key", wraps=runtime_integrity.integrity_key) as key_reads, mock.patch.object(
                chat, "_transcript_event_integrity", wraps=chat._transcript_event_integrity,
            ) as event_checks:
                start = time.perf_counter()
                actual = self.read()
                timings.append(time.perf_counter() - start)
                self.assertLessEqual(key_reads.call_count, 2, "Key-file I/O grew with transcript event count")
                self.assertEqual(event_checks.call_count, len(records), "A warm history read skipped fresh integrity checks")
                self.assertEqual([one.to_dict() for one in actual], [one.to_dict() for one in expected])
        print(json.dumps({"large_history_bytes": self.events.stat().st_size, "events": len(records), "read_seconds": timings}))

    def test_unchanged_projection_verifies_once_and_does_not_rewrite_snapshot(self):
        _records, turns = self.seed()
        before = (self.events.read_bytes(), self.snapshot.read_bytes(), self.snapshot.stat().st_mtime_ns)
        with mock.patch.object(chat, "_read_transcript_event_records", wraps=chat._read_transcript_event_records) as reads, mock.patch.object(
            chat.os, "replace", wraps=os.replace,
        ) as replacements:
            chat._keep_it(self.config, "arbitrary-route", turns, "saved-history")
            self.assertEqual(reads.call_count, 1, "One transaction replayed the authenticated chain twice")
            replacements.assert_not_called()
        self.assertEqual(before, (self.events.read_bytes(), self.snapshot.read_bytes(), self.snapshot.stat().st_mtime_ns))

    def test_missing_compatibility_snapshot_is_recreated_from_valid_history(self):
        _records, turns = self.seed()
        before = self.events.read_bytes()
        self.snapshot.unlink()
        chat._keep_it(self.config, "arbitrary-route", turns, "saved-history")
        self.assertEqual(json.loads(self.snapshot.read_text(encoding="utf-8")), [one.to_dict() for one in turns])
        self.assertEqual(self.events.read_bytes(), before)

    def test_stale_compatibility_snapshot_is_repaired_without_extending_history(self):
        _records, turns = self.seed()
        before = self.events.read_bytes()
        self.snapshot.write_text("[]\n", encoding="utf-8")
        with mock.patch.object(chat.os, "replace", wraps=os.replace) as replacements:
            chat._keep_it(self.config, "arbitrary-route", turns, "saved-history")
        self.assertEqual(replacements.call_count, 1)
        self.assertEqual(json.loads(self.snapshot.read_text(encoding="utf-8")), [one.to_dict() for one in turns])
        self.assertEqual(self.events.read_bytes(), before)

    def test_same_size_same_timestamp_public_rehash_still_fails_fresh_keyed_verification(self):
        records, _turns = self.seed()
        self.read()
        before = self.events.stat()
        records[0]["turns"][0]["text"] = records[0]["turns"][0]["text"].replace("history", "forgery", 1)
        for index, event in enumerate(records):
            event["previous_hash"] = records[index - 1]["hash"] if index else ""
            event["hash"] = chat._transcript_event_hash(event)
        self.events.write_text("".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for event in records
        ), encoding="utf-8")
        os.utime(self.events, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.events.stat().st_size, before.st_size)
        with self.assertRaisesRegex(chat.ChatError, "keyed integrity"):
            self.read()

    def test_changed_integrity_key_invalidates_previously_read_history(self):
        self.seed()
        self.read()
        (runtime_integrity.runtime_root() / "integrity.key").write_bytes(b"Z" * 32)
        with self.assertRaisesRegex(chat.ChatError, "keyed integrity"):
            self.read()

    def test_invalid_key_file_is_rejected_after_a_successful_read(self):
        self.seed()
        self.read()
        (runtime_integrity.runtime_root() / "integrity.key").write_bytes(b"invalid")
        with self.assertRaisesRegex(HarnessError, "integrity key is invalid"):
            self.read()

    def test_interrupted_anchor_advance_verifies_suffix_then_recovers_once(self):
        _records, turns = self.seed()
        before_anchor = chat._transcript_anchor_path(self.events).read_bytes()
        turns.append(chat.Said("you", "A durable new turn", "2026-01-01T00:01:00Z"))
        with mock.patch.object(chat, "_write_transcript_anchor", side_effect=OSError("interrupted anchor move")):
            with self.assertRaisesRegex(OSError, "interrupted anchor move"):
                chat._keep_it(self.config, "arbitrary-route", turns, "saved-history")
        self.assertEqual(chat._transcript_anchor_path(self.events).read_bytes(), before_anchor)
        self.assertEqual([one.to_dict() for one in self.read()], [one.to_dict() for one in turns])
        self.assertNotEqual(chat._transcript_anchor_path(self.events).read_bytes(), before_anchor)
        self.assertEqual([one.to_dict() for one in self.read()], [one.to_dict() for one in turns])


class SavedChatInventoryPerformanceTests(unittest.TestCase):
    def setUp(self):
        fixture = chat_fixtures.PairScopedChatsTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.config, self.board = fixture.config, fixture.board
        self.config.data["providers"] = {
            "claude": {"kind": "openai", "model": "first-model", "endpoint": "http://127.0.0.1/provider-a", "api_key_env": "UNUSED_TEST_KEY_A"},
            "codex": {"kind": "openai", "model": "second-model", "endpoint": "http://127.0.0.1/provider-b", "api_key_env": "UNUSED_TEST_KEY_B"},
        }
        swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        for _ in range(15):
            swarm_chats.create(self.config, self.board, "agent-1", "agent-2")

    def test_inventory_never_opens_collaboration_history(self):
        with mock.patch(
            "our_harness.collaboration_ledger.collaboration_problem",
            side_effect=AssertionError("Inventory replayed a collaboration ledger"),
        ):
            listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual(len(listed["chats"]), 17)
        self.assertTrue(all(one["collaboration_checked"] is False for one in listed["chats"]))

    def test_inventory_and_selected_metadata_survive_an_active_project_writer(self):
        from our_harness.collaboration_ledger import CollaborationLedger
        from our_harness.safety import ProjectTransactionLock

        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        selected = next(one for one in listed["chats"] if one["id"] == listed["active"])
        route, filed_as = selected["transcript_route"], selected["filed_as"]
        CollaborationLedger(self.config, route, filed_as).begin(
            "Portable saved history", self.board["agents"], mode="project_work",
        )
        chat.keep_exchange(self.config, route, "Saved question", "Saved answer", filed_as=filed_as)
        locked, release, finished = threading.Event(), threading.Event(), threading.Event()
        failures = []
        results = {}

        def writer():
            with ProjectTransactionLock(self.config.project_root).held(1):
                locked.set()
                release.wait(8)

        def reader():
            try:
                results["list"] = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
                results["chat"] = swarm_chats.resolve(
                    self.config, self.board, "agent-1", selected["id"], allow_binding_drift=True,
                )
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()

        writing = threading.Thread(target=writer)
        reading = threading.Thread(target=reader)
        writing.start()
        try:
            self.assertTrue(locked.wait(2))
            reading.start()
            self.assertTrue(finished.wait(2), "Opening saved chats waited for an unrelated project writer")
        finally:
            release.set()
            writing.join(10)
            if reading.ident is not None:
                reading.join(10)
        self.assertEqual(failures, [])
        self.assertEqual(len(results["list"]["chats"]), 17)
        self.assertEqual([one.text for one in chat.read_it(self.config, route, filed_as)],
                         ["Saved question", "Saved answer"])
        problem = results["chat"]["collaboration_problem"]
        self.assertEqual(problem["code"], "collaboration_record_busy")
        self.assertFalse(problem["action"], "A busy writer must never offer destructive reset")
        reopened = swarm_chats.resolve(self.config, self.board, "agent-1", selected["id"])
        self.assertIsNone(reopened["collaboration_problem"])

    def test_inventory_checks_each_route_and_project_once_per_request(self):
        with mock.patch.object(swarm_chats, "_route_binding", wraps=swarm_chats._route_binding) as routes, mock.patch.object(
            swarm_chats, "_project_work_authority", wraps=swarm_chats._project_work_authority,
        ) as projects:
            listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual(len(listed["chats"]), 17)
        self.assertEqual(routes.call_count, 2, "Inventory repeated provider inspection for every saved chat")
        self.assertEqual(projects.call_count, 2, "Inventory repeated the same project authority inspection")
        self.assertTrue(all(not one["binding_problem"] for one in listed["chats"]))

    def test_another_inventory_rechecks_changed_project_availability(self):
        first = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        original = first["chats"][0]["projects"][0]["work_authority"]
        self.assertNotEqual(original["reason_code"], "missing")
        self.board["projects"][0]["is_there"] = False
        fresh = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        for conversation in fresh["chats"]:
            affected = next(one for one in conversation["projects"] if one["id"] == "project-1")
            self.assertFalse(affected["work_authority"]["can_run"])
            self.assertEqual(affected["work_authority"]["reason_code"], "missing")
        self.assertNotEqual(original["reason_code"], "missing", "Later inventory mutated an earlier response")

    def test_another_inventory_and_admission_recheck_changed_provider_configuration(self):
        first = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        selected = next(one for one in first["chats"] if len(one["pair"]) == 2)
        self.config.data["providers"]["codex"]["model"] = "changed-model"
        with mock.patch.object(swarm_chats, "_route_binding", wraps=swarm_chats._route_binding) as routes:
            fresh = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual(routes.call_count, 2)
        changed = [one for one in fresh["chats"] if len(one["pair"]) == 2]
        self.assertTrue(all(one["binding_problem"]["code"] == "agent_binding_changed" for one in changed))
        with self.assertRaisesRegex(HarnessError, "will not send that history"):
            swarm_chats.resolve(self.config, self.board, "agent-1", selected["id"])


if __name__ == "__main__":
    unittest.main()
