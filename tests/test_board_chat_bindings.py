from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, swarm, swarm_chats
from our_harness.collaboration_ledger import CollaborationLedger
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError, ProviderOutcomeUnknown
from our_harness.providers import base as provider_base
from our_harness.swarm_runs import SwarmRunStore, bind, provider_effect


class BoardChatBindingTests(unittest.TestCase):
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
            "route-a": {"kind": "claude-cli", "model": "claude-a"},
            "route-b": {"kind": "codex-cli", "model": "codex-b"},
        }

    def board(
        self, workspace_id: str, *, route: str = "route-a",
        project_path: Path | None = None,
    ) -> dict:
        return {
            "schema_version": 1,
            "binding_schema_version": 1,
            "workspace_id": workspace_id,
            "agents": [
                {"id": "agent-1", "name": "Lead", "who": route, "ready": True},
                {"id": "agent-2", "name": "Peer", "who": "route-b", "ready": True},
            ],
            "projects": [{
                "id": "project-1", "name": "Work",
                "path": str(project_path or self.first), "is_there": True,
            }],
            "works_on": [
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
            "talks_to": [{"one": "agent-1", "other": "agent-2"}],
        }

    def legacy_web_chat(self, board: dict) -> dict:
        with mock.patch.dict(
            chat.PROVIDER_TRANSPORT_CONTRACT_REVISIONS,
            {"web-chat": swarm_chats.LEGACY_WEB_RELAY_CONTRACT},
        ):
            listed = swarm_chats.list_for_agent(
                self.config, board, "agent-1",
            )
        made = next(
            one for one in listed["chats"] if one["id"] == listed["active"]
        )
        # A relay-v1 chat was saved before route identities existed.
        self.forget_route_identities(made["id"])
        return made

    def forget_route_identities(self, chat_id: str) -> None:
        """Make one saved chat look like it was saved before route identity v1."""

        registry = swarm_chats._read(self.config)
        raw = next(one for one in registry["chats"] if one["id"] == chat_id)
        raw["binding"].pop("route_identities", None)
        swarm_chats._write(self.config, registry)

    def make_binding_schema_one(self, chat_id: str) -> None:
        registry = swarm_chats._read(self.config)
        raw = next(one for one in registry["chats"] if one["id"] == chat_id)
        binding = raw["binding"]
        raw["binding"] = {
            "binding_schema_version": 1,
            "agent_routes": {
                member_id: {
                    key: held[key] for key in (
                        "route", "failure_context_version",
                        "route_fingerprint_sha256", "transport_contract",
                    )
                }
                for member_id, held in binding["agent_routes"].items()
            },
            "project": {
                key: binding["project"][key]
                for key in ("id", "path_fingerprint_sha256")
            },
        }
        swarm_chats._write(self.config, registry)

    def test_same_agent_ids_on_another_board_cannot_claim_chats(self) -> None:
        first_board = self.board("workspace-11111111111111111111111111111111")
        second_board = self.board("workspace-22222222222222222222222222222222")
        first = swarm_chats.list_for_agent(self.config, first_board, "agent-1")
        first_chat = first["chats"][0]
        chat.keep_exchange(
            self.config, "route-a", "private goal", "private answer",
            filed_as=first_chat["filed_as"],
        )

        second = swarm_chats.list_for_agent(self.config, second_board, "agent-1")
        second_chat = second["chats"][0]
        self.assertNotEqual(first_chat["id"], second_chat["id"])
        self.assertNotEqual(first_chat["filed_as"], second_chat["filed_as"])
        self.assertEqual(second_chat["workspace_id"], second_board["workspace_id"])
        self.assertEqual(chat.read_it(
            self.config, "route-a", second_chat["filed_as"]
        ), [])

        reopened = swarm_chats.list_for_agent(
            self.config, first_board, "agent-1"
        )
        self.assertEqual(reopened["active"], first_chat["id"])
        self.assertEqual(chat.read_it(
            self.config, "route-a", first_chat["filed_as"]
        )[-1].text, "private answer")

    def test_route_change_refuses_dispatch_but_keeps_history_readable(self) -> None:
        board = self.board("workspace-33333333333333333333333333333333")
        original = swarm_chats.list_for_agent(self.config, board, "agent-1")["chats"][0]
        chat.keep_exchange(
            self.config, "route-a", "old request", "old answer",
            filed_as=original["filed_as"],
        )
        changed = self.board(
            board["workspace_id"], route="route-b",
        )

        listed = swarm_chats.list_for_agent(self.config, changed, "agent-1")
        protected = next(one for one in listed["chats"] if one["id"] == original["id"])
        self.assertEqual(protected["binding_problem"]["code"], "agent_binding_changed")
        # Pointing the agent at another named route is a different provider;
        # its old transcript stays with the old route.
        self.assertFalse(protected["binding_problem"]["can_review_reconnect"])
        with self.assertRaisesRegex(Exception, "will not send that history"):
            swarm_chats.resolve(
                self.config, changed, "agent-1", original["id"]
            )
        inspected = swarm_chats.resolve(
            self.config, changed, "agent-1", original["id"],
            allow_binding_drift=True,
        )
        self.assertEqual(inspected["transcript_route"], "route-a")
        self.assertEqual(chat.read_it(
            self.config, inspected["transcript_route"], inspected["filed_as"]
        )[-1].text, "old answer")

        fresh = swarm_chats.create(
            self.config, changed, "agent-1", "agent-2"
        )
        current = next(one for one in fresh["chats"] if one["id"] == fresh["active"])
        self.assertIsNone(current["binding_problem"])
        self.assertEqual(
            swarm_chats.resolve(
                self.config, changed, "agent-1", current["id"]
            )["id"],
            current["id"],
        )

    def test_exact_web_relay_v1_binding_upgrades_and_survives_restart(self) -> None:
        route = "web:chatgpt-example"
        self.config.data["providers"][route] = {
            "kind": "web-chat", "model": "saved-profile",
        }
        board = self.board(
            "workspace-10101010101010101010101010101010", route=route,
        )
        original = self.legacy_web_chat(board)
        old_route = original["binding"]["agent_routes"]["agent-1"]
        self.assertEqual(
            old_route["transport_contract"],
            swarm_chats.LEGACY_WEB_RELAY_CONTRACT,
        )

        migrated = next(
            one for one in swarm_chats.list_for_agent(
                self.config, board, "agent-1",
            )["chats"] if one["id"] == original["id"]
        )
        self.assertIsNone(migrated["binding_problem"])
        self.assertEqual(
            migrated["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.CURRENT_WEB_RELAY_CONTRACT,
        )
        self.assertEqual(migrated["effective_dispatch_strength"], "verified")
        where = self.root / swarm_chats.WHERE_THEY_LIVE
        first_persisted = json.loads(where.read_text(encoding="utf-8"))
        first_revision = first_persisted["revision"]

        restarted = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        reopened = next(
            one for one in swarm_chats.list_for_agent(
                restarted, board, "agent-1",
            )["chats"] if one["id"] == original["id"]
        )
        self.assertIsNone(reopened["binding_problem"])
        self.assertEqual(
            reopened["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.CURRENT_WEB_RELAY_CONTRACT,
        )
        self.assertEqual(
            json.loads(where.read_text(encoding="utf-8"))["revision"],
            first_revision,
        )

    def test_exact_schema_one_web_binding_upgrades_without_blessing_peer(self) -> None:
        route = "web:chatgpt-legacy"
        self.config.data["providers"][route] = {
            "kind": "web-chat", "model": "saved-profile",
        }
        board = self.board(
            "workspace-20202020202020202020202020202020", route=route,
        )
        original = self.legacy_web_chat(board)
        self.make_binding_schema_one(original["id"])

        migrated = next(
            one for one in swarm_chats.list_for_agent(
                self.config, board, "agent-1",
            )["chats"] if one["id"] == original["id"]
        )
        self.assertIsNone(migrated["binding_problem"])
        self.assertEqual(migrated["binding"]["binding_schema_version"], 1)
        self.assertEqual(
            migrated["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.CURRENT_WEB_RELAY_CONTRACT,
        )
        self.assertEqual(
            migrated["binding"]["agent_routes"]["agent-2"][
                "effective_dispatch_strength"
            ],
            "legacy-unverified",
        )
        self.assertEqual(migrated["project_binding_strength"], "legacy-path-only")

    def test_web_relay_migration_refuses_changed_route(self) -> None:
        old_route = "web:chatgpt-old-route"
        new_route = "web:chatgpt-new-route"
        board = self.board(
            "workspace-30303030303030303030303030303030", route=old_route,
        )
        original = self.legacy_web_chat(board)
        changed = self.board(board["workspace_id"], route=new_route)

        protected = next(
            one for one in swarm_chats.list_for_agent(
                self.config, changed, "agent-1",
            )["chats"] if one["id"] == original["id"]
        )
        self.assertEqual(protected["binding_problem"]["code"], "agent_binding_changed")
        self.assertEqual(
            protected["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.LEGACY_WEB_RELAY_CONTRACT,
        )

    def test_web_relay_migration_refuses_changed_profile(self) -> None:
        route = "web:chatgpt-profile"
        self.config.data["providers"][route] = {
            "kind": "web-chat", "model": "before",
        }
        board = self.board(
            "workspace-40404040404040404040404040404040", route=route,
        )
        original = self.legacy_web_chat(board)
        self.config.data["providers"][route]["model"] = "after"

        protected = next(
            one for one in swarm_chats.list_for_agent(
                self.config, board, "agent-1",
            )["chats"] if one["id"] == original["id"]
        )
        self.assertEqual(protected["binding_problem"]["code"], "agent_binding_changed")
        self.assertEqual(
            protected["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.LEGACY_WEB_RELAY_CONTRACT,
        )

    def test_web_relay_migration_refuses_changed_effective_dispatch(self) -> None:
        route = "web:chatgpt-dispatch"
        self.config.data["providers"][route] = {
            "kind": "web-chat", "model": "saved-profile",
        }
        board = self.board(
            "workspace-50505050505050505050505050505050", route=route,
        )
        original = self.legacy_web_chat(board)
        registry = swarm_chats._read(self.config)
        raw = next(one for one in registry["chats"] if one["id"] == original["id"])
        raw["binding"]["agent_routes"]["agent-1"][
            "effective_dispatch_fingerprint_sha256"
        ] = "0" * 64
        swarm_chats._write(self.config, registry)

        protected = next(
            one for one in swarm_chats.list_for_agent(
                self.config, board, "agent-1",
            )["chats"] if one["id"] == original["id"]
        )
        self.assertEqual(protected["binding_problem"]["code"], "agent_binding_changed")
        self.assertEqual(
            protected["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.LEGACY_WEB_RELAY_CONTRACT,
        )

    def test_web_relay_migration_does_not_bless_changed_peer_executable(self) -> None:
        route = "web:chatgpt-peer-executable"
        first = self.root / "provider-a" / "agent-tool"
        second = self.root / "provider-b" / "agent-tool"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        self.config.data["providers"]["route-b"] = {
            "kind": "local", "model": "model-b", "command": ["agent-tool"],
        }
        board = self.board(
            "workspace-51515151515151515151515151515151", route=route,
        )
        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(first),
        ):
            original = self.legacy_web_chat(board)

        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(second),
        ):
            protected = next(
                one for one in swarm_chats.list_for_agent(
                    self.config, board, "agent-1",
                )["chats"] if one["id"] == original["id"]
            )

        changed = {
            one["agent_id"]: one["kind"]
            for one in protected["binding_problem"]["changed_agents"]
        }
        self.assertEqual(changed["agent-2"], "effective_dispatch_changed")
        self.assertEqual(
            protected["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.LEGACY_WEB_RELAY_CONTRACT,
        )

    def test_web_relay_migration_refuses_project_change(self) -> None:
        route = "web:chatgpt-project"
        board = self.board(
            "workspace-60606060606060606060606060606060", route=route,
        )
        original = self.legacy_web_chat(board)
        changed = self.board(
            board["workspace_id"], route=route, project_path=self.second,
        )

        protected = next(
            one for one in swarm_chats.list_for_agent(
                self.config, changed, "agent-1",
            )["chats"] if one["id"] == original["id"]
        )
        self.assertEqual(protected["binding_problem"]["code"], "agent_binding_changed")
        self.assertEqual(
            protected["binding"]["agent_routes"]["agent-1"][
                "transport_contract"
            ],
            swarm_chats.LEGACY_WEB_RELAY_CONTRACT,
        )

    def test_exact_strict_schema_contract_upgrade_restores_ordinary_chat(self) -> None:
        from our_harness.providers import codex_cli

        board = self.board("workspace-12121212121212121212121212121212")
        with mock.patch.object(provider_base.shutil, "which", return_value=None):
            with mock.patch.object(
                codex_cli.CodexCLIProvider, "_effective_dispatch_contract",
                return_value="codex-cli/effective-dispatch/v1",
            ):
                original = swarm_chats.list_for_agent(
                    self.config, board, "agent-1",
                )["chats"][0]
                chat.keep_exchange(
                    self.config, "route-a", "old question", "old answer",
                    filed_as=original["filed_as"],
                )
            self.assertEqual(
                original["binding"]["agent_routes"]["agent-2"][
                    "effective_dispatch_contract"
                ],
                "codex-cli/effective-dispatch/v1",
            )

            self.config.data["providers"]["route-b"]["command"] = [
                "different-codex-command",
            ]
            protected = next(
                one for one in swarm_chats.list_for_agent(
                    self.config, board, "agent-1",
                )["chats"] if one["id"] == original["id"]
            )
            self.assertEqual(
                protected["binding_problem"]["code"], "agent_binding_changed",
            )
            self.assertEqual(
                protected["binding"]["agent_routes"]["agent-2"][
                    "effective_dispatch_contract"
                ],
                "codex-cli/effective-dispatch/v1",
            )
            with self.assertRaisesRegex(Exception, "will not send that history"):
                swarm_chats.resolve(
                    self.config, board, "agent-1", original["id"],
                )

            self.config.data["providers"]["route-b"].pop("command")
            recovered = swarm_chats.resolve(
                self.config, board, "agent-1", original["id"],
            )

        self.assertEqual(recovered["id"], original["id"])
        self.assertIsNone(recovered["binding_problem"])
        self.assertEqual(
            recovered["binding"]["agent_routes"]["agent-2"][
                "effective_dispatch_contract"
            ],
            "codex-cli/effective-dispatch/v2",
        )
        self.assertEqual(
            chat.read_it(
                self.config, "route-a", recovered["filed_as"],
            )[-1].text,
            "old answer",
        )

    def test_strict_schema_upgrade_never_persists_an_unstable_projection(self) -> None:
        from our_harness.providers import codex_cli

        board = self.board("workspace-13131313131313131313131313131313")
        with mock.patch.object(provider_base.shutil, "which", return_value=None):
            with mock.patch.object(
                codex_cli.CodexCLIProvider, "_effective_dispatch_contract",
                return_value="codex-cli/effective-dispatch/v1",
            ):
                listed = swarm_chats.list_for_agent(
                    self.config, board, "agent-2",
                )
            original = next(one for one in listed["chats"] if one["pair"] == ["agent-2"])

        registry = swarm_chats._read(self.config)
        raw = next(one for one in registry["chats"] if one["id"] == original["id"])
        registry["chats"] = [raw]
        # The strict-schema bridge is for chats saved before route identity;
        # with a recorded identity the refresh below would continue instead.
        raw["binding"].pop("route_identities", None)
        swarm_chats._write(self.config, registry)
        held = copy.deepcopy(raw["binding"]["agent_routes"]["agent-2"])
        first_current = copy.deepcopy(held)
        first_current["effective_dispatch_contract"] = (
            "codex-cli/effective-dispatch/v2"
        )
        first_current["effective_dispatch_fingerprint_sha256"] = "a" * 64
        later_current = copy.deepcopy(first_current)
        later_current["effective_dispatch_fingerprint_sha256"] = "b" * 64
        calls = 0

        def changing_projection(*_args: object, **_kwargs: object) -> dict:
            nonlocal calls
            calls += 1
            return copy.deepcopy(first_current if calls == 1 else later_current)

        with mock.patch.object(
            swarm_chats, "_route_binding", side_effect=changing_projection,
        ), mock.patch.object(
            swarm_chats, "_route_binding_for_effective_contract",
            return_value=copy.deepcopy(held),
        ):
            with self.assertRaisesRegex(swarm.SwarmError, "will not send that history"):
                swarm_chats.resolve(
                    self.config, board, "agent-2", original["id"],
                )

        self.assertGreaterEqual(calls, 2)
        persisted = swarm_chats._read(self.config)
        persisted_raw = next(
            one for one in persisted["chats"] if one["id"] == original["id"]
        )
        self.assertEqual(
            persisted_raw["binding"]["agent_routes"]["agent-2"], held,
        )

    def local_tool_routes(self) -> tuple[Path, Path]:
        first = self.root / "provider-a" / "agent-tool"
        second = self.root / "provider-b" / "agent-tool"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        self.config.data["providers"] = {
            "route-a": {
                "kind": "local", "model": "model-a", "command": ["agent-tool"],
            },
            "route-b": {
                "kind": "local", "model": "model-b", "command": ["agent-tool"],
            },
        }
        return first, second

    def test_executable_update_continues_and_records_the_new_dispatch(self) -> None:
        # A CLI auto-update (new file, new path, new version) is not a
        # different provider: the chat continues and the new observation is
        # recorded, so a later goal admission sees the current setup.
        first, second = self.local_tool_routes()
        board = self.board("workspace-dddddddddddddddddddddddddddddddd")
        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(first),
        ):
            conversation = swarm_chats.list_for_agent(
                self.config, board, "agent-1"
            )["chats"][0]
        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(second),
        ):
            listed = next(
                one for one in swarm_chats.list_for_agent(
                    self.config, board, "agent-1"
                )["chats"] if one["id"] == conversation["id"]
            )
            resolved = swarm_chats.resolve(
                self.config, board, "agent-1", conversation["id"],
            )
            current = swarm_chats._verified_chat_route_projection(
                swarm_chats._route_binding(self.config, board["agents"][0])
            )

        self.assertIsNone(listed["binding_problem"])
        self.assertIsNone(resolved["binding_problem"])
        self.assertNotEqual(
            conversation["binding"]["agent_routes"]["agent-1"][
                "effective_dispatch_fingerprint_sha256"
            ],
            current["effective_dispatch_fingerprint_sha256"],
        )
        persisted = next(
            one for one in swarm_chats._read(self.config)["chats"]
            if one["id"] == conversation["id"]
        )
        self.assertEqual(persisted["binding"]["agent_routes"]["agent-1"], current)
        # Goal admission compares these entries field for field; the
        # identity lives beside them and never adds a field to them.
        self.assertEqual(
            set(persisted["binding"]["agent_routes"]["agent-1"]),
            set(swarm_chats._VERIFIED_CHAT_ROUTE_FIELDS),
        )

    def test_cli_version_change_or_probe_timeout_never_pauses_the_chat(self) -> None:
        from our_harness.providers.subscription_cli import SubscriptionCLIProvider

        tool = self.root / "bin" / "claude.cmd"
        tool.parent.mkdir()
        tool.write_text("claude", encoding="utf-8")
        board = self.board("workspace-d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2")
        observations = iter([
            {"state": "observed", "version_output_sha256": "1" * 64},
            {"state": "observed", "version_output_sha256": "2" * 64},
            {"state": "timed-out", "version_output_sha256": "3" * 64},
        ])
        provider_base._dispatch_version_cache.clear()
        provider_base._dispatch_version_unsettled.clear()
        with mock.patch.object(provider_base.shutil, "which", return_value=str(tool)):
            with mock.patch.object(
                SubscriptionCLIProvider, "_effective_dispatch_version",
                side_effect=lambda _command: next(observations),
            ):
                conversation = swarm_chats.list_for_agent(
                    self.config, board, "agent-1",
                )["chats"][0]
                for _ in range(2):
                    # A new version (the CLI updated itself) and then a slow
                    # --version that timed out: both are recorded, neither pauses.
                    provider_base._dispatch_version_cache.clear()
                    provider_base._dispatch_version_unsettled.clear()
                    resolved = swarm_chats.resolve(
                        self.config, board, "agent-1", conversation["id"],
                    )
                    self.assertIsNone(resolved["binding_problem"])
        self.assertNotEqual(
            resolved["binding"]["agent_routes"]["agent-1"][
                "effective_dispatch_fingerprint_sha256"
            ],
            conversation["binding"]["agent_routes"]["agent-1"][
                "effective_dispatch_fingerprint_sha256"
            ],
        )

    def test_pre_identity_executable_drift_is_fenced_but_reviewable(self) -> None:
        first, second = self.local_tool_routes()
        board = self.board("workspace-d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1")
        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(first),
        ):
            conversation = swarm_chats.list_for_agent(
                self.config, board, "agent-1"
            )["chats"][0]
        self.forget_route_identities(conversation["id"])
        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(second),
        ):
            protected = next(
                one for one in swarm_chats.list_for_agent(
                    self.config, board, "agent-1"
                )["chats"] if one["id"] == conversation["id"]
            )

        self.assertEqual(
            protected["binding_problem"]["code"], "agent_binding_changed"
        )
        self.assertEqual(
            protected["binding_problem"]["changed_agents"][0]["kind"],
            "effective_dispatch_changed",
        )
        self.assertTrue(protected["binding_problem"]["can_review_reconnect"])

    def test_tunable_edits_continue_across_restart(self) -> None:
        board = self.board("workspace-44444444444444444444444444444444")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        chat.keep_exchange(
            self.config, "route-a", "old question", "old answer",
            filed_as=conversation["filed_as"],
        )
        restarted = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        self.assertEqual(
            swarm_chats.resolve(
                restarted, board, "agent-1", conversation["id"]
            )["id"],
            conversation["id"],
        )

        edited = restarted.data["providers"]["route-a"]
        edited.update({
            "model": "different-model", "reasoning_effort": "high",
            "timeout_seconds": 1200, "arguments": ["--verbose"],
            "max_concurrency": 2, "temperature": 0.7,
        })
        listed = swarm_chats.list_for_agent(restarted, board, "agent-1")
        self.assertIsNone(listed["chats"][0]["binding_problem"])
        self.assertEqual(
            swarm_chats.resolve(
                restarted, board, "agent-1", conversation["id"]
            )["id"],
            conversation["id"],
        )
        # A second restart reads the refreshed binding, not the old one.
        again = LoadedConfig(copy.deepcopy(restarted.data), self.root, [], {})
        resolved = swarm_chats.resolve(again, board, "agent-1", conversation["id"])
        self.assertIsNone(resolved["binding_problem"])
        self.assertEqual(chat.read_it(
            again, "route-a", resolved["filed_as"],
        )[-1].text, "old answer")
        _kind, context = chat._route_failure_context(again, "route-a")
        self.assertEqual(
            resolved["binding"]["agent_routes"]["agent-1"]["route_fingerprint_sha256"],
            context["route_fingerprint_sha256"],
        )

    def test_identity_change_is_reviewable_and_never_silent(self) -> None:
        board = self.board("workspace-45454545454545454545454545454545")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        original = copy.deepcopy(self.config.data["providers"]["route-a"])
        for change in (
            {"kind": "copilot-cli"},
            {"api_key_env": "ANOTHER_ACCOUNT_KEY"},
            {"command": ["another-program"]},
        ):
            with self.subTest(change=change):
                self.config.data["providers"]["route-a"] = {**original, **change}
                listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
                problem = listed["chats"][0]["binding_problem"]
                self.assertEqual(problem["code"], "agent_binding_changed")
                self.assertEqual(
                    problem["changed_agents"][0]["kind"], "route_identity_changed",
                )
                self.assertTrue(problem["can_review_reconnect"])
                self.assertIn("provider identity changed", problem["message"])
                with self.assertRaisesRegex(Exception, "will not send that history"):
                    swarm_chats.resolve(
                        self.config, board, "agent-1", conversation["id"]
                    )
        # Nothing was rebound while it was different: restoring the setup
        # continues the same chat.
        self.config.data["providers"]["route-a"] = original
        self.assertIsNone(swarm_chats.resolve(
            self.config, board, "agent-1", conversation["id"],
        )["binding_problem"])

    def test_pre_identity_chat_is_migrated_while_it_still_matches(self) -> None:
        board = self.board("workspace-46464646464646464646464646464646")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        self.forget_route_identities(conversation["id"])
        self.assertNotIn(
            "route_identities",
            next(one for one in swarm_chats._read(self.config)["chats"]
                 if one["id"] == conversation["id"])["binding"],
        )

        swarm_chats.list_for_agent(self.config, board, "agent-1")
        migrated = next(
            one for one in swarm_chats._read(self.config)["chats"]
            if one["id"] == conversation["id"]
        )
        self.assertEqual(
            migrated["binding"]["route_identities"]["agent-1"],
            chat.route_identity(self.config, "route-a"),
        )
        self.config.data["providers"]["route-a"]["model"] = "migrated-then-edited"
        self.assertIsNone(swarm_chats.resolve(
            self.config, board, "agent-1", conversation["id"],
        )["binding_problem"])

    def test_pre_identity_chat_that_already_drifted_offers_reconnect(self) -> None:
        board = self.board("workspace-47474747474747474747474747474747")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        self.forget_route_identities(conversation["id"])
        self.config.data["providers"]["route-a"]["model"] = "edited-before-upgrade"

        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        problem = listed["chats"][0]["binding_problem"]
        self.assertEqual(problem["code"], "agent_binding_changed")
        self.assertEqual(
            problem["changed_agents"][0]["kind"], "route_settings_changed",
        )
        # Not "start fresh" only: the saved chat can be reviewed and resumed.
        self.assertTrue(problem["can_review_reconnect"])
        # The unchanged peer is migrated; the drifted route is never blessed.
        identities = next(
            one for one in swarm_chats._read(self.config)["chats"]
            if one["id"] == conversation["id"]
        )["binding"].get("route_identities", {})
        self.assertNotIn("agent-1", identities)
        self.assertIn("agent-2", identities)

    def test_route_identity_ignores_tunables_but_not_account_or_program(self) -> None:
        base = {"kind": "claude-cli", "model": "a", "command": ["claude", "-x"],
                "api_key_env": "FIRST_ACCOUNT_KEY",
                "endpoint": "https://user:secret@api.example/v1"}
        self.config.data["providers"] = {"route-a": dict(base)}
        before = chat.route_identity(self.config, "route-a")
        for key, value in (
            ("model", "b"), ("reasoning_effort", "low"), ("timeout_seconds", 5),
            ("arguments", ["--verbose"]), ("max_output_tokens", 10),
            ("command", ["claude", "-x", "--model", "other", "--verbose"]),
            ("endpoint", "https://user:rotated@api.example/v1"),
        ):
            self.config.data["providers"]["route-a"] = {**base, key: value}
            self.assertEqual(chat.route_identity(self.config, "route-a"), before, key)
        for key, value in (
            ("kind", "codex-cli"), ("api_key_env", "OTHER"),
            ("auth_mode", "work"), ("command", ["other-program"]),
            ("command", ["claude", "--different-flags"]),
            ("endpoint", "https://other.example/v1"),
        ):
            self.config.data["providers"]["route-a"] = {**base, key: value}
            self.assertNotEqual(chat.route_identity(self.config, "route-a"), before, key)
        self.assertEqual(before["route_identity_version"], chat.ROUTE_IDENTITY_VERSION)
        self.assertRegex(before["route_identity_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("claude", json.dumps(before))

    def test_account_selectors_in_flags_or_environment_are_identity(self) -> None:
        base = {"kind": "codex-cli", "model": "a", "command": ["codex"],
                "arguments": ["--model", "a", "-c", "model_reasoning_effort=low"]}
        self.config.data["providers"] = {"route-a": copy.deepcopy(base)}
        before = chat.route_identity(self.config, "route-a")
        for arguments in (
            ["--model", "b", "-c", "model_reasoning_effort=high"],
            ["--model=b", "--timeout", "900", "-c", 'model="o3"'],
        ):
            self.config.data["providers"]["route-a"] = {**base, "arguments": arguments}
            self.assertEqual(chat.route_identity(self.config, "route-a"), before, arguments)
        for change in (
            {"arguments": ["--profile", "other"]},
            {"arguments": ["-p", "other"]},
            {"arguments": ["--account=someone-else"]},
            {"arguments": ["-c", "profile=other"]},
            {"arguments": ["--config", "C:/other/config.toml"]},
            {"arguments": ["--api-key-env", "OTHER_KEY"]},
            {"command": ["env", "CODEX_HOME=C:/other-home", "codex"]},
            {"command": ["codex", "CLAUDE_CONFIG_DIR=C:/other"]},
        ):
            self.config.data["providers"]["route-a"] = {**base, **change}
            self.assertNotEqual(chat.route_identity(self.config, "route-a"), before, change)
        # Claude's -p is --print, not a profile selector.
        self.config.data["providers"]["route-a"] = {"kind": "claude-cli", "arguments": ["-p"]}
        self.assertEqual(chat.route_account_selectors(self.config, "route-a"), [])
        self.assertEqual(before["route_identity_version"], chat.ROUTE_IDENTITY_VERSION)

    def test_any_other_command_word_or_unlisted_flag_is_identity(self) -> None:
        cases = [
            ({"kind": "assistant-cli", "command": ["python", "bridge_openai.py"]},
             {"kind": "assistant-cli", "command": ["python", "bridge_anthropic.py"]}),
            ({"kind": "claude-cli", "command": ["npx", "-y", "@anthropic-ai/claude-code"]},
             {"kind": "claude-cli", "command": ["npx", "-y", "some-other-cli"]}),
            ({"kind": "claude-cli", "command": ["ssh", "alice@hostA", "claude"]},
             {"kind": "claude-cli", "command": ["ssh", "bob@hostB", "claude"]}),
            ({"kind": "codex-cli", "command": ["wsl", "-d", "Work", "codex"]},
             {"kind": "codex-cli", "command": ["wsl", "-d", "Personal", "codex"]}),
            ({"kind": "codex-cli", "command": ["codex"]},
             {"kind": "codex-cli", "command": ["codex", "--oss", "--local-provider", "ollama"]}),
            ({"kind": "assistant-cli", "command": ["ask"], "arguments": ["--vendor", "openai"]},
             {"kind": "assistant-cli", "command": ["ask"], "arguments": ["--vendor", "anthropic"]}),
            # For assistant-cli even a flag named --model is identity.
            ({"kind": "assistant-cli", "command": ["ask", "--model", "a"]},
             {"kind": "assistant-cli", "command": ["ask", "--model", "b"]}),
        ]
        for before, after in cases:
            with self.subTest(after=after):
                self.config.data["providers"] = {"route-a": before}
                first = chat.route_identity(self.config, "route-a")
                self.config.data["providers"] = {"route-a": after}
                self.assertNotEqual(chat.route_identity(self.config, "route-a"), first)
        for kind, before, after in (
            ("claude-cli", ["claude", "-p", "--model", "a", "--output-format", "json"],
             ["claude", "--print", "--model=b", "--output-format", "stream-json", "--verbose"]),
            ("codex-cli", ["codex", "exec", "-m", "a"], ["codex", "exec", "-m", "b", "--json"]),
            ("gemini-cli", ["gemini", "-m", "a"], ["gemini", "--model", "b"]),
        ):
            with self.subTest(kind=kind):
                self.config.data["providers"] = {"route-a": {"kind": kind, "command": before}}
                first = chat.route_identity(self.config, "route-a")
                self.config.data["providers"] = {"route-a": {"kind": kind, "command": after}}
                self.assertEqual(chat.route_identity(self.config, "route-a"), first)

    def test_wrapper_flags_before_the_cli_program_are_identity(self) -> None:
        for kind, before, after in (
            ("codex-cli", ["python", "-m", "bridge_vendor_a"], ["python", "-m", "bridge_vendor_b"]),
            ("gemini-cli", ["wsl", "-d", "X", "-m", "a", "gemini"], ["wsl", "-d", "X", "-m", "b", "gemini"]),
            ("claude-cli", ["npx", "--model=pkgA"], ["npx", "--model=pkgB"]),
            ("codex-cli", ["ssh", "-m", "hostA", "codex"], ["ssh", "-m", "hostB", "codex"]),
            # A tunable flag never swallows the next flag as its value.
            ("claude-cli", ["claude", "--model", "--settings", "a.json"],
             ["claude", "--model", "--settings", "b.json"]),
            ("codex-cli", ["codex", "--timeout", "--profile", "a"],
             ["codex", "--timeout", "--profile", "b"]),
            # A host, container, distro or module that is merely named like
            # the program is not the program: the whole command is identity.
            ("codex-cli", ["ssh", "codex", "python3", "-m", "bridge_a"],
             ["ssh", "codex", "python3", "-m", "bridge_b"]),
            ("codex-cli", ["docker", "exec", "codex", "python", "-m", "bridge_a"],
             ["docker", "exec", "codex", "python", "-m", "bridge_b"]),
            ("codex-cli", ["python", "-m", "codex", "-m", "vendor_a"],
             ["python", "-m", "codex", "-m", "vendor_b"]),
            ("codex-cli", ["python", "-m", "bridge", "codex", "-m", "a"],
             ["python", "-m", "bridge", "codex", "-m", "b"]),
        ):
            with self.subTest(before=before):
                self.config.data["providers"] = {"route-a": {"kind": kind, "command": before}}
                first = chat.route_identity(self.config, "route-a")
                self.config.data["providers"] = {"route-a": {"kind": kind, "command": after}}
                self.assertNotEqual(chat.route_identity(self.config, "route-a"), first)
        # Tunables after the program, and an omitted default command, still match.
        for kind, before, after in (
            ("codex-cli", ["npx", "-y", "@openai/codex@latest", "-m", "a"],
             ["npx", "-y", "@openai/codex@latest", "-m", "b"]),
            ("codex-cli", ["C:/n/codex-x86_64-pc-windows-msvc.exe", "-m", "a"],
             ["C:/n/codex-x86_64-pc-windows-msvc.exe", "-m", "b"]),
            ("claude-cli", ["node", "C:/npm/node_modules/@anthropic-ai/claude-code/cli.js", "--model", "a"],
             ["node", "C:/npm/node_modules/@anthropic-ai/claude-code/cli.js", "--model", "b"]),
            ("claude-cli", ["cmd", "/c", "claude.cmd", "--model", "a"],
             ["cmd", "/c", "claude.cmd", "--model", "b"]),
            ("codex-cli", ["wsl", "-d", "Work", "--", "codex", "-m", "a"],
             ["wsl", "-d", "Work", "--", "codex", "-m", "b"]),
            ("claude-cli", None, ["claude", "--model", "x", "--permission-mode", "plan"]),
        ):
            with self.subTest(after=after):
                self.config.data["providers"] = {"route-a": {"kind": kind, **({"command": before} if before else {})}}
                first = chat.route_identity(self.config, "route-a")
                self.config.data["providers"] = {"route-a": {"kind": kind, "command": after}}
                self.assertEqual(chat.route_identity(self.config, "route-a"), first)

    def test_arguments_of_a_command_without_the_program_are_identity(self) -> None:
        self.config.data["providers"] = {"route-a": {
            "kind": "codex-cli", "command": ["python", "-m", "bridge"], "arguments": ["-m", "vendor_a"],
        }}
        first = chat.route_identity(self.config, "route-a")
        self.config.data["providers"]["route-a"]["arguments"] = ["-m", "vendor_b"]
        self.assertNotEqual(chat.route_identity(self.config, "route-a"), first)

    def test_python_m_bridge_swap_pauses_a_codex_chat(self) -> None:
        self.config.data["providers"]["route-a"] = {
            "kind": "codex-cli", "command": ["python", "-m", "bridge_vendor_a"],
        }
        board = self.board("workspace-8a8a8a8a8a8a8a8a8a8a8a8a8a8a8a8a")
        conversation = swarm_chats.list_for_agent(self.config, board, "agent-1")["chats"][0]
        self.config.data["providers"]["route-a"]["command"] = ["python", "-m", "bridge_vendor_b"]
        problem = swarm_chats.list_for_agent(
            self.config, board, "agent-1")["chats"][0]["binding_problem"]
        self.assertEqual(problem["changed_agents"][0]["kind"], "route_identity_changed")
        self.assertTrue(problem["can_review_reconnect"])

    def test_endpoint_username_and_plain_query_are_identity_but_secrets_are_not(self) -> None:
        def identity(endpoint: str) -> dict:
            self.config.data["providers"] = {
                "route-a": {"kind": "openai-compatible", "endpoint": endpoint, "model": "m"},
            }
            return chat.route_identity(self.config, "route-a")

        base = identity("https://alice@h.example/v1?tenant=a&api-key=one")
        self.assertNotEqual(identity("https://bob@h.example/v1?tenant=a&api-key=one"), base)
        self.assertNotEqual(identity("https://alice@h.example/v1?tenant=b&api-key=one"), base)
        # A rotated password or key is not a different provider.
        self.assertEqual(identity("https://alice:pw@h.example/v1?tenant=a&api-key=two"), base)
        self.assertEqual(identity("https://alice@h.example/v1?api-key=three&tenant=a"), base)

    def test_local_execution_container_is_identity(self) -> None:
        self.config.data["providers"] = {
            "route-a": {"kind": "local", "model": "m", "command": ["tool"]},
        }
        self.config.data.setdefault("execution", {})
        before = chat.route_identity(self.config, "route-a")
        self.config.data["execution"]["docker_image"] = "other-image:latest"
        self.assertNotEqual(chat.route_identity(self.config, "route-a"), before)

    def test_bridge_or_host_swap_pauses_the_chat_for_review(self) -> None:
        for profile, changed in (
            ({"kind": "assistant-cli", "command": ["python", "bridge_vendor_a.py"]},
             ["python", "bridge_vendor_b.py"]),
            ({"kind": "claude-cli", "command": ["ssh", "alice@hostA", "claude"]},
             ["ssh", "bob@hostB", "claude"]),
        ):
            with self.subTest(changed=changed):
                self.config.data["providers"]["route-a"] = copy.deepcopy(profile)
                board = self.board("workspace-7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a" + changed[1][-2:].encode().hex()[:2])
                conversation = swarm_chats.list_for_agent(
                    self.config, board, "agent-1")["chats"][0]
                self.config.data["providers"]["route-a"]["command"] = changed
                problem = swarm_chats.list_for_agent(
                    self.config, board, "agent-1")["chats"][0]["binding_problem"]
                self.assertEqual(problem["changed_agents"][0]["kind"], "route_identity_changed")
                self.assertTrue(problem["can_review_reconnect"])
                with self.assertRaisesRegex(Exception, "will not send that history"):
                    swarm_chats.resolve(self.config, board, "agent-1", conversation["id"])

    def downgrade_route_identities_to_v1(self, chat_id: str, version: int = 1) -> None:
        """Make one saved chat look like it was saved under an older identity."""

        registry = swarm_chats._read(self.config)
        raw = next(one for one in registry["chats"] if one["id"] == chat_id)
        raw["binding"]["route_identities"] = {
            member: chat.route_identity(self.config, held["route"], version=version)
            for member, held in raw["binding"]["agent_routes"].items()
        }
        swarm_chats._write(self.config, registry)

    def test_model_flag_change_continues_but_profile_flag_pauses_for_review(self) -> None:
        self.config.data["providers"]["route-a"]["arguments"] = ["--model", "a"]
        board = self.board("workspace-48484848484848484848484848484848")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        self.config.data["providers"]["route-a"]["arguments"] = ["--model", "b"]
        self.assertIsNone(swarm_chats.resolve(
            self.config, board, "agent-1", conversation["id"],
        )["binding_problem"])

        self.config.data["providers"]["route-a"]["arguments"] = [
            "--model", "b", "--profile", "other",
        ]
        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        problem = listed["chats"][0]["binding_problem"]
        self.assertEqual(problem["changed_agents"][0]["kind"], "route_identity_changed")
        self.assertTrue(problem["can_review_reconnect"])
        with self.assertRaisesRegex(Exception, "will not send that history"):
            swarm_chats.resolve(self.config, board, "agent-1", conversation["id"])

    def test_route_identity_v1_records_migrate_without_pausing(self) -> None:
        board = self.board("workspace-49494949494949494949494949494949")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]

        def saved_identities() -> dict:
            return next(
                one for one in swarm_chats._read(self.config)["chats"]
                if one["id"] == conversation["id"]
            )["binding"]["route_identities"]

        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                # Unchanged setup: upgraded silently on the next inventory.
                self.downgrade_route_identities_to_v1(conversation["id"], version)
                self.assertIsNone(swarm_chats.list_for_agent(
                    self.config, board, "agent-1")["chats"][0]["binding_problem"])
                self.assertEqual(saved_identities()["agent-1"],
                                 chat.route_identity(self.config, "route-a"))
        # Restart keeps the upgraded record.
        restarted = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        self.assertIsNone(swarm_chats.resolve(
            restarted, board, "agent-1", conversation["id"])["binding_problem"])
        self.assertEqual(saved_identities()["agent-1"]["route_identity_version"], chat.ROUTE_IDENTITY_VERSION)

    def test_old_identity_with_a_changed_profile_is_reviewed_not_migrated(self) -> None:
        # An old digest ignored inputs v3 covers, so a matching old digest over
        # a changed profile proves nothing; it is reviewed, never migrated.
        for version, change in (
            (1, lambda profile: profile.update(model="edited-after-v1")),
            (2, lambda profile: profile.update(command=["ssh", "bob@hostB", "claude"])),
        ):
            with self.subTest(version=version):
                self.config.data["providers"]["route-a"] = {
                    "kind": "claude-cli", "model": "claude-a",
                    "command": ["ssh", "alice@hostA", "claude"],
                }
                board = self.board(f"workspace-{version}c{version}c" + "c" * 28)
                conversation = swarm_chats.list_for_agent(
                    self.config, board, "agent-1")["chats"][0]
                self.downgrade_route_identities_to_v1(conversation["id"], version)
                change(self.config.data["providers"]["route-a"])
                problem = swarm_chats.list_for_agent(
                    self.config, board, "agent-1")["chats"][0]["binding_problem"]
                self.assertEqual(problem["changed_agents"][0]["kind"], "route_identity_changed")
                self.assertTrue(problem["can_review_reconnect"])

    def test_old_identity_survives_a_cli_update_without_review(self) -> None:
        first, second = self.local_tool_routes()
        board = self.board("workspace-4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b4b")
        with mock.patch.object(provider_base.shutil, "which", return_value=str(first)):
            conversation = swarm_chats.list_for_agent(
                self.config, board, "agent-1")["chats"][0]
            self.downgrade_route_identities_to_v1(conversation["id"], 2)
        with mock.patch.object(provider_base.shutil, "which", return_value=str(second)):
            self.assertIsNone(swarm_chats.resolve(
                self.config, board, "agent-1", conversation["id"])["binding_problem"])

    def test_route_identity_v1_with_a_changed_profile_and_a_selector_is_reviewed(self) -> None:
        self.config.data["providers"]["route-a"]["arguments"] = ["--profile", "work"]
        board = self.board("workspace-4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        self.downgrade_route_identities_to_v1(conversation["id"])
        # v1 ignored flags, so it cannot tell whether --profile changed too.
        self.config.data["providers"]["route-a"]["model"] = "edited-after-v1"
        problem = swarm_chats.list_for_agent(
            self.config, board, "agent-1")["chats"][0]["binding_problem"]
        self.assertEqual(problem["changed_agents"][0]["kind"], "route_identity_changed")
        self.assertTrue(problem["can_review_reconnect"])
        # With the saved profile unchanged the v1 record is proven and upgraded.
        self.config.data["providers"]["route-a"]["model"] = "claude-a"
        self.assertIsNone(swarm_chats.resolve(
            self.config, board, "agent-1", conversation["id"])["binding_problem"])

    def test_v1_record_whose_profile_selector_was_removed_is_reviewed(self) -> None:
        self.config.data["providers"]["route-b"] = {
            "kind": "codex-cli", "model": "codex-b", "command": ["codex", "--profile", "work"],
        }
        board = self.board("workspace-7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1")["chats"][0]
        self.downgrade_route_identities_to_v1(conversation["id"])
        # Back to the default account: never migrated silently.
        self.config.data["providers"]["route-b"]["command"] = ["codex"]
        problem = swarm_chats.list_for_agent(
            self.config, board, "agent-1")["chats"][0]["binding_problem"]
        self.assertEqual(problem["changed_agents"][0]["agent_id"], "agent-2")
        self.assertEqual(problem["changed_agents"][0]["kind"], "route_identity_changed")
        self.assertTrue(problem["can_review_reconnect"])

    def test_project_path_rebind_is_never_silent(self) -> None:
        board = self.board("workspace-55555555555555555555555555555555")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        rebound = self.board(board["workspace_id"], project_path=self.second)

        listed = swarm_chats.list_for_agent(self.config, rebound, "agent-1")
        protected = next(one for one in listed["chats"] if one["id"] == conversation["id"])
        self.assertEqual(protected["binding_problem"]["code"], "project_binding_changed")
        with self.assertRaisesRegex(Exception, "different folder"):
            swarm_chats.resolve(
                self.config, rebound, "agent-1", conversation["id"]
            )

    def test_same_path_directory_replacement_fences_a_strong_new_binding(self) -> None:
        board = self.board("workspace-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        held_project = conversation["binding"]["project"]
        self.assertEqual(
            conversation["binding"]["binding_schema_version"],
            swarm_chats.CHAT_BINDING_SCHEMA_VERSION,
        )
        self.assertEqual(held_project["identity_strength"], "filesystem")
        self.assertEqual(conversation["effective_dispatch_strength"], "verified")

        retired = self.root / "retired-first-directory"
        self.first.rename(retired)
        self.first.mkdir()
        self.assertNotEqual(
            held_project["directory_identity_sha256"],
            swarm_chats._filesystem_project_identity(str(self.first))[
                "directory_identity_sha256"
            ],
        )

        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        protected = next(
            one for one in listed["chats"] if one["id"] == conversation["id"]
        )
        self.assertEqual(
            protected["binding_problem"]["code"], "project_binding_changed"
        )
        self.assertEqual(
            protected["binding_problem"]["reason"], "directory_identity_changed"
        )
        with self.assertRaisesRegex(Exception, "different local folder object"):
            swarm_chats.resolve(
                self.config, board, "agent-1", conversation["id"]
            )

    def test_explicit_project_reselect_rebinds_a_moved_folder_and_keeps_the_chat(self) -> None:
        # "Use a different folder on this computer": same project id, new path.
        board = self.board("workspace-dddddddddddddddddddddddddddddddd")
        conversation = next(
            one for one in swarm_chats.list_for_agent(
                self.config, board, "agent-1"
            )["chats"] if len(one["pair"]) == 2
        )
        chat.keep_exchange(
            self.config, "route-a", "earlier request", "earlier answer",
            filed_as=conversation["filed_as"],
        )
        stale = CollaborationLedger(
            self.config, "route-a", conversation["filed_as"], session_id="old-folder",
        ).begin("old folder work", board["agents"], mode="project_work")
        rebound = self.board(board["workspace_id"], project_path=self.second)
        protected = next(
            one for one in swarm_chats.list_for_agent(
                self.config, rebound, "agent-1"
            )["chats"] if one["id"] == conversation["id"]
        )
        self.assertEqual(protected["binding_problem"]["reason"], "project_path_changed")
        self.assertTrue(protected["binding_problem"]["can_rebind_project"])

        chosen = swarm_chats.select_project(
            self.config, rebound, "agent-1", conversation["id"], "project-1"
        )
        current = next(
            one for one in chosen["chats"] if one["id"] == conversation["id"]
        )
        self.assertEqual(chosen["active"], conversation["id"])
        self.assertIsNone(current["binding_problem"])
        self.assertEqual(current["filed_as"], conversation["filed_as"])
        self.assertEqual(current["project"], "project-1")
        self.assertEqual(current["project_binding_strength"], "filesystem")
        # Writers started against the old folder cannot land after the rebind.
        with self.assertRaisesRegex(Exception, "no longer current"):
            stale.record_state("late", {"status": "wrong"})

        restarted = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        resolved = swarm_chats.resolve(
            restarted, rebound, "agent-1", conversation["id"]
        )
        self.assertIsNone(resolved["binding_problem"])
        self.assertEqual(chat.read_it(
            restarted, "route-a", conversation["filed_as"]
        )[-1].text, "earlier answer")
        # The rebind is to the chosen folder only; the old path is now foreign.
        with self.assertRaisesRegex(Exception, "different folder"):
            swarm_chats.resolve(restarted, board, "agent-1", conversation["id"])

    def test_explicit_project_reselect_rebinds_a_recloned_folder(self) -> None:
        board = self.board("workspace-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        self.first.rename(self.root / "retired-before-reclone")
        self.first.mkdir()
        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        protected = next(
            one for one in listed["chats"] if one["id"] == conversation["id"]
        )
        self.assertEqual(
            protected["binding_problem"]["reason"], "directory_identity_changed"
        )
        # Listing and resolving never rebind on their own.
        with self.assertRaisesRegex(Exception, "different local folder object"):
            swarm_chats.resolve(self.config, board, "agent-1", conversation["id"])

        chosen = swarm_chats.select_project(
            self.config, board, "agent-1", conversation["id"], "project-1"
        )
        current = next(
            one for one in chosen["chats"] if one["id"] == conversation["id"]
        )
        self.assertIsNone(current["binding_problem"])
        self.assertEqual(
            current["binding"]["project"]["directory_identity_sha256"],
            swarm_chats._filesystem_project_identity(str(self.first))[
                "directory_identity_sha256"
            ],
        )
        self.assertEqual(
            swarm_chats.resolve(
                self.config, board, "agent-1", conversation["id"]
            )["id"],
            conversation["id"],
        )

    def test_project_reselect_never_clears_a_provider_binding_problem(self) -> None:
        board = self.board("workspace-ffffffffffffffffffffffffffffffff")
        conversation = next(
            one for one in swarm_chats.list_for_agent(
                self.config, board, "agent-1"
            )["chats"] if len(one["pair"]) == 2
        )
        before = next(
            one for one in swarm_chats._read(self.config)["chats"]
            if one["id"] == conversation["id"]
        )["binding"]
        for changed in (
            self.board(board["workspace_id"], route="route-b"),
            self.board(
                board["workspace_id"], route="route-b", project_path=self.second,
            ),
        ):
            with self.subTest(project=changed["projects"][0]["path"]):
                with self.assertRaisesRegex(Exception, "will not send that history"):
                    swarm_chats.select_project(
                        self.config, changed, "agent-1", conversation["id"],
                        "project-1",
                    )
                held = next(
                    one for one in swarm_chats._read(self.config)["chats"]
                    if one["id"] == conversation["id"]
                )
                self.assertEqual(
                    held["binding"]["agent_routes"]["agent-1"]["route"], "route-a"
                )
                self.assertEqual(held["binding"], before)

    def test_legacy_path_binding_is_visible_and_upgrades_only_on_explicit_rebind(self) -> None:
        board = self.board("workspace-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        initial = swarm_chats.list_for_agent(self.config, board, "agent-1")
        conversation = next(
            one for one in initial["chats"] if len(one["pair"]) == 2
        )
        direct = next(one for one in initial["chats"] if len(one["pair"]) == 1)
        self.assertEqual(direct["project"], "project-1")
        where = self.root / swarm_chats.WHERE_THEY_LIVE
        registry = json.loads(where.read_text(encoding="utf-8"))
        raw = next(one for one in registry["chats"] if one["id"] == conversation["id"])
        raw["binding"] = {
            "binding_schema_version": 1,
            "agent_routes": raw["binding"]["agent_routes"],
            "project": {
                "id": raw["binding"]["project"]["id"],
                "path_fingerprint_sha256": (
                    raw["binding"]["project"]["path_fingerprint_sha256"]
                ),
            },
        }
        swarm_chats._write(self.config, registry)

        retired = self.root / "retired-legacy-directory"
        self.first.rename(retired)
        self.first.mkdir()
        with mock.patch.object(
            swarm_chats, "_filesystem_project_identity",
            wraps=swarm_chats._filesystem_project_identity,
        ) as identity_probe:
            listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        # The permanent singleton has one modern project binding. The legacy
        # pair must not cause a second filesystem identity probe until the
        # explicit rebind below.
        identity_probe.assert_called_once_with(str(self.first))
        legacy = next(
            one for one in listed["chats"] if one["id"] == conversation["id"]
        )
        self.assertIsNone(legacy["binding_problem"])
        self.assertEqual(legacy["project_binding_strength"], "legacy-path-only")
        self.assertEqual(
            legacy["effective_dispatch_strength"], "legacy-unverified"
        )
        self.assertEqual(
            legacy["binding"]["project"]["identity_strength"],
            "legacy-path-only",
        )

        upgraded = swarm_chats.select_project(
            self.config, board, "agent-1", conversation["id"], "project-1"
        )
        current = next(
            one for one in upgraded["chats"] if one["id"] == conversation["id"]
        )
        self.assertEqual(current["project_binding_strength"], "filesystem")
        self.assertEqual(current["effective_dispatch_strength"], "verified")
        self.assertEqual(
            current["binding"]["binding_schema_version"],
            swarm_chats.CHAT_BINDING_SCHEMA_VERSION,
        )

        rebound = self.root / "retired-current-directory"
        self.first.rename(rebound)
        self.first.mkdir()
        protected = next(
            one for one in swarm_chats.list_for_agent(
                self.config, board, "agent-1"
            )["chats"] if one["id"] == conversation["id"]
        )
        self.assertEqual(
            protected["binding_problem"]["reason"], "directory_identity_changed"
        )

    def test_inspecting_a_missing_project_never_creates_authority(self) -> None:
        missing = self.root / "not-created-by-a-chat-read"
        board = self.board(
            "workspace-cccccccccccccccccccccccccccccccc",
            project_path=missing,
        )

        conversation = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        self.assertFalse(missing.exists())
        self.assertEqual(
            conversation["project_binding_strength"], "unavailable"
        )
        with self.assertRaisesRegex(Exception, "stable local identity"):
            swarm_chats.select_project(
                self.config, board, "agent-1", conversation["id"], "project-1"
            )
        self.assertFalse(missing.exists())

    def test_v5_registry_is_claimed_once_by_the_legacy_live_board(self) -> None:
        board = self.board("workspace-legacy-1234567890abcdef12345678")
        chat_id = "chat-1234567890abcdef"
        filed_as = swarm_chats._filed_as(["agent-1", "agent-2"], chat_id)
        where = self.root / swarm_chats.WHERE_THEY_LIVE
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps({
            "schema_version": 5,
            "chats": [{
                "id": chat_id, "pair": ["agent-1", "agent-2"], "name": "Chat 1",
                "project": "project-1", "filed_as": filed_as,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "legacy_recovered": True, "legacy_source": "", "archived_at": "",
                "web_legacy_candidate": True,
            }],
            "active": {"agent-1": chat_id},
            "chosen_active": {"agent-1": chat_id},
            "known_pairs": ["agent-1|agent-2"],
        }), encoding="utf-8")

        listed = swarm_chats.list_for_agent(self.config, board, "agent-1")
        self.assertEqual(listed["active"], chat_id)
        self.assertEqual(listed["chats"][0]["filed_as"], filed_as)
        self.assertEqual(listed["chats"][0]["filed_as_version"], 1)
        self.assertIsNone(listed["chats"][0]["binding_problem"])
        self.assertEqual(
            listed["chats"][0]["project_binding_strength"], "legacy-path-only"
        )
        persisted = json.loads(where.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["schema_version"], swarm_chats.REGISTRY_SCHEMA_VERSION
        )
        self.assertEqual(persisted["chats"][0]["workspace_id"], board["workspace_id"])

    def test_start_again_rotates_only_the_remote_provider_conversation_identity(self) -> None:
        board = self.board(
            "workspace-1234567890abcdef1234567890abcdef",
            route="web:chatgpt-example",
        )
        original = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        old_key = original["web_conversation_key"]
        self.assertEqual(old_key, original["filed_as"])

        restarted = swarm_chats.restart_provider_conversation(
            self.config, board, "agent-1", original["id"],
        )

        self.assertEqual(restarted["id"], original["id"])
        self.assertEqual(restarted["filed_as"], original["filed_as"])
        self.assertNotEqual(restarted["web_conversation_key"], old_key)
        self.assertEqual(
            restarted["destination"]["web_conversation_key"],
            restarted["web_conversation_key"],
        )
        self.assertFalse(restarted["web_legacy_candidate"])
        reopened = swarm_chats.list_for_agent(
            self.config, board, "agent-1"
        )["chats"][0]
        self.assertEqual(
            reopened["web_conversation_key"],
            restarted["web_conversation_key"],
        )

    def test_start_again_preserves_old_unknown_effect_and_unfences_fresh_key(self) -> None:
        board = self.board(
            "workspace-abcdefabcdefabcdefabcdefabcdefab",
            route="web:chatgpt-replay-safety",
        )
        route = board["agents"][0]["who"]
        with tempfile.TemporaryDirectory() as runtime_directory, mock.patch.dict(
            os.environ,
            {
                "OUR_HARNESS_SWARM_RUN_DIR": str(
                    Path(runtime_directory) / "swarm-runs"
                ),
                "OUR_HARNESS_PIPELINE_RUN_DIR": str(
                    Path(runtime_directory) / "pipeline-runs"
                ),
            },
        ):
            original = swarm_chats.list_for_agent(
                self.config, board, "agent-1"
            )["chats"][0]
            old_key = original["web_conversation_key"]
            first_store = SwarmRunStore.for_communication(self.config)
            accepted, _ = first_store.accept(
                "old-web-effect", {"kind": "chat", "chat_id": original["id"]}
            )
            old_run = first_store.start(accepted["run_id"])["run_id"]
            with bind(first_store, old_run):
                with self.assertRaises(ProviderOutcomeUnknown):
                    with provider_effect(
                        self.config, route, old_key, "old-request-digest"
                    ):
                        raise ProviderOutcomeUnknown(
                            "renderer vanished after the provider accepted Send"
                        )
            first_store.fail(old_run, "old provider conversation is uncertain")
            old_projection = first_store.projection(old_run)
            old_audit = copy.deepcopy(old_projection["events"])
            self.assertEqual(old_projection["status"], "delivery_unknown")
            self.assertIn("provider_dispatched", [one["kind"] for one in old_audit])
            self.assertIn("delivery_unknown", [one["kind"] for one in old_audit])

            reopened_store = SwarmRunStore.for_communication(self.config)
            accepted, _ = reopened_store.accept(
                "fresh-web-effect", {"kind": "chat", "chat_id": original["id"]}
            )
            fresh_run = reopened_store.start(accepted["run_id"])["run_id"]
            with bind(reopened_store, fresh_run):
                with self.assertRaisesRegex(
                    HarnessError, "uncertain prior delivery"
                ):
                    with provider_effect(
                        self.config, route, old_key, "different-request-digest"
                    ):
                        self.fail("the old provider conversation must remain fenced")

            restarted = swarm_chats.restart_provider_conversation(
                self.config, board, "agent-1", original["id"]
            )
            fresh_key = restarted["web_conversation_key"]
            self.assertNotEqual(fresh_key, old_key)
            self.assertEqual(restarted["id"], original["id"])
            self.assertEqual(restarted["filed_as"], original["filed_as"])

            with bind(reopened_store, fresh_run):
                with provider_effect(
                    self.config, route, fresh_key, "fresh-request-digest"
                ):
                    pass
                with self.assertRaisesRegex(
                    HarnessError, "uncertain prior delivery"
                ):
                    with provider_effect(
                        self.config, route, old_key, "post-rotation-old-digest"
                    ):
                        self.fail("rotation must not erase the old effect fence")
            reopened_store.checkpoint(
                fresh_run, "fresh_provider_acknowledged", {"accepted": True}
            )
            reopened_store.finish(fresh_run, {"accepted": True})
            fresh_projection = reopened_store.projection(fresh_run)
            self.assertEqual(fresh_projection["status"], "complete")
            self.assertIn(
                "acknowledged",
                [one["kind"] for one in fresh_projection["events"]],
            )

            final_store = SwarmRunStore.for_communication(self.config)
            old_after_rotation = final_store.projection(old_run)
            self.assertEqual(old_after_rotation["status"], "delivery_unknown")
            self.assertEqual(old_after_rotation["events"], old_audit)

    def test_one_busy_saved_board_cannot_exhaust_another_boards_chat_capacity(self) -> None:
        first_id = "workspace-77777777777777777777777777777777"
        second = self.board("workspace-88888888888888888888888888888888")
        registry = swarm_chats._empty()
        registry["chats"] = [
            {"workspace_id": first_id, "pair": [f"old-{number}"]}
            for number in range(swarm_chats.MOST_CHATS)
        ]

        made = swarm_chats._new_chat(
            self.config, registry, second, ["agent-1", "agent-2"]
        )

        self.assertEqual(made["workspace_id"], second["workspace_id"])
        self.assertEqual(len(registry["chats"]), swarm_chats.MOST_CHATS + 1)

    def test_import_gets_fresh_identity_and_local_reopen_keeps_it(self) -> None:
        live = self.root / "live-board.json"
        library = self.root / "saved-boards"
        incoming_id = "workspace-66666666666666666666666666666666"
        portable_board = self.board(incoming_id)
        for agent in portable_board["agents"]:
            agent.pop("ready", None)
        document = {
            "format": swarm.SAVED_BOARD_DOCUMENT,
            "name": "Imported",
            "saved_at": "2026-01-01T00:00:00Z",
            "board": portable_board,
        }
        with (
            mock.patch.object(swarm, "where_it_lives", return_value=live),
            mock.patch.object(swarm, "where_the_kept_ones_live", return_value=library),
        ):
            swarm.import_kept_board(document)
            saved_file = next(library.glob("*.json"))
            stored = json.loads(saved_file.read_text(encoding="utf-8"))["board"]
            self.assertNotEqual(stored["workspace_id"], incoming_id)
            first = swarm.open_this_board("Imported", self.config)
            second = swarm.open_this_board("Imported", self.config)

        self.assertEqual(first.workspace_id, stored["workspace_id"])
        self.assertEqual(second.workspace_id, stored["workspace_id"])

    def test_active_board_rejects_future_board_and_binding_schemas(self) -> None:
        live = self.root / "live-board.json"
        for field, version, message in (
            ("schema_version", 2, "newer schema"),
            ("binding_schema_version", 2, "newer chat-binding schema"),
        ):
            with self.subTest(field=field):
                payload = self.board("workspace-99999999999999999999999999999999")
                payload[field] = version
                live.write_text(json.dumps(payload), encoding="utf-8")
                with mock.patch.object(swarm, "where_it_lives", return_value=live):
                    with self.assertRaisesRegex(Exception, message):
                        swarm.load()
                self.assertEqual(
                    json.loads(live.read_text(encoding="utf-8"))[field], version,
                )

    def test_invalid_nonempty_workspace_identity_is_never_treated_as_legacy(self) -> None:
        malformed = self.board("workspace-not-a-valid-identity")
        with self.assertRaisesRegex(Exception, "invalid workspace identity"):
            swarm.read_it(malformed)
        with self.assertRaisesRegex(Exception, "invalid workspace identity"):
            swarm_chats.list_for_agent(self.config, malformed, "agent-1")

    def test_save_as_forks_chat_identity_but_resaving_the_open_board_keeps_it(self) -> None:
        live = self.root / "live-board.json"
        library = self.root / "saved-boards"
        starting = self.board("workspace-99999999999999999999999999999999")

        def saved(name: str) -> dict:
            return json.loads(
                (library / swarm._filed_under(name)).read_text(encoding="utf-8")
            )["board"]

        with (
            mock.patch.object(swarm, "where_it_lives", return_value=live),
            mock.patch.object(swarm, "where_the_kept_ones_live", return_value=library),
        ):
            # A portable/client-supplied identity is not authoritative. The
            # engine owns the live board identity and each Save As fork.
            swarm.save(starting, self.config)
            swarm.keep_this_board("Board A", self.config)
            swarm.keep_this_board("Board B", self.config)
            board_a = saved("Board A")
            board_b = saved("Board B")
            self.assertNotEqual(board_a["workspace_id"], board_b["workspace_id"])

            opened_a = swarm.open_this_board("Board A", self.config)
            chat_a = swarm_chats.list_for_agent(
                self.config, opened_a.to_dict(), "agent-1"
            )["chats"][0]
            swarm.keep_this_board("Board A", self.config)
            self.assertEqual(saved("Board A")["workspace_id"], opened_a.workspace_id)

            # Saving the currently open A under a different name is a fork,
            # even though every visible agent and project id is identical.
            swarm.keep_this_board("Board C", self.config)
            self.assertNotEqual(saved("Board C")["workspace_id"], opened_a.workspace_id)
            swarm.keep_this_board("board a", self.config)
            self.assertNotEqual(saved("board a")["workspace_id"], opened_a.workspace_id)

            opened_b = swarm.open_this_board("Board B", self.config)
            chat_b = swarm_chats.list_for_agent(
                self.config, opened_b.to_dict(), "agent-1"
            )["chats"][0]
            self.assertNotEqual(chat_a["id"], chat_b["id"])
            self.assertNotEqual(chat_a["filed_as"], chat_b["filed_as"])

            reopened_a = swarm.open_this_board("Board A", self.config)
            listed_a = swarm_chats.list_for_agent(
                self.config, reopened_a.to_dict(), "agent-1"
            )
            self.assertEqual(listed_a["active"], chat_a["id"])

    def test_chat_presentation_includes_scoped_collaboration_problem(self) -> None:
        board = self.board("workspace-12121212121212121212121212121212")
        original = swarm_chats.list_for_agent(
            self.config, board, "agent-1",
        )["chats"][0]
        problem = {
            "schema_version": 1,
            "code": "collaboration_record_untrusted",
            "action": "reset_collaboration_record",
        }
        with mock.patch(
            "our_harness.collaboration_ledger.collaboration_problem",
            return_value=problem,
        ) as inspect:
            presented = swarm_chats.resolve(
                self.config, board, "agent-1", original["id"],
            )

        self.assertEqual(presented["collaboration_problem"], problem)
        inspect.assert_any_call(
            self.config,
            original["binding"]["agent_routes"]["agent-1"]["route"],
            original["filed_as"],
        )

    def test_ui_exposes_one_click_fresh_chat_recovery_and_blocks_old_actions(self) -> None:
        project = Path(__file__).resolve().parents[1]
        script = (project / "src/our_harness/ui/app.js").read_text(encoding="utf-8")
        styles = (project / "src/our_harness/ui/styles.css").read_text(encoding="utf-8")

        self.assertIn("Setup changed — transcript protected", script)
        self.assertIn("Start fresh with current setup", script)
        self.assertIn("createConversationFor(agentId", script)
        self.assertIn("conversation?.binding_problem", script)
        self.assertIn("the-big-chat-binding-repair", script)
        self.assertIn(".the-big-chat-binding-repair", styles)


if __name__ == "__main__":
    unittest.main()
