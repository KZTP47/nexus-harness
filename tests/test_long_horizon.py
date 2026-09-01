from __future__ import annotations

import copy
import base64
from contextlib import closing
import json
import multiprocessing
import re
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import long_horizon
from our_harness import server as harness_server
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.providers import base as provider_base


THREAD_COORDINATION_TIMEOUT_SECONDS = 30.0


def _cross_process_goal_admission(
    state_path: str, authority_path: str, project_path: str, request_id: str,
    ready: object, begin: object, results: object,
) -> None:
    """Spawn-safe admission worker used to prove the SQLite owner boundary."""

    try:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "codex": {
                "kind": "openai", "model": "gpt-test",
                "endpoint": "http://127.0.0.1/openai", "api_key_env": "TEST_OPENAI_KEY",
            },
        }
        authority = Path(authority_path)
        project = Path(project_path)
        config = LoadedConfig(data, authority, [], {})
        board = {
            "agents": [{"id": "lead", "name": "Lead", "who": "codex", "ready": True}],
            "projects": [{
                "id": "project", "name": "Project", "path": str(project),
                "is_there": True, "tasks": [],
            }],
            "works_on": [{"agent": "lead", "project": "project"}],
        }
        long_horizon._base = lambda: Path(state_path)
        store = long_horizon.GoalStore(config)
        ready.put(request_id)
        if not begin.wait(THREAD_COORDINATION_TIMEOUT_SECONDS):
            raise RuntimeError("cross-process admission barrier timed out")
        goal = store.create(
            board, "project", ["Exact work " + request_id], request_id,
            conversation_id="chat-" + request_id,
        )
        results.put({
            "ok": True, "request_id": request_id,
            "goal_id": goal["goal_id"], "status": goal["status"],
            "queue": goal["project_queue"],
        })
    except Exception as exc:  # pragma: no cover - returned to parent for assertion
        results.put({"ok": False, "error": repr(exc)})


def _cross_process_runtime_replay(
    state_path: str, authority_path: str, project_path: str,
    ready: object, begin: object, release: object, dispatch_count: object,
    results: object,
) -> None:
    """Spawn-safe runtime replay worker for the durable scheduler CAS."""

    runtime = None
    try:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "codex": {
                "kind": "openai", "model": "gpt-test",
                "endpoint": "http://127.0.0.1/openai", "api_key_env": "TEST_OPENAI_KEY",
            },
        }
        authority = Path(authority_path)
        project = Path(project_path)
        config = LoadedConfig(data, authority, [], {})
        board = {
            "agents": [{"id": "lead", "name": "Lead", "who": "codex", "ready": True}],
            "projects": [{
                "id": "project", "name": "Project", "path": str(project),
                "is_there": True, "tasks": [],
            }],
            "works_on": [{"agent": "lead", "project": "project"}],
        }
        long_horizon._base = lambda: Path(state_path)
        runtime = long_horizon.LongHorizonRuntime(config)

        def provider(*_args, **kwargs):
            before = kwargs.get("before_provider_dispatch")
            after = kwargs.get("after_provider_response")
            if before:
                before("initial")
            with dispatch_count.get_lock():
                dispatch_count.value += 1
            if not release.wait(THREAD_COORDINATION_TIMEOUT_SECONDS):
                raise RuntimeError("provider release barrier timed out")
            if after:
                after("initial")
            return {"text": json.dumps(action(criteria_evidence=[{
                "criterion": "Original objective is satisfied",
                "evidence_refs": ["verified-no-change"],
            }]))}

        ready.put("ready")
        if not begin.wait(THREAD_COORDINATION_TIMEOUT_SECONDS):
            raise RuntimeError("runtime replay barrier timed out")
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider), \
                mock.patch.object(
                    long_horizon.swarm_work, "_run_selected_project_verification",
                    return_value={"status": "passed", "basis": "cross-process scheduler"},
                ):
            goal = runtime.start(
                board, "project", ["Dispatch this exact request once"],
                "same-runtime-request", conversation_id="same-chat",
            )
            deadline = time.monotonic() + THREAD_COORDINATION_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                current = runtime.store.get(goal["goal_id"])
                if current["status"] in {"complete", "failed", "cancelled", "paused"}:
                    break
                time.sleep(0.02)
            results.put({
                "ok": True, "goal_id": goal["goal_id"],
                "status": runtime.store.get(goal["goal_id"])["status"],
            })
    except Exception as exc:  # pragma: no cover - returned to parent for assertion
        results.put({"ok": False, "error": repr(exc)})
    finally:
        if runtime is not None:
            runtime.close()


def action(kind: str = "complete", **updates):
    value = {
        "action": kind,
        "summary": "Concrete work completed",
        "evidence": ["verified-no-change: inspected the requested behavior"],
        "risk": "low",
        "changes": [],
        "needs_files": [],
        "tasks": [],
        "handoff_agent_id": "",
        "questions": [],
        "criteria_evidence": [],
    }
    value.update(updates)
    for change in value.get("changes", []):
        if isinstance(change, dict):
            change.setdefault("reason", "test change")
    return value


class LongHorizonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.authority = self.base / "authority"
        self.project = self.base / "project"
        self.authority.mkdir()
        self.project.mkdir()
        config_data = copy.deepcopy(DEFAULT_CONFIG)
        config_data["providers"] = {
            "codex": {
                "kind": "openai", "model": "gpt-test",
                "endpoint": "http://127.0.0.1/openai", "api_key_env": "TEST_OPENAI_KEY",
            },
            "claude": {
                "kind": "anthropic", "model": "claude-test",
                "endpoint": "http://127.0.0.1/anthropic", "api_key_env": "TEST_ANTHROPIC_KEY",
            },
        }
        self.config = LoadedConfig(config_data, self.authority, [], {})
        self.board = {
            "agents": [
                {"id": "lead", "name": "Lead", "who": "codex", "ready": True},
                {"id": "same-route", "name": "Same route", "who": "codex", "ready": True},
                {"id": "reviewer", "name": "Reviewer", "who": "claude", "ready": True},
            ],
            "projects": [{
                "id": "project", "name": "Project", "path": str(self.project),
                "is_there": True, "tasks": [],
            }],
            "works_on": [
                {"agent": "lead", "project": "project"},
                {"agent": "same-route", "project": "project"},
                {"agent": "reviewer", "project": "project"},
            ],
        }
        self.base_patch = mock.patch.object(long_horizon, "_base", return_value=self.base / "state")
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)

    def store(self):
        return long_horizon.GoalStore(self.config)

    def assert_one_compact_rollover_tombstone(self, store):
        with closing(sqlite3.connect(store.database)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM long_goals "
                "WHERE status IN ('complete','cancelled')"
            ).fetchone()[0], long_horizon.MAX_GOALS)
            rows = db.execute(
                "SELECT * FROM long_goal_request_tombstones"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            raw = str(row["tombstone_json"])
            tombstone = json.loads(raw)
            self.assertEqual(set(tombstone), {
                "request_tombstone_schema_version", "request_tombstone",
                "goal_id", "request_id", "client_request_id", "authority_key",
                "status", "retired_ms", "admission_digest", "project", "conversation_id",
                "requested_agent_ids", "lead_agent_id", "parent_goal_id",
            })
            self.assertEqual(
                tombstone["request_tombstone_schema_version"],
                long_horizon.REQUEST_TOMBSTONE_SCHEMA_VERSION,
            )
            self.assertTrue(tombstone["request_tombstone"])
            self.assertLess(len(raw.encode("utf-8")), 2_048)
            self.assertRegex(str(row["tombstone_sha256"]), r"^[0-9a-f]{64}$")
            self.assertRegex(str(row["integrity_mac"]), r"^[0-9a-f]{64}$")
            self.assertEqual(int(row["retired_ms"]), tombstone["retired_ms"])
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM long_goals WHERE goal_id=?",
                (tombstone["goal_id"],),
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM long_goal_events WHERE goal_id=?",
                (tombstone["goal_id"],),
            ).fetchone()[0], 0)
        return tombstone

    def stage_review(self, store, goal, task, proposed):
        store.record_dispatch(goal["goal_id"], task, "review-proposal")
        proposed.setdefault("_nexus_baselines", {
            str(one.get("path") or "").replace("\\", "/"): "missing"
            for one in proposed.get("changes", [])
        })
        store.record_action(goal["goal_id"], task, proposed)
        staged, interrupts = store.stage_review_if_needed(goal["goal_id"], task, proposed)
        self.assertTrue(staged)
        return interrupts

    def test_one_agent_can_finish_without_peer_or_plan_review_rounds(self):
        self.board["agents"] = [self.board["agents"][0]]
        self.board["works_on"] = [self.board["works_on"][0]]
        completed_action = action(
            changes=[{"path": "done.txt", "content": "done\n", "delete": False, "reason": "fulfil goal"}],
            evidence=["file:done.txt"],
            criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["file:done.txt"],
            }],
        )
        with mock.patch.object(
            long_horizon.chat_lab, "ask_once",
            side_effect=lambda *args, **kwargs: (
                kwargs["before_provider_dispatch"]("initial")
                or {"text": json.dumps(completed_action)}
            ),
        ) as ask, mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification",
            return_value={"status": "passed", "basis": "unit test"},
        ):
            runtime = long_horizon.LongHorizonRuntime(self.config)
            self.addCleanup(runtime.close)
            goal = runtime.store.create(self.board, "project", ["Inspect the repository"], "one-agent")
            completed = runtime.run(goal["goal_id"])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(completed["budget"]["provider_calls"], 1)
        self.assertTrue(all(one["status"] == "passed" for one in completed["verification"]["criteria_results"]))

    def test_request_id_and_goal_access_are_scoped_to_project_authority(self):
        first = self.store().create(self.board, "project", ["First"], "same-request")
        other_authority = self.base / "other-authority"
        other_authority.mkdir()
        other_config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), other_authority, [], {})
        other = long_horizon.GoalStore(other_config)
        second = other.create(self.board, "project", ["Second"], "same-request")
        self.assertNotEqual(first["goal_id"], second["goal_id"])
        with self.assertRaisesRegex(HarnessError, "different Nexus project authority"):
            other.get(first["goal_id"])
        self.assertEqual([one["goal_id"] for one in other.list()], [second["goal_id"]])

    def test_chat_bound_goal_contains_only_the_pair_and_requires_each_contribution(self):
        goal = self.store().create(
            self.board, "project", ["Each agent creates its own requested file"],
            "exact-pair", lead_id="lead",
            participant_ids=["lead", "reviewer"], conversation_id="chat-exact",
        )

        self.assertEqual([one["id"] for one in goal["agents"]], ["lead", "reviewer"])
        self.assertNotIn("same-route", [one["id"] for one in goal["agents"]])
        self.assertEqual(goal["requested_agent_ids"], ["lead", "reviewer"])
        self.assertEqual(goal["conversation_id"], "chat-exact")
        self.assertEqual(
            {one["required_contributor_id"] for one in goal["tasks"]},
            {"lead", "reviewer"},
        )
        self.assertEqual(
            {one["assigned_agent_id"] for one in goal["tasks"]},
            {"lead", "reviewer"},
        )
        self.assertFalse(goal["provider_setup_changed"])
        self.assertTrue(all(
            one.get("route_binding", {}).get("route_fingerprint_sha256")
            for one in goal["agents"]
        ))
        self.assertTrue(all(
            one.get("route_binding", {}).get(
                "effective_dispatch_fingerprint_sha256"
            )
            for one in goal["agents"]
        ))

    def test_explicit_board_goal_every_mode_is_durable_and_intent_idempotent(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ) as start:
            created = runtime.start(
                self.board, "project", ["Produce independently verified output"],
                "composer-request", lead_id="lead",
                success_criteria=["Both vendor contributions are saved"],
                participant_ids=["lead", "reviewer"],
            )
            replayed = runtime.start(
                self.board, "project", ["Produce independently verified output"],
                "composer-request", lead_id="lead",
                success_criteria=["Both vendor contributions are saved"],
                participant_ids=["lead", "reviewer"],
            )
            with self.assertRaisesRegex(HarnessError, "request identity.*different"):
                runtime.start(
                    self.board, "project", ["A changed objective"],
                    "composer-request", lead_id="lead",
                    success_criteria=["Both vendor contributions are saved"],
                    participant_ids=["lead", "reviewer"],
                )

        self.assertEqual(created["goal_id"], replayed["goal_id"])
        self.assertTrue(replayed["reused"])
        self.assertEqual(start.call_count, 2)
        stored = runtime.store.get(created["goal_id"])
        self.assertEqual(stored["requested_agent_ids"], ["lead", "reviewer"])
        self.assertEqual(
            {one["required_contributor_id"] for one in stored["tasks"]},
            {"lead", "reviewer"},
        )

        # A renderer or Electron restart reconstructs the exact admitted team
        # and request identity from product-owned durable state.
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        recovered = restarted.store.get_by_request("composer-request")
        self.assertEqual(recovered["goal_id"], created["goal_id"])
        self.assertEqual(recovered["requested_agent_ids"], ["lead", "reviewer"])

    def test_adaptive_goal_atomically_binds_the_selected_team_without_forcing_fanout(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            created = runtime.start(
                self.board, "project", ["Choose the best next specialist"],
                "adaptive-team", lead_id="lead",
                participant_ids=["lead", "reviewer"],
                require_all_participants=False,
            )
            replayed = runtime.start(
                self.board, "project", ["Choose the best next specialist"],
                "adaptive-team", lead_id="lead",
                participant_ids=["lead", "reviewer"],
                require_all_participants=False,
            )
            with self.assertRaisesRegex(HarnessError, "request identity.*different"):
                runtime.start(
                    self.board, "project", ["Choose the best next specialist"],
                    "adaptive-team", lead_id="lead",
                    participant_ids=["lead", "same-route"],
                    require_all_participants=False,
                )
            with self.assertRaisesRegex(HarnessError, "request identity.*different"):
                runtime.start(
                    self.board, "project", ["Choose the best next specialist"],
                    "adaptive-team", lead_id="lead",
                    participant_ids=["lead", "reviewer"],
                    require_all_participants=True,
                )

        self.assertEqual(created["goal_id"], replayed["goal_id"])
        stored = runtime.store.get(created["goal_id"])
        self.assertEqual([one["id"] for one in stored["agents"]], ["lead", "reviewer"])
        self.assertEqual(stored["requested_agent_ids"], ["lead", "reviewer"])
        self.assertFalse(stored["require_all_participants"])
        self.assertEqual(len(stored["tasks"]), 1)
        self.assertFalse(stored["tasks"][0].get("required_contributor_id"))

    def test_explicit_board_goal_http_route_uses_real_admission_and_rejects_bad_team(self):
        panel = harness_server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(panel.server_close)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        panel._long_horizon = runtime
        thread = threading.Thread(target=panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(panel.shutdown)

        def post(body):
            request = urllib.request.Request(
                f"http://127.0.0.1:{panel.server_address[1]}/api/long-horizon/start-board",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Harness-Token": panel.token},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as answer:
                    return answer.status, json.loads(answer.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        authority = {
            "can_run": True,
            "project_authority_id": long_horizon.project_identity(self.project),
            "project_root": str(self.project),
        }
        goal = {
            "schema_version": 1,
            "project_id": "project",
            "objectives": ["Exercise the real board-goal HTTP admission"],
            "success_criteria": ["Both selected agents contribute"],
            "lead_id": "lead",
            "collaboration_mode": "every",
            "participant_ids": ["lead", "reviewer"],
        }
        with mock.patch.object(
            panel, "swarm_standing", return_value={"board": self.board},
        ), mock.patch.object(
            panel, "require_project_execution_authority", return_value=authority,
        ), mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            status, answer = post({"request_id": "http-composer", "goal": goal})
            replay_status, replay = post({"request_id": "http-composer", "goal": goal})
            bad_status, bad = post({
                "request_id": "http-bad-team",
                "goal": {**goal, "participant_ids": ["not-on-this-board"]},
            })
            changed_status, changed = post({
                "request_id": "http-composer",
                "goal": {**goal, "objectives": ["Different intent must not replay"]},
            })
            runtime.store.control(answer["goals"][0]["goal_id"], "cancel")
            adaptive_status, adaptive = post({
                "request_id": "http-adaptive",
                "goal": {
                    **goal,
                    "objectives": ["Let the selected team route work adaptively"],
                    "collaboration_mode": "adaptive",
                },
            })

        self.assertEqual(status, 202)
        self.assertEqual(answer["engine"], "long_horizon")
        self.assertEqual(len(answer["goals"]), 1)
        self.assertEqual(replay_status, 202)
        self.assertEqual(replay["goals"][0]["goal_id"], answer["goals"][0]["goal_id"])
        self.assertEqual(bad_status, 400)
        self.assertRegex(bad["error"], "Unavailable")
        self.assertEqual(changed_status, 400)
        self.assertRegex(changed["error"], "request identity.*different")
        self.assertEqual(adaptive_status, 202)
        adaptive_goal = runtime.store.get(adaptive["goals"][0]["goal_id"])
        self.assertEqual(adaptive_goal["requested_agent_ids"], ["lead", "reviewer"])
        self.assertFalse(adaptive_goal["require_all_participants"])
        self.assertEqual(len(adaptive_goal["tasks"]), 1)

    def test_path_resolved_executable_drift_blocks_resume_before_dispatch(self):
        first = self.base / "path-a" / "agent-tool"
        second = self.base / "path-b" / "agent-tool"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "local-route": {
                "kind": "local", "model": "local-model",
                "command": ["agent-tool", "--json"],
            },
        }
        config = LoadedConfig(data, self.authority, [], {})
        board = copy.deepcopy(self.board)
        board["agents"] = [board["agents"][0]]
        board["agents"][0]["who"] = "local-route"
        board["works_on"] = [board["works_on"][0]]

        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(first),
        ):
            store = long_horizon.GoalStore(config)
            goal = store.create(
                board, "project", ["Do exact work"], "path-dispatch-drift"
            )
            store.control(goal["goal_id"], "pause")

        runtime = long_horizon.LongHorizonRuntime(config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            provider_base.shutil, "which", return_value=str(second),
        ), mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            shown = runtime.store.get_by_request("path-dispatch-drift")
            self.assertTrue(shown["provider_setup_changed"])
            with self.assertRaisesRegex(HarnessError, "provider setup changed"):
                runtime.control(goal["goal_id"], "resume")
        ask.assert_not_called()
        self.assertEqual(runtime.store.get(goal["goal_id"])["status"], "paused")

    def test_executable_drift_is_rechecked_at_physical_dispatch_boundary(self):
        first = self.base / "dispatch-a" / "agent-tool"
        second = self.base / "dispatch-b" / "agent-tool"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "local-route": {
                "kind": "local", "model": "local-model",
                "command": ["agent-tool", "--json"],
            },
        }
        config = LoadedConfig(data, self.authority, [], {})
        board = copy.deepcopy(self.board)
        board["agents"] = [board["agents"][0]]
        board["agents"][0]["who"] = "local-route"
        board["works_on"] = [board["works_on"][0]]
        selected = {"path": str(first)}

        def resolved(_command):
            return selected["path"]

        with mock.patch.object(provider_base.shutil, "which", side_effect=resolved):
            runtime = long_horizon.LongHorizonRuntime(config)
            self.addCleanup(runtime.close)
            goal = runtime.store.create(
                board, "project", ["Do exact work"], "dispatch-boundary-drift"
            )

            def swap_before_send(*_args, **kwargs):
                selected["path"] = str(second)
                kwargs["before_provider_dispatch"]("initial")
                raise AssertionError("A changed executable must stop before provider send")

            with mock.patch.object(
                long_horizon.chat_lab, "ask_once", side_effect=swap_before_send,
            ) as ask:
                result = runtime.run(goal["goal_id"])

        self.assertEqual(ask.call_count, 1)
        self.assertNotEqual(result["status"], "complete")
        events = runtime.store.events(goal["goal_id"])["events"]
        self.assertFalse(any(one["type"] == "provider_dispatched" for one in events))
        self.assertIn(
            "provider setup changed",
            runtime.store.get(goal["goal_id"])["tasks"][0]["last_error"],
        )

    def test_paused_goal_cannot_silently_resume_after_route_profile_changes(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Do exact work"], "bound-provider")
        store.control(goal["goal_id"], "pause")

        changed_data = copy.deepcopy(DEFAULT_CONFIG)
        changed_data["providers"] = {
            "codex": {"kind": "codex-cli", "model": "a-different-model"},
        }
        changed_config = LoadedConfig(changed_data, self.authority, [], {})
        runtime = long_horizon.LongHorizonRuntime(changed_config)
        self.addCleanup(runtime.close)

        shown = runtime.store.get_by_request("bound-provider")
        self.assertTrue(shown["provider_setup_changed"])
        self.assertEqual(
            shown["provider_setup_status"]["recovery_action"],
            "start_new_goal_with_current_setup",
        )
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            with self.assertRaisesRegex(HarnessError, "provider setup changed"):
                runtime.control(goal["goal_id"], "resume")
        ask.assert_not_called()
        self.assertEqual(runtime.store.get(goal["goal_id"])["status"], "paused")

    def test_provider_drift_blocks_fork_before_creating_a_git_worktree(self):
        goal = self.store().create(
            self.board, "project", ["Do exact work"], "bound-provider-fork",
        )
        changed_data = copy.deepcopy(DEFAULT_CONFIG)
        changed_data["providers"] = {
            "codex": {"kind": "codex-cli", "model": "a-different-model"},
        }
        runtime = long_horizon.LongHorizonRuntime(
            LoadedConfig(changed_data, self.authority, [], {}),
        )
        self.addCleanup(runtime.close)

        with mock.patch.object(
            long_horizon.subprocess, "run",
            side_effect=AssertionError("Git must not run for a stale provider binding"),
        ) as git:
            with self.assertRaisesRegex(HarnessError, "provider setup changed"):
                runtime.fork(goal["goal_id"], "stale-provider-fork")
        git.assert_not_called()
        self.assertFalse((self.base / "state" / "goal-worktrees").exists())

    def test_legacy_goal_without_route_binding_is_visible_but_never_dispatched(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Inspect safely"], "legacy-provider")

        def remove_binding(document, _db):
            for agent in document["agents"]:
                agent.pop("route_binding", None)

        store._mutate(goal["goal_id"], remove_binding)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        shown = runtime.store.get_by_request("legacy-provider")
        self.assertTrue(shown["provider_setup_changed"])
        self.assertIn("predates", shown["provider_setup_status"]["agents"][0]["reason"])
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            with self.assertRaisesRegex(HarnessError, "provider setup changed"):
                runtime.start_background(goal["goal_id"])
        ask.assert_not_called()

    def test_legacy_route_binding_remains_readable_but_cannot_resume_execution(self):
        store = self.store()
        goal = store.create(
            self.board, "project", ["Inspect safely"], "legacy-dispatch-binding"
        )

        def downgrade(document, _db):
            for agent in document["agents"]:
                binding = agent["route_binding"]
                binding["binding_schema_version"] = 1
                for key in (
                    "effective_dispatch_version",
                    "effective_dispatch_fingerprint_sha256",
                    "effective_dispatch_contract",
                ):
                    binding.pop(key, None)
            document["status"] = "paused"

        store._mutate(goal["goal_id"], downgrade)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        shown = runtime.store.get_by_request("legacy-dispatch-binding")
        self.assertTrue(shown["provider_setup_changed"])
        self.assertIn("predates", shown["provider_setup_status"]["agents"][0]["reason"])
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            with self.assertRaisesRegex(HarnessError, "provider setup changed"):
                runtime.control(goal["goal_id"], "resume")
        ask.assert_not_called()

    def test_unready_named_peer_is_rejected_before_target_authority_is_claimed(self):
        self.board["agents"][2]["ready"] = False
        with self.assertRaisesRegex(HarnessError, "Every selected agent"):
            self.store().create(
                self.board, "project", ["Work together"], "unready-pair",
                participant_ids=["lead", "reviewer"], conversation_id="chat-unready",
            )
        self.assertFalse((self.project / ".harness" / "project-authority.json").exists())

    def test_direct_start_reuses_only_the_exact_admitted_intent(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            first = runtime.start(
                self.board, "project", ["Create the pair artifacts"], "same-intent",
                lead_id="lead", participant_ids=["lead", "reviewer"],
                conversation_id="chat-one",
            )
            replay = runtime.start(
                self.board, "project", ["Create the pair artifacts"], "same-intent",
                lead_id="lead", participant_ids=["lead", "reviewer"],
                conversation_id="chat-one",
            )
            self.assertEqual(replay["goal_id"], first["goal_id"])
            with self.assertRaisesRegex(HarnessError, "already bound"):
                runtime.start(
                    self.board, "project", ["Different objective"], "same-intent",
                    lead_id="lead", participant_ids=["lead", "reviewer"],
                    conversation_id="chat-one",
                )
            with self.assertRaisesRegex(HarnessError, "different project, chat"):
                runtime.start(
                    self.board, "project", ["Create the pair artifacts"], "same-intent",
                    lead_id="lead", participant_ids=["lead", "reviewer"],
                    conversation_id="chat-two",
                )
            with self.assertRaisesRegex(HarnessError, "already bound"):
                runtime.start(
                    self.board, "project", ["Create the pair artifacts"], "same-intent",
                    lead_id="lead", participant_ids=["lead", "reviewer"],
                    conversation_id="chat-one",
                    attachments=[{"name": "new.txt", "data": "different"}],
                )

    def test_fork_binds_the_new_project_authority_and_legacy_missing_id_fails_closed(self):
        store = self.store()
        source = store.create(self.board, "project", ["Prepare fork"], "fork-source")
        fork_root = self.base / "isolated-fork"
        fork_root.mkdir()
        forked = store.clone_to_project(
            store.get(source["goal_id"]), "project-fork", "Project fork",
            fork_root, "fork-bound",
        )
        self.assertNotEqual(forked["project_authority_id"], source["project_authority_id"])
        self.assertEqual(
            long_horizon.LongHorizonRuntime._require_goal_authority(forked),
            forked["project_authority_id"],
        )

        store._mutate(source["goal_id"], lambda document, _db: document.pop(
            "project_authority_id", None
        ))
        legacy = store.get(source["goal_id"])
        with self.assertRaisesRegex(HarnessError, "predates target-folder authority"):
            long_horizon.LongHorizonRuntime._require_goal_authority(legacy)

    def test_provider_budget_counts_every_actual_dispatch(self):
        store = self.store()
        goal = store.create(
            self.board, "project", ["Read then work"], "budget",
            policy={"max_provider_calls": 2},
        )
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "first")
        store.record_dispatch(goal["goal_id"], task, "second", phase="requested_files")
        with self.assertRaisesRegex(HarnessError, "budget"):
            store.record_dispatch(goal["goal_id"], task, "third")
        self.assertEqual(store.get(goal["goal_id"])["budget"]["provider_calls"], 2)

    def test_runtime_accounts_for_schema_repair_as_a_second_transport_dispatch(self):
        completed_action = action(
            changes=[{"path": "budgeted.txt", "content": "done\n", "delete": False}],
            criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["file:budgeted.txt"],
            }],
        )
        def repaired_answer(*_args, **kwargs):
            callback = kwargs["before_provider_dispatch"]
            receipt = kwargs["after_provider_response"]
            callback("initial")
            receipt("initial")
            callback("schema_repair")
            receipt("schema_repair")
            return {"text": json.dumps(completed_action)}
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=repaired_answer), mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification",
            return_value={"status": "passed", "basis": "unit test"},
        ):
            goal = runtime.store.create(
                self.board, "project", ["Budget schema repair"], "schema-budget",
                policy={"max_provider_calls": 2},
            )
            completed = runtime.run(goal["goal_id"])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["budget"]["provider_calls"], 2)
        events = runtime.store.events(goal["goal_id"])["events"]
        dispatched = [one for one in events if one["type"] == "provider_dispatched"]
        received = [one for one in events if one["type"] == "provider_reply_received"]
        self.assertEqual(
            [one["payload"]["phase"] for one in dispatched],
            ["initial", "initial_schema_repair"],
        )
        self.assertEqual(
            [one["payload"]["phase"] for one in received],
            ["initial", "initial_schema_repair"],
        )
        self.assertEqual(
            [one["payload"]["effect_id"] for one in received],
            [one["payload"]["effect_id"] for one in dispatched],
        )

    def test_dispatch_and_ack_are_durable_and_ordered_before_apply(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Ordered effect"], "ordered")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "digest")
        paused_action = action(
            changes=[{"path": "pause.txt", "content": "done\n", "delete": False, "reason": "fulfil goal"}],
            criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["file:pause.txt"],
            }],
        )
        paused_action["_nexus_baselines"] = {"pause.txt": "missing"}
        store.record_action(goal["goal_id"], task, paused_action)
        events = store.events(goal["goal_id"])["events"]
        kinds = [one["type"] for one in events]
        self.assertLess(kinds.index("provider_dispatched"), kinds.index("provider_acknowledged"))
        dispatched = next(one for one in events if one["type"] == "provider_dispatched")
        acknowledged = next(one for one in events if one["type"] == "provider_acknowledged")
        self.assertEqual(dispatched["payload"]["effect_id"], acknowledged["payload"]["effect_id"])
        self.assertEqual(dispatched["run_id"], goal["goal_id"])

    def test_uncertain_provider_effect_requires_explicit_reconciliation(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Do once"], "uncertain")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "digest")
        store.fail_task(goal["goal_id"], task, "connection dropped", uncertain=True)
        with self.assertRaisesRegex(HarnessError, "Reconcile or supersede"):
            store.control(goal["goal_id"], "resume")
        held = store.get(goal["goal_id"])["tasks"][0]
        self.assertTrue(held["outcome_unknown"])
        self.assertEqual(held["state"], "blocked")
        with self.assertRaisesRegex(HarnessError, "Reconcile"):
            store.control(goal["goal_id"], "retry", {"task_id": held["id"]})
        retried = store.control(goal["goal_id"], "retry", {"task_id": held["id"], "reconciled": True})
        self.assertEqual(retried["tasks"][0]["state"], "ready")

    def test_known_provider_failure_reassigns_to_a_distinct_healthy_agent(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Keep making progress"], "known-failover")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "digest")

        store.fail_task(
            goal["goal_id"], task, "Codex returned a known provider error",
            allow_failover=True,
        )

        held = store.get(goal["goal_id"])
        reassigned = held["tasks"][0]
        self.assertEqual(held["status"], "queued")
        self.assertEqual(reassigned["state"], "ready")
        self.assertEqual(reassigned["assigned_agent_id"], "reviewer")
        self.assertEqual(reassigned["provider_effect_state"], "known_failure_reassigned")
        self.assertEqual(reassigned["failed_agent_ids"], ["lead"])
        self.assertIn(
            "task_reassigned_after_provider_failure",
            [one["type"] for one in store.events(goal["goal_id"])["events"]],
        )

    def test_review_and_failover_use_effective_provider_identity_not_route_alias(self):
        board = copy.deepcopy(self.board)
        board["agents"] = [
            {"id": "author", "name": "Author", "who": "alias-a", "ready": True},
            {"id": "alias", "name": "Alias", "who": "alias-b", "ready": True},
            {"id": "independent", "name": "Independent", "who": "provider-c", "ready": True},
        ]
        board["works_on"] = [
            {"agent": one["id"], "project": "project"} for one in board["agents"]
        ]

        def context(_config, route):
            effective = "a" * 64 if route in {"alias-a", "alias-b"} else "c" * 64
            return "fixture", {
                "failure_context_version": 1,
                "route_fingerprint_sha256": long_horizon.hashlib.sha256(
                    route.encode("utf-8")
                ).hexdigest(),
                "transport_contract": "fixture/route/v1",
                "effective_dispatch_version": 1,
                "effective_dispatch_fingerprint_sha256": effective,
                "effective_dispatch_contract": "fixture/effective/v1",
                "provider_principal_version": 1,
                "provider_principal_fingerprint_sha256": effective,
                "provider_principal_contract": "nexus/provider-principal/v1",
            }

        store = self.store()
        with mock.patch.object(long_horizon.chat_lab, "_route_failure_context", side_effect=context):
            review_goal = store.create(
                board, "project", ["Propose risky work"], "effective-review",
            )

        claimed = store.claim_ready(review_goal["goal_id"], "review-worker")[0]
        self.stage_review(store, review_goal, claimed, action("request_review", risk="high"))
        reviewed = store.get(review_goal["goal_id"])
        review = next(one for one in reviewed["tasks"] if one.get("kind") == "review")
        self.assertEqual(review["assigned_agent_id"], "independent")
        public_agents = {
            one["id"]: one for one in store.public(reviewed)["agents"]
        }
        self.assertEqual(
            public_agents["author"]["provider_identity_sha256"],
            public_agents["alias"]["provider_identity_sha256"],
        )
        self.assertNotEqual(
            public_agents["author"]["provider_identity_sha256"],
            public_agents["independent"]["provider_identity_sha256"],
        )
        with self.assertRaisesRegex(HarnessError, "different provider identity"):
            store.control(review_goal["goal_id"], "reassign", {
                "task_id": review["id"], "agent_id": "alias",
            })
        store.control(review_goal["goal_id"], "cancel")

        with mock.patch.object(long_horizon.chat_lab, "_route_failure_context", side_effect=context):
            manual_goal = store.create(
                board, "project", ["Request a manual review"], "effective-manual-review",
            )
        with self.assertRaisesRegex(HarnessError, "different effective provider identity"):
            store.control(manual_goal["goal_id"], "request_review", {
                "task_id": manual_goal["tasks"][0]["id"], "agent_id": "alias",
            })
        store.control(manual_goal["goal_id"], "cancel")

        with mock.patch.object(long_horizon.chat_lab, "_route_failure_context", side_effect=context):
            failover_goal = store.create(
                board, "project", ["Recover from a known failure"], "effective-failover",
            )
        first = store.claim_ready(failover_goal["goal_id"], "failure-worker")[0]
        store.record_dispatch(failover_goal["goal_id"], first, "known-failure")
        store.fail_task(
            failover_goal["goal_id"], first, "known provider failure",
            allow_failover=True,
        )
        failed = store.get(failover_goal["goal_id"])["tasks"][0]
        self.assertEqual(failed["assigned_agent_id"], "independent")
        self.assertEqual(len(failed["failed_provider_identities"]), 1)
        reopened = long_horizon.GoalStore(self.config).get(failover_goal["goal_id"])
        self.assertEqual(reopened["tasks"][0]["assigned_agent_id"], "independent")
        self.assertEqual(
            reopened["tasks"][0]["failed_provider_identities"],
            failed["failed_provider_identities"],
        )
        self.assertFalse(long_horizon._providers_independent(
            reviewed["agents"][0], {"route_binding": {}},
        ))

    def test_runtime_fails_over_known_provider_error_before_dispatch(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        calls: list[str] = []

        def answer(_config, route, _text, **kwargs):
            calls.append(route)
            if route == "codex":
                raise HarnessError("codex route is unavailable before dispatch")
            kwargs["before_provider_dispatch"]("initial")
            return {"text": json.dumps(action("complete"))}

        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=answer), \
                mock.patch.object(long_horizon.swarm_work, "_run_selected_project_verification"):
            goal = runtime.store.create(
                self.board, "project", ["Fail over before send"], "pre-dispatch-failover"
            )
            first = runtime.store.claim_ready(goal["goal_id"], "worker-a")[0]
            _task, failed = runtime._execute_one(goal["goal_id"], first["id"])
            second = runtime.store.claim_ready(goal["goal_id"], "worker-b")[0]
            _task, completed = runtime._execute_one(goal["goal_id"], second["id"])

        self.assertEqual(failed["action"], "failed")
        self.assertEqual(completed["action"], "complete")
        self.assertEqual(calls, ["codex", "claude"])
        self.assertIn(
            "task_reassigned_after_provider_failure",
            [one["type"] for one in runtime.store.events(goal["goal_id"])["events"]],
        )

    def test_pause_at_dispatch_admission_is_not_revived_or_misclassified(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(
            self.board, "project", ["Respect pause before send"], "pause-before-send"
        )
        claimed = runtime.store.claim_ready(goal["goal_id"], "worker")[0]

        def paused_before_send(_config, _route, _text, **kwargs):
            runtime.store.control(goal["goal_id"], "pause")
            kwargs["before_provider_dispatch"]("initial")
            raise AssertionError("dispatch admission should have raised")

        with mock.patch.object(
            long_horizon.chat_lab, "ask_once", side_effect=paused_before_send
        ):
            _task, result = runtime._execute_one(goal["goal_id"], claimed["id"])

        held = runtime.store.get(goal["goal_id"])
        self.assertEqual(result["action"], "deferred")
        self.assertEqual(held["status"], "paused")
        self.assertEqual(held["tasks"][0]["state"], "ready")
        self.assertEqual(held["tasks"][0]["assigned_agent_id"], "lead")
        self.assertNotIn(
            "task_reassigned_after_provider_failure",
            [one["type"] for one in runtime.store.events(goal["goal_id"])["events"]],
        )

    def test_pause_during_known_provider_failure_keeps_goal_paused(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(
            self.board, "project", ["Respect in-flight pause"], "pause-in-flight"
        )
        claimed = runtime.store.claim_ready(goal["goal_id"], "worker")[0]

        def known_failure(_config, _route, _text, **kwargs):
            kwargs["before_provider_dispatch"]("initial")
            runtime.store.control(goal["goal_id"], "pause")
            raise HarnessError("provider returned a known failure")

        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=known_failure):
            _task, result = runtime._execute_one(goal["goal_id"], claimed["id"])

        held = runtime.store.get(goal["goal_id"])
        self.assertEqual(result["action"], "failed")
        self.assertEqual(held["status"], "paused")
        self.assertEqual(held["tasks"][0]["state"], "ready")
        self.assertEqual(held["tasks"][0]["assigned_agent_id"], "reviewer")

    def test_pause_before_schema_repair_preserves_received_reply_boundary(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(
            self.board, "project", ["Do not replay malformed reply"], "pause-before-repair"
        )
        claimed = runtime.store.claim_ready(goal["goal_id"], "worker")[0]

        def pause_after_reply(_config, _route, _text, **kwargs):
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            runtime.store.control(goal["goal_id"], "pause")
            kwargs["before_provider_dispatch"]("schema_repair")
            raise AssertionError("schema repair dispatch admission should have raised")

        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=pause_after_reply):
            _task, result = runtime._execute_one(goal["goal_id"], claimed["id"])

        held = runtime.store.get(goal["goal_id"])
        self.assertEqual(result["action"], "deferred")
        self.assertEqual(held["status"], "paused")
        self.assertEqual(held["tasks"][0]["state"], "blocked")
        self.assertTrue(held["tasks"][0]["reconciliation_required"])
        self.assertEqual(
            held["tasks"][0]["provider_effect_state"],
            "reply_received_reconciliation_required",
        )

    def test_failover_never_cycles_back_to_an_already_failed_provider_route(self):
        board = copy.deepcopy(self.board)
        board["agents"].append({
            "id": "lead-alias", "name": "Lead alias", "who": "codex",
            "job": "backup", "ready": True,
        })
        store = self.store()
        goal = store.create(board, "project", ["Do not replay failed routes"], "route-history")

        first = store.claim_ready(goal["goal_id"], "worker-a")[0]
        store.record_dispatch(goal["goal_id"], first, "digest-a")
        store.fail_task(goal["goal_id"], first, "codex known failure", allow_failover=True)

        second = store.claim_ready(goal["goal_id"], "worker-b")[0]
        self.assertEqual(second["assigned_agent_id"], "reviewer")
        store.record_dispatch(goal["goal_id"], second, "digest-b")
        store.fail_task(goal["goal_id"], second, "claude known failure", allow_failover=True)

        held = store.get(goal["goal_id"])
        self.assertEqual(held["status"], "paused")
        self.assertEqual(held["tasks"][0]["state"], "blocked")
        self.assertEqual(set(held["tasks"][0]["failed_provider_routes"]), {"codex", "claude"})
        self.assertNotEqual(held["tasks"][0]["assigned_agent_id"], "lead-alias")

    def test_pause_defers_acknowledged_result_without_second_provider_call(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Pause safely"], "pause")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "digest")
        paused_action = action(
            changes=[{"path": "pause.txt", "content": "done\n", "delete": False, "reason": "fulfil goal"}],
            criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["file:pause.txt"],
            }],
        )
        paused_action["_nexus_baselines"] = {"pause.txt": "missing"}
        store.record_action(goal["goal_id"], task, paused_action)
        store.control(goal["goal_id"], "pause")
        store.defer_pending_action(goal["goal_id"], task)
        paused = store.get(goal["goal_id"])
        self.assertEqual(paused["tasks"][0]["state"], "pending_apply")
        store.control(goal["goal_id"], "resume")
        with mock.patch.object(
            long_horizon.chat_lab, "ask_once", side_effect=AssertionError("provider was resent")
        ), mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification",
            return_value={"status": "passed", "basis": "resume test"},
        ):
            runtime = long_horizon.LongHorizonRuntime(self.config)
            self.addCleanup(runtime.close)
            finished = runtime.run(goal["goal_id"])
        self.assertEqual(finished["status"], "complete")

    def test_large_acknowledged_action_survives_restart_and_applies_exact_content(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Write a large generated file"], "large-pending")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        content = "large-content-line\n" * 14_000
        pending = action(
            changes=[{"path": "large.txt", "content": content, "delete": False, "reason": "goal"}],
            criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["file:large.txt"],
            }],
        )
        pending["_nexus_baselines"] = {"large.txt": "missing"}
        self.assertGreater(len(json.dumps(pending)), 200_000)
        store.record_dispatch(goal["goal_id"], task, "digest")
        self.assertTrue(store.record_action(goal["goal_id"], task, pending))
        store.control(goal["goal_id"], "pause")
        store.defer_pending_action(goal["goal_id"], task)

        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        runtime.store.control(goal["goal_id"], "resume")
        with mock.patch.object(
            long_horizon.chat_lab, "ask_once", side_effect=AssertionError("acknowledged action was resent"),
        ), mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification",
            return_value={"status": "passed", "basis": "large restart test"},
        ):
            completed = runtime.run(goal["goal_id"])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual((self.project / "large.txt").read_text(encoding="utf-8"), content)

    def test_delegation_rejects_parent_cycle(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Parent"], "cycle")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        delegated = action("delegate", tasks=[{
            "title": "Child", "description": "Bounded child",
            "assigned_agent_id": "reviewer", "depends_on": [task["id"]],
            "parallel_safe": True, "resource_paths": ["child.txt"],
        }])
        with self.assertRaisesRegex(HarnessError, "cannot depend on the parent"):
            store.apply_action(goal["goal_id"], task, delegated)

    def test_dynamic_delegation_claims_only_independent_resources_and_providers(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Parent"], "parallel-delegation")
        parent = store.claim_ready(goal["goal_id"], "worker")[0]
        delegated = action("delegate", tasks=[
            {"title": "Code", "description": "Change source", "assigned_agent_id": "lead",
             "depends_on": [], "parallel_safe": True, "resource_paths": ["src/"]},
            {"title": "Tests", "description": "Change tests", "assigned_agent_id": "reviewer",
             "depends_on": [], "parallel_safe": True, "resource_paths": ["tests/"]},
            {"title": "Docs", "description": "Change docs", "assigned_agent_id": "same-route",
             "depends_on": [], "parallel_safe": True, "resource_paths": ["docs/"]},
        ])
        store.apply_action(goal["goal_id"], parent, delegated)
        claimed = store.claim_ready(goal["goal_id"], "parallel-worker")
        self.assertEqual({one["title"] for one in claimed}, {"Code", "Tests"})
        self.assertEqual(len({next(agent["who"] for agent in self.board["agents"]
                                   if agent["id"] == one["assigned_agent_id"]) for one in claimed}), 2)

    def test_handoff_rituals_stop_without_exhausting_the_global_budget(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Do useful work"], "handoff-loop")
        first = store.claim_ready(goal["goal_id"], "worker")[0]
        with self.assertRaisesRegex(HarnessError, "same agent"):
            store.apply_action(goal["goal_id"], first, action(
                "handoff", handoff_agent_id=first["assigned_agent_id"],
            ))
        # Restore the rolled-back running lease through an alternating handoff loop.
        for turn in range(long_horizon.MAX_NO_PROGRESS + 2):
            current = store.get(goal["goal_id"])["tasks"][0]
            if current["state"] == "running":
                task = current
            elif current["state"] == "blocked":
                break
            else:
                task = store.claim_ready(goal["goal_id"], f"worker-{turn}")[0]
            target = "reviewer" if task["assigned_agent_id"] == "lead" else "lead"
            store.apply_action(goal["goal_id"], task, action("handoff", handoff_agent_id=target))
        stopped = store.get(goal["goal_id"])["tasks"][0]
        self.assertEqual(stopped["state"], "blocked")
        self.assertLessEqual(stopped["attempts"], long_horizon.MAX_NO_PROGRESS + 2)

    def test_review_task_cannot_create_review_of_review_chain(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Review once"], "review-chain")
        parent = store.claim_ready(goal["goal_id"], "worker")[0]
        self.stage_review(store, goal, parent, action("request_review"))
        review = store.claim_ready(goal["goal_id"], "review-worker")[0]
        with self.assertRaisesRegex(HarnessError, "review"):
            store.apply_action(goal["goal_id"], review, action("request_review"))

    def test_repeated_identical_user_question_stops_as_no_progress(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Use the answer"], "question-loop")
        question_action = action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "same", "prompt": "Which option?", "multiple": False,
                "allow_other": True, "options": [
                    {"label": "A", "description": "Use A", "recommended": True},
                    {"label": "B", "description": "Use B", "recommended": False},
                ],
            }],
        )
        for turn in range(long_horizon.MAX_NO_PROGRESS + 2):
            task = store.claim_ready(goal["goal_id"], f"question-worker-{turn}")[0]
            interrupt_ids = store.apply_action(goal["goal_id"], task, question_action)
            current = store.get(goal["goal_id"])
            if current["tasks"][0]["state"] == "blocked":
                break
            self.assertEqual(len(interrupt_ids), 1)
            store.resolve_interrupts(goal["goal_id"], {
                "answers": {interrupt_ids[0]: "A"},
                "expected_revision": current["revision"],
                "pending_ids": interrupt_ids,
            })
        stopped = store.get(goal["goal_id"])
        self.assertEqual(stopped["status"], "paused")
        self.assertEqual(stopped["tasks"][0]["state"], "blocked")
        self.assertFalse(any(one["state"] == "pending" for one in stopped["interrupts"]))

    def test_parallel_apply_stops_at_first_human_decision_boundary(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(
            self.board, "project", ["Clarify requirement", "Write after decision"],
            "parallel-human-boundary",
        )
        def make_parallel(document, _db):
            document["tasks"][0]["resource_paths"] = ["choice.txt"]
            document["tasks"][1]["resource_paths"] = ["after.txt"]
            document["tasks"][1]["assigned_agent_id"] = "reviewer"
            reviewer = next(one for one in document["agents"] if one["id"] == "reviewer")
            reviewer["route_binding"]["effective_dispatch_fingerprint_sha256"] = "c" * 64
            reviewer["route_binding"]["provider_principal_fingerprint_sha256"] = "c" * 64
        runtime.store._mutate(goal["goal_id"], make_parallel)
        first, second = runtime.store.claim_ready(goal["goal_id"], "parallel-worker")
        question = action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "choice", "prompt": "Which behavior is intended?",
                "multiple": False, "allow_other": True, "options": [
                    {"label": "A", "description": "Use A", "recommended": True},
                    {"label": "B", "description": "Use B", "recommended": False},
                ],
            }],
        )
        write = action(changes=[{
            "path": "after.txt", "content": "must wait\n", "delete": False,
            "reason": "apply only after the decision",
        }])
        write["_nexus_baselines"] = {"after.txt": "missing"}
        for task, proposed in ((first, question), (second, write)):
            runtime.store.record_dispatch(goal["goal_id"], task, task["id"])
            runtime.store.record_action(goal["goal_id"], task, proposed)
        routed = runtime._apply_node({
            "goal_id": goal["goal_id"],
            "actions": [
                {"task": first, "action": question},
                {"task": second, "action": write},
            ],
        })
        held = runtime.store.get(goal["goal_id"])
        deferred = next(one for one in held["tasks"] if one["id"] == second["id"])
        self.assertEqual(routed["route"], "human")
        self.assertEqual(held["status"], "waiting_for_user")
        self.assertEqual(deferred["state"], "pending_apply")
        self.assertTrue(deferred["pending_action"])
        self.assertFalse((self.project / "after.txt").exists())

    def test_one_agent_risk_decision_uses_the_exact_ui_selected_option(self):
        self.board["agents"] = [self.board["agents"][0]]
        self.board["works_on"] = [self.board["works_on"][0]]
        store = self.store()
        goal = store.create(self.board, "project", ["Work with one agent"], "review-question-stop")
        task = store.claim_ready(goal["goal_id"], "review-question-worker")[0]
        interrupt_ids = self.stage_review(store, goal, task, action("request_review"))
        current = store.get(goal["goal_id"])
        prompt = current["interrupts"][-1]["questions"][0]["prompt"]
        store.resolve_interrupts(goal["goal_id"], {
            "answers": {interrupt_ids[0]: f"{prompt}: Stop this task"},
            "expected_revision": current["revision"], "pending_ids": interrupt_ids,
        })
        stopped = store.get(goal["goal_id"])
        self.assertEqual(stopped["status"], "paused")
        self.assertEqual(stopped["tasks"][0]["state"], "blocked")
        self.assertFalse(stopped["tasks"][0]["pending_action"])

    def test_rejected_independent_review_blocks_parent_instead_of_stranding_it(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Risky work"], "review-reject")
        parent = store.claim_ready(goal["goal_id"], "worker")[0]
        self.stage_review(store, goal, parent, action("request_review", risk="high"))
        review = store.claim_ready(goal["goal_id"], "reviewer-worker")[0]
        packet_ref = "review-packet:" + review["review_packet_sha256"]
        store.apply_action(goal["goal_id"], review, action(
            "blocked", summary="Security regression found",
            evidence=[packet_ref], review_verdict="reject",
            review_findings=["The proposal introduces a security regression."],
        ))
        updated = store.get(goal["goal_id"])
        held_parent = next(one for one in updated["tasks"] if one["id"] == parent["id"])
        self.assertEqual(held_parent["state"], "blocked")
        self.assertIn("Security regression", held_parent["last_error"])

    def test_delete_transaction_requires_review_even_when_agent_marks_low_risk(self):
        (self.project / "obsolete.txt").write_text("keep me\n", encoding="utf-8")
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Remove obsolete file"], "delete-review")
        task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "obsolete.txt", "content": "", "delete": True, "reason": "obsolete",
        }])
        proposed["_nexus_baselines"] = {
            "obsolete.txt": long_horizon._path_baseline_marker(self.project, "obsolete.txt")
        }
        runtime.store.record_dispatch(goal["goal_id"], task, "delete")
        runtime.store.record_action(goal["goal_id"], task, proposed)
        runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": task, "action": proposed}]})
        updated = runtime.store.get(goal["goal_id"])
        parent = next(one for one in updated["tasks"] if one["id"] == task["id"])
        self.assertEqual(parent["state"], "waiting_review")
        self.assertEqual((self.project / "obsolete.txt").read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue(any(one["kind"] == "review" and one["review_of"] == task["id"]
                            for one in updated["tasks"]))

    def test_steering_changes_active_prompt_but_preserves_original(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Original objective"], "steer")
        steered = store.control(goal["goal_id"], "steer", {"text": "Do not alter public APIs"})
        self.assertEqual(steered["original_objective"], "Original objective")
        self.assertIn("Do not alter public APIs", steered["objective"])
        self.assertEqual(steered["objective_revisions"][-1]["reason"], "steer")

    def test_steering_discards_inflight_result_before_file_apply(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Write old result"], "steer-race")
        task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        dispatched = threading.Event()
        release = threading.Event()
        stale = action(
            changes=[{"path": "stale.txt", "content": "must not apply\n", "delete": False}],
        )
        def delayed_answer(*_args, **kwargs):
            kwargs["before_provider_dispatch"]("initial")
            dispatched.set()
            self.assertTrue(release.wait(5))
            return {"text": json.dumps(stale)}
        result = {}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=delayed_answer):
            worker = threading.Thread(
                target=lambda: result.update({"item": runtime._execute_one(goal["goal_id"], task["id"])}),
            )
            worker.start()
            self.assertTrue(dispatched.wait(5))
            runtime.store.control(goal["goal_id"], "steer", {"text": "Do not create stale.txt"})
            release.set()
            worker.join(5)
        returned_task, returned_action = result["item"]
        self.assertEqual(returned_action["action"], "superseded")
        runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{
            "task": returned_task, "action": returned_action,
        }]})
        self.assertFalse((self.project / "stale.txt").exists())
        self.assertEqual(runtime.store.get(goal["goal_id"])["tasks"][0]["state"], "ready")

    def test_steering_after_acknowledgement_supersedes_before_file_apply(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Write stale file"], "steer-after-ack")
        task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        stale = action(changes=[{
            "path": "stale-after-ack.txt", "content": "must not apply\n", "delete": False,
        }])
        runtime.store.record_dispatch(goal["goal_id"], task, "digest")
        runtime.store.record_action(goal["goal_id"], task, stale)
        with mock.patch.object(runtime, "start_background", side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id)):
            runtime.control(goal["goal_id"], "steer", {"text": "Do not create stale-after-ack.txt"})
        runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": task, "action": stale}]})
        self.assertFalse((self.project / "stale-after-ack.txt").exists())
        held = runtime.store.get(goal["goal_id"])["tasks"][0]
        self.assertEqual(held["state"], "ready")
        self.assertEqual(held["provider_effect_state"], "superseded_by_steering")

    def test_reassign_rejects_acknowledged_pending_apply_provenance(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Pending work"], "reassign-pending")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "digest")
        store.record_action(goal["goal_id"], task, action())
        store.control(goal["goal_id"], "pause")
        store.defer_pending_action(goal["goal_id"], task)
        with self.assertRaisesRegex(HarnessError, "acknowledged or in-flight"):
            store.control(goal["goal_id"], "reassign", {
                "task_id": task["id"], "agent_id": "reviewer",
            })

    def test_custom_success_criterion_requires_matching_authenticated_reference(self):
        store = self.store()
        goal = store.create(
            self.board, "project", ["Implement API"], "criteria",
            success_criteria=["API returns 200"],
        )
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        artifact = {"kind": "file_transaction", "transaction_id": "tx-1",
                    "changes": [{"path": "api.py"}], "patch_sha256": "a" * 64}
        completed_action = action(criteria_evidence=[{
            "criterion": "Original objective is satisfied", "evidence_refs": ["artifact:tx-1"],
        }, {
            "criterion": "API returns 200", "evidence_refs": ["artifact:tx-1"],
        }])
        store.apply_action(goal["goal_id"], task, completed_action, artifact=artifact)
        completed = store.complete_verification(goal["goal_id"], {"status": "passed", "basis": "tests"})
        self.assertEqual(completed["status"], "complete")
        criterion = next(one for one in completed["verification"]["criteria_results"]
                         if one["criterion"] == "API returns 200")
        self.assertEqual(criterion["evidence_refs"], ["artifact:tx-1"])

    def test_large_verification_preserves_structured_pass_evidence_after_reopen(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Persist noisy verification"], "large-verification")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.apply_action(goal["goal_id"], task, action(criteria_evidence=[{
            "criterion": "Original objective is satisfied", "evidence_refs": ["artifact:noisy-tx"],
        }]), artifact={
            "kind": "file_transaction", "transaction_id": "noisy-tx",
            "changes": [{"path": "verified.py", "delete": False}], "patch_sha256": "e" * 64,
        })
        noisy = {
            "status": "passed", "basis": "selected_project",
            "commands": [{
                "argv": ["python", "-m", "unittest"], "returncode": 0,
                "stdout": "passed output\n" * 30_000, "stderr": "",
            }],
            "reason": "All deterministic commands passed.",
        }
        completed = store.complete_verification(goal["goal_id"], noisy)
        self.assertEqual(completed["status"], "complete")
        reopened = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        verification = reopened["verification"]
        self.assertEqual(verification["status"], "passed")
        self.assertEqual(verification["basis"], "selected_project")
        self.assertEqual(verification["commands"][0]["returncode"], 0)
        self.assertIn("[truncated", verification["commands"][0]["stdout"])
        self.assertTrue(all(one["status"] == "passed" for one in verification["criteria_results"]))

    def test_unchanged_green_repository_does_not_prove_original_objective(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Implement a missing feature"], "false-positive")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "digest")
        store.record_action(goal["goal_id"], task, action())
        merkle, manifest = long_horizon.swarm_work._project_tree_merkle(self.project)
        store.apply_action(goal["goal_id"], task, action(), artifact={
            "kind": "verified_no_change", "tree_merkle": merkle, "file_count": len(manifest),
        })
        checked = store.complete_verification(goal["goal_id"], {"status": "passed", "basis": "green repo"})
        self.assertEqual(checked["status"], "queued")
        original = next(one for one in checked["verification"]["criteria_results"]
                        if one["criterion"] == "Original objective is satisfied")
        self.assertEqual(original["status"], "failed")
        self.assertTrue(any(one["kind"] == "repair" for one in checked["tasks"]))

    def test_explicit_no_change_criterion_is_bound_to_authenticated_snapshot(self):
        store = self.store()
        goal = store.create(
            self.board, "project", ["Audit the existing behavior"], "explicit-no-change",
            success_criteria=["The read-only audit is complete"],
        )
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        merkle, manifest = long_horizon.swarm_work._project_tree_merkle(self.project)
        store.apply_action(goal["goal_id"], task, action(criteria_evidence=[{
            "criterion": "Original objective is satisfied",
            "evidence_refs": ["verified-no-change: provider declared read-only completion"],
        }, {
            "criterion": "The read-only audit is complete",
            "evidence_refs": ["verified-no-change"],
        }]), artifact={
            "kind": "verified_no_change", "tree_merkle": merkle,
            "file_count": len(manifest),
        })
        completed = store.complete_verification(
            goal["goal_id"], {"status": "passed", "basis": "read-only audit"}
        )
        self.assertEqual(completed["status"], "complete")
        snapshot_ref = "snapshot:" + merkle
        for criterion in completed["verification"]["criteria_results"]:
            if criterion["criterion"] in {
                "Original objective is satisfied", "The read-only audit is complete",
            }:
                self.assertEqual(criterion["evidence_refs"], [snapshot_ref])

    def test_retry_clears_and_publishes_reconciled_applied_transaction(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Recover applied work"], "retry-applied")
        artifact = {
            "kind": "file_transaction", "transaction_id": "1234567890-retryapply",
            "changes": [{"path": "done.txt", "delete": False}],
            "patch": "patch", "patch_sha256": "f" * 64,
        }
        def crash_boundary(document, _db):
            task = document["tasks"][0]
            task.update({
                "state": "blocked", "provider_effect_state": "acknowledged",
                "reconciliation_required": True, "pending_action": action(),
                "pending_transaction": {
                    "state": "applied", "artifact": artifact,
                    "transaction_id": artifact["transaction_id"],
                },
            })
            document["status"] = "paused"
        store._mutate(goal["goal_id"], crash_boundary)
        retried = store.control(goal["goal_id"], "retry", {
            "task_id": goal["tasks"][0]["id"], "reconciled": True,
        })
        held = retried["tasks"][0]
        self.assertEqual(held["state"], "ready")
        self.assertFalse(held["pending_action"])
        self.assertFalse(held["pending_transaction"])
        self.assertFalse(long_horizon._task_has_unsettled_effect(held))
        self.assertTrue(any(
            one.get("transaction_id") == artifact["transaction_id"]
            for one in retried["artifacts"]
        ))

    def test_restart_before_file_manifest_resumes_acknowledged_action_without_resend(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Write file"], "pre-manifest-crash")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        pending_action = action(
            changes=[{"path": "new.txt", "content": "new\n", "delete": False, "reason": "goal"}],
            criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["file:new.txt"],
            }],
        )
        store.record_dispatch(goal["goal_id"], task, "digest")
        store.record_action(goal["goal_id"], task, pending_action)
        store.prepare_transaction(goal["goal_id"], task, "1234567890-deadbeef00", pending_action["changes"])
        def make_dead(document, _db):
            document["worker"] = {"pid": 99999999, "token": "dead", "worker_id": "dead"}
        store._mutate(goal["goal_id"], make_dead)
        recovered = store.recover_dead(goal["goal_id"])
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(recovered["tasks"][0]["state"], "pending_apply")
        self.assertEqual(recovered["tasks"][0]["pending_transaction"], {})

    def test_restart_after_provider_reply_requires_reconciliation_before_resend(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Parse received reply"], "reply-crash")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.record_dispatch(goal["goal_id"], task, "digest")
        store.record_provider_reply(goal["goal_id"], task, phase="initial")

        def make_dead(document, _db):
            document["worker"] = {"pid": 99999999, "token": "dead", "worker_id": "dead"}

        store._mutate(goal["goal_id"], make_dead)
        recovered = store.recover_dead(goal["goal_id"])
        held = recovered["tasks"][0]
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(held["state"], "blocked")
        self.assertTrue(held["reconciliation_required"])
        self.assertEqual(
            held["provider_effect_state"], "reply_received_reconciliation_required"
        )
        with self.assertRaisesRegex(HarnessError, "Reconcile"):
            store.control(goal["goal_id"], "retry", {"task_id": held["id"]})
        retried = store.control(
            goal["goal_id"], "retry", {"task_id": held["id"], "reconciled": True}
        )
        self.assertEqual(retried["tasks"][0]["state"], "ready")

    def test_initial_goal_and_criteria_bounds_fail_before_snapshot_creation(self):
        store = self.store()
        with self.assertRaisesRegex(HarnessError, "task budget"):
            store.create(self.board, "project", ["one", "two"], "too-many", policy={"max_tasks": 1})
        with self.assertRaisesRegex(HarnessError, "success criteria"):
            store.create(self.board, "project", ["one"], "too-many-criteria",
                         success_criteria=[f"criterion {n}" for n in range(long_horizon.MAX_CRITERIA + 1)])

    def test_nested_project_roots_are_persisted_and_serialized(self):
        nested = self.project / "nested"
        nested.mkdir()
        board = copy.deepcopy(self.board)
        board["projects"] = [
            {"id": "outer", "name": "Outer", "path": str(self.project), "is_there": True, "tasks": ["Outer"]},
            {"id": "inner", "name": "Inner", "path": str(nested), "is_there": True, "tasks": ["Inner"]},
        ]
        board["works_on"] = [
            {"agent": "lead", "project": "outer"}, {"agent": "lead", "project": "inner"},
        ]
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ) as start:
            goals = runtime.start_board(board, "nested")
        self.assertEqual([one["status"] for one in goals], ["queued", "waiting_for_project"])
        self.assertEqual(start.call_count, 1)
        self.assertEqual(
            goals[1]["project_queue"]["blocked_by_goal_id"], goals[0]["goal_id"],
        )

    def test_board_prevalidates_every_project_before_creating_or_starting_any_goal(self):
        second = self.base / "second-project"
        second.mkdir()
        board = copy.deepcopy(self.board)
        board["projects"] = [
            {"id": "first", "name": "First", "path": str(self.project),
             "is_there": True, "tasks": ["Valid first goal"]},
            {"id": "second", "name": "Second", "path": str(second),
             "is_there": True, "tasks": ["Invalid later goal"]},
        ]
        board["works_on"] = [{"agent": "lead", "project": "first"}]
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(runtime, "start_background") as start:
            with self.assertRaisesRegex(HarnessError, "ready agent"):
                runtime.start_board(board, "all-or-none-board")
        start.assert_not_called()
        self.assertEqual(runtime.store.list(100), [])

    def test_legacy_board_request_rejects_changed_intent(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        board = copy.deepcopy(self.board)
        board["projects"][0]["tasks"] = ["First saved intent"]
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            created = runtime.start_board(board, "stable-board-request")[0]
            replayed = runtime.start_board(board, "stable-board-request")[0]
            self.assertEqual(replayed["goal_id"], created["goal_id"])
            board["projects"][0]["tasks"] = ["Different saved intent"]
            with self.assertRaisesRegex(HarnessError, "different .*objective"):
                runtime.start_board(board, "stable-board-request")

    def test_overlap_is_persisted_as_waiter_without_dispatch(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ) as start:
            owner = runtime.start(
                self.board, "project", ["First"], "first-live",
                conversation_id="chat-first",
            )
            waiter = runtime.start(
                self.board, "project", ["Second exact objective"], "second-waiting",
                conversation_id="chat-second",
            )
        self.assertEqual(start.call_count, 1)
        self.assertEqual(waiter["status"], "waiting_for_project")
        self.assertEqual(waiter["conversation_id"], "chat-second")
        self.assertEqual(waiter["objective"], "Second exact objective")
        self.assertEqual(waiter["project_queue"]["state"], "waiting")
        self.assertEqual(waiter["project_queue"]["blocked_by_goal_id"], owner["goal_id"])
        self.assertEqual(waiter["execution_contract"]["schema_version"], 1)
        self.assertEqual(waiter["execution_contract"]["mode"], "exclusive_project")
        self.assertRegex(waiter["execution_contract"]["fingerprint_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(runtime.store.claim_ready(waiter["goal_id"], "must-not-run"), [])
        self.assertEqual(len(runtime.store.list(100)), 2)

    def test_waiting_goal_controls_cannot_bypass_project_owner(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        old = runtime.store.create(self.board, "project", ["Paused old goal"], "old-paused")
        runtime.store.control(old["goal_id"], "pause")
        waiting = runtime.store.create(self.board, "project", ["Waiting new goal"], "new-waiting")
        controls = [
            ("resume", {}), ("retry", {"task_id": waiting["tasks"][0]["id"]}),
            ("steer", {"text": "New direction"}),
            ("message", {"text": "Continue", "task_id": waiting["tasks"][0]["id"]}),
            ("reassign", {"task_id": waiting["tasks"][0]["id"], "agent_id": "reviewer"}),
            ("request_review", {"task_id": waiting["tasks"][0]["id"], "agent_id": "reviewer"}),
        ]
        for control, payload in controls:
            with self.subTest(control=control), self.assertRaisesRegex(HarnessError, "waiting for"):
                runtime.control(waiting["goal_id"], control, payload)
            self.assertEqual(runtime.store.get(old["goal_id"])["status"], "paused")
            self.assertEqual(
                runtime.store.get(waiting["goal_id"])["status"], "waiting_for_project",
            )
        owners = runtime.store.active_overlapping_project(self.project)
        self.assertEqual([one["goal_id"] for one in owners], [old["goal_id"]])

    def test_cancel_promotes_oldest_waiter_once_and_rebases_the_next(self):
        store = self.store()
        owner = store.create(self.board, "project", ["Owner"], "owner")
        time.sleep(0.002)
        first = store.create(self.board, "project", ["First waiter"], "first-waiter")
        time.sleep(0.002)
        second = store.create(self.board, "project", ["Second waiter"], "second-waiter")

        released = store.control(owner["goal_id"], "cancel")

        self.assertEqual(released["status"], "cancelled")
        self.assertEqual(released["project_queue"]["state"], "released")
        self.assertEqual(released["promoted_goal_ids"], [first["goal_id"]])
        promoted = store.get(first["goal_id"])
        still_waiting = store.get(second["goal_id"])
        self.assertEqual(promoted["status"], "queued")
        self.assertEqual(promoted["project_queue"]["state"], "owner")
        self.assertGreater(promoted["project_queue"]["promoted_ms"], 0)
        self.assertEqual(still_waiting["status"], "waiting_for_project")
        self.assertEqual(
            still_waiting["project_queue"]["blocked_by_goal_id"], first["goal_id"],
        )
        event_types = [
            one["type"] for one in store.events(first["goal_id"])["events"]
        ]
        self.assertEqual(event_types.count("goal_project_promoted"), 1)

        second_release = store.control(first["goal_id"], "cancel")
        self.assertEqual(second_release["promoted_goal_ids"], [second["goal_id"]])
        self.assertEqual(store.get(second["goal_id"])["status"], "queued")

    def test_complete_releases_project_and_promotes_waiter(self):
        store = self.store()
        owner = store.create(self.board, "project", ["Owner"], "complete-owner")
        waiter = store.create(self.board, "project", ["Waiter"], "complete-waiter")

        def ready_to_complete(document, _db):
            for task in document["tasks"]:
                task["state"] = "complete"
                task["criteria_evidence"] = [{
                    "criterion": "Original objective is satisfied",
                    "evidence_refs": ["snapshot:" + "a" * 64],
                }]
            document["artifacts"] = [{
                "kind": "verified_no_change", "tree_merkle": "a" * 64,
            }]

        store._mutate(owner["goal_id"], ready_to_complete)
        current = store.get(owner["goal_id"])
        completed = store.complete_verification(
            owner["goal_id"], {"status": "passed", "basis": "unit"},
            expected_revision=current["revision"], expected_objective_epoch=1,
        )
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["project_queue"]["state"], "released")
        self.assertEqual(completed["promoted_goal_ids"], [waiter["goal_id"]])
        self.assertEqual(store.get(waiter["goal_id"])["status"], "queued")

    def test_runtime_starts_same_authority_goal_after_atomic_promotion(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        owner = runtime.store.create(self.board, "project", ["Owner"], "runtime-owner")
        waiter = runtime.store.create(self.board, "project", ["Waiter"], "runtime-waiter")
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ) as start:
            released = runtime.control(owner["goal_id"], "cancel")
        self.assertEqual(released["promoted_goal_ids"], [waiter["goal_id"]])
        start.assert_called_once_with(waiter["goal_id"])

    def test_failed_goal_retains_ownership_until_cancel_or_resume(self):
        store = self.store()
        owner = store.create(self.board, "project", ["Owner"], "failed-owner")

        def fail_resumably(document, db):
            document["status"] = "failed"
            document["note"] = "Injected resumable failure"
            store._event(db, document, "goal_failed", payload={"resumable": True})

        store._mutate(owner["goal_id"], fail_resumably)
        failed = store.get(owner["goal_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["project_queue"]["state"], "owner")
        waiter = store.create(self.board, "project", ["Wait"], "after-failed")
        self.assertEqual(waiter["status"], "waiting_for_project")
        self.assertEqual(
            waiter["project_queue"]["blocked_by_goal_id"], owner["goal_id"],
        )

        resumed = store.control(owner["goal_id"], "resume")
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["project_queue"]["state"], "owner")
        cancelled = store.control(owner["goal_id"], "cancel")
        self.assertEqual(cancelled["promoted_goal_ids"], [waiter["goal_id"]])

    def test_cancel_directly_from_failed_releases_and_promotes(self):
        store = self.store()
        failed = store.create(self.board, "project", ["Failed owner"], "failed-cancel")

        def fail(document, _db):
            document["status"] = "failed"
            document["note"] = "Injected resumable failure"
            document["tasks"][0]["state"] = "failed"

        store._mutate(failed["goal_id"], fail)
        waiter = store.create(self.board, "project", ["Waiting"], "failed-cancel-waiter")

        cancelled = store.control(failed["goal_id"], "cancel")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["project_queue"]["state"], "released")
        self.assertEqual(cancelled["promoted_goal_ids"], [waiter["goal_id"]])
        self.assertEqual(store.get(waiter["goal_id"])["status"], "queued")

    def test_legacy_failed_overlap_migrates_released_without_resend_and_resume_waits(self):
        store = self.store()
        pairs = []
        for suffix, owner_status in (("paused", "paused"), ("running", "running")):
            target = self.base / f"legacy-target-{suffix}"
            staging = self.base / f"legacy-staging-{suffix}"
            target.mkdir()
            staging.mkdir()
            board = copy.deepcopy(self.board)
            board["projects"][0].update({
                "id": f"target-{suffix}", "path": str(target), "name": "Target",
            })
            board["works_on"] = [{
                "agent": one["agent"], "project": f"target-{suffix}",
            } for one in self.board["works_on"]]
            old = store.create(
                board, f"target-{suffix}", ["Legacy failed evidence"],
                f"legacy-failed-{suffix}",
            )

            def make_legacy_failed(document, _db):
                document["status"] = "failed"
                document["note"] = "Legacy failure evidence remains inspectable"
                document["tasks"][0]["state"] = "failed"
                document["tasks"][0]["evidence"] = ["legacy-evidence"]
                document.pop("project_queue", None)

            store._mutate(old["goal_id"], make_legacy_failed)
            staging_board = copy.deepcopy(board)
            staging_board["projects"][0]["path"] = str(staging)
            newer = store.create(
                staging_board, f"target-{suffix}", ["New owner"],
                f"new-owner-{suffix}",
            )

            def move_new_owner(document, _db):
                authority_id = long_horizon.project_identity(target)
                document["status"] = owner_status
                document["project"]["path"] = str(target)
                document["project_authority_id"] = authority_id
                document["execution_contract"] = long_horizon._exclusive_project_contract(
                    target, authority_id,
                )
                document["project_queue"] = store._queue_record("owner", document["created_ms"])

            store._mutate(newer["goal_id"], move_new_owner)
            pairs.append((old, newer, owner_status))

        with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            reopened = long_horizon.GoalStore(self.config)
        ask.assert_not_called()
        for old, newer, owner_status in pairs:
            migrated = reopened.get(old["goal_id"])
            self.assertEqual(migrated["status"], "failed")
            self.assertEqual(migrated["project_queue"]["state"], "released")
            self.assertEqual(migrated["tasks"][0]["evidence"], ["legacy-evidence"])
            self.assertEqual(reopened.get(newer["goal_id"])["status"], owner_status)
            resumed = reopened.control(old["goal_id"], "resume")
            self.assertEqual(resumed["status"], "waiting_for_project")
            self.assertEqual(
                resumed["project_queue"]["blocked_by_goal_id"], newer["goal_id"],
            )

    def test_runtime_resume_reacquires_released_legacy_failed_goal(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Legacy failed"], "runtime-legacy-resume")

        def make_legacy_failed(document, _db):
            document["status"] = "failed"
            document["tasks"][0]["state"] = "failed"
            document.pop("project_queue", None)

        store._mutate(goal["goal_id"], make_legacy_failed)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.store.get(goal["goal_id"])["project_queue"]["state"], "released")
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ) as start:
            resumed = runtime.control(goal["goal_id"], "resume")
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["project_queue"]["state"], "owner")
        start.assert_called_once_with(goal["goal_id"])

    def test_effectful_legacy_failed_migration_retains_project_owner(self):
        store = self.store()
        goal = store.create(
            self.board, "project", ["Unresolved legacy reply"], "effectful-legacy-failed",
        )
        task = store.claim_ready(goal["goal_id"], "legacy-effect-worker")[0]
        store.record_dispatch(goal["goal_id"], task, "legacy-effect")

        def fail_without_queue(document, _db):
            document["status"] = "failed"
            document["tasks"][0]["state"] = "failed"
            document.pop("project_queue", None)

        store._mutate(goal["goal_id"], fail_without_queue)
        reopened = long_horizon.GoalStore(self.config)
        migrated = reopened.get(goal["goal_id"])
        self.assertEqual(migrated["status"], "failed")
        self.assertEqual(migrated["project_queue"]["state"], "owner")
        waiter = reopened.create(
            self.board, "project", ["Must wait"], "effectful-legacy-waiter",
        )
        self.assertEqual(waiter["status"], "waiting_for_project")
        self.assertEqual(
            waiter["project_queue"]["blocked_by_goal_id"], goal["goal_id"],
        )

    def test_effectful_legacy_failed_overlap_fails_closed_during_migration(self):
        store = self.store()
        old = store.create(
            self.board, "project", ["Unresolved old effect"], "effectful-overlap-old",
        )
        task = store.claim_ready(old["goal_id"], "effectful-overlap-worker")[0]
        store.record_dispatch(old["goal_id"], task, "effectful-overlap")

        def fail_without_queue(document, _db):
            document["status"] = "failed"
            document["tasks"][0]["state"] = "failed"
            document.pop("project_queue", None)

        store._mutate(old["goal_id"], fail_without_queue)
        staging = self.base / "effectful-overlap-staging"
        staging.mkdir()
        staging_board = copy.deepcopy(self.board)
        staging_board["projects"][0]["path"] = str(staging)
        newer = store.create(
            staging_board, "project", ["New active owner"], "effectful-overlap-new",
        )

        def overlap_new_owner(document, _db):
            authority_id = long_horizon.project_identity(self.project)
            document["status"] = "paused"
            document["project"]["path"] = str(self.project)
            document["project_authority_id"] = authority_id
            document["execution_contract"] = long_horizon._exclusive_project_contract(
                self.project, authority_id,
            )
            document["project_queue"] = store._queue_record(
                "owner", document["created_ms"],
            )

        store._mutate(newer["goal_id"], overlap_new_owner)
        with self.assertRaisesRegex(HarnessError, "ownership conflicts"):
            long_horizon.GoalStore(self.config)

    def test_waiting_for_user_goal_retains_project_ownership(self):
        store = self.store()
        owner = store.create(self.board, "project", ["Need user"], "user-owner")

        def wait_for_user(document, db):
            document["status"] = "waiting_for_user"
            document["note"] = "Waiting for an exact user decision"
            store._event(db, document, "goal_waiting", payload={"reason": "test"})

        store._mutate(owner["goal_id"], wait_for_user)
        waiter = store.create(self.board, "project", ["Later"], "after-user-wait")
        self.assertEqual(waiter["status"], "waiting_for_project")
        self.assertEqual(waiter["project_queue"]["blocked_by_goal_id"], owner["goal_id"])
        self.assertEqual(
            [one["goal_id"] for one in store.active_overlapping_project(self.project)],
            [owner["goal_id"]],
        )

    def test_unrelated_roots_have_independent_durable_owners(self):
        other_root = self.base / "different project β"
        other_root.mkdir()
        board = copy.deepcopy(self.board)
        board["projects"].append({
            "id": "other", "name": "Other", "path": str(other_root),
            "is_there": True, "tasks": [],
        })
        board["works_on"].append({"agent": "lead", "project": "other"})
        store = self.store()
        first = store.create(board, "project", ["First"], "different-root-first")
        second = store.create(board, "other", ["Second"], "different-root-second")
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "queued")
        self.assertEqual(first["project_queue"]["state"], "owner")
        self.assertEqual(second["project_queue"]["state"], "owner")

    def test_cross_authority_store_waits_and_observes_promotion(self):
        first_store = self.store()
        owner = first_store.create(self.board, "project", ["First"], "authority-first")
        other_authority = self.base / "other-config-authority"
        other_authority.mkdir()
        other_config = LoadedConfig(copy.deepcopy(self.config.data), other_authority, [], {})
        other_store = long_horizon.GoalStore(other_config)
        waiter = other_store.create(
            self.board, "project", ["Second"], "authority-second",
            conversation_id="other-chat",
        )
        self.assertEqual(waiter["status"], "waiting_for_project")
        self.assertEqual(waiter["project_queue"]["blocked_by_goal_id"], owner["goal_id"])

        first_store.control(owner["goal_id"], "cancel")

        promoted = other_store.get(waiter["goal_id"])
        self.assertEqual(promoted["status"], "queued")
        self.assertEqual(promoted["project_queue"]["state"], "owner")

    def test_cancel_drains_provider_before_cross_authority_waiter_dispatches(self):
        other_authority = self.base / "live-waiter-authority"
        other_authority.mkdir()
        other_config = LoadedConfig(copy.deepcopy(self.config.data), other_authority, [], {})
        owner_runtime = long_horizon.LongHorizonRuntime(self.config)
        waiter_runtime = long_horizon.LongHorizonRuntime(other_config)
        self.addCleanup(owner_runtime.close)
        self.addCleanup(waiter_runtime.close)
        provider_entered = threading.Event()
        release_provider = threading.Event()
        dispatches: list[str] = []
        dispatch_lock = threading.Lock()

        def provider(*_args, **kwargs):
            before = kwargs.get("before_provider_dispatch")
            after = kwargs.get("after_provider_response")
            if before:
                before("initial")
            with dispatch_lock:
                dispatches.append(str(kwargs.get("conversation_key") or ""))
                number = len(dispatches)
            if number == 1:
                provider_entered.set()
                if not release_provider.wait(THREAD_COORDINATION_TIMEOUT_SECONDS):
                    raise RuntimeError("blocking provider test timed out")
            if after:
                after("initial")
            return {"text": json.dumps(action(criteria_evidence=[{
                "criterion": "Original objective is satisfied",
                "evidence_refs": ["verified-no-change"],
            }]))}

        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider), \
                mock.patch.object(
                    long_horizon.swarm_work, "_run_selected_project_verification",
                    return_value={"status": "passed", "basis": "drain ordering"},
                ):
            owner = owner_runtime.start(
                self.board, "project", ["Blocking owner"], "blocking-owner",
            )
            self.assertTrue(provider_entered.wait(THREAD_COORDINATION_TIMEOUT_SECONDS))
            waiter = waiter_runtime.start(
                self.board, "project", ["Foreign waiter"], "foreign-live-waiter",
            )
            self.assertEqual(waiter["status"], "waiting_for_project")

            draining = owner_runtime.control(owner["goal_id"], "cancel")
            self.assertEqual(draining["status"], "cancelling")
            self.assertEqual(draining["project_queue"]["state"], "owner")
            self.assertEqual(
                waiter_runtime.store.get(waiter["goal_id"])["status"],
                "waiting_for_project",
            )
            time.sleep(0.15)
            with dispatch_lock:
                self.assertEqual(len(dispatches), 1)

            release_provider.set()
            deadline = time.monotonic() + THREAD_COORDINATION_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                owner_state = owner_runtime.store.get(owner["goal_id"])["status"]
                waiter_state = waiter_runtime.store.get(waiter["goal_id"])["status"]
                with dispatch_lock:
                    count = len(dispatches)
                if owner_state == "cancelled" and count >= 2 \
                        and waiter_state in {"running", "complete"}:
                    break
                time.sleep(0.02)
            self.assertEqual(owner_runtime.store.get(owner["goal_id"])["status"], "cancelled")
            with dispatch_lock:
                self.assertEqual(len(dispatches), 2)
            self.assertNotEqual(
                waiter_runtime.store.get(waiter["goal_id"])["status"],
                "waiting_for_project",
            )

    def test_cancelling_goal_rejects_other_controls_and_new_context_tools(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Drain safely"], "cancel-control-fence")
        scheduler_id = "runtime-scheduler"
        self.assertTrue(store.claim_scheduler(goal["goal_id"], scheduler_id))
        task = store.claim_ready(goal["goal_id"], scheduler_id)[0]
        draining = store.control(goal["goal_id"], "cancel")
        self.assertEqual(draining["status"], "cancelling")
        with self.assertRaisesRegex(HarnessError, "draining cancellation"):
            store.control(goal["goal_id"], "pause")
        with self.assertRaisesRegex(HarnessError, "changed or paused|draining cancellation"):
            store.reserve_context_tool(goal["goal_id"], task, {
                "call_id": "late-tool", "name": "read_file", "arguments": {"path": "x"},
            })
        with self.assertRaisesRegex(HarnessError, "draining cancellation"):
            store.prepare_transaction(
                goal["goal_id"], task, "late-transaction", [{
                    "path": "late.txt", "content": "late", "delete": False,
                }],
            )
        with self.assertRaisesRegex(HarnessError, "draining cancellation"):
            store.stage_review_if_needed(goal["goal_id"], task, action(risk="high"))
        with self.assertRaisesRegex(HarnessError, "draining"):
            store.apply_action(goal["goal_id"], task, action())
        with self.assertRaisesRegex(HarnessError, "terminal goal"):
            store.resolve_interrupts(goal["goal_id"], {
                "expected_revision": draining["revision"], "pending_ids": [], "answers": {},
            })
        self.assertTrue(store.record_action(goal["goal_id"], task, action()))
        failed_apply = store.fail_pending_apply(goal["goal_id"], "late apply error")
        self.assertEqual(failed_apply["status"], "cancelling")
        finalized = store.control(goal["goal_id"], "cancel", {
            "drain_complete": True, "scheduler_id": scheduler_id,
        })
        self.assertEqual(finalized["status"], "cancelled")

    def test_cancelling_allows_receipt_of_already_applied_transaction(self):
        other_root = self.base / "applied-during-drain"
        other_root.mkdir()
        board = copy.deepcopy(self.board)
        board["projects"][0].update({"id": "applied-drain", "path": str(other_root)})
        board["works_on"] = [{"agent": "lead", "project": "applied-drain"}]
        store = self.store()
        goal = store.create(
            board, "applied-drain", ["Receipt applied boundary"], "applied-drain",
        )
        scheduler_id = "applied-drain-scheduler"
        self.assertTrue(store.claim_scheduler(goal["goal_id"], scheduler_id))
        task = store.claim_ready(goal["goal_id"], scheduler_id)[0]
        transaction_id = "applied-drain-transaction"
        store.prepare_transaction(goal["goal_id"], task, transaction_id, [{
            "path": "done.txt", "content": "done", "delete": False,
        }])
        self.assertEqual(store.control(goal["goal_id"], "cancel")["status"], "cancelling")
        store.record_transaction_applied(goal["goal_id"], task, {
            "kind": "file_transaction", "transaction_id": transaction_id,
            "changes": [], "patch": "", "patch_sha256": "0" * 64,
        })
        current = store.get(goal["goal_id"])
        self.assertEqual(current["status"], "cancelling")
        self.assertEqual(current["tasks"][0]["pending_transaction"]["state"], "applied")

    def test_cancel_replay_is_a_true_no_write_for_terminal_and_draining_goals(self):
        store = self.store()
        terminal = store.create(self.board, "project", ["Cancel once"], "cancel-once")
        first = store.control(terminal["goal_id"], "cancel")
        replay = store.control(terminal["goal_id"], "cancel")
        self.assertEqual(replay["revision"], first["revision"])
        self.assertEqual(replay["event_seq"], first["event_seq"])

        draining = store.create(self.board, "project", ["Drain once"], "drain-once")
        scheduler_id = "drain-once-scheduler"
        self.assertTrue(store.claim_scheduler(draining["goal_id"], scheduler_id))
        store.claim_ready(draining["goal_id"], scheduler_id)
        requested = store.control(draining["goal_id"], "cancel")
        repeated = store.control(draining["goal_id"], "cancel")
        self.assertEqual(repeated["revision"], requested["revision"])
        self.assertEqual(repeated["event_seq"], requested["event_seq"])
        store.control(draining["goal_id"], "cancel", {
            "drain_complete": True, "scheduler_id": scheduler_id,
        })

    def test_list_never_hides_old_active_owner_behind_newest_hundred_rows(self):
        store = self.store()
        oldest = store.create(
            self.board, "project", ["Old blocking owner"], "old-visible-owner",
        )
        for index in range(105):
            root = self.base / f"visible-active-{index:03d}"
            root.mkdir()
            store.clone_to_project(
                oldest, f"visible-{index}", f"Visible {index}", root,
                f"visible-active-request-{index}",
            )

        listed = store.list(limit=20)

        self.assertIn(oldest["goal_id"], {one["goal_id"] for one in listed})
        self.assertGreaterEqual(len(listed), 106)

    def test_foreign_runtime_cancel_between_final_read_and_release_is_finalized(self):
        owner_runtime = long_horizon.LongHorizonRuntime(self.config)
        cancelling_runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(owner_runtime.close)
        self.addCleanup(cancelling_runtime.close)
        owner = owner_runtime.store.create(
            self.board, "project", ["Owner boundary"], "foreign-finalizer-owner",
        )
        waiter = owner_runtime.store.create(
            self.board, "project", ["Wait next"], "foreign-finalizer-waiter",
        )
        release_entered = threading.Event()
        allow_release = threading.Event()
        original_release = owner_runtime.store.release_scheduler

        def graph_pause(*_args, **_kwargs):
            owner_runtime.store.control(owner["goal_id"], "pause")

        def blocked_release(goal_id, scheduler_id):
            release_entered.set()
            if not allow_release.wait(THREAD_COORDINATION_TIMEOUT_SECONDS):
                raise RuntimeError("scheduler release barrier timed out")
            return original_release(goal_id, scheduler_id)

        with mock.patch.object(owner_runtime.graph, "invoke", side_effect=graph_pause), \
                mock.patch.object(
                    owner_runtime.store, "release_scheduler", side_effect=blocked_release,
                ), mock.patch.object(owner_runtime, "_start_promoted_goals"):
            runner = threading.Thread(
                target=owner_runtime.run, args=(owner["goal_id"],), daemon=True,
            )
            runner.start()
            self.assertTrue(release_entered.wait(THREAD_COORDINATION_TIMEOUT_SECONDS))
            requested = cancelling_runtime.control(owner["goal_id"], "cancel")
            self.assertEqual(requested["status"], "cancelling")
            self.assertEqual(
                cancelling_runtime.store.get(waiter["goal_id"])["status"],
                "waiting_for_project",
            )
            allow_release.set()
            runner.join(THREAD_COORDINATION_TIMEOUT_SECONDS)
            self.assertFalse(runner.is_alive())

        self.assertEqual(owner_runtime.store.get(owner["goal_id"])["status"], "cancelled")
        self.assertEqual(owner_runtime.store.get(waiter["goal_id"])["status"], "queued")

    def test_restart_preserves_waiter_and_never_resends_promoted_checkpoint(self):
        store = self.store()
        owner = store.create(self.board, "project", ["Owner"], "restart-owner")
        waiter = store.create(self.board, "project", ["Waiter"], "restart-waiter")
        owner_task = store.claim_ready(owner["goal_id"], "owner-worker")[0]
        store.record_dispatch(owner["goal_id"], owner_task, "owner-dispatch")

        def make_owner_dead(document, _db):
            document["worker"] = {
                "pid": 99999999, "token": "dead", "worker_id": "dead",
                "kind": "runtime", "schema_version": 1, "acquired_ms": 1,
            }

        store._mutate(owner["goal_id"], make_owner_dead)
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            runtime = long_horizon.LongHorizonRuntime(self.config)
            runtime.recover_all()
            self.assertEqual(runtime.store.get(owner["goal_id"])["status"], "paused")
            self.assertEqual(
                runtime.store.get(waiter["goal_id"])["status"], "waiting_for_project",
            )
            ask.assert_not_called()
            runtime.close()
            runtime.store.control(owner["goal_id"], "cancel")
            self.assertEqual(runtime.store.get(waiter["goal_id"])["status"], "queued")
            waiter_task = runtime.store.claim_ready(
                waiter["goal_id"], "promoted-worker",
            )[0]
            runtime.store.record_dispatch(
                waiter["goal_id"], waiter_task, "promoted-dispatch",
            )
            runtime.store._mutate(waiter["goal_id"], make_owner_dead)
            restarted = long_horizon.LongHorizonRuntime(self.config)
            self.addCleanup(restarted.close)
            restarted.recover_all()
            self.assertEqual(restarted.store.get(waiter["goal_id"])["status"], "paused")
            ask.assert_not_called()

    def test_exact_large_objective_is_preserved_and_oversize_is_rejected(self):
        store = self.store()
        exact = "Leading whitespace stays\n" + ("x" * 20_500) + "\nExact tail"
        goal = store.create(
            self.board, "project", [exact], "large-exact",
            conversation_id="large-chat",
        )
        self.assertEqual(goal["objective"], exact)
        self.assertEqual(goal["original_objective"], exact)
        self.assertEqual(goal["tasks"][0]["description"], exact)
        replayed = store.create(
            self.board, "project", [exact], "large-exact",
            conversation_id="large-chat",
        )
        self.assertEqual(replayed["goal_id"], goal["goal_id"])
        self.assertTrue(replayed["reused"])

        with self.assertRaisesRegex(HarnessError, "too large"):
            store.create(
                self.board, "project",
                ["z" * (long_horizon.MAX_OBJECTIVE_CHARACTERS + 1)],
                "oversize-explicit",
            )
        self.assertIsNone(store.get_by_request("oversize-explicit"))

    def test_authenticated_schema_v2_goal_adds_execution_contract_without_resend(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Legacy exact work"], "legacy-contract")

        def remove_additive_metadata(document, _db):
            document.pop("execution_contract", None)
            document.pop("project_queue", None)

        store._mutate(goal["goal_id"], remove_additive_metadata)
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            reopened = long_horizon.GoalStore(self.config)
        migrated = reopened.get(goal["goal_id"])
        self.assertEqual(migrated["schema_version"], long_horizon.SCHEMA_VERSION)
        self.assertEqual(migrated["execution_contract"]["schema_version"], 1)
        self.assertEqual(migrated["project_queue"]["state"], "owner")
        self.assertIn(
            "execution_contract_migrated",
            [one["type"] for one in reopened.events(goal["goal_id"])["events"]],
        )
        ask.assert_not_called()

    def test_runtime_admission_digest_binds_text_beyond_former_twenty_k_limit(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        prefix = "p" * 20_100
        exact = prefix + " original tail"
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            created = runtime.start(
                self.board, "project", [exact], "digest-large",
                conversation_id="digest-chat",
            )
            replayed = runtime.start(
                self.board, "project", [exact], "digest-large",
                conversation_id="digest-chat",
            )
            with self.assertRaisesRegex(HarnessError, "different .*objective"):
                runtime.start(
                    self.board, "project", [prefix + " changed tail"], "digest-large",
                    conversation_id="digest-chat",
                )
        self.assertEqual(created["objective"], exact)
        self.assertEqual(replayed["goal_id"], created["goal_id"])

    def test_max_goals_plus_one_create_replay_is_permanent_and_tamper_evident(self):
        store = self.store()
        intentions = {}
        for index in range(long_horizon.MAX_GOALS + 1):
            request_id = f"retired-create-{index}"
            objective = f"Preserve exact retired create intent {index}"
            conversation_id = f"retired-create-chat-{index}"
            goal = store.create(
                self.board, "project", [objective], request_id,
                conversation_id=conversation_id,
            )
            intentions[request_id] = (objective, conversation_id, goal["goal_id"])
            store.control(goal["goal_id"], "cancel")

        tombstone = self.assert_one_compact_rollover_tombstone(store)
        request_id = tombstone["client_request_id"]
        objective, conversation_id, goal_id = intentions[request_id]
        reopened = long_horizon.GoalStore(self.config)
        with mock.patch.object(
            reopened, "get", side_effect=AssertionError("pruned replay called get()"),
        ) as get:
            fetched = reopened.get_by_request(request_id)
            replayed = reopened.create(
                self.board, "project", [objective], request_id,
                conversation_id=conversation_id,
            )
            with self.assertRaisesRegex(HarnessError, "retired .*different work"):
                reopened.create(
                    self.board, "project", [objective + " changed"], request_id,
                    conversation_id=conversation_id,
                )
        get.assert_not_called()
        self.assertEqual(fetched["goal_id"], goal_id)
        self.assertEqual(replayed["goal_id"], goal_id)
        self.assertTrue(fetched["request_tombstone"])
        self.assertTrue(fetched["reused"])
        self.assertTrue(replayed["reused"])

        with closing(sqlite3.connect(reopened.database)) as db:
            db.execute(
                "UPDATE long_goal_request_tombstones SET tombstone_json=? "
                "WHERE request_id=?",
                (json.dumps({"request_tombstone": True}), tombstone["request_id"]),
            )
            db.commit()
        with self.assertRaisesRegex(HarnessError, "integrity verification"):
            reopened.get_by_request(request_id)

    def test_max_goals_plus_one_runtime_replay_never_dispatches_or_loads_goal(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        intentions = {}
        with mock.patch.object(runtime, "_enable_auto_start_watcher"), \
                mock.patch.object(runtime, "start_background"):
            for index in range(long_horizon.MAX_GOALS + 1):
                request_id = f"retired-runtime-{index}"
                objective = f"Preserve exact retired runtime intent {index}"
                conversation_id = f"retired-runtime-chat-{index}"
                goal = runtime.start(
                    self.board, "project", [objective], request_id,
                    conversation_id=conversation_id,
                )
                intentions[request_id] = (objective, conversation_id, goal["goal_id"])
                runtime.store.control(goal["goal_id"], "cancel")
        tombstone = self.assert_one_compact_rollover_tombstone(runtime.store)
        runtime.close()

        request_id = tombstone["client_request_id"]
        objective, conversation_id, goal_id = intentions[request_id]
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        with mock.patch.object(
            restarted.store, "get",
            side_effect=AssertionError("pruned runtime replay called get()"),
        ) as get, mock.patch.object(
            restarted, "_enable_auto_start_watcher",
            side_effect=AssertionError("retired replay enabled auto-start"),
        ) as watcher, mock.patch.object(
            restarted, "_require_no_external_owner",
            side_effect=AssertionError("retired replay reconciled project ownership"),
        ) as ownership, mock.patch.object(
            restarted.store, "reconcile_project_queue",
            side_effect=AssertionError("retired replay reconciled the queue"),
        ) as reconcile, mock.patch.object(
            restarted, "start_background",
            side_effect=AssertionError("retired replay started background work"),
        ) as background, mock.patch.object(
            long_horizon.chat_lab, "ask_once",
            side_effect=AssertionError("retired replay dispatched a provider"),
        ) as provider:
            replayed = restarted.start(
                self.board, "project", [objective], request_id,
                conversation_id=conversation_id,
            )
            with self.assertRaisesRegex(HarnessError, "already bound to a different"):
                restarted.start(
                    self.board, "project", [objective + " changed"], request_id,
                    conversation_id=conversation_id,
                )
        self.assertEqual(replayed["goal_id"], goal_id)
        self.assertTrue(replayed["request_tombstone"])
        self.assertTrue(replayed["reused"])
        for held in (get, watcher, ownership, reconcile, background, provider):
            held.assert_not_called()

    def test_runtime_preflight_binds_every_direct_intent_field_live_and_after_rollover(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        request_id = "preflight-full-binding"
        objectives = ["Preserve the first boundary", "Preserve the second boundary"]
        text_equivalent_single_objective = "\n\n".join(objectives)
        criteria = ["Return evidence from the exact saved contract"]
        policy = {"max_tasks": 4, "max_parallel": 2, "review_risk": "high"}
        attachments = [{
            "name": "binding.txt", "type": "text/plain", "size": 5,
            "data": "data:text/plain;base64,aGVsbG8=",
        }]
        participants = ["lead", "reviewer"]
        exact_chat = "shared-prefix-" + ("c" * 146) + ("a" * 96)
        conflicting_chat = "shared-prefix-" + ("c" * 146) + ("b" * 96)
        self.assertEqual(len(exact_chat), 256)
        self.assertEqual(exact_chat[:160], conflicting_chat[:160])
        exact = {
            "lead_id": "lead", "success_criteria": criteria,
            "policy": policy, "attachments": attachments,
            "participant_ids": participants, "conversation_id": exact_chat,
        }
        request_prefix = "r" * long_horizon.MAX_REQUEST_ID_CHARACTERS
        inspected_boundary = runtime.preflight_start(
            self.board, "project", ["Preserve the exact request boundary"],
            request_prefix, conversation_id="request-boundary-chat",
        )
        self.assertIsNone(inspected_boundary["goal"])
        with mock.patch.object(
            runtime.store, "get_by_request",
            side_effect=AssertionError("oversized request identity reached goal lookup"),
        ) as request_lookup, mock.patch.object(
            runtime, "_enable_auto_start_watcher",
            side_effect=AssertionError("oversized request identity enabled the watcher"),
        ) as request_watcher:
            with self.assertRaisesRegex(HarnessError, "at most 160 characters"):
                runtime.start(
                    self.board, "project", ["Reject a request prefix alias"],
                    request_prefix + "x", conversation_id="request-boundary-chat",
                )
        request_lookup.assert_not_called()
        request_watcher.assert_not_called()
        with mock.patch.object(
            runtime.store, "get_by_request",
            side_effect=AssertionError("oversized chat identity reached goal lookup"),
        ) as lookup, mock.patch.object(
            runtime, "_enable_auto_start_watcher",
            side_effect=AssertionError("oversized chat identity enabled the watcher"),
        ) as watcher:
            with self.assertRaisesRegex(HarnessError, "at most 256 characters"):
                runtime.start(
                    self.board, "project", objectives, "preflight-oversized-chat",
                    **{**exact, "conversation_id": "x" * 257},
                )
        lookup.assert_not_called()
        watcher.assert_not_called()
        inspected = runtime.preflight_start(
            self.board, "project", objectives, request_id, **exact,
        )
        goal = runtime.store.create(
            self.board, "project", objectives, request_id,
            admission_digest=inspected["admission_digest"],
            expected_project_authority_id=inspected["project_authority_id"],
            **{key: value for key, value in exact.items() if key != "attachments"},
        )

        changed = [
            (objectives, {**exact, "conversation_id": conflicting_chat}),
            (["Changed first boundary", objectives[1]], exact),
            (objectives, {**exact, "success_criteria": ["Different evidence"]}),
            (objectives, {**exact, "policy": {**policy, "max_parallel": 1}}),
            ([text_equivalent_single_objective], exact),
            (objectives, {**exact, "attachments": [{**attachments[0], "data": (
                "data:text/plain;base64,d29ybGQ="
            )}]}),
        ]

        def assert_preflight_contract(runtime_under_test, expected_goal):
            with mock.patch.object(
                runtime_under_test.store, "get",
                side_effect=AssertionError("preflight loaded a goal by id"),
            ) as get, mock.patch.object(
                runtime_under_test, "_enable_auto_start_watcher",
                side_effect=AssertionError("preflight enabled the watcher"),
            ) as watcher, mock.patch.object(
                runtime_under_test, "_require_no_external_owner",
                side_effect=AssertionError("preflight claimed project ownership"),
            ) as ownership, mock.patch.object(
                runtime_under_test.store, "reconcile_project_queue",
                side_effect=AssertionError("preflight reconciled the project queue"),
            ) as reconcile, mock.patch.object(
                runtime_under_test, "start_background",
                side_effect=AssertionError("preflight started background work"),
            ) as background, mock.patch.object(
                long_horizon.chat_lab, "ask_once",
                side_effect=AssertionError("preflight dispatched a provider"),
            ) as provider:
                replay = runtime_under_test.preflight_start(
                    self.board, "project", objectives, request_id, **exact,
                )
                self.assertEqual(replay["goal"]["goal_id"], expected_goal["goal_id"])
                self.assertEqual(
                    replay["admission_digest"], expected_goal["admission_digest"],
                )
                for changed_objectives, changed_fields in changed:
                    with self.assertRaisesRegex(HarnessError, "already bound to a different"):
                        runtime_under_test.preflight_start(
                            self.board, "project", changed_objectives,
                            request_id, **changed_fields,
                        )
            for held in (get, watcher, ownership, reconcile, background, provider):
                held.assert_not_called()

        assert_preflight_contract(runtime, goal)
        runtime.store.control(goal["goal_id"], "cancel")
        for index in range(long_horizon.MAX_GOALS):
            filler = runtime.store.create(
                self.board, "project", [f"Preflight rollover filler {index}"],
                f"preflight-rollover-{index}",
            )
            runtime.store.control(filler["goal_id"], "cancel")
        tombstone = runtime.store.get_by_request(request_id)
        self.assertIsNotNone(tombstone)
        self.assertTrue(tombstone["request_tombstone"])
        runtime.close()

        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        assert_preflight_contract(restarted, tombstone)

    def test_max_goals_plus_one_fork_replay_is_permanent_and_parent_bound(self):
        store = self.store()
        clock = iter(range(10_000, 1_000_000))
        with mock.patch.object(long_horizon, "_now", side_effect=lambda: next(clock)):
            source = store.create(
                self.board, "project", ["Fork this exact checkpoint"], "fork-source",
            )
            fork_root = self.base / "fork-root"
            fork_root.mkdir()
            forked = store.clone_to_project(
                store.get(source["goal_id"]), "project-fork", "Project fork",
                fork_root, "retired-fork",
            )
            other_source = store.create(
                self.board, "project", ["A different fork checkpoint"],
                "other-fork-source",
            )
            with self.assertRaisesRegex(
                HarnessError, "fork request identity already belongs to another goal",
            ):
                store.clone_to_project(
                    store.get(other_source["goal_id"]), "project-fork", "Project fork",
                    fork_root, "retired-fork",
                )
            store.control(forked["goal_id"], "cancel")
            for index in range(long_horizon.MAX_GOALS):
                filler = store.create(
                    self.board, "project", [f"Fork rollover filler {index}"],
                    f"fork-filler-{index}",
                )
                store.control(filler["goal_id"], "cancel")

        tombstone = self.assert_one_compact_rollover_tombstone(store)
        self.assertEqual(tombstone["client_request_id"], "retired-fork")
        self.assertEqual(tombstone["parent_goal_id"], source["goal_id"])
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        with mock.patch.object(
            restarted.store, "get", side_effect=AssertionError("pruned fork called get()"),
        ) as get, mock.patch.object(
            long_horizon.subprocess, "run",
            side_effect=AssertionError("pruned fork touched Git"),
        ) as git:
            replayed = restarted.fork(source["goal_id"], "retired-fork")
            with self.assertRaisesRegex(HarnessError, "already belongs to another goal"):
                restarted.fork("different-parent-goal", "retired-fork")
        self.assertEqual(replayed["goal_id"], forked["goal_id"])
        self.assertTrue(replayed["request_tombstone"])
        self.assertTrue(replayed["reused"])
        get.assert_not_called()
        git.assert_not_called()

    def test_max_goals_plus_one_tombstone_failure_rolls_back_terminal_prune(self):
        store = self.store()
        for index in range(long_horizon.MAX_GOALS):
            goal = store.create(
                self.board, "project", [f"Atomic rollover {index}"],
                f"atomic-rollover-{index}",
            )
            store.control(goal["goal_id"], "cancel")
        overflow = store.create(
            self.board, "project", ["Atomic rollover overflow"], "atomic-overflow",
        )
        with mock.patch.object(
            store, "_remember_request_tombstone",
            side_effect=HarnessError("synthetic tombstone write failure"),
        ), self.assertRaisesRegex(HarnessError, "synthetic tombstone write failure"):
            store.control(overflow["goal_id"], "cancel")

        self.assertEqual(store.get(overflow["goal_id"])["status"], "queued")
        with closing(sqlite3.connect(store.database)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM long_goals "
                "WHERE status IN ('complete','cancelled')"
            ).fetchone()[0], long_horizon.MAX_GOALS)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM long_goal_request_tombstones"
            ).fetchone()[0], 0)

    def test_cross_process_same_project_admission_has_one_owner_and_one_waiter(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        begin = context.Event()
        results = context.Queue()
        other_authority = self.base / "cross-process-authority"
        other_authority.mkdir()
        state_path = self.base / "state"
        processes = [
            context.Process(
                target=_cross_process_goal_admission,
                args=(
                    str(state_path), str(authority), str(self.project), request_id,
                    ready, begin, results,
                ),
            )
            for authority, request_id in (
                (self.authority, "process-a"), (other_authority, "process-b"),
            )
        ]
        for process in processes:
            process.start()
        self.addCleanup(lambda: [
            process.terminate() for process in processes if process.is_alive()
        ])
        self.assertEqual(
            {ready.get(timeout=THREAD_COORDINATION_TIMEOUT_SECONDS) for _ in processes},
            {"process-a", "process-b"},
        )
        begin.set()
        outcomes = [
            results.get(timeout=THREAD_COORDINATION_TIMEOUT_SECONDS) for _ in processes
        ]
        for process in processes:
            process.join(THREAD_COORDINATION_TIMEOUT_SECONDS)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(one["ok"] for one in outcomes), outcomes)
        self.assertCountEqual(
            [one["status"] for one in outcomes], ["queued", "waiting_for_project"],
        )
        owner = next(one for one in outcomes if one["status"] == "queued")
        waiter = next(one for one in outcomes if one["status"] == "waiting_for_project")
        self.assertEqual(waiter["queue"]["blocked_by_goal_id"], owner["goal_id"])

        configs = {
            "process-a": self.config,
            "process-b": LoadedConfig(
                copy.deepcopy(self.config.data), other_authority, [], {},
            ),
        }
        cross_board = {
            "agents": [{"id": "lead", "name": "Lead", "who": "codex", "ready": True}],
            "projects": [{
                "id": "project", "name": "Project", "path": str(self.project),
                "is_there": True, "tasks": [],
            }],
            "works_on": [{"agent": "lead", "project": "project"}],
        }
        owner_store = long_horizon.GoalStore(configs[owner["request_id"]])
        waiter_store = long_horizon.GoalStore(configs[waiter["request_id"]])
        released = owner_store.control(owner["goal_id"], "cancel")
        self.assertEqual(released["promoted_goal_ids"], [waiter["goal_id"]])
        promoted = waiter_store.get(waiter["goal_id"])
        self.assertEqual(promoted["status"], "queued")
        replayed = waiter_store.create(
            cross_board, "project", ["Exact work " + waiter["request_id"]],
            waiter["request_id"], conversation_id="chat-" + waiter["request_id"],
        )
        self.assertEqual(replayed["goal_id"], waiter["goal_id"])
        self.assertTrue(replayed["reused"])

    def test_cross_process_same_request_replay_has_one_provider_dispatch(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        begin = context.Event()
        release = context.Event()
        dispatch_count = context.Value("i", 0)
        results = context.Queue()
        state_path = self.base / "runtime-replay-state"
        processes = [
            context.Process(
                target=_cross_process_runtime_replay,
                args=(
                    str(state_path), str(self.authority), str(self.project),
                    ready, begin, release, dispatch_count, results,
                ),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        self.addCleanup(lambda: [
            process.terminate() for process in processes if process.is_alive()
        ])
        for _ in processes:
            self.assertEqual(
                ready.get(timeout=THREAD_COORDINATION_TIMEOUT_SECONDS), "ready",
            )
        begin.set()
        deadline = time.monotonic() + THREAD_COORDINATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline and dispatch_count.value < 1:
            time.sleep(0.02)
        self.assertEqual(dispatch_count.value, 1)
        time.sleep(0.35)
        self.assertEqual(
            dispatch_count.value, 1,
            "two runtime processes dispatched the idempotent goal concurrently",
        )
        release.set()
        outcomes = [
            results.get(timeout=THREAD_COORDINATION_TIMEOUT_SECONDS) for _ in processes
        ]
        for process in processes:
            process.join(THREAD_COORDINATION_TIMEOUT_SECONDS)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(one.get("ok") for one in outcomes), outcomes)
        self.assertEqual(len({one["goal_id"] for one in outcomes}), 1)
        self.assertEqual(dispatch_count.value, 1)
        self.assertTrue(all(one["status"] == "complete" for one in outcomes), outcomes)

    def test_shared_server_ownership_fences_legacy_and_long_horizon_both_directions(self):
        panel = harness_server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(panel.server_close)
        fake_runs = mock.Mock()
        fake_runs.active_runs.return_value = [{
            "run_id": "legacy-active",
            "snapshot": {
                "selected_mode": "work", "project_id": "project",
                "board": self.board, "conversation": {"project": "project"},
            },
        }]
        fake_queue = mock.Mock()
        fake_queue.active_project_paths.return_value = []
        panel._swarm_runs = fake_runs
        panel._swarm_goal_queue = fake_queue
        self.assertTrue(panel.legacy_project_conflicts(self.project))

        runtime = long_horizon.LongHorizonRuntime(
            self.config, external_project_conflicts=panel.legacy_project_conflicts,
        )
        panel._long_horizon = runtime
        with self.assertRaisesRegex(HarnessError, "Legacy project work already owns"):
            runtime.start(self.board, "project", ["Long work"], "blocked-by-legacy")
        self.assertIsNone(runtime.store.get_by_request("blocked-by-legacy"))

        fake_runs.active_runs.return_value = []
        long_goal = runtime.store.create(self.board, "project", ["Active long work"], "active-long")
        with self.assertRaisesRegex(HarnessError, "Long-horizon goal work already owns"):
            panel.require_no_long_horizon_owner(self.board, "project")
        self.assertEqual(runtime.store.get(long_goal["goal_id"])["status"], "queued")

    def test_shared_server_ownership_includes_board_workspace_and_pipeline_runs(self):
        panel = harness_server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(panel.server_close)
        fake_runs = mock.Mock()
        fake_runs.active_runs.return_value = [{
            "run_id": "board-active",
            "snapshot": {"kind": "board_order", "board": self.board},
        }]
        fake_queue = mock.Mock()
        fake_queue.active_project_paths.return_value = []
        panel._swarm_runs = fake_runs
        panel._swarm_goal_queue = fake_queue
        self.assertTrue(panel.run_lock.acquire(blocking=False))
        self.addCleanup(panel.run_lock.release)
        self.assertTrue(panel.pipeline_lock.acquire(blocking=False))
        self.addCleanup(panel.pipeline_lock.release)

        self.assertEqual(
            panel.legacy_project_conflicts(self.project),
            ["legacy-board-run:board-active"],
        )
        self.assertCountEqual(
            panel.legacy_project_conflicts(self.authority),
            [
                f"workspace-run:{self.authority.resolve()}",
                f"pipeline-run:{self.authority.resolve()}",
            ],
        )

    def test_shared_server_admission_lock_closes_legacy_and_long_start_race(self):
        panel = harness_server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(panel.server_close)
        active_holder = {"run": None}
        fake_runs = mock.Mock()
        fake_runs.active_runs.side_effect = lambda: (
            [active_holder["run"]] if active_holder["run"] is not None else []
        )
        fake_queue = mock.Mock()
        fake_queue.active_project_paths.return_value = []
        panel._swarm_runs = fake_runs
        panel._swarm_goal_queue = fake_queue
        runtime = long_horizon.LongHorizonRuntime(
            self.config, external_project_conflicts=panel.legacy_project_conflicts,
        )
        panel._long_horizon = runtime
        def legacy_claim(entered, release):
            with panel.project_admission_lock, panel.swarm_lock:
                panel.require_no_long_horizon_owner(self.board, "project")
                entered.set()
                self.assertTrue(release.wait(5))
                active_holder["run"] = {
                    "run_id": "legacy-race", "snapshot": {
                        "selected_mode": "work", "project_id": "project",
                        "board": self.board, "conversation": {"project": "project"},
                    },
                }

        active_holder["run"] = None
        entered = threading.Event()
        release = threading.Event()
        error = {}
        legacy = threading.Thread(target=legacy_claim, args=(entered, release))
        def attempt_long():
            try:
                with panel.project_admission_lock, panel.swarm_lock:
                    runtime.start(self.board, "project", ["Racing start"], "race-start")
            except Exception as exc:
                error["value"] = exc
        contender = threading.Thread(target=attempt_long)
        legacy.start()
        self.assertTrue(entered.wait(5))
        contender.start()
        self.assertTrue(contender.is_alive())
        release.set()
        legacy.join(5)
        contender.join(5)
        self.assertRegex(str(error.get("value")), "Legacy project work already owns")
        self.assertIsNone(runtime.store.get_by_request("race-start"))

    def test_legacy_conflicts_include_older_work_hidden_by_newer_unrelated_run(self):
        unrelated = self.base / "unrelated-project"
        unrelated.mkdir()
        unrelated_board = copy.deepcopy(self.board)
        unrelated_board["projects"][0].update({
            "id": "unrelated", "name": "Unrelated", "path": str(unrelated),
        })
        unrelated_board["works_on"] = [{"agent": "lead", "project": "unrelated"}]

        panel = harness_server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(panel.server_close)
        fake_runs = mock.Mock()
        fake_runs.active_runs.return_value = [
            {
                "run_id": "newer-unrelated",
                "snapshot": {
                    "selected_mode": "work", "project_id": "unrelated",
                    "board": unrelated_board, "conversation": {"project": "unrelated"},
                },
            },
            {
                "run_id": "older-overlapping",
                "snapshot": {
                    "selected_mode": "work", "project_id": "project",
                    "board": self.board, "conversation": {"project": "project"},
                },
            },
        ]
        fake_queue = mock.Mock()
        fake_queue.active_project_paths.return_value = []
        panel._swarm_runs = fake_runs
        panel._swarm_goal_queue = fake_queue

        self.assertEqual(
            panel.legacy_project_conflicts(self.project),
            ["legacy-run:older-overlapping"],
        )

    def test_attachment_failure_cannot_persist_or_block_a_goal(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            long_horizon.chat_lab, "keep_attachments", side_effect=HarnessError("attachment too large"),
        ), mock.patch.object(runtime, "start_background"):
            with self.assertRaisesRegex(HarnessError, "attachment too large"):
                runtime.start(
                    self.board, "project", ["Use the attached specification"],
                    "attachment-fails", attachments=[{"name": "spec.docx"}],
                )
        self.assertIsNone(runtime.store.get_by_request("attachment-fails"))
        self.assertEqual(runtime.store.list(100), [])

    def test_attachments_are_atomic_and_idempotent_with_goal_creation(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        attachment_file = self.base / "kept-spec.txt"
        attachment_file.write_text("specification", encoding="utf-8")
        kept = [{"id": "file-1", "name": "spec.txt", "type": "text/plain", "size": 13}]
        provider_files = [{**kept[0], "path": str(attachment_file)}]
        with mock.patch.object(
            long_horizon.chat_lab, "keep_attachments",
            return_value=(kept, provider_files, "Requirement from document"),
        ) as ingest, mock.patch.object(
            runtime, "start_background", side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            created = runtime.start(
                self.board, "project", ["Implement specification"], "attachment-once",
                attachments=[{"name": "spec.txt"}],
            )
            reused = runtime.start(
                self.board, "project", ["Implement specification"], "attachment-once",
                attachments=[{"name": "spec.txt"}],
            )
        self.assertEqual(ingest.call_count, 1)
        self.assertTrue(reused["reused"])
        stored = runtime.store.get(created["goal_id"])
        self.assertIn("Requirement from document", stored["objective"])
        self.assertEqual(stored["original_objective"], stored["objective"])
        self.assertEqual(stored["input_attachments"], kept)
        self.assertEqual(stored["input_provider_attachments"][0]["path"], str(attachment_file))

    def test_restart_auto_starts_only_pristine_queued_boundary(self):
        store = self.store()
        created = store.create(self.board, "project", ["Created before worker start"], "queued-create-crash")
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        with mock.patch.object(
            runtime, "start_background",
            side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            recovered = runtime.recover_all()
            runtime._auto_start_enabled = False
        self.assertEqual(len(recovered), 1)
        self.assertEqual(runtime.store.get(created["goal_id"])["status"], "queued")

        def consume_test_auto_start(document, _db):
            document["project_queue"]["auto_start_pending"] = False

        runtime.store._mutate(created["goal_id"], consume_test_auto_start)

        other_root = self.base / "after-apply"
        other_root.mkdir()
        board = copy.deepcopy(self.board)
        board["projects"][0]["id"] = "after-apply"
        board["projects"][0]["path"] = str(other_root)
        board["works_on"] = [{"agent": "lead", "project": "after-apply"}]
        after_apply = runtime.store.create(board, "after-apply", ["Applied before next node"], "queued-after-apply")
        task = runtime.store.claim_ready(after_apply["goal_id"], "worker")[0]
        runtime.store.apply_action(after_apply["goal_id"], task, action(), artifact={
            "kind": "verified_no_change", "tree_merkle": "c" * 64, "file_count": 0,
        })
        def dead_worker(document, _db):
            document["worker"] = {"pid": 99999999, "token": "dead", "worker_id": "dead"}
        runtime.store._mutate(after_apply["goal_id"], dead_worker)
        second = runtime.recover_all()
        self.assertEqual(len(second), 2)
        self.assertEqual(runtime.store.get(created["goal_id"])["status"], "paused")
        self.assertEqual(runtime.store.get(after_apply["goal_id"])["status"], "paused")

    def test_auto_start_watcher_pages_past_missing_roots_to_valid_goal(self):
        store = self.store()
        goals = []
        for index in range(18):
            root = self.base / f"watcher-root-{index:02d}"
            root.mkdir()
            board = copy.deepcopy(self.board)
            board["projects"][0].update({
                "id": f"watcher-{index}", "path": str(root),
            })
            board["works_on"] = [{
                "agent": "lead", "project": f"watcher-{index}",
            }]
            goals.append(store.create(
                board, f"watcher-{index}", [f"Goal {index}"], f"watcher-goal-{index}",
            ))
        runtime = long_horizon.LongHorizonRuntime(self.config)
        attempts: list[str] = []
        valid_goal_id = goals[-1]["goal_id"]

        def attempt(goal_id, answers=None):
            attempts.append(goal_id)
            if goal_id != valid_goal_id:
                raise FileNotFoundError("removed test project")
            return runtime.store.get(goal_id)

        with mock.patch.object(runtime, "start_background", side_effect=attempt):
            runtime._enable_auto_start_watcher()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and valid_goal_id not in attempts:
                time.sleep(0.02)
            runtime.close()
        self.assertIn(valid_goal_id, attempts)
        self.assertGreaterEqual(len(set(attempts)), 18)

    def test_recovery_keeps_pristine_goal_visible_when_provider_setup_changed(self):
        store = self.store()
        goal = store.create(
            self.board, "project", ["Keep checkpoint visible"], "recovery-provider-drift",
        )

        def drift_provider(document, _db):
            document["agents"][0]["route_binding"][
                "route_fingerprint_sha256"
            ] = "0" * 64

        store._mutate(goal["goal_id"], drift_provider)
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            runtime = long_horizon.LongHorizonRuntime(self.config)
            recovered = runtime.recover_all()
            runtime.close()
        self.assertTrue(recovered)
        current = store.get(goal["goal_id"])
        self.assertEqual(current["status"], "queued")
        self.assertTrue(store.provider_setup_status(current)["changed"])
        ask.assert_not_called()

    def test_verification_result_is_superseded_by_steer_and_cannot_revive_cancel(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)

        def ready_for_verification(board, project_id, request_id):
            goal = runtime.store.create(board, project_id, ["Verified objective"], request_id)
            task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
            runtime.store.apply_action(goal["goal_id"], task, action(criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["artifact:verify-tx"],
            }]), artifact={
                "kind": "file_transaction", "transaction_id": "verify-tx",
                "changes": [{"path": "verified.txt", "delete": False}], "patch_sha256": "d" * 64,
            })
            return runtime.store.get(goal["goal_id"])

        def control_while_verifying(goal, control, payload=None):
            entered = threading.Event()
            release = threading.Event()
            controlled = threading.Event()
            results = {}
            failures = []

            def delayed_verification(*_args, **_kwargs):
                entered.set()
                if not release.wait(THREAD_COORDINATION_TIMEOUT_SECONDS * 2):
                    raise TimeoutError("the verification race was not released during bounded cleanup")
                return {"status": "passed", "basis": "stale checks"}

            def verify():
                try:
                    results["verification"] = runtime._verify_node({"goal_id": goal["goal_id"]})
                except BaseException as exc:
                    failures.append(("verification", exc))

            def apply_control():
                try:
                    results["control"] = runtime.control(goal["goal_id"], control, payload)
                except BaseException as exc:
                    failures.append(("control", exc))
                finally:
                    controlled.set()

            verifier = threading.Thread(target=verify, name=f"verify-race-{control}", daemon=True)
            controller = threading.Thread(target=apply_control, name=f"control-race-{control}", daemon=True)
            with mock.patch.object(
                long_horizon.swarm_work, "_run_selected_project_verification",
                side_effect=delayed_verification,
            ):
                verifier.start()
                try:
                    self.assertTrue(
                        entered.wait(THREAD_COORDINATION_TIMEOUT_SECONDS),
                        "verification did not reach the controlled race boundary",
                    )
                    controller.start()
                    self.assertTrue(
                        controlled.wait(THREAD_COORDINATION_TIMEOUT_SECONDS),
                        f"{control} control did not finish before the bounded test timeout",
                    )
                finally:
                    # Always release and join both sides so an assertion cannot strand a
                    # test thread or let its late mutation leak into the next race.
                    release.set()
                    if controller.ident is not None:
                        controller.join(THREAD_COORDINATION_TIMEOUT_SECONDS)
                    verifier.join(THREAD_COORDINATION_TIMEOUT_SECONDS)

            self.assertFalse(controller.is_alive(), f"{control} control thread did not stop")
            self.assertFalse(verifier.is_alive(), "verification thread did not stop after release")
            if failures:
                phase, failure = failures[0]
                raise AssertionError(f"{phase} thread failed during the verification race") from failure
            return results

        first = ready_for_verification(self.board, "project", "verify-steer-race")
        with mock.patch.object(
            runtime, "start_background", side_effect=lambda goal_id, answers=None: runtime.store.get(goal_id),
        ):
            steer_race = control_while_verifying(
                first, "steer", {"text": "Also satisfy the new constraint"},
            )
        steered = runtime.store.get(first["goal_id"])
        self.assertNotEqual(steered["status"], "complete")
        self.assertEqual(steered["verification"]["status"], "superseded")
        self.assertEqual(steer_race["verification"], {"route": "schedule"})

        runtime.store.control(first["goal_id"], "cancel")
        second = ready_for_verification(self.board, "project", "verify-cancel-race")
        cancel_race = control_while_verifying(second, "cancel")
        cancelled = runtime.store.get(second["goal_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["verification"]["status"], "not_run")
        self.assertEqual(cancel_race["verification"], {"route": "end"})

        third = ready_for_verification(self.board, "project", "verify-pause-race")
        pause_race = control_while_verifying(third, "pause")
        paused = runtime.store.get(third["goal_id"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["verification"]["status"], "superseded")
        self.assertEqual(pause_race["verification"], {"route": "end"})

    def test_pause_and_cancel_serialize_at_file_transaction_boundary(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        original_apply = long_horizon.FileTransaction.apply

        for control in ("pause", "cancel"):
            with self.subTest(control=control):
                root = self.base / f"boundary-{control}"
                root.mkdir()
                board = copy.deepcopy(self.board)
                board["projects"][0].update({"id": control, "path": str(root)})
                board["works_on"] = [{"agent": "lead", "project": control}]
                goal = runtime.store.create(board, control, [f"Boundary {control}"], f"boundary-{control}")
                task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
                pending = action(changes=[{
                    "path": "boundary.txt", "content": control + "\n", "delete": False,
                }])
                pending["_nexus_baselines"] = {"boundary.txt": "missing"}
                runtime.store.record_dispatch(goal["goal_id"], task, "digest")
                runtime.store.record_action(goal["goal_id"], task, pending)
                entered = threading.Event()
                release = threading.Event()
                controlled = threading.Event()
                def delayed_apply(transaction, plans, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return original_apply(transaction, plans, **kwargs)
                apply_thread = threading.Thread(target=lambda: runtime._apply_node({
                    "goal_id": goal["goal_id"], "actions": [{"task": task, "action": pending}],
                }))
                control_thread = threading.Thread(target=lambda: (
                    runtime.control(goal["goal_id"], control), controlled.set()
                ))
                with mock.patch.object(long_horizon.FileTransaction, "apply", delayed_apply):
                    apply_thread.start()
                    self.assertTrue(entered.wait(5))
                    control_thread.start()
                    self.assertFalse(controlled.wait(0.1))
                    release.set()
                    apply_thread.join(5)
                    control_thread.join(5)
                self.assertTrue(controlled.is_set())
                self.assertEqual((root / "boundary.txt").read_text(encoding="utf-8"), control + "\n")
                self.assertEqual(runtime.store.get(goal["goal_id"])["status"],
                                 "paused" if control == "pause" else "cancelled")

    def test_active_overlap_query_is_not_limited_to_newest_hundred_goals(self):
        store = self.store()
        oldest = store.create(self.board, "project", ["Old active owner"], "old-owner")
        for position in range(100):
            root = self.base / f"other-{position}"
            root.mkdir()
            board = copy.deepcopy(self.board)
            board["projects"][0].update({"id": f"other-{position}", "path": str(root)})
            board["works_on"] = [{"agent": "lead", "project": f"other-{position}"}]
            store.create(board, f"other-{position}", ["Other"], f"other-{position}")
        overlaps = store.active_overlapping_project(self.project)
        self.assertEqual([one["goal_id"] for one in overlaps], [oldest["goal_id"]])

    def test_interrupt_answer_is_bound_to_displayed_revision_and_pending_set(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Need decision"], "interrupt-revision")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        questions = [{
            "id": "choice", "prompt": "Which path?", "multiple": False, "allow_other": True,
            "options": [
                {"label": "A", "description": "Choose A", "recommended": True},
                {"label": "B", "description": "Choose B", "recommended": False},
            ],
        }]
        store.apply_action(goal["goal_id"], task, action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=questions,
        ))
        shown = store.get(goal["goal_id"])
        pending_ids = [one["id"] for one in shown["interrupts"] if one["state"] == "pending"]
        store.control(goal["goal_id"], "pause")
        with self.assertRaisesRegex(HarnessError, "changed after this decision card"):
            store.resolve_interrupts(goal["goal_id"], {
                "answers": {pending_ids[0]: "A"},
                "expected_revision": shown["revision"],
                "pending_ids": pending_ids,
            })

    def test_pending_decision_cannot_be_bypassed_by_pause_resume_or_steering(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Need decision"], "pending-invariant")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.apply_action(goal["goal_id"], task, action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "choice", "prompt": "Which path?", "multiple": False,
                "allow_other": True, "options": [
                    {"label": "A", "description": "Choose A", "recommended": True},
                    {"label": "B", "description": "Choose B", "recommended": False},
                ],
            }],
        ))
        store.control(goal["goal_id"], "pause")
        for control, payload in [
            ("resume", {}), ("steer", {"text": "Skip the question"}),
            ("criteria", {"success_criteria": ["Changed"]}),
        ]:
            with self.subTest(control=control), self.assertRaisesRegex(HarnessError, "pending decision"):
                store.control(goal["goal_id"], control, payload)
        held = store.get(goal["goal_id"])
        self.assertEqual(held["status"], "paused")
        self.assertTrue(any(one["state"] == "pending" for one in held["interrupts"]))

    def test_completed_goal_controls_cannot_mutate_evidence_or_state(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Finish once"], "immutable-complete")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        artifact = {
            "kind": "file_transaction", "transaction_id": "immutable-tx",
            "changes": [{"path": "done.txt", "delete": False}], "patch_sha256": "b" * 64,
        }
        store.apply_action(goal["goal_id"], task, action(criteria_evidence=[{
            "criterion": "Original objective is satisfied", "evidence_refs": ["artifact:immutable-tx"],
        }]), artifact=artifact)
        completed = store.complete_verification(goal["goal_id"], {"status": "passed", "basis": "tests"})
        self.assertEqual(completed["status"], "complete")
        before = store.get(goal["goal_id"])
        attempts = [
            ("steer", {"text": "Change it"}),
            ("message", {"text": "Do more", "task_id": task["id"]}),
            ("criteria", {"success_criteria": ["New unchecked criterion"]}),
            ("reassign", {"task_id": task["id"], "agent_id": "reviewer"}),
            ("request_review", {"task_id": task["id"], "agent_id": "reviewer"}),
        ]
        for control, payload in attempts:
            with self.subTest(control=control), self.assertRaisesRegex(HarnessError, "terminal goal is immutable"):
                store.control(goal["goal_id"], control, payload)
        self.assertEqual(store.get(goal["goal_id"]), before)

    def test_user_requested_review_from_paused_goal_becomes_runnable(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Review on request"], "paused-review")
        task = goal["tasks"][0]
        store.control(goal["goal_id"], "pause")
        queued = store.control(goal["goal_id"], "request_review", {
            "task_id": task["id"], "agent_id": "reviewer",
        })
        self.assertEqual(queued["status"], "queued")
        self.assertTrue(any(one["kind"] == "review" and one["state"] == "ready"
                            for one in queued["tasks"]))

    def test_event_chain_detects_tampering_and_reports_pagination(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Events"], "events")
        page = store.events(goal["goal_id"], 0, 1)
        self.assertIn("has_more", page)
        with closing(sqlite3.connect(store.database)) as db:
            db.execute(
                "UPDATE long_goal_events SET event_json=? WHERE goal_id=? AND seq=1",
                ('{"changed":true}', goal["goal_id"]),
            )
            db.commit()
        with self.assertRaisesRegex(HarnessError, "integrity"):
            store.events(goal["goal_id"])

    def test_ui_and_server_expose_new_engine_and_legacy_fallback(self):
        html = (Path(__file__).parents[1] / "src/our_harness/ui/index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "src/our_harness/ui/app.js").read_text(encoding="utf-8")
        server = (Path(__file__).parents[1] / "src/our_harness/server.py").read_text(encoding="utf-8")
        self.assertIn('id="missionControl"', html)
        self.assertIn('id="swarmLegacyGoals"', html)
        self.assertIn('id="missionReassign"', html)
        self.assertIn('id="missionRetry"', html)
        self.assertIn('id="missionCriteria"', html)
        self.assertIn("/api/long-horizon/start-board", script)
        self.assertIn("/api/long-horizon/answer", script)
        self.assertIn('parsed.path == "/api/long-horizon/events"', server)
        control_route = server[server.index('elif self.path == "/api/long-horizon/control"'):
                               server.index('elif self.path == "/api/long-horizon/answer"')]
        answer_route = server[server.index('elif self.path == "/api/long-horizon/answer"'):
                              server.index('elif self.path == "/api/swarm/goal-queue/cancel"')]
        self.assertIn("require_project_execution_authority", control_route)
        self.assertIn("require_project_execution_authority", answer_route)
        self.assertIn("runtime.control(goal_id, action, payload)", control_route)
        self.assertIn("require_no_long_horizon_owner", server)
        self.assertIn("external_project_conflicts=self.legacy_project_conflicts", server)
        big_chat = re.search(
            r"async function sendFromTheBigChat\([\s\S]+?\n}\n\nfunction wireUpTheTray",
            script,
        ).group(0)
        self.assertIn('if (mode === "work")', big_chat)
        self.assertLess(big_chat.index('request("/api/long-horizon/start"'),
                        big_chat.index('request("/api/swarm/say"'))
        self.assertIn('"pending_apply"', script)
        self.assertIn("expected_revision: longGoal.revision", script)
        self.assertIn("hasPendingDecision", script)
        self.assertIn("const immutable = !longGoal", script)
        self.assertIn(
            '["complete", "cancelled", "cancelling"].includes(longGoal.status)',
            script,
        )

    def test_mission_control_protects_goal_history_after_provider_setup_changes(self):
        root = Path(__file__).parents[1] / "src/our_harness/ui"
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        styles = (root / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="missionProviderSetupChanged"', html)
        self.assertIn('id="missionProviderSetupReview"', html)
        self.assertIn('id="missionProviderSetupPrepare"', html)
        self.assertIn("Nexus will not redirect this goal’s history", html)
        render = script[
            script.index("function renderMissionControl"):
            script.index("function renderMissionEvents")
        ]
        self.assertIn("const providerSetupChanged = missionProviderSetupChanged()", render)
        self.assertIn('$("missionResume").disabled = !longGoal || providerSetupChanged', render)
        self.assertIn('$("missionRetry").disabled = immutable || providerSetupChanged', render)
        self.assertIn('$("missionReassign").disabled = immutable || providerSetupChanged', render)
        self.assertIn("submit.disabled = immutable || providerSetupChanged", render)
        guard = script[
            script.index("async function missionControl"):
            script.index("async function cancelLongGoal")
        ]
        self.assertIn('"resume", "retry", "reassign"', guard)
        self.assertIn("missionProviderSetupChanged()", guard)
        recovery = script[
            script.index("async function prepareNewGoalWithCurrentProviderSetup"):
            script.index("function renderMissionControl")
        ]
        self.assertIn('action: "cancel"', recovery)
        self.assertIn("localStorage.removeItem(LONG_GOAL_REQUEST_KEY)", recovery)
        self.assertIn("focusCurrentBoardGoalSetup()", recovery)
        self.assertNotIn("agent.who =", recovery)
        self.assertIn(".mission-provider-setup-changed", styles)
        self.assertIn("longProjectWorkActive", script)
        self.assertNotIn("Both agents must work", html)
        self.assertIn("Work uses the event-driven goal engine", html)

    def test_blocked_prerequisite_propagates_and_scheduler_pauses_concretely(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Parent"], "blocked-dependency")
        parent = store.claim_ready(goal["goal_id"], "worker")[0]
        store.apply_action(goal["goal_id"], parent, action("delegate", tasks=[{
            "title": "Child", "description": "Blocked child", "assigned_agent_id": "reviewer",
            "depends_on": [], "parallel_safe": True, "resource_paths": [],
        }]))
        child = store.claim_ready(goal["goal_id"], "child-worker")[0]
        store.apply_action(goal["goal_id"], child, action("blocked", summary="Missing compiler"))
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        runtime._schedule_node({"goal_id": goal["goal_id"]})
        stopped = store.get(goal["goal_id"])
        held_parent = next(one for one in stopped["tasks"] if one["id"] == parent["id"])
        self.assertEqual(held_parent["state"], "blocked")
        self.assertEqual(stopped["status"], "paused")
        self.assertIn("No runnable task", stopped["note"])

    def test_delegation_rejects_transitive_parent_cycle(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Parent", "Existing"], "transitive-cycle")
        parent, existing = goal["tasks"]
        def make_existing_depend_on_parent(document, _db):
            by_id = {one["id"]: one for one in document["tasks"]}
            by_id[existing["id"]]["depends_on"] = [parent["id"]]
            by_id[existing["id"]]["state"] = "waiting"
        store._mutate(goal["goal_id"], make_existing_depend_on_parent)
        claimed = store.claim_ready(goal["goal_id"], "worker")[0]
        with self.assertRaisesRegex(HarnessError, "transitive dependency cycle"):
            store.apply_action(goal["goal_id"], claimed, action("delegate", tasks=[{
                "title": "Cycle child", "description": "Would close the cycle",
                "assigned_agent_id": "reviewer", "depends_on": [existing["id"]],
                "parallel_safe": True, "resource_paths": [],
            }]))

    def test_risky_proposal_applies_once_only_after_evidence_bound_review(self):
        target = self.project / "obsolete.txt"
        target.write_text("old\n", encoding="utf-8")
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Delete obsolete"], "approve-delete")
        parent = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "obsolete.txt", "content": "", "delete": True, "reason": "obsolete",
        }])
        proposed["_nexus_baselines"] = {
            "obsolete.txt": long_horizon._path_baseline_marker(self.project, "obsolete.txt")
        }
        runtime.store.record_dispatch(goal["goal_id"], parent, "proposal")
        runtime.store.record_action(goal["goal_id"], parent, proposed)
        runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": parent, "action": proposed}]})
        self.assertTrue(target.exists())
        review = runtime.store.claim_ready(goal["goal_id"], "review-worker")[0]
        packet_ref = "review-packet:" + review["review_packet_sha256"]
        runtime.store.apply_action(goal["goal_id"], review, action(
            "complete", evidence=[packet_ref], review_verdict="approve",
            review_findings=["The deletion is bounded to the obsolete file."],
        ), artifact={"kind": "verified_no_change", "tree_merkle": "review-snapshot"})
        pending = runtime.store.get(goal["goal_id"])
        self.assertEqual(next(one for one in pending["tasks"] if one["id"] == parent["id"])["state"], "pending_apply")
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=AssertionError("provider resent")):
            runtime._apply_node({"goal_id": goal["goal_id"], "task_ids": [parent["id"]]})
        self.assertFalse(target.exists())
        self.assertEqual(runtime.store.get(goal["goal_id"])["budget"]["provider_calls"], 1)
        self.assertEqual(sum(one["kind"] == "review" for one in runtime.store.get(goal["goal_id"])["tasks"]), 1)

    def test_reviewer_context_is_targeted_and_requires_real_review_evidence(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Target"], "targeted-review")
        target = goal["tasks"][0]
        unrelated = copy.deepcopy(target)
        unrelated.update({"id": "unrelated-task", "title": "Unrelated", "state": "complete"})
        def add_unrelated_secret(document, _db):
            unrelated["evidence"] = ["UNRELATED_SECRET_EVIDENCE"]
            unrelated["artifacts"] = [{"path": "UNRELATED_SECRET_ARTIFACT"}]
            document["tasks"].append(copy.deepcopy(unrelated))
            document["budget"]["tasks_created"] += 1
        store._mutate(goal["goal_id"], add_unrelated_secret)
        claimed = store.claim_ready(goal["goal_id"], "worker")[0]
        self.stage_review(store, goal, claimed, action("request_review", risk="high"))
        current = store.get(goal["goal_id"])
        review = next(one for one in current["tasks"] if one["kind"] == "review")
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        context = runtime._agent_context(current, review)
        self.assertIn("TARGETED REVIEW PACKET", context)
        self.assertIn(claimed["id"], context)
        self.assertNotIn("UNRELATED_SECRET_EVIDENCE", context)
        self.assertNotIn("UNRELATED_SECRET_ARTIFACT", context)
        leased = next(
            one for one in store.claim_ready(goal["goal_id"], "review-worker")
            if one.get("kind") == "review"
        )
        with self.assertRaisesRegex(HarnessError, "Review approval needs"):
            store.apply_action(goal["goal_id"], leased, action("complete"), artifact={
                "kind": "verified_no_change", "tree_merkle": "empty-review",
            })

    def test_foreign_goal_ids_cannot_read_mutate_answer_events_or_fork(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Private"], "foreign-source")
        other_root = self.base / "other-authority"
        other_root.mkdir()
        other_config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), other_root, [], {})
        other_runtime = long_horizon.LongHorizonRuntime(other_config)
        self.addCleanup(other_runtime.close)
        operations = [
            lambda: other_runtime.store.get(goal["goal_id"]),
            lambda: other_runtime.store.events(goal["goal_id"]),
            lambda: other_runtime.store.control(goal["goal_id"], "pause"),
            lambda: other_runtime.store.resolve_interrupts(goal["goal_id"], {}),
            lambda: other_runtime.fork(goal["goal_id"], "foreign-fork"),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(
                HarnessError, "different Nexus project authority"
            ):
                operation()

    def test_provider_plan_cannot_overwrite_edit_made_during_model_turn(self):
        target = self.project / "shared.txt"
        target.write_text("model saw this\n", encoding="utf-8")
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Update shared.txt"], "baseline-race")
        task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        dispatched = threading.Event()
        release = threading.Event()
        proposed = action(changes=[{
            "path": "shared.txt", "content": "agent version\n", "delete": False, "reason": "update",
        }])
        def delayed(*_args, **kwargs):
            kwargs["before_provider_dispatch"]("initial")
            dispatched.set()
            self.assertTrue(release.wait(5))
            return {"text": json.dumps(proposed)}
        result = {}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=delayed):
            worker = threading.Thread(target=lambda: result.update({
                "item": runtime._execute_one(goal["goal_id"], task["id"]),
            }))
            worker.start()
            self.assertTrue(dispatched.wait(5))
            target.write_text("user edit wins\n", encoding="utf-8")
            release.set()
            worker.join(5)
        returned_task, returned_action = result["item"]
        with self.assertRaisesRegex(HarnessError, "Baseline conflict"):
            runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{
                "task": returned_task, "action": returned_action,
            }]})
        self.assertEqual(target.read_text(encoding="utf-8"), "user edit wins\n")

    def test_project_switch_resets_long_horizon_authority_and_rejects_unfinished_goal(self):
        panel = harness_server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(panel.server_close)
        old_runtime = panel.long_horizon
        goal = old_runtime.store.create(self.board, "project", ["Unfinished"], "switch-active")
        other = self.base / "new-project"
        other.mkdir()
        with self.assertRaisesRegex(HarnessError, "Long-horizon project work is unfinished"):
            panel.move_to(str(other))
        old_runtime.store.control(goal["goal_id"], "cancel")
        panel.move_to(str(other))
        self.assertIsNone(panel._long_horizon)
        new_runtime = panel.long_horizon
        self.assertIsNot(new_runtime, old_runtime)
        self.assertNotEqual(new_runtime.store.authority_key, old_runtime.store.authority_key)
        self.assertEqual(new_runtime.store.list(), [])

    def test_control_plane_prose_is_redacted_at_every_ingress(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        store = self.store()
        goal = store.create(self.board, "project", [f"Use api_key={secret}"], "redacted")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        ids = store.apply_action(goal["goal_id"], task, action(
            "ask_user", summary=f"token={secret}", evidence=[f"password={secret}"],
            interrupt_reason="requirement_ambiguity", questions=[{
                "id": "secret-q", "prompt": f"Use token={secret}?", "multiple": False,
                "allow_other": True, "options": [{
                    "label": "Yes", "description": f"credential={secret}", "recommended": True,
                }],
            }],
        ))
        current = store.get(goal["goal_id"])
        store.resolve_interrupts(goal["goal_id"], {
            "answers": {ids[0]: f"password={secret}"},
            "expected_revision": current["revision"], "pending_ids": ids,
        })
        store.control(goal["goal_id"], "pause")
        store.control(goal["goal_id"], "steer", {"text": f"auth_token={secret}"})
        store.control(goal["goal_id"], "pause")
        store.control(goal["goal_id"], "criteria", {"success_criteria": [f"secret={secret}"]})
        final = store.get(goal["goal_id"])
        raw = json.dumps(final, sort_keys=True)
        self.assertNotIn(secret, raw)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        self.assertNotIn(secret, runtime._agent_context(final, final["tasks"][0]))

    def test_context_tools_are_iterative_budgeted_and_durably_journaled(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Find omitted behavior"], "tools")
        task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        responses = [
            action("work", tool_calls=[{
                "call_id": "search-1", "name": "search_workspace", "arguments": {"query": "needle"},
            }]),
            action("work", tool_calls=[{
                "call_id": "verify-1", "name": "run_selected_verification", "arguments": {},
            }]),
            action("complete"),
        ]
        fake_tools = mock.Mock()
        fake_tools.execute.side_effect = [
            {"matches": [{"path": "deep/omitted.py", "line": 4}]},
            {"status": "passed", "basis": "targeted"},
        ]
        with mock.patch.object(long_horizon.swarm_work, "CollaborationLedger") as ledger_class, \
                mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools", return_value=fake_tools), \
                mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            ledger_class.return_value.begin.return_value = mock.Mock(session_id="tools-session")
            ask.side_effect = lambda *_args, **kwargs: (
                kwargs["before_provider_dispatch"]("initial")
                or {"text": json.dumps(responses.pop(0))}
            )
            _task, returned = runtime._execute_one(goal["goal_id"], task["id"])
        self.assertEqual(returned["action"], "complete")
        self.assertEqual(fake_tools.execute.call_count, 2)
        current = runtime.store.get(goal["goal_id"])
        self.assertEqual(current["budget"]["context_tool_calls"], 2)
        kinds = [one["type"] for one in runtime.store.events(goal["goal_id"])["events"]]
        self.assertEqual(kinds.count("context_step_acknowledged"), 2)
        self.assertEqual(kinds.count("context_tool_result"), 2)

    def test_web_provider_accepts_fence_repairs_once_and_rejects_invalid_second_reply(self):
        board = copy.deepcopy(self.board)
        board["agents"][0]["who"] = "web:claude-test"
        for mode in ("fenced", "repair", "invalid"):
            with self.subTest(mode=mode):
                runtime = long_horizon.LongHorizonRuntime(self.config)
                self.addCleanup(runtime.close)
                goal = runtime.store.create(board, "project", [f"Web {mode}"], f"web-{mode}")
                task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
                valid = json.dumps(action("complete"))
                replies = {
                    "fenced": [f"```json\n{valid}\n```"],
                    "repair": ["not json", f"```json\n{valid}\n```"],
                    "invalid": ["not json", "still not json"],
                }[mode]
                with mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
                    ask.side_effect = lambda *_args, **kwargs: (
                        kwargs["before_provider_dispatch"]("initial")
                        or {"text": replies.pop(0)}
                    )
                    _task, returned = runtime._execute_one(goal["goal_id"], task["id"])
                if mode == "invalid":
                    self.assertEqual(returned["action"], "failed")
                    current = runtime.store.get(goal["goal_id"])
                    self.assertEqual(current["status"], "queued")
                    self.assertEqual(
                        current["tasks"][0]["provider_effect_state"],
                        "known_failure_reassigned",
                    )
                else:
                    self.assertEqual(returned["action"], "complete")
                    self.assertEqual(ask.call_count, 1 if mode == "fenced" else 2)
                runtime.store.control(goal["goal_id"], "cancel")

    def test_attachment_failures_leave_no_partial_request_directory(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        valid = {"name": "one.txt", "type": "text/plain",
                 "data": base64.b64encode(b"one").decode("ascii")}
        invalid = {"name": "two.txt", "type": "text/plain", "data": "not-base64"}
        with self.assertRaises(HarnessError):
            runtime.start(self.board, "project", ["Use files"], "partial-input", attachments=[valid, invalid])
        attachment_base = runtime.store.root / "long-horizon-inputs" / runtime.store.authority_key
        self.assertFalse(any(attachment_base.rglob("*")) if attachment_base.exists() else False)

    def test_recovered_applied_transaction_is_never_hidden_by_cancel_or_steer(self):
        for control in ("cancel", "steer"):
            with self.subTest(control=control):
                root = self.base / f"recovered-{control}"
                root.mkdir()
                board = copy.deepcopy(self.board)
                board["projects"][0].update({"id": control, "path": str(root)})
                board["works_on"] = [{"agent": "lead", "project": control}]
                store = self.store()
                goal = store.create(board, control, ["Recovered effect"], f"recovered-{control}")
                transaction_id = f"1234567890-{control[:10]:0<10}"
                changes = [{
                    "path": "changed.txt", "content": "already applied\n",
                    "delete": False, "reason": "exercise recovered transaction",
                }]
                plans = long_horizon.swarm_work._validated_changes(root, changes)
                manifest = long_horizon.FileTransaction(root).apply(
                    plans, transaction_id=transaction_id,
                )
                artifact = {
                    "kind": "file_transaction", "transaction_id": transaction_id,
                    "changes": manifest["changes"], "patch": manifest["patch"],
                    "patch_sha256": manifest["patch_sha256"],
                }
                def crash_boundary(document, _db):
                    task = document["tasks"][0]
                    task.update({
                        "state": "pending_apply", "provider_effect_state": "acknowledged",
                        "pending_action": action(),
                        "pending_transaction": {"state": "applied", "artifact": artifact,
                                                "transaction_id": artifact["transaction_id"]},
                    })
                    document["status"] = "paused"
                store._mutate(goal["goal_id"], crash_boundary)
                payload = {"text": "Reconcile the already-applied work"} if control == "steer" else {}
                updated = store.control(goal["goal_id"], control, payload)
                self.assertTrue(any(one.get("transaction_id") == artifact["transaction_id"]
                                    for one in updated["artifacts"]))
                held = updated["tasks"][0]
                self.assertFalse(held["pending_transaction"])
                self.assertFalse(held["pending_action"])
                if control == "steer":
                    self.assertTrue(any(one["kind"] == "steering" for one in updated["tasks"]))

    def test_cancel_reconciles_prepared_file_effect_before_releasing_project(self):
        target = self.project / "cancel.txt"
        target.write_text("before\n", encoding="utf-8")
        store = self.store()
        goal = store.create(self.board, "project", ["Prepare then cancel"], "cancel-prepared")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "cancel.txt", "content": "after\n", "delete": False,
            "reason": "exercise cancellation rollback",
        }])
        proposed["_nexus_baselines"] = {
            "cancel.txt": long_horizon._path_baseline_marker(self.project, "cancel.txt")
        }
        store.record_dispatch(goal["goal_id"], task, "cancel-prepared")
        store.record_action(goal["goal_id"], task, proposed)
        transaction_id = long_horizon.FileTransaction.new_transaction_id()
        store.prepare_transaction(goal["goal_id"], task, transaction_id, proposed["changes"])
        plans = long_horizon.swarm_work._validated_changes(self.project, proposed["changes"])
        long_horizon.FileTransaction(self.project).prepare(plans, transaction_id=transaction_id)
        # This is the crash boundary FileTransaction.rollback supports: all
        # target bytes reached the after boundary while the manifest is still prepared.
        target.write_bytes(b"after\n")

        cancelled = store.control(goal["goal_id"], "cancel")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertFalse(cancelled["tasks"][0]["pending_transaction"])
        self.assertFalse(cancelled["tasks"][0]["pending_action"])
        self.assertFalse(store.active_overlapping_project(self.project))
        self.assertIn(
            "transaction_rolled_back_for_cancellation",
            [one["type"] for one in store.events(goal["goal_id"])["events"]],
        )

    def test_released_legacy_failed_cancel_cannot_rollback_newer_owner_files(self):
        target = self.project / "legacy-cancel.txt"
        target.write_text("before\n", encoding="utf-8")
        store = self.store()
        legacy = store.create(
            self.board, "project", ["Legacy prepared work"], "legacy-prepared-cancel",
        )
        task = store.claim_ready(legacy["goal_id"], "legacy-worker")[0]
        proposed = action(changes=[{
            "path": "legacy-cancel.txt", "content": "legacy-after\n", "delete": False,
            "reason": "legacy prepared effect",
        }])
        proposed["_nexus_baselines"] = {
            "legacy-cancel.txt": long_horizon._path_baseline_marker(
                self.project, "legacy-cancel.txt",
            )
        }
        store.record_dispatch(legacy["goal_id"], task, "legacy-prepared")
        store.record_action(legacy["goal_id"], task, proposed)
        transaction_id = long_horizon.FileTransaction.new_transaction_id()
        store.prepare_transaction(
            legacy["goal_id"], task, transaction_id, proposed["changes"],
        )
        plans = long_horizon.swarm_work._validated_changes(
            self.project, proposed["changes"],
        )
        long_horizon.FileTransaction(self.project).prepare(
            plans, transaction_id=transaction_id,
        )
        target.write_text("new-owner-output\n", encoding="utf-8")

        def legacy_failed_without_queue(document, _db):
            document["status"] = "failed"
            # Simulate a row migrated by the earlier additive contract, before
            # effectful failed checkpoints were retained as owners.
            document["project_queue"] = store._queue_record(
                "released", document["created_ms"],
            )

        store._mutate(legacy["goal_id"], legacy_failed_without_queue)
        migrated = long_horizon.GoalStore(self.config)
        self.assertEqual(
            migrated.get(legacy["goal_id"])["project_queue"]["state"], "released",
        )
        owner = migrated.create(
            self.board, "project", ["Protect current output"], "new-owner-protects-file",
        )

        with self.assertRaisesRegex(HarnessError, "must wait"):
            migrated.control(legacy["goal_id"], "cancel")

        self.assertEqual(target.read_text(encoding="utf-8"), "new-owner-output\n")
        self.assertEqual(migrated.get(owner["goal_id"])["project_queue"]["state"], "owner")
        held = migrated.get(legacy["goal_id"])["tasks"][0]
        self.assertEqual(held["pending_transaction"]["transaction_id"], transaction_id)

    def test_cancel_accepts_only_a_proven_db_before_filesystem_boundary(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Cancel before prepare"], "cancel-no-manifest")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "not-created.txt", "content": "after\n", "delete": False,
            "reason": "exercise pre-filesystem cancellation",
        }])
        proposed["_nexus_baselines"] = {"not-created.txt": "missing"}
        store.record_dispatch(goal["goal_id"], task, "cancel-before-filesystem")
        store.record_action(goal["goal_id"], task, proposed)
        store.prepare_transaction(
            goal["goal_id"], task, long_horizon.FileTransaction.new_transaction_id(),
            proposed["changes"],
        )

        cancelled = store.control(goal["goal_id"], "cancel")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse((self.project / "not-created.txt").exists())
        self.assertIn(
            "transaction_cancelled_before_prepare",
            [one["type"] for one in store.events(goal["goal_id"])["events"]],
        )

    def test_cancel_settles_an_acknowledged_action_before_file_prepare(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Cancel acknowledged work"], "cancel-action")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "never-applied.txt", "content": "after\n", "delete": False,
            "reason": "exercise acknowledged cancellation",
        }])
        proposed["_nexus_baselines"] = {"never-applied.txt": "missing"}
        store.record_dispatch(goal["goal_id"], task, "cancel-action")
        store.record_action(goal["goal_id"], task, proposed)

        cancelled = store.control(goal["goal_id"], "cancel")

        held = cancelled["tasks"][0]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(held["pending_action"])
        self.assertFalse(held["pending_transaction"])
        self.assertFalse(long_horizon._task_has_unsettled_effect(held))
        self.assertFalse((self.project / "never-applied.txt").exists())
        reopened = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertFalse(long_horizon._task_has_unsettled_effect(reopened["tasks"][0]))
        self.assertIn(
            "provider_effect_cancelled_before_file_apply",
            [one["type"] for one in store.events(goal["goal_id"])["events"]],
        )

    def test_cancel_failure_stays_paused_and_keeps_durable_project_ownership(self):
        target = self.project / "blocked-cancel.txt"
        target.write_text("before\n", encoding="utf-8")
        store = self.store()
        goal = store.create(self.board, "project", ["Do not release uncertain files"], "cancel-blocked")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "blocked-cancel.txt", "content": "after\n", "delete": False,
            "reason": "exercise failed cancellation",
        }])
        proposed["_nexus_baselines"] = {
            "blocked-cancel.txt": long_horizon._path_baseline_marker(
                self.project, "blocked-cancel.txt",
            )
        }
        store.record_dispatch(goal["goal_id"], task, "cancel-blocked")
        store.record_action(goal["goal_id"], task, proposed)
        transaction_id = long_horizon.FileTransaction.new_transaction_id()
        store.prepare_transaction(goal["goal_id"], task, transaction_id, proposed["changes"])
        plans = long_horizon.swarm_work._validated_changes(self.project, proposed["changes"])
        long_horizon.FileTransaction(self.project).prepare(plans, transaction_id=transaction_id)

        with mock.patch.object(
            long_horizon.FileTransaction, "rollback",
            side_effect=HarnessError("injected rollback refusal"),
        ), self.assertRaisesRegex(HarnessError, "Cancellation paused"):
            store.control(goal["goal_id"], "cancel")

        held = store.get(goal["goal_id"])
        self.assertEqual(held["status"], "paused")
        self.assertTrue(held["tasks"][0]["pending_transaction"])
        self.assertEqual(held["tasks"][0]["state"], "blocked")
        self.assertTrue(store.active_overlapping_project(self.project))
        reopened = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertEqual(reopened["status"], "paused")
        self.assertTrue(reopened["tasks"][0]["pending_transaction"])

    def test_cancel_refuses_an_applied_transaction_changed_after_recording(self):
        target = self.project / "applied-cancel.txt"
        target.write_text("before\n", encoding="utf-8")
        store = self.store()
        goal = store.create(self.board, "project", ["Verify before cancellation"], "cancel-applied-mismatch")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "applied-cancel.txt", "content": "after\n", "delete": False,
            "reason": "exercise applied verification",
        }])
        proposed["_nexus_baselines"] = {
            "applied-cancel.txt": long_horizon._path_baseline_marker(
                self.project, "applied-cancel.txt",
            )
        }
        store.record_dispatch(goal["goal_id"], task, "cancel-applied")
        store.record_action(goal["goal_id"], task, proposed)
        transaction_id = long_horizon.FileTransaction.new_transaction_id()
        store.prepare_transaction(goal["goal_id"], task, transaction_id, proposed["changes"])
        plans = long_horizon.swarm_work._validated_changes(self.project, proposed["changes"])
        manifest = long_horizon.FileTransaction(self.project).apply(
            plans, transaction_id=transaction_id,
        )
        artifact = {
            "kind": "file_transaction", "transaction_id": transaction_id,
            "changes": manifest["changes"], "patch": manifest["patch"],
            "patch_sha256": manifest["patch_sha256"],
        }
        store.record_transaction_applied(goal["goal_id"], task, artifact)
        target.write_text("changed later\n", encoding="utf-8")

        with self.assertRaisesRegex(HarnessError, "Cancellation paused"):
            store.control(goal["goal_id"], "cancel")

        held = store.get(goal["goal_id"])
        self.assertEqual(held["status"], "paused")
        self.assertTrue(held["tasks"][0]["pending_transaction"])
        self.assertTrue(store.active_overlapping_project(self.project))

    def test_recovery_verification_mismatch_blocks_only_the_goal(self):
        target = self.project / "recover.txt"
        target.write_text("before\n", encoding="utf-8")
        store = self.store()
        goal = store.create(self.board, "project", ["Recover apply"], "recover-mismatch")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action(changes=[{
            "path": "recover.txt", "content": "applied\n", "delete": False, "reason": "update",
        }])
        proposed["_nexus_baselines"] = {
            "recover.txt": long_horizon._path_baseline_marker(self.project, "recover.txt")
        }
        store.record_dispatch(goal["goal_id"], task, "recover")
        store.record_action(goal["goal_id"], task, proposed)
        transaction_id = long_horizon.FileTransaction.new_transaction_id()
        store.prepare_transaction(goal["goal_id"], task, transaction_id, proposed["changes"])
        plans = long_horizon.swarm_work._validated_changes(self.project, proposed["changes"])
        long_horizon.FileTransaction(self.project).apply(plans, transaction_id=transaction_id)
        target.write_text("edited after crash\n", encoding="utf-8")
        def dead_worker(document, _db):
            document["worker"] = {"pid": 99999999, "token": "dead", "worker_id": "dead"}
        store._mutate(goal["goal_id"], dead_worker)
        recovered = store.recover_dead(goal["goal_id"])
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(recovered["tasks"][0]["state"], "blocked")
        self.assertIn("manual reconciliation", recovered["tasks"][0]["last_error"])
        self.assertEqual(target.read_text(encoding="utf-8"), "edited after crash\n")

    def test_reassign_rejects_pending_and_unknown_provenance(self):
        store = self.store()
        for state in ("pending", "unknown"):
            with self.subTest(state=state):
                goal = store.create(self.board, "project", [state], f"reassign-{state}")
                def block(document, _db):
                    task = document["tasks"][0]
                    task["state"] = "blocked"
                    if state == "pending":
                        task["pending_action"] = action()
                        task["provider_effect_state"] = "acknowledged"
                    else:
                        task["outcome_unknown"] = True
                        task["provider_effect_state"] = "outcome_unknown"
                    document["status"] = "paused"
                store._mutate(goal["goal_id"], block)
                with self.assertRaisesRegex(HarnessError, "Reconcile"):
                    store.control(goal["goal_id"], "reassign", {
                        "task_id": goal["tasks"][0]["id"], "agent_id": "reviewer",
                    })

    def test_user_requested_review_has_authenticated_packet_and_does_not_skip_ready_work(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Still must execute"], "user-review-ready")
        store.control(goal["goal_id"], "pause")
        queued = store.control(goal["goal_id"], "request_review", {
            "task_id": goal["tasks"][0]["id"], "agent_id": "reviewer",
        })
        review = next(one for one in queued["tasks"] if one["kind"] == "review")
        self.assertRegex(review["review_packet_sha256"], r"^[0-9a-f]{64}$")
        leased = store.claim_ready(goal["goal_id"], "review-worker")[0]
        packet_ref = "review-packet:" + leased["review_packet_sha256"]
        store.apply_action(goal["goal_id"], leased, action(
            "complete", evidence=[packet_ref], review_verdict="approve",
            review_findings=["The requested task definition is coherent."],
        ), artifact={"kind": "verified_no_change", "tree_merkle": "reviewed"})
        parent = store.get(goal["goal_id"])["tasks"][0]
        self.assertEqual(parent["state"], "ready")

    def test_broad_review_packet_lists_every_path_without_flattening(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Broad risky change"], "broad-review")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        changes = [
            {"path": f"src/file-{index}.txt", "content": str(index) * 15_000,
             "delete": False, "reason": f"change {index}"}
            for index in range(7)
        ]
        proposed = action("request_review", risk="high", changes=changes)
        self.stage_review(store, goal, task, proposed)
        current = store.get(goal["goal_id"])
        review = next(one for one in current["tasks"] if one["kind"] == "review")
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        context = runtime._agent_context(current, review)
        for index in range(7):
            self.assertIn(f"src/file-{index}.txt", context)
        self.assertIn("read_proposed_change", context)
        self.assertNotIn('"truncated":true,"summary"', context)

    def test_pause_after_tool_request_stops_tools_and_followup_provider_calls(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Explore safely"], "pause-tool-boundary")
        task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action("work", tool_calls=[{
            "call_id": "read-1", "name": "read_file", "arguments": {"path": "missing.txt"},
        }])
        entered = threading.Event()
        release = threading.Event()
        original_ack = runtime.store.acknowledge_context_step
        def delayed_ack(*args, **kwargs):
            original_ack(*args, **kwargs)
            entered.set()
            self.assertTrue(release.wait(5))
        fake_tools = mock.Mock()
        result = {}
        with mock.patch.object(runtime.store, "acknowledge_context_step", side_effect=delayed_ack), \
                mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools", return_value=fake_tools), \
                mock.patch.object(long_horizon.chat_lab, "ask_once") as ask:
            ask.side_effect = lambda *_args, **kwargs: (
                kwargs["before_provider_dispatch"]("initial") or {"text": json.dumps(proposed)}
            )
            worker = threading.Thread(target=lambda: result.update({
                "item": runtime._execute_one(goal["goal_id"], task["id"]),
            }))
            worker.start()
            self.assertTrue(entered.wait(5))
            runtime.store.control(goal["goal_id"], "pause")
            release.set()
            worker.join(5)
        self.assertEqual(ask.call_count, 1)
        fake_tools.execute.assert_not_called()
        self.assertEqual(result["item"][1]["action"], "deferred")

    def test_forbidden_action_change_combinations_never_touch_files(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        forbidden = {
            "ask_user": {"interrupt_reason": "new_authority", "questions": [{
                "id": "q", "prompt": "Proceed?", "multiple": False, "allow_other": True,
                "options": [{"label": "No", "description": "Stop", "recommended": True}],
            }]},
            "delegate": {"tasks": [{
                "title": "child", "description": "child", "assigned_agent_id": "reviewer",
                "depends_on": [], "parallel_safe": True, "resource_paths": [],
            }]},
            "handoff": {"handoff_agent_id": "reviewer"},
            "blocked": {},
        }
        for kind, updates in forbidden.items():
            with self.subTest(kind=kind):
                goal = runtime.store.create(self.board, "project", [kind], f"forbidden-{kind}")
                task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
                proposed = action(kind, changes=[{
                    "path": f"{kind}.txt", "content": "bad\n", "delete": False, "reason": "bad",
                }], **updates)
                with self.assertRaisesRegex(HarnessError, "cannot also change"):
                    runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{
                        "task": task, "action": proposed,
                    }]})
                self.assertFalse((self.project / f"{kind}.txt").exists())
                runtime.store.control(goal["goal_id"], "cancel")

    def test_review_task_is_read_only_even_when_it_returns_a_delete(self):
        target = self.project / "must-stay.txt"
        target.write_text("stay\n", encoding="utf-8")
        store = self.store()
        goal = store.create(self.board, "project", ["Review"], "readonly-review")
        parent = store.claim_ready(goal["goal_id"], "worker")[0]
        self.stage_review(store, goal, parent, action("request_review", risk="high"))
        review = store.claim_ready(goal["goal_id"], "review-worker")[0]
        with self.assertRaisesRegex(HarnessError, "read-only"):
            store.apply_action(goal["goal_id"], review, action(
                "complete", changes=[{
                    "path": "must-stay.txt", "content": "", "delete": True, "reason": "malicious",
                }], review_verdict="approve", review_findings=["delete it"],
                evidence=["review-packet:" + review["review_packet_sha256"]],
            ))
        self.assertEqual(target.read_text(encoding="utf-8"), "stay\n")

    def test_answering_ordinary_question_does_not_fake_risk_rejection_for_other_blocker(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Blocked A", "Question B"], "parallel-answer")
        def prepare(document, _db):
            first, second = document["tasks"]
            first.update({"state": "blocked", "last_error": "Independent blocker"})
            second["assigned_agent_id"] = "reviewer"
        store._mutate(goal["goal_id"], prepare)
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        ids = store.apply_action(goal["goal_id"], task, action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "choice", "prompt": "Which?", "multiple": False, "allow_other": True,
                "options": [{"label": "A", "description": "Use A", "recommended": True}],
            }],
        ))
        current = store.get(goal["goal_id"])
        store.resolve_interrupts(goal["goal_id"], {
            "answers": {ids[0]: "Which?: A"}, "expected_revision": current["revision"],
            "pending_ids": ids,
        })
        answered = store.get(goal["goal_id"])
        self.assertEqual(answered["status"], "queued")
        self.assertNotIn("rejected a risky proposal", answered["note"])

    def test_dependency_manifest_skips_large_dependency_and_build_trees(self):
        for folder in ("node_modules", ".venv", "dist", "build"):
            held = self.project / folder
            held.mkdir()
            (held / "huge.bin").write_bytes(b"x" * 100_000)
        (self.project / "source.py").write_text("print('ok')\n", encoding="utf-8")
        seen = []
        original = long_horizon.swarm_work.file_sha256
        def counting(path):
            seen.append(str(path))
            return original(path)
        with mock.patch.object(long_horizon.swarm_work, "file_sha256", side_effect=counting):
            manifest = long_horizon._project_baseline_manifest(self.project)
        self.assertIn("source.py", manifest)
        self.assertFalse(any(any(folder in path for folder in ("node_modules", ".venv", "dist", "build"))
                             for path in seen))

    def test_cancelled_waiting_goal_cannot_be_revived_by_stale_answer(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Ask once"], "cancel-decision")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        ids = store.apply_action(goal["goal_id"], task, action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "choice", "prompt": "Which?", "multiple": False, "allow_other": True,
                "options": [{"label": "A", "description": "Use A", "recommended": True}],
            }],
        ))
        shown = store.get(goal["goal_id"])
        store.control(goal["goal_id"], "cancel")
        with self.assertRaisesRegex(HarnessError, "terminal goal is immutable"):
            store.resolve_interrupts(goal["goal_id"], {
                "answers": {ids[0]: "Which?: A"}, "expected_revision": shown["revision"],
                "pending_ids": ids,
            })
        cancelled = store.get(goal["goal_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse([one for one in cancelled["interrupts"] if one["state"] == "pending"])
        self.assertTrue(all(one["state"] == "cancelled" for one in cancelled["tasks"]))

    def test_pending_decision_cannot_be_forked(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Ask once"], "fork-decision")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        store.apply_action(goal["goal_id"], task, action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "choice", "prompt": "Which?", "multiple": False, "allow_other": True,
                "options": [{"label": "A", "description": "Use A", "recommended": True}],
            }],
        ))
        fork_root = self.base / "fork"
        fork_root.mkdir()
        with self.assertRaisesRegex(HarnessError, "pending decision"):
            store.clone_to_project(
                store.get(goal["goal_id"]), "fork", "Fork", fork_root, "fork-request",
            )

    def test_progress_fingerprint_ignores_fresh_transaction_ids(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Do useful work"], "semantic-progress")
        for turn in range(long_horizon.MAX_NO_PROGRESS + 1):
            task = store.claim_ready(goal["goal_id"], f"worker-{turn}")[0]
            artifact = {
                "kind": "file_transaction", "transaction_id": f"tx-{turn}",
                "patch_sha256": "same-patch", "changes": [{
                    "path": "same.txt", "before_sha256": "a", "after_sha256": "b",
                    "delete": False,
                }],
            }
            store.apply_action(goal["goal_id"], task, action("work"), artifact=artifact)
            if store.get(goal["goal_id"])["status"] == "paused":
                break
        stopped = store.get(goal["goal_id"])
        self.assertEqual(stopped["status"], "paused")
        current = stopped["tasks"][0]
        self.assertEqual(current["state"], "blocked")
        self.assertLessEqual(current["attempts"], long_horizon.MAX_NO_PROGRESS + 1)

    def test_identical_delegation_is_bounded_per_parent(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Delegate only if useful"], "delegation-loop")
        proposal = action("delegate", tasks=[{
            "title": "Repeat child", "description": "Do exactly the same child work",
            "assigned_agent_id": "reviewer", "depends_on": [], "parallel_safe": True,
            "resource_paths": [],
        }])
        for turn in range(long_horizon.MAX_NO_PROGRESS + 1):
            parent = store.claim_ready(goal["goal_id"], f"parent-{turn}")[0]
            store.apply_action(goal["goal_id"], parent, proposal)
            current = store.get(goal["goal_id"])
            if current["status"] == "paused":
                break
            child_id = current["tasks"][-1]["id"]
            def finish_child(document, _db, child_id=child_id):
                child = next(one for one in document["tasks"] if one["id"] == child_id)
                child["state"] = "complete"
                store._refresh_waiting(document)
            store._mutate(goal["goal_id"], finish_child)
        stopped = store.get(goal["goal_id"])
        self.assertEqual(stopped["status"], "paused")
        self.assertEqual(stopped["tasks"][0]["state"], "blocked")
        self.assertLess(len(stopped["tasks"]), 20)

    def test_pending_effect_blocks_resume_retry_and_message_until_steered(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Apply safely"], "pending-controls")
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        proposed = action("work", changes=[{
            "path": "pending.txt", "content": "new\n", "delete": False, "reason": "work",
        }])
        store.record_dispatch(goal["goal_id"], task, "effect")
        store.record_action(goal["goal_id"], task, proposed)
        transaction_id = long_horizon.FileTransaction.new_transaction_id()
        plans = long_horizon.swarm_work._validated_changes(self.project, proposed["changes"])
        long_horizon.FileTransaction(self.project).prepare(plans, transaction_id)
        store.prepare_transaction(goal["goal_id"], task, transaction_id, proposed["changes"])
        store.fail_pending_apply(goal["goal_id"], "apply failed")
        with self.assertRaisesRegex(HarnessError, "pending provider/file effect"):
            store.control(goal["goal_id"], "retry", {"task_id": task["id"]})
        with self.assertRaisesRegex(HarnessError, "pending provider/file effects"):
            store.control(goal["goal_id"], "resume")
        with self.assertRaisesRegex(HarnessError, "pending provider/file effect"):
            store.control(goal["goal_id"], "message", {"task_id": task["id"], "text": "Continue"})
        steered = store.control(goal["goal_id"], "steer", {"task_id": task["id"], "text": "Use a fresh plan"})
        refreshed = next(one for one in steered["tasks"] if one["id"] == task["id"])
        self.assertEqual(refreshed["state"], "ready")
        self.assertFalse(refreshed["pending_action"])
        self.assertFalse(refreshed["pending_transaction"])

    def test_stale_decision_is_rejected_synchronously_before_background_resume(self):
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Ask once"], "stale-sync")
        task = runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        ids = runtime.store.apply_action(goal["goal_id"], task, action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "choice", "prompt": "Which?", "multiple": False, "allow_other": True,
                "options": [{"label": "A", "description": "Use A", "recommended": True}],
            }],
        ))
        with mock.patch.object(runtime, "start_background") as start:
            with self.assertRaisesRegex(HarnessError, "changed after this decision card"):
                runtime.resume(goal["goal_id"], {
                    "answers": {ids[0]: "Which?: A"}, "expected_revision": 1,
                    "pending_ids": ids,
                })
        start.assert_not_called()
        self.assertEqual(runtime.store.get(goal["goal_id"])["status"], "waiting_for_user")

    def test_steer_supersedes_pending_review_chain(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Original"], "steer-review")
        parent = store.claim_ready(goal["goal_id"], "worker")[0]
        self.stage_review(store, goal, parent, action("request_review", risk="high"))
        current = store.get(goal["goal_id"])
        review = next(one for one in current["tasks"] if one["kind"] == "review")
        store.claim_ready(goal["goal_id"], "review-worker")
        steered = store.control(goal["goal_id"], "steer", {
            "task_id": parent["id"], "text": "Change the objective now",
        })
        new_parent = next(one for one in steered["tasks"] if one["id"] == parent["id"])
        new_review = next(one for one in steered["tasks"] if one["id"] == review["id"])
        self.assertEqual(new_parent["state"], "ready")
        self.assertFalse(new_parent["pending_action"])
        self.assertEqual(new_review["state"], "cancelled")

    def test_review_reassignment_preserves_provider_independence(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Review independently"], "review-reassign")
        parent = store.claim_ready(goal["goal_id"], "worker")[0]
        self.stage_review(store, goal, parent, action("request_review", risk="high"))
        review = next(one for one in store.get(goal["goal_id"])["tasks"] if one["kind"] == "review")
        for forbidden in ("lead", "same-route"):
            with self.subTest(agent=forbidden):
                with self.assertRaisesRegex(HarnessError, "different provider identity"):
                    store.control(goal["goal_id"], "reassign", {
                        "task_id": review["id"], "agent_id": forbidden,
                    })

    def test_repeated_single_agent_risk_question_is_bounded(self):
        self.board["agents"] = [self.board["agents"][0]]
        self.board["works_on"] = [self.board["works_on"][0]]
        store = self.store()
        goal = store.create(self.board, "project", ["Risky change"], "risk-question-loop")
        proposed = action("request_review", risk="high")
        for turn in range(long_horizon.MAX_NO_PROGRESS + 1):
            task = store.claim_ready(goal["goal_id"], f"worker-{turn}")[0]
            store.record_dispatch(goal["goal_id"], task, f"effect-{turn}")
            store.record_action(goal["goal_id"], task, proposed)
            staged, ids = store.stage_review_if_needed(goal["goal_id"], task, proposed)
            self.assertTrue(staged)
            current = store.get(goal["goal_id"])
            if not ids:
                break
            store.resolve_interrupts(goal["goal_id"], {
                "answers": {ids[0]: "Stop this task"},
                "expected_revision": current["revision"], "pending_ids": ids,
            })
            store.control(goal["goal_id"], "resume")
        stopped = store.get(goal["goal_id"])
        self.assertEqual(stopped["status"], "paused")
        self.assertEqual(stopped["tasks"][0]["state"], "blocked")
        self.assertLessEqual(stopped["tasks"][0]["attempts"], long_horizon.MAX_NO_PROGRESS + 1)

    def test_improved_single_agent_risk_proposal_resets_repeat_counter(self):
        self.board["agents"] = [self.board["agents"][0]]
        self.board["works_on"] = [self.board["works_on"][0]]
        store = self.store()
        goal = store.create(self.board, "project", ["Improve risky work"], "changing-risk")
        for turn in range(long_horizon.MAX_NO_PROGRESS + 2):
            task = store.claim_ready(goal["goal_id"], f"worker-{turn}")[0]
            proposed = action(
                "request_review", risk="high", summary=f"Improved proposal {turn}",
                evidence=[f"new evidence {turn}"],
            )
            store.record_dispatch(goal["goal_id"], task, f"effect-{turn}")
            store.record_action(goal["goal_id"], task, proposed)
            staged, ids = store.stage_review_if_needed(goal["goal_id"], task, proposed)
            self.assertTrue(staged)
            self.assertTrue(ids)
            current = store.get(goal["goal_id"])
            self.assertEqual(current["tasks"][0].get("question_repeat_count"), 0)
            store.resolve_interrupts(goal["goal_id"], {
                "answers": {ids[0]: "Stop this task"},
                "expected_revision": current["revision"], "pending_ids": ids,
            })
            store.control(goal["goal_id"], "resume")
        self.assertNotEqual(store.get(goal["goal_id"])["status"], "paused")


if __name__ == "__main__":
    unittest.main()
