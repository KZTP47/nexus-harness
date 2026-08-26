from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, swarm, swarm_chats
from our_harness.collaboration_ledger import CollaborationLedger
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import ProviderResponse


class PairScopedChatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.first = self.root / "first"
        self.second = self.root / "second"
        self.first.mkdir()
        self.second.mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.config.data["providers"] = {
            "claude": {"kind": "claude-cli", "model": "claude"},
            "codex": {"kind": "codex-cli", "model": "codex"},
        }
        self.board = {
            "agents": [
                {"id": "agent-1", "name": "Claude", "who": "claude", "ready": True},
                {"id": "agent-2", "name": "Codex", "who": "codex", "ready": True},
            ],
            "projects": [
                {"id": "project-1", "name": "First", "path": str(self.first), "is_there": True},
                {"id": "project-2", "name": "Second", "path": str(self.second), "is_there": True},
            ],
            "works_on": [
                {"agent": agent, "project": project}
                for agent in ("agent-1", "agent-2")
                for project in ("project-1", "project-2")
            ],
            "talks_to": [{"one": "agent-1", "other": "agent-2"}],
        }

    def test_first_pair_chat_is_canonical_and_selects_a_shared_project(self) -> None:
        first = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        reverse = swarm_chats.list_for_agent(self.config, self.board, "agent-2")
        self.assertEqual(len(first["chats"]), 1)
        self.assertEqual(first["chats"][0]["id"], reverse["chats"][0]["id"])
        self.assertEqual(first["chats"][0]["pair"], ["agent-1", "agent-2"])
        self.assertEqual(first["chats"][0]["project"], "project-1")
        self.assertEqual(
            [one["name"] for one in first["chats"][0]["pair_agents"]],
            ["Claude", "Codex"],
        )

    def test_multiple_chats_keep_distinct_transcripts_and_active_projects(self) -> None:
        first = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        first_chat = first["chats"][0]
        made = swarm_chats.create(self.config, self.board, "agent-1", "agent-2")
        second_chat = next(one for one in made["chats"] if one["id"] == made["active"])
        self.assertNotEqual(first_chat["filed_as"], second_chat["filed_as"])
        self.assertTrue(first_chat["web_legacy_candidate"])
        self.assertFalse(second_chat["web_legacy_candidate"])

        changed = swarm_chats.select_project(
            self.config, self.board, "agent-1", second_chat["id"], "project-2"
        )
        selected = next(one for one in changed["chats"] if one["id"] == second_chat["id"])
        self.assertEqual(selected["project"], "project-2")

        chat.keep_exchange(
            self.config, "claude", "hello", "second answer",
            filed_as=second_chat["filed_as"],
        )
        self.assertEqual(chat.read_it(
            self.config, "claude", first_chat["filed_as"]
        ), [])
        self.assertEqual(chat.read_it(
            self.config, "claude", second_chat["filed_as"]
        )[-1].text, "second answer")

    def test_legacy_agent_transcript_is_never_adopted_by_a_pair(self) -> None:
        legacy = "Claude"
        chat.keep_exchange(
            self.config, "claude", "old unrelated request", "old unrelated answer",
            filed_as=legacy,
        )
        legacy_source = chat.where_it_is_kept(
            self.config, "claude", legacy
        ).name

        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        conversation = next(
            one for one in listed["chats"] if one["pair"] == ["agent-1", "agent-2"]
        )
        recovered = next(
            one for one in listed["chats"] if one["pair"] == ["agent-1"]
        )

        self.assertTrue(conversation["filed_as"].startswith("pair-chat-"))
        self.assertEqual(chat.read_it(
            self.config, "claude", conversation["filed_as"]
        ), [])
        self.assertEqual(chat.read_it(
            self.config, "claude", legacy
        )[-1].text, "old unrelated answer")
        self.assertEqual(recovered["name"], "Recovered older chat")
        self.assertEqual(recovered["legacy_source"], legacy_source)
        self.assertEqual(chat.read_it(
            self.config, "claude", recovered["filed_as"]
        )[-1].text, "old unrelated answer")

        # Recovery is idempotent and never mutates or removes the source.
        again = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual(len([
            one for one in again["chats"] if one["legacy_source"]
        ]), 1)
        self.assertTrue(chat.where_it_is_kept(
            self.config, "claude", legacy
        ).is_file())

    def test_legacy_recovery_uses_stable_filed_name_after_agent_rename(self) -> None:
        chat.keep_exchange(
            self.config, "claude", "before rename", "still here", filed_as="Claude"
        )
        board = copy.deepcopy(self.board)
        board["agents"][0]["name"] = "Claude renamed"
        board["agents"][0]["filed_as"] = "Claude"

        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        recovered = next(one for one in listed["chats"] if one["legacy_source"])

        self.assertEqual(recovered["pair"], ["agent-1"])
        self.assertEqual(chat.read_it(
            self.config, "claude", recovered["filed_as"]
        )[-1].text, "still here")

    def test_schema_two_transcript_alias_is_repaired_to_pair_owned_storage(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        conversation = listed["chats"][0]
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["schema_version"] = 2
        registry["chats"][0]["filed_as"] = "Claude"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        chat.keep_exchange(
            self.config, "claude", "contaminated", "wrong pair",
            filed_as="Claude",
        )

        repaired = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        active = repaired["chats"][0]

        self.assertEqual(active["filed_as"], conversation["filed_as"])
        self.assertEqual(chat.read_it(
            self.config, "claude", active["filed_as"]
        ), [])
        self.assertEqual(chat.read_it(
            self.config, "claude", "Claude"
        )[-1].text, "wrong pair")
        persisted = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 5)
        self.assertEqual(persisted["chats"][0]["filed_as"], conversation["filed_as"])

    def test_proven_legacy_pair_blocks_are_recovered_but_unlabelled_turns_are_not(self) -> None:
        chat.keep_exchange(
            self.config, "claude", "ambiguous direct question", "ambiguous answer",
            filed_as="Claude",
        )
        chat.keep_multiparty_exchange(
            self.config, "claude", "Work together", "Pair final",
            filed_as="Claude",
            lead=self.board["agents"][0],
            participants=self.board["agents"],
            contributions=[
                {
                    "speaker_id": "agent-1", "speaker_name": "Claude",
                    "speaker_route": "claude", "recipient_id": "agent-1,agent-2",
                    "recipient_name": "Team deliberation", "phase": "lead_draft",
                    "text": "Claude draft",
                },
                {
                    "speaker_id": "agent-2", "speaker_name": "Codex",
                    "speaker_route": "codex", "recipient_id": "agent-1",
                    "recipient_name": "Claude", "phase": "agent_reply",
                    "text": "Codex reply",
                },
            ],
        )

        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        conversation = listed["chats"][0]
        recovered = chat.read_it(
            self.config, "claude", conversation["filed_as"]
        )

        self.assertEqual([one.text for one in recovered], [
            "Work together", "Claude draft", "Codex reply", "Pair final",
        ])
        self.assertNotIn("ambiguous direct question", [one.text for one in recovered])
        # Re-listing is idempotent and the source remains an untouched archive.
        swarm_chats.list_for_agent(self.config, self.board, "agent-2")
        self.assertEqual(len(chat.read_it(
            self.config, "codex", conversation["filed_as"]
        )), 4)
        self.assertIn("ambiguous direct question", [
            one.text for one in chat.read_it(self.config, "claude", "Claude")
        ])

    def test_proven_history_cannot_leak_into_a_different_pair(self) -> None:
        chat.keep_multiparty_exchange(
            self.config, "claude", "Claude and Codex only", "Pair final",
            filed_as="Claude",
            lead=self.board["agents"][0], participants=self.board["agents"],
            contributions=[{
                "speaker_id": "agent-2", "speaker_name": "Codex",
                "speaker_route": "codex", "recipient_id": "agent-1",
                "recipient_name": "Claude", "phase": "agent_reply",
                "text": "Codex-only reply",
            }],
        )
        board = copy.deepcopy(self.board)
        board["agents"].append({
            "id": "agent-3", "name": "Reviewer", "who": "claude", "ready": True,
        })
        board["talks_to"].append({"one": "agent-1", "other": "agent-3"})

        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        correct = next(one for one in listed["chats"] if one["pair"] == ["agent-1", "agent-2"])
        other = next(one for one in listed["chats"] if one["pair"] == ["agent-1", "agent-3"])

        self.assertIn("Codex-only reply", [one.text for one in chat.read_it(
            self.config, "claude", correct["filed_as"]
        )])
        self.assertEqual(chat.read_it(
            self.config, "claude", other["filed_as"]
        ), [])

    def test_project_must_be_shared_by_both_agents(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        chat_id = listed["active"]
        board = copy.deepcopy(self.board)
        board["works_on"] = [
            one for one in board["works_on"]
            if not (one["agent"] == "agent-2" and one["project"] == "project-2")
        ]
        with self.assertRaisesRegex(Exception, "Both agents"):
            swarm_chats.select_project(
                self.config, board, "agent-1", chat_id, "project-2"
            )

    def test_default_chat_prefers_a_connected_pair_with_a_shared_project(self) -> None:
        board = copy.deepcopy(self.board)
        board["agents"].insert(1, {
            "id": "agent-no-project", "name": "Reviewer", "who": "claude",
            "ready": True,
        })
        board["talks_to"].insert(0, {
            "one": "agent-1", "other": "agent-no-project",
        })

        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        active = next(
            one for one in listed["chats"] if one["id"] == listed["active"]
        )

        self.assertEqual(active["pair"], ["agent-1", "agent-2"])
        self.assertEqual(active["project"], "project-1")

    def test_an_explicitly_selected_projectless_pair_stays_selected(self) -> None:
        board = copy.deepcopy(self.board)
        board["agents"].insert(1, {
            "id": "agent-no-project", "name": "Reviewer", "who": "claude",
            "ready": True,
        })
        board["talks_to"].insert(0, {
            "one": "agent-1", "other": "agent-no-project",
        })
        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        projectless = next(
            one for one in listed["chats"]
            if one["pair"] == ["agent-1", "agent-no-project"]
        )

        swarm_chats.activate(
            self.config, board, "agent-1", projectless["id"]
        )
        again = swarm_chats.list_for_agent(self.config, board, "agent-1")

        self.assertEqual(again["active"], projectless["id"])

    def test_archive_and_restore_never_remove_the_selected_transcript(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        first = listed["chats"][0]
        made = swarm_chats.create(self.config, self.board, "agent-1", "agent-2")
        second = next(one for one in made["chats"] if one["id"] == made["active"])
        chat.keep_exchange(
            self.config, "claude", "one", "kept", filed_as=first["filed_as"]
        )
        chat.keep_exchange(
            self.config, "claude", "two", "removed", filed_as=second["filed_as"]
        )

        after = swarm_chats.delete(
            self.config, self.board, "agent-1", second["id"]
        )
        archived = next(one for one in after["chats"] if one["id"] == second["id"])
        self.assertTrue(archived["archived_at"])
        self.assertEqual(after["active"], first["id"])
        self.assertTrue(chat.where_it_is_kept(
            self.config, "claude", first["filed_as"]
        ).is_file())
        self.assertTrue(chat.where_it_is_kept(
            self.config, "claude", second["filed_as"]
        ).is_file())
        self.assertEqual(chat.read_it(
            self.config, "claude", second["filed_as"]
        )[-1].text, "removed")

        restored = swarm_chats.restore(
            self.config, self.board, "agent-1", second["id"]
        )
        again = next(one for one in restored["chats"] if one["id"] == second["id"])
        self.assertFalse(again["archived_at"])
        self.assertEqual(restored["active"], second["id"])

    def test_archive_and_project_rebind_fence_inflight_collaboration_writers(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        conversation = listed["chats"][0]
        participants = self.board["agents"]
        stale = CollaborationLedger(
            self.config, "claude", conversation["filed_as"], session_id="old"
        ).begin("old goal", participants, mode="project_work")
        swarm_chats.select_project(
            self.config, self.board, "agent-1", conversation["id"], "project-2"
        )
        with self.assertRaisesRegex(Exception, "no longer current"):
            stale.record_state("late", {"status": "wrong"})

        stale = CollaborationLedger(
            self.config, "claude", conversation["filed_as"], session_id="old-2"
        ).begin("another old goal", participants, mode="project_work")
        swarm_chats.delete(
            self.config, self.board, "agent-1", conversation["id"]
        )
        with self.assertRaisesRegex(Exception, "no longer current"):
            stale.record_state("late", {"status": "wrong"})

    def test_swarm_save_fences_chats_when_provider_binding_changes(self) -> None:
        board_path = self.root / "swarm.json"
        with mock.patch.object(swarm, "where_it_lives", return_value=board_path):
            saved = swarm.save(self.board, self.config)
            listed = swarm_chats.list_for_agent(
                self.config, saved.to_dict(), "agent-1"
            )
            conversation = listed["chats"][0]
            stale = CollaborationLedger(
                self.config, "claude", conversation["filed_as"], session_id="binding-old"
            ).begin("old binding", saved.to_dict()["agents"], mode="discussion")
            changed = saved.to_dict()
            changed["agents"][0]["who"] = "codex"
            swarm.save(changed, self.config)
        with self.assertRaisesRegex(Exception, "no longer current"):
            stale.record_state("late", {"status": "wrong"})

    def test_corrupt_registry_recovers_from_last_good_copy_without_loss(self) -> None:
        first = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        made = swarm_chats.create(self.config, self.board, "agent-1", "agent-2")
        expected = {one["id"] for one in made["chats"]}
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        backup = registry_path.with_name(swarm_chats.BACKUP_NAME)
        history = registry_path.with_name(swarm_chats.HISTORY_FOLDER)
        self.assertTrue(backup.is_file())
        self.assertTrue(any(history.glob("*.json")))

        registry_path.write_text("{broken", encoding="utf-8")
        recovered = swarm_chats.list_for_agent(self.config, self.board, "agent-1")

        self.assertEqual({one["id"] for one in recovered["chats"]}, expected)
        self.assertEqual(recovered["registry_recovered_from"], swarm_chats.BACKUP_NAME)
        self.assertEqual(
            {one["id"] for one in json.loads(
                registry_path.read_text(encoding="utf-8")
            )["chats"]},
            expected,
        )

    def test_disconnected_pair_is_preserved_but_cannot_be_used(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        chat_id = listed["active"]
        board = copy.deepcopy(self.board)
        board["talks_to"] = []
        again = swarm_chats.list_for_agent(self.config, board, "agent-1")
        kept = next(one for one in again["chats"] if one["id"] == chat_id)
        self.assertFalse(kept["connected"])
        with self.assertRaisesRegex(Exception, "Reconnect"):
            swarm_chats.resolve(self.config, board, "agent-1", chat_id)

    def test_direct_pair_turn_records_the_real_speaker_and_both_recipients(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        conversation = listed["chats"][0]

        class Provider:
            def complete(self, _request):
                return ProviderResponse(text="A labelled answer")

        with mock.patch.object(chat, "create_provider", return_value=Provider()):
            chat.say(
                self.config, "claude", "Who is answering?",
                filed_as=conversation["filed_as"],
                speaker=self.board["agents"][0],
                recipients=conversation["pair_agents"],
            )
        turns = chat.read_it(self.config, "claude", conversation["filed_as"])
        self.assertEqual(turns[0].recipient_name, "Claude, Codex")
        self.assertEqual(turns[1].speaker_name, "Claude")
        self.assertEqual(turns[1].speaker_id, "agent-1")
        self.assertEqual(turns[1].phase, "final_answer")


if __name__ == "__main__":
    unittest.main()
