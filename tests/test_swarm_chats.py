from __future__ import annotations

import copy
import itertools
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, swarm, swarm_chats
from our_harness.collaboration_ledger import CollaborationLedger
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError, ProviderResponse


def _spawned_config(root: str) -> LoadedConfig:
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    config.data["providers"] = {
        "claude": {"kind": "claude-cli", "model": "claude"},
        "codex": {"kind": "codex-cli", "model": "codex"},
    }
    return config


def _pause_chat_project_write(
    root: str, board: dict, chat_id: str, before_write, release_write,
) -> None:
    config = _spawned_config(root)
    original_write = swarm_chats._write

    def paused_write(config: LoadedConfig, registry: dict) -> None:
        before_write.set()
        if not release_write.wait(20.0):
            raise RuntimeError("Timed out waiting to release the paused registry write")
        original_write(config, registry)

    with mock.patch.object(swarm_chats, "_write", side_effect=paused_write):
        swarm_chats.select_project(
            config, board, "agent-1", chat_id, "project-2"
        )


def _signal_chat_project_read(
    root: str, board: dict, chat_id: str, started, read_entered,
) -> None:
    config = _spawned_config(root)
    original_read = swarm_chats._read

    def signalled_read(config: LoadedConfig) -> dict:
        read_entered.set()
        return original_read(config)

    started.set()
    with mock.patch.object(swarm_chats, "_read", side_effect=signalled_read):
        swarm_chats.select_project(
            config, board, "agent-1", chat_id, "project-2"
        )


def _stop_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
    process.join(5.0)
    if not process.is_alive():
        process.close()


class PairScopedChatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.runtime_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_temporary.cleanup)
        runtime_root = Path(self.runtime_temporary.name).resolve()
        runtime_environment = mock.patch.dict(os.environ, {
            "OUR_HARNESS_SWARM_RUN_DIR": str(runtime_root / "swarm-runtime"),
            "OUR_HARNESS_PIPELINE_RUN_DIR": str(runtime_root / "pipeline-runtime"),
        })
        runtime_environment.start()
        self.addCleanup(runtime_environment.stop)
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
        pair_chat = next(
            one for one in first["chats"]
            if one["pair"] == ["agent-1", "agent-2"]
        )
        reverse_pair = next(
            one for one in reverse["chats"]
            if one["pair"] == ["agent-1", "agent-2"]
        )
        direct = next(one for one in first["chats"] if one["pair"] == ["agent-1"])
        reverse_direct = next(
            one for one in reverse["chats"] if one["pair"] == ["agent-2"]
        )
        self.assertEqual(len(first["chats"]), 2)
        self.assertEqual(len(reverse["chats"]), 2)
        self.assertEqual(pair_chat["id"], reverse_pair["id"])
        self.assertNotEqual(direct["id"], reverse_direct["id"])
        self.assertEqual(first["active"], pair_chat["id"])
        self.assertEqual(reverse["active"], reverse_pair["id"])
        self.assertEqual(pair_chat["project"], "project-1")
        self.assertEqual(
            [one["name"] for one in pair_chat["pair_agents"]],
            ["Claude", "Codex"],
        )
        repeated = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual([
            one["id"] for one in repeated["chats"] if one["pair"] == ["agent-1"]
        ], [direct["id"]])

    def test_pair_stays_default_when_only_the_lone_agent_has_a_project(self) -> None:
        board = copy.deepcopy(self.board)
        board["works_on"] = [{"agent": "agent-1", "project": "project-1"}]

        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        pair_chat = next(one for one in listed["chats"] if len(one["pair"]) == 2)
        direct = next(one for one in listed["chats"] if len(one["pair"]) == 1)

        self.assertEqual(pair_chat["project"], "")
        self.assertEqual(direct["project"], "project-1")
        self.assertEqual(listed["active"], pair_chat["id"])

    def test_direct_seed_survives_connection_changes_and_restart(self) -> None:
        disconnected = copy.deepcopy(self.board)
        disconnected["talks_to"] = []
        initial = swarm_chats.list_for_agent(self.config, disconnected, "agent-1")
        direct = next(one for one in initial["chats"] if one["pair"] == ["agent-1"])
        self.assertEqual(initial["active"], direct["id"])

        connected = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        pair_chat = next(one for one in connected["chats"] if len(one["pair"]) == 2)
        self.assertEqual(connected["active"], pair_chat["id"])
        self.assertEqual([
            one["id"] for one in connected["chats"] if one["pair"] == ["agent-1"]
        ], [direct["id"]])

        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        reopened.data["providers"] = copy.deepcopy(self.config.data["providers"])
        after_restart = swarm_chats.list_for_agent(reopened, disconnected, "agent-1")
        reconnected = swarm_chats.list_for_agent(reopened, self.board, "agent-1")
        self.assertCountEqual(
            [one["id"] for one in after_restart["chats"]],
            [direct["id"], pair_chat["id"]],
        )
        self.assertCountEqual(
            [one["id"] for one in reconnected["chats"]],
            [direct["id"], pair_chat["id"]],
        )

    def test_stale_known_singleton_key_repairs_a_missing_initial_chat(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        direct = next(one for one in listed["chats"] if one["pair"] == ["agent-1"])
        registry = swarm_chats._read(self.config)
        singleton_key = swarm_chats._pair_scope_key(
            swarm_chats._board_workspace_id(self.board), ["agent-1"]
        )
        self.assertIn(singleton_key, registry["known_pairs"])
        registry["chats"] = [
            one for one in registry["chats"] if one["id"] != direct["id"]
        ]
        swarm_chats._write(self.config, registry)

        repaired = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        direct_chats = [
            one for one in repaired["chats"] if one["pair"] == ["agent-1"]
        ]
        self.assertEqual(len(direct_chats), 1)
        self.assertNotEqual(direct_chats[0]["id"], direct["id"])

    def test_upgrade_reserves_a_singleton_slot_at_the_workspace_chat_cap(self) -> None:
        agents = [
            {
                "id": f"agent-{number}", "name": f"Agent {number}",
                "who": "claude", "ready": True,
            }
            for number in range(1, swarm.MOST_AGENTS + 1)
        ]
        board = {
            "workspace_id": "workspace-cccccccccccccccccccccccccccccccc",
            "agents": agents, "projects": [], "works_on": [],
            "talks_to": [
                {"one": one["id"], "other": other["id"]}
                for one, other in itertools.combinations(agents, 2)
            ],
        }
        registry = swarm_chats._empty()
        pairs = list(itertools.combinations(
            [one["id"] for one in agents], 2
        ))[:swarm_chats.MOST_CHATS]
        for pair in pairs:
            swarm_chats._new_chat(
                self.config, registry, board, sorted(pair)
            )
        self.assertEqual(len(registry["chats"]), swarm_chats.MOST_CHATS)
        swarm_chats._write(self.config, registry)

        upgraded = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )
        direct = [
            one for one in upgraded["chats"] if one["pair"] == ["agent-1"]
        ]
        self.assertEqual(len(direct), 1)
        persisted = swarm_chats._read(self.config)
        self.assertEqual(
            len(persisted["chats"]), swarm_chats.MOST_CHATS + 1
        )
        repeated = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )
        self.assertEqual([
            one["id"] for one in repeated["chats"]
            if one["pair"] == ["agent-1"]
        ], [direct[0]["id"]])
        with self.assertRaisesRegex(Exception, "maximum number of chats"):
            swarm_chats.create(
                self.config, board, "agent-1", "", scope="single"
            )

    def test_full_migration_recovers_the_last_singleton_without_alias_lockout(self) -> None:
        agents = [
            {
                "id": f"agent-{number}", "name": f"Agent {number}",
                "filed_as": f"Stable agent {number}",
                "who": "claude", "ready": True,
            }
            for number in range(1, swarm.MOST_AGENTS + 1)
        ]
        board = {
            "agents": agents, "projects": [], "works_on": [], "talks_to": [],
        }
        registry = swarm_chats._empty()
        pairs = list(itertools.combinations(
            [one["id"] for one in agents], 2
        ))[:swarm_chats.MOST_CHATS]
        for pair in pairs:
            swarm_chats._new_chat(
                self.config, registry, board, sorted(pair)
            )
        for number in range(1, swarm.MOST_AGENTS):
            swarm_chats._new_chat(
                self.config, registry, board, [f"agent-{number}"],
                required_singleton_seed=True,
            )
        self.assertEqual(
            len(registry["chats"]),
            swarm_chats.MOST_CHATS + swarm.MOST_AGENTS - 1,
        )
        with self.assertRaisesRegex(Exception, "maximum number of chats"):
            swarm_chats._new_chat(
                self.config, registry, board, ["agent-1"],
                required_singleton_seed=True,
            )
        swarm_chats._write(self.config, registry)
        last = agents[-1]
        for filed_as in (last["filed_as"], last["name"]):
            chat.keep_exchange(
                self.config, "claude", f"question for {filed_as}",
                f"answer for {filed_as}", filed_as=filed_as,
            )

        listed = swarm_chats.list_for_agent(
            self.config, board, last["id"]
        )
        direct = [
            one for one in listed["chats"] if one["pair"] == [last["id"]]
        ]
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0]["name"], "Recovered older chat")
        self.assertEqual(
            len(swarm_chats._read(self.config)["chats"]),
            swarm_chats.MOST_CHATS + swarm.MOST_AGENTS,
        )
        repeated = swarm_chats.list_for_agent(
            self.config, board, last["id"]
        )
        self.assertEqual([
            one["id"] for one in repeated["chats"]
            if one["pair"] == [last["id"]]
        ], [direct[0]["id"]])

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

    def test_explicit_single_agent_chats_remain_distinct_with_a_connected_peer(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        pair_chat = next(one for one in listed["chats"] if len(one["pair"]) == 2)
        first_single = next(one for one in listed["chats"] if len(one["pair"]) == 1)
        chat.keep_exchange(
            self.config, "claude", "pair question", "pair answer",
            filed_as=pair_chat["filed_as"],
        )
        self.assertEqual(first_single["pair"], ["agent-1"])
        self.assertEqual(first_single["name"], "Chat 1")
        chat.keep_exchange(
            self.config, "claude", "direct question", "direct answer",
            filed_as=first_single["filed_as"],
        )

        second_result = swarm_chats.create(
            self.config, self.board, "agent-1", "", scope="single"
        )
        second_single = next(
            one for one in second_result["chats"]
            if one["id"] == second_result["active"]
        )
        self.assertEqual(second_single["pair"], ["agent-1"])
        self.assertEqual(second_single["name"], "Chat 2")
        for identity_field in ("id", "filed_as", "web_conversation_key"):
            self.assertEqual(len({
                one[identity_field]
                for one in (pair_chat, first_single, second_single)
            }), 3, identity_field)
        self.assertEqual(chat.read_it(
            self.config, "claude", second_single["filed_as"]
        ), [])

        archived = swarm_chats.delete(
            self.config, self.board, "agent-1", first_single["id"]
        )
        archived_single = next(
            one for one in archived["chats"] if one["id"] == first_single["id"]
        )
        self.assertTrue(archived_single["archived_at"])
        self.assertEqual(archived["active"], second_single["id"])
        restored = swarm_chats.restore(
            self.config, self.board, "agent-1", first_single["id"]
        )
        self.assertEqual(restored["active"], first_single["id"])

        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        reopened.data["providers"] = copy.deepcopy(self.config.data["providers"])
        persisted = swarm_chats.list_for_agent(reopened, self.board, "agent-1")
        by_id = {one["id"]: one for one in persisted["chats"]}
        self.assertEqual(persisted["active"], first_single["id"])
        self.assertEqual(by_id[first_single["id"]]["pair"], ["agent-1"])
        self.assertEqual(
            by_id[first_single["id"]]["filed_as"], first_single["filed_as"]
        )
        self.assertEqual(
            by_id[first_single["id"]]["web_conversation_key"],
            first_single["web_conversation_key"],
        )
        self.assertEqual(chat.read_it(
            reopened, "claude", first_single["filed_as"]
        )[-1].text, "direct answer")
        self.assertEqual(chat.read_it(
            reopened, "claude", pair_chat["filed_as"]
        )[-1].text, "pair answer")

    def test_single_agent_scope_is_explicit_and_never_bypasses_peer_lines(self) -> None:
        with self.assertRaisesRegex(Exception, "cannot also name a peer"):
            swarm_chats.create(
                self.config, self.board, "agent-1", "agent-2", scope="single"
            )
        with self.assertRaisesRegex(Exception, "supported saved-chat scope"):
            swarm_chats.create(
                self.config, self.board, "agent-1", "", scope="surprise"
            )
        board = copy.deepcopy(self.board)
        board["agents"].append({
            "id": "agent-3", "name": "Unconnected", "who": "codex", "ready": True,
        })
        with self.assertRaisesRegex(Exception, "green communication line"):
            swarm_chats.create(
                self.config, board, "agent-1", "agent-3"
            )

        disconnected = copy.deepcopy(self.board)
        disconnected["talks_to"] = []
        first = swarm_chats.list_for_agent(
            self.config, disconnected, "agent-2"
        )
        made = swarm_chats.create(
            self.config, disconnected, "agent-2", ""
        )
        self.assertEqual(len([
            one for one in made["chats"] if one["pair"] == ["agent-2"]
        ]), len([
            one for one in first["chats"] if one["pair"] == ["agent-2"]
        ]) + 1)

    def test_two_processes_cannot_overwrite_different_chat_project_updates(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        first_chat = listed["chats"][0]
        made = swarm_chats.create(
            self.config, self.board, "agent-1", "agent-2"
        )
        second_chat = next(
            one for one in made["chats"] if one["id"] != first_chat["id"]
        )
        context = multiprocessing.get_context("spawn")
        first_before_write = context.Event()
        release_first_write = context.Event()
        second_started = context.Event()
        second_read_entered = context.Event()
        first = context.Process(
            target=_pause_chat_project_write,
            args=(
                str(self.root), self.board, first_chat["id"],
                first_before_write, release_first_write,
            ),
        )
        second = context.Process(
            target=_signal_chat_project_read,
            args=(
                str(self.root), self.board, second_chat["id"],
                second_started, second_read_entered,
            ),
        )
        first.start()
        self.addCleanup(_stop_process, first)
        try:
            self.assertTrue(
                first_before_write.wait(15.0),
                "The first process never reached its paused registry write",
            )
            second.start()
            self.addCleanup(_stop_process, second)
            self.assertTrue(
                second_started.wait(15.0),
                "The second process never attempted its registry mutation",
            )
            self.assertFalse(
                second_read_entered.wait(1.0),
                "A sibling process read a stale registry while the first RMW was open",
            )
        finally:
            release_first_write.set()

        first.join(20.0)
        second.join(20.0)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)

        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        by_id = {one["id"]: one for one in registry["chats"]}
        self.assertEqual(by_id[first_chat["id"]]["project"], "project-2")
        self.assertEqual(by_id[second_chat["id"]]["project"], "project-2")

    def test_registry_lock_link_is_rejected_without_touching_its_target(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "outside-lock-target"
        target.write_bytes(b"outside sentinel")
        lock_path = (
            self.root / ".harness" / "chats" / "_board-conversations.lock"
        )
        lock_path.parent.mkdir(parents=True)
        try:
            lock_path.symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"File symlinks are unavailable on this platform: {exc}")

        with self.assertRaisesRegex(HarnessError, "Linked path components"):
            swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual(target.read_bytes(), b"outside sentinel")

    def test_registry_lock_is_released_when_writer_process_dies(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        chat_id = listed["active"]
        context = multiprocessing.get_context("spawn")
        first_before_write = context.Event()
        never_release = context.Event()
        first = context.Process(
            target=_pause_chat_project_write,
            args=(
                str(self.root), self.board, chat_id,
                first_before_write, never_release,
            ),
        )
        first.start()
        self.addCleanup(_stop_process, first)
        self.assertTrue(
            first_before_write.wait(15.0),
            "The doomed process never acquired and reached the registry write",
        )
        first.terminate()
        first.join(10.0)
        self.assertFalse(first.is_alive())
        self.assertNotEqual(first.exitcode, 0)

        second_started = context.Event()
        second_read_entered = context.Event()
        second = context.Process(
            target=_signal_chat_project_read,
            args=(
                str(self.root), self.board, chat_id,
                second_started, second_read_entered,
            ),
        )
        second.start()
        self.addCleanup(_stop_process, second)
        second.join(15.0)
        self.assertFalse(second.is_alive())
        self.assertEqual(second.exitcode, 0)
        self.assertTrue(second_started.is_set())
        self.assertTrue(second_read_entered.is_set())

        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        persisted = next(one for one in registry["chats"] if one["id"] == chat_id)
        self.assertEqual(persisted["project"], "project-2")

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
        self.assertEqual(len([
            one for one in listed["chats"] if one["pair"] == ["agent-1"]
        ]), 1)

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
        self.assertEqual(len([
            one for one in again["chats"] if one["pair"] == ["agent-1"]
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
        self.assertEqual(
            persisted["schema_version"], swarm_chats.REGISTRY_SCHEMA_VERSION
        )
        self.assertEqual(persisted["chats"][0]["filed_as"], conversation["filed_as"])

    def test_schema_six_adds_stable_web_key_without_rekeying_local_chat(self) -> None:
        listed = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        original = listed["chats"][0]
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        backup_path = registry_path.with_name(swarm_chats.BACKUP_NAME)
        version_six = json.loads(registry_path.read_text(encoding="utf-8"))
        version_six["schema_version"] = 6
        for conversation in version_six["chats"]:
            conversation.pop("web_conversation_key", None)
        version_six["integrity_sha256"] = swarm_chats._registry_integrity(version_six)
        version_six_text = json.dumps(version_six, indent=2) + "\n"
        registry_path.write_text(version_six_text, encoding="utf-8")
        backup_path.write_text(version_six_text, encoding="utf-8")

        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        reopened.data["providers"] = copy.deepcopy(self.config.data["providers"])
        migrated = swarm_chats.list_for_agent(reopened, self.board, "agent-1")
        migrated_chat = migrated["chats"][0]
        self.assertEqual(migrated_chat["id"], original["id"])
        self.assertEqual(migrated_chat["filed_as"], original["filed_as"])
        self.assertEqual(
            migrated_chat["web_conversation_key"], original["filed_as"]
        )

        persisted_text = registry_path.read_text(encoding="utf-8")
        persisted = json.loads(persisted_text)
        persisted_chat = persisted["chats"][0]
        self.assertEqual(
            persisted["schema_version"], swarm_chats.REGISTRY_SCHEMA_VERSION
        )
        self.assertGreater(persisted["revision"], version_six["revision"])
        self.assertEqual(
            persisted["integrity_sha256"], swarm_chats._registry_integrity(persisted)
        )
        self.assertEqual(persisted_chat["id"], original["id"])
        self.assertEqual(persisted_chat["filed_as"], original["filed_as"])
        self.assertEqual(persisted_chat["web_conversation_key"], original["filed_as"])

        reopened_again = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        reopened_again.data["providers"] = copy.deepcopy(
            self.config.data["providers"]
        )
        stable = swarm_chats.list_for_agent(
            reopened_again, self.board, "agent-1"
        )["chats"][0]
        self.assertEqual(
            (stable["id"], stable["filed_as"], stable["web_conversation_key"]),
            (original["id"], original["filed_as"], original["filed_as"]),
        )
        self.assertEqual(registry_path.read_text(encoding="utf-8"), persisted_text)

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
        with self.assertRaisesRegex(Exception, "both agents"):
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

    def test_valid_older_shortened_primary_never_beats_newer_backup(self) -> None:
        swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        made = swarm_chats.create(self.config, self.board, "agent-1", "agent-2")
        expected = {one["id"] for one in made["chats"]}
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        backup_path = registry_path.with_name(swarm_chats.BACKUP_NAME)
        complete = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertGreater(complete["revision"], 0)
        self.assertEqual(
            complete["integrity_sha256"],
            swarm_chats._registry_integrity(complete),
        )

        shortened = copy.deepcopy(complete)
        shortened["revision"] -= 1
        shortened["chats"] = shortened["chats"][:1]
        shortened["integrity_sha256"] = swarm_chats._registry_integrity(shortened)
        registry_path.write_text(json.dumps(shortened, indent=2) + "\n", encoding="utf-8")

        recovered = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual({one["id"] for one in recovered["chats"]}, expected)
        self.assertEqual(recovered["registry_recovered_from"], swarm_chats.BACKUP_NAME)
        rewritten = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertGreater(rewritten["revision"], complete["revision"])
        self.assertEqual(
            rewritten["integrity_sha256"],
            swarm_chats._registry_integrity(rewritten),
        )

    def test_valid_json_with_broken_integrity_recovers_from_backup(self) -> None:
        made = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        expected = {one["id"] for one in made["chats"]}
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        damaged = json.loads(registry_path.read_text(encoding="utf-8"))
        damaged["chats"] = []
        # Deliberately retain the old seal: the JSON is valid but incomplete.
        registry_path.write_text(json.dumps(damaged, indent=2) + "\n", encoding="utf-8")

        recovered = swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual({one["id"] for one in recovered["chats"]}, expected)
        self.assertEqual(recovered["registry_recovered_from"], swarm_chats.BACKUP_NAME)

    def test_future_registry_schema_is_not_downgraded(self) -> None:
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        future = {
            "schema_version": swarm_chats.REGISTRY_SCHEMA_VERSION + 1,
            "revision": 99,
            "chats": [], "active": {}, "chosen_active": {}, "known_pairs": [],
        }
        registry_path.write_text(json.dumps(future), encoding="utf-8")

        with self.assertRaisesRegex(Exception, "newer saved-chat schema"):
            swarm_chats.list_for_agent(self.config, self.board, "agent-1")
        self.assertEqual(
            json.loads(registry_path.read_text(encoding="utf-8"))["schema_version"],
            swarm_chats.REGISTRY_SCHEMA_VERSION + 1,
        )

    def test_registry_backup_symlink_is_rejected_before_read(self) -> None:
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "outside.json"
        target.write_text(json.dumps(swarm_chats._empty()), encoding="utf-8")
        backup = registry_path.with_name(swarm_chats.BACKUP_NAME)
        try:
            backup.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"This host cannot create a file symlink: {exc}")

        with self.assertRaisesRegex(Exception, "Linked path components"):
            swarm_chats.list_for_agent(self.config, self.board, "agent-1")

    def test_registry_history_symlink_is_rejected_before_recovery(self) -> None:
        registry_path = self.root / swarm_chats.WHERE_THEY_LIVE
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text("{broken", encoding="utf-8")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        history = registry_path.with_name(swarm_chats.HISTORY_FOLDER)
        try:
            history.symlink_to(Path(outside.name), target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"This host cannot create a directory symlink: {exc}")

        with self.assertRaisesRegex(Exception, "Linked path components"):
            swarm_chats.list_for_agent(self.config, self.board, "agent-1")

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
                return ProviderResponse(text="A labelled answer", finish_reason="stop")

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
