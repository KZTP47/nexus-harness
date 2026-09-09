from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from our_harness import long_horizon
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def reply(kind="complete", summary="I checked the current result and agree it is complete.", **updates):
    answer = {
        "action": kind, "summary": summary, "evidence": ["Inspected the current project"],
        "risk": "low", "changes": [], "needs_files": [], "tasks": [],
        "handoff_agent_id": "", "questions": [],
        "criteria_evidence": [{
            "criterion": "Original objective is satisfied",
            "evidence_refs": ["verified-no-change"],
        }],
    }
    answer.update(updates)
    return answer


def change(path, content):
    return {"path": path, "content": content, "delete": False, "reason": "Implement the shared objective"}


class LongHorizonDialogueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "arbitrary user game"
        self.authority = self.base / "installation"
        self.project.mkdir()
        self.authority.mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "builder-route": {"kind": "openai", "model": "fixture", "endpoint": "http://127.0.0.1/one", "api_key_env": "FIXTURE_KEY"},
            "peer-route": {"kind": "anthropic", "model": "fixture", "endpoint": "http://127.0.0.1/two", "api_key_env": "FIXTURE_KEY"},
        }
        self.config = LoadedConfig(data, self.authority, [], {})
        self.board = {
            "agents": [
                {"id": "builder", "name": "Ada", "who": "builder-route", "ready": True},
                {"id": "peer", "name": "Lin", "who": "peer-route", "ready": True},
            ],
            "projects": [{"id": "game", "name": "Game", "path": str(self.project), "is_there": True}],
            "works_on": [{"agent": name, "project": "game"} for name in ("builder", "peer")],
        }
        patcher = mock.patch.object(long_horizon, "_base", return_value=self.base / "state")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)

    def create(self, request="dialogue", **options):
        return self.runtime.store.create(
            self.board, "game", ["Build and verify a small interactive game"], request,
            lead_id="builder", participant_ids=["builder", "peer"],
            conversation_id="chat-" + request, **options,
        )

    def provider(self, responses, seen):
        def ask(_config, route, _text, **kwargs):
            seen.append((route, kwargs["context"]))
            kwargs["before_provider_dispatch"]("initial")
            answer = responses(len(seen), route, kwargs) if callable(responses) else responses[len(seen) - 1]
            if isinstance(answer, Exception):
                raise answer
            kwargs["after_provider_response"]("initial")
            return {"text": json.dumps(answer)}
        return ask

    def run_replies(self, goal, responses, *, verification_result=None):
        seen = []
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=self.provider(responses, seen)), mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification",
            return_value=verification_result or {"status": "passed", "basis": "separate deterministic fixture check"},
        ):
            result = self.runtime.run(goal["goal_id"])
        return result, seen

    def test_real_file_iterations_alternate_and_peer_changes_reopen_agreement(self):
        goal = self.create()
        result, seen = self.run_replies(goal, [
            reply(summary="Lin, I built the game; check the controls.", changes=[change("game.js", "export const lives = 1;\n")],
                  criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]),
            reply(summary="Ada, I fixed the lives count. Please inspect my change.", changes=[change("game.js", "export const lives = 3;\n")],
                  criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]),
            reply(summary="Lin, I checked your lives fix and agree the current game is ready."),
        ])
        self.assertEqual([route for route, _ in seen], ["builder-route", "peer-route", "builder-route"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual((self.project / "game.js").read_text(), "export const lives = 3;\n")
        self.assertIn("I fixed the lives count", seen[2][1])
        self.assertLess(seen[2][1].rfind("I built the game"), seen[2][1].rfind("I fixed the lives count"))
        self.assertEqual(result["dialogue"]["artifact_generation"], 2)
        self.assertEqual([one["agent_id"] for one in result["dialogue"]["messages"]], ["builder", "peer", "builder"])
        self.assertTrue(all(one["agreed_artifact_generation"] == 2 for one in result["tasks"]))
        events = self.runtime.store.events(goal["goal_id"])["events"]
        speech = [one for one in events if one["type"] == "provider_acknowledged"]
        self.assertEqual([one["payload"]["summary_delivery"]["agent_id"] for one in speech], ["peer", "builder", "peer"])

    def test_work_without_files_yields_to_peer_instead_of_monopolizing_goal(self):
        goal = self.create()
        result, seen = self.run_replies(goal, [
            reply("work", "Lin, inspect the browser controls while I investigate the input."),
            reply("work", "Ada, the input handler needs a default state."),
            reply(summary="Lin, I implemented the default state.", changes=[change("input.js", "export const input = {};\n")],
                  criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:input.js"]}]),
            reply(summary="Ada, the current input is correct and the result is complete."),
        ])
        self.assertEqual(result["status"], "complete")
        self.assertEqual([route for route, _ in seen], ["builder-route", "peer-route"] * 2)
        self.assertIn("input handler needs a default state", seen[2][1])

    def test_larger_existing_team_uses_cyclic_turns_without_starving_third_member(self):
        self.board["agents"].append({
            "id": "third", "name": "Sam", "who": "builder-route", "ready": True,
        })
        self.board["works_on"].append({"agent": "third", "project": "game"})
        goal = self.runtime.store.create(
            self.board, "game", ["Inspect the existing game together"], "three-members",
            lead_id="builder", participant_ids=["builder", "peer", "third"],
            conversation_id="chat-three",
        )
        result, seen = self.run_replies(goal, [
            reply("work", "I inspected input."), reply("work", "I inspected display."),
            reply("work", "I inspected scoring."), reply(), reply(), reply(),
        ])
        self.assertEqual(result["status"], "complete")
        self.assertEqual([one["agent_id"] for one in result["dialogue"]["messages"]],
                         ["builder", "peer", "third"] * 2)

    def test_context_file_request_summary_is_visible_before_terminal_reply(self):
        (self.project / "input.js").write_text("export const input = {};\n")
        goal = self.create()
        result, seen = self.run_replies(goal, [
            reply("work", "Lin, I am reading the input handler now.", needs_files=["input.js"]),
            reply(summary="Lin, the input handler is correct."),
            reply(summary="Ada, I agree with your input inspection."),
        ])
        self.assertEqual(result["status"], "complete")
        summaries = [one["payload"]["summary"] for one in self.runtime.store.events(goal["goal_id"])["events"]
                     if one["type"] == "provider_acknowledged"]
        self.assertEqual(len(summaries), 3)
        self.assertEqual(summaries[0], "Lin, I am reading the input handler now.")
        self.assertIn("I am reading the input handler now", seen[1][1])
        self.assertIn("I am reading the input handler now", seen[2][1])

    def test_context_tool_summary_publishes_with_real_tool_result_and_reaches_peer(self):
        goal = self.create()
        fake_tools = mock.Mock()
        fake_tools.execute.return_value = {"matches": [{"path": "game.js", "line": 1}]}
        with mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools", return_value=fake_tools):
            result, seen = self.run_replies(goal, [
                reply("work", "Lin, I will search for the game input now.", tool_calls=[{
                    "call_id": "input-search", "name": "search_workspace",
                    "arguments": {"query": "input", "max_results": 8},
                }]),
                reply(summary="Lin, the current game input is consistent."),
                reply(summary="Ada, I checked your search and agree."),
            ])
        self.assertEqual(result["status"], "complete")
        fake_tools.execute.assert_called_once()
        self.assertIn("CONTEXT TOOL RESULTS", seen[1][1])
        self.assertIn("I will search for the game input now", seen[2][1])
        self.assertEqual(len(result["dialogue"]["messages"]), 3)

    def test_real_context_tools_read_files_and_discard_old_results_after_peer_edit(self):
        (self.project / "game.js").write_text("export const marker = 'OLD_OBSERVATION_67';\n")
        goal = self.create()
        result, seen = self.run_replies(goal, [
            reply("work", "Lin, I am inspecting the game source.", tool_calls=[{
                "call_id": "read-game", "name": "read_file", "arguments": {
                    "path": "game.js", "start_line": 1, "end_line": 20, "max_bytes": 4_000,
                },
            }]),
            reply(summary="Lin, I inspected the current source."),
            reply(summary="Ada, I updated the source; check the new state.",
                  changes=[change("game.js", "export const marker = 'NEW_OBSERVATION_83';\n")],
                  criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]),
            reply(summary="Lin, I re-read your new source and agree."),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertIn("OLD_OBSERVATION_67", seen[1][1])
        self.assertNotIn("CONTEXT TOOL RESULTS", seen[3][1])
        current_files = seen[3][1].split("REQUESTED FILE CONTENTS\n", 1)[1].split("\n\nUSER STEERING", 1)[0]
        self.assertNotIn("OLD_OBSERVATION_67", current_files)
        self.assertIn("NEW_OBSERVATION_83", current_files)
        self.assertIn("CONTEXT FRESHNESS", seen[3][1])
        builder = next(one for one in result["tasks"] if one["assigned_agent_id"] == "builder")
        self.assertEqual(builder["context_steps"][0]["state"], "superseded")
        self.assertEqual(builder["context_steps"][0]["context_binding"]["schema_version"], 6)

    def test_real_selected_verification_tool_receives_existing_artifacts_and_project_commands(self):
        self.board["projects"][0]["test_commands"] = [["fixture-runner", "checks/game.js"]]
        goal = self.create()
        responses = [
            reply(summary="Lin, the game source is ready for your tests.", changes=[change("game.js", "export const lives = 3;\n")],
                  criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]),
            reply("work", "Ada, I am running the project verification.", tool_calls=[{
                "call_id": "verify-game", "name": "run_selected_verification", "arguments": {},
            }]),
            reply(summary="Ada, the current game passes verification and I agree."),
        ]
        seen, verifications, profiles = [], [], []
        def verify(_config, root, project, _goal, changed, *_args, **_kwargs):
            verifications.append((root, project, list(changed)))
            profiles.append(_kwargs.get("verification_profile"))
            return {"status": "passed", "basis": "fixture verifier boundary"}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=self.provider(responses, seen)), mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification", side_effect=verify,
        ):
            result = self.runtime.run(goal["goal_id"])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(verifications), 2)
        self.assertEqual(profiles, ["shared_goal_v1", "shared_goal_v1"])
        self.assertTrue(all(changed == ["game.js"] for _, _, changed in verifications))
        self.assertTrue(all(project["test_commands"] == [["fixture-runner", "checks/game.js"]]
                            for _, project, _ in verifications))
        self.assertIn("fixture verifier boundary", seen[2][1])

    def test_project_verification_approval_survives_goal_and_request_replay_is_bound(self):
        project = self.board["projects"][0]
        project["test_commands"] = [["runner", "tests/check.py"]]
        project["approved_test_command_digest"] = "a" * 64
        arguments = {
            "lead_id": "builder", "participant_ids": ["builder", "peer"],
            "conversation_id": "chat-approved",
        }
        admission = self.runtime.store.inspect_runtime_admission(
            self.board, "game", ["Run the selected checks"], "approved", **arguments,
        )
        goal = self.runtime.store.create(
            self.board, "game", ["Run the selected checks"], "approved",
            admission_digest=admission["admission_digest"], **arguments,
        )
        held = long_horizon.verification_project(self.config, goal)
        self.assertEqual(held["test_commands"], project["test_commands"])
        self.assertEqual(held["approved_test_command_digest"], "a" * 64)
        replay = self.runtime.store.inspect_runtime_admission(
            self.board, "game", ["Run the selected checks"], "approved", **arguments,
        )
        self.assertFalse(replay["conflict"])
        project["test_commands"] = [["different-runner", "tests/new-check.py"]]
        changed = self.runtime.store.inspect_runtime_admission(
            self.board, "game", ["Run the selected checks"], "approved", **arguments,
        )
        self.assertTrue(changed["conflict"])
        self.assertNotEqual(changed["admission_digest"], admission["admission_digest"])

    def test_explicit_resume_refreshes_checks_and_reverifies_without_replaying_completed_agents(self):
        goal = self.create("refresh-checks")
        paused, _ = self.run_replies(goal, [
            reply(summary="Lin, verify the game source.", changes=[change("game.js", "export const lives = 3;\n")],
                  criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]),
            reply("work", "Ada, I am checking the test setup.", tool_calls=[{
                "call_id": "initial-check", "name": "run_selected_verification", "arguments": {},
            }]),
            reply(summary="Ada, the files are complete; the test command needs configuration."),
        ], verification_result={"status": "unavailable", "basis": "discovered", "reason": "No configured check", "commands": []})
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(all(one["state"] == "complete" for one in paused["tasks"]))
        old_binding = long_horizon._context_binding(paused)
        selected = copy.deepcopy(self.board["projects"][0])
        selected["test_commands"] = [["selected-runner", "checks/game.js"]]
        with mock.patch.object(self.runtime, "start_background") as background:
            resumed = self.runtime.resume(goal["goal_id"], project_verification_settings=selected)
        background.assert_called_once_with(goal["goal_id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["verification_settings_revision"], 2)
        self.assertEqual(resumed["verification"]["status"], "not_run")
        self.assertEqual(resumed["artifacts"], paused["artifacts"])
        self.assertEqual(resumed["dialogue"], paused["dialogue"])
        self.assertEqual(resumed["conversation_id"], paused["conversation_id"])
        self.assertNotEqual(old_binding, long_horizon._context_binding(resumed))
        peer = next(one for one in resumed["tasks"] if one["assigned_agent_id"] == "peer")
        self.assertEqual(peer["context_steps"][0]["state"], "superseded")
        reopened = long_horizon.GoalStore(self.config)
        self.assertEqual(reopened.get(goal["goal_id"])["verification_contract"], resumed["verification_contract"])
        observed = []
        def verify(_config, _root, project, *_args, **_kwargs):
            observed.append(project["test_commands"])
            return {"status": "passed", "basis": "configured checks after restart"}
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as providers, mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification", side_effect=verify,
        ):
            complete = self.runtime.run(goal["goal_id"])
        self.assertEqual(complete["status"], "complete")
        providers.assert_not_called()
        self.assertEqual(observed, [selected["test_commands"]])
        updated = [event for event in reopened.events(goal["goal_id"])["events"] if event["type"] == "verification_settings_updated"]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["payload"]["trigger"], "explicit_resume")
        self.assertNotIn("selected-runner", json.dumps(updated[0]["payload"]))

    def test_resume_adopts_only_current_discovery_approval_and_rejects_changed_manifest(self):
        manifest = self.project / "package.json"
        manifest.write_text(json.dumps({"name": "fixture", "scripts": {"test": "node --test game.test.js"}}))
        goal = self.create("approve-checks")
        self.runtime.store.control(goal["goal_id"], "pause")
        selected = copy.deepcopy(self.board["projects"][0])
        commands, source = long_horizon.swarm_work._verification_commands(self.config, self.project, selected)
        self.assertEqual(source, "discovered")
        self.assertTrue(commands)
        selected["approved_test_command_digest"] = long_horizon.swarm_work._command_approval_digest(
            self.project, commands, declared_path=selected["path"],
        )
        resumed = self.runtime.store.control(goal["goal_id"], "resume", project_verification_settings=selected)
        self.assertEqual(resumed["verification_contract"]["approved_test_command_digest"], selected["approved_test_command_digest"])
        self.runtime.store.control(goal["goal_id"], "pause")
        before = self.runtime.store.get(goal["goal_id"])
        manifest.write_text(json.dumps({"name": "fixture", "version": "2.0.0", "scripts": {"test": "node --test game.test.js"}}))
        with self.assertRaisesRegex(HarnessError, "checks changed"):
            self.runtime.store.control(goal["goal_id"], "resume", project_verification_settings=selected)
        after = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(after["status"], "paused")
        self.assertEqual(after["verification_contract"], before["verification_contract"])
        self.assertEqual(after["revision"], before["revision"])
        selected["approved_test_command_digest"] = long_horizon.swarm_work._command_approval_digest(
            self.project, commands, declared_path=selected["path"],
        )
        accepted = self.runtime.store.control(goal["goal_id"], "resume", project_verification_settings=selected)
        self.assertEqual(accepted["verification_settings_revision"], 3)

    def test_resume_settings_cannot_cross_project_provider_or_live_worker_boundaries(self):
        goal = self.create("settings-boundaries")
        self.runtime.store.control(goal["goal_id"], "pause")
        selected = copy.deepcopy(self.board["projects"][0])
        selected["test_commands"] = [["selected-runner", "checks"]]
        other = self.base / "other project"
        other.mkdir()
        for wrong in ({**selected, "id": "other"}, {**selected, "path": str(other)}):
            with self.assertRaisesRegex(HarnessError, "exact selected project"):
                self.runtime.resume(goal["goal_id"], project_verification_settings=wrong)
        self.config.data["providers"]["builder-route"]["model"] = "changed-provider"
        with self.assertRaises(HarnessError):
            self.runtime.resume(goal["goal_id"], project_verification_settings=selected)
        self.config.data["providers"]["builder-route"]["model"] = "fixture"
        self.runtime.store.control(goal["goal_id"], "resume")
        self.runtime.store.claim_ready(goal["goal_id"], "still-working")
        self.runtime.store.control(goal["goal_id"], "pause")
        with self.assertRaisesRegex(HarnessError, "finish pausing"):
            self.runtime.resume(goal["goal_id"], project_verification_settings=selected)
        held = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(held["status"], "paused")
        self.assertEqual(held["verification_settings_revision"], 1)

    def test_resume_never_imports_foreign_host_commands_and_clears_unused_discovery_approval(self):
        self.config.data["project"]["test_commands"] = [["host-only-runner", "wrong-repository"]]
        goal = self.create("foreign-command-resume")
        self.runtime.store.control(goal["goal_id"], "pause")
        selected = copy.deepcopy(self.board["projects"][0])
        selected["test_commands"] = [["selected-runner", "checks"]]
        selected["approved_test_command_digest"] = "a" * 64
        resumed = self.runtime.store.control(goal["goal_id"], "resume", project_verification_settings=selected)
        held = resumed["verification_contract"]
        self.assertEqual(held["test_commands"], selected["test_commands"])
        self.assertEqual(held["approved_test_command_digest"], "")
        self.assertFalse(held["uses_project_config"])
        self.assertNotIn("host-only-runner", json.dumps(held))

    def test_fork_preserves_explicit_custom_runner_evidence_after_restart(self):
        from our_harness.goal_verification import verification_project
        selected = self.board["projects"][0]
        selected["test_commands"] = [["selected-runner", "quality_check.py"]]
        selected["test_evidence_contracts"] = [{
            "command": selected["test_commands"][0],
            "total_field": "checks.executed", "failed_field": "checks.failed",
        }]
        self.config.data["project"]["test_evidence_contracts"] = [{"total_field": "host_only"}]
        source = self.create("fork-explicit-evidence")
        target = self.base / "isolated explicit project"
        target.mkdir()
        fork = self.runtime.store.clone_to_project(source, "forked", "Forked", target, "explicit-fork")
        reopened = long_horizon.GoalStore(self.config).get(fork["goal_id"])
        restored = verification_project(self.config, reopened)
        self.assertEqual(restored["test_commands"], selected["test_commands"])
        self.assertEqual(restored["test_evidence_contracts"], selected["test_evidence_contracts"])
        self.assertNotIn("host_only", json.dumps(restored))
        self.assertNotEqual(fork["verification_contract"]["fingerprint_sha256"], source["verification_contract"]["fingerprint_sha256"])

    def test_fork_captures_same_root_configured_runner_evidence(self):
        from our_harness.goal_verification import verification_project
        data = copy.deepcopy(self.config.data)
        data["project"]["test_commands"] = [["project-runner", "quality_check.py"]]
        data["project"]["test_evidence_contracts"] = [{
            "command": data["project"]["test_commands"][0],
            "total_field": "checks.executed", "failed_field": "checks.failed",
        }]
        owning = LoadedConfig(data, self.project, [], {})
        store = long_horizon.GoalStore(owning)
        source = store.create(
            self.board, "game", ["Build and verify a game"], "fork-configured-evidence",
            lead_id="builder", participant_ids=["builder", "peer"],
        )
        target = self.base / "isolated configured project"
        target.mkdir()
        fork = store.clone_to_project(source, "forked", "Forked", target, "configured-fork")
        restored = verification_project(owning, fork)
        self.assertEqual(restored["test_commands"], data["project"]["test_commands"])
        self.assertEqual(restored["test_evidence_contracts"], data["project"]["test_evidence_contracts"])
        self.assertFalse(fork["verification_contract"]["uses_project_config"])
        data["project"]["test_evidence_contracts"][0]["total_field"] = "changed_original"
        self.assertEqual(verification_project(owning, fork)["test_evidence_contracts"], restored["test_evidence_contracts"])

    def test_fork_requires_fresh_discovered_command_approval_for_its_root(self):
        from our_harness.goal_verification import verification_project, run_configured_goal_verification
        manifest = json.dumps({"name": "fixture", "scripts": {"test": "node --test game.test.js"}})
        (self.project / "package.json").write_text(manifest)
        selected = self.board["projects"][0]
        commands, source_kind = long_horizon.swarm_work._verification_commands(self.config, self.project, selected)
        self.assertEqual(source_kind, "discovered")
        selected["approved_test_command_digest"] = long_horizon.swarm_work._command_approval_digest(
            self.project, commands, declared_path=selected["path"],
        )
        source = self.create("fork-discovered-approval")
        target = self.base / "isolated approval project"
        target.mkdir()
        (target / "package.json").write_text(manifest)
        fork = self.runtime.store.clone_to_project(source, "forked", "Forked", target, "approval-fork")
        restored = verification_project(self.config, fork)
        self.assertEqual(restored["approved_test_command_digest"], "")
        with mock.patch.object(long_horizon.swarm_work, "_run_disposable_verification_command") as runner:
            checked = run_configured_goal_verification(
                self.config, target, restored, fork["objective"], ["game.js"],
            )
        runner.assert_not_called()
        self.assertEqual(checked["basis"], "discovered_command_approval_required")
        self.assertNotEqual(checked["approval_digest"], selected["approved_test_command_digest"])

    def test_cancelled_dialogue_never_dispatches_another_peer(self):
        goal = self.create()
        self.runtime.store.control(goal["goal_id"], "cancel")
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as dispatch:
            result = self.runtime.run(goal["goal_id"])
        self.assertEqual(result["status"], "cancelled")
        dispatch.assert_not_called()

    def test_later_turn_sees_exact_verification_failure_output(self):
        goal = self.create()
        self.runtime.store._mutate(goal["goal_id"], lambda document, _db: document.update({
            "verification": {"status": "failed", "reason": "Input check failed", "commands": [{
                "command": ["runner", "checks"], "stderr": "specific failing assertion: lives must be 3",
            }]},
        }))
        saved = self.runtime.store.get(goal["goal_id"])
        context = self.runtime._agent_context(saved, saved["tasks"][0])
        self.assertIn("specific failing assertion: lives must be 3", context)

    def test_failure_stays_explicit_and_other_participant_still_gets_a_turn(self):
        goal = self.create()
        result, seen = self.run_replies(goal, [HarnessError("Fixture provider unavailable"), reply()])
        self.assertEqual([route for route, _ in seen], ["builder-route", "peer-route"])
        self.assertEqual(result["status"], "paused")
        self.assertNotEqual(result["tasks"][0]["state"], "complete")
        self.assertIn("Fixture provider unavailable", seen[1][1])
        self.assertEqual([one["agent_id"] for one in result["dialogue"]["messages"]], ["peer"])

    def test_identical_no_progress_conversation_stops_before_global_budget(self):
        goal = self.create(policy={"max_provider_calls": 80})
        result, seen = self.run_replies(goal, lambda number, _route, _kwargs: reply(
            "work", "I am still planning the same unchanged step.",
        ))
        self.assertEqual(result["status"], "paused")
        self.assertLessEqual(len(seen), 2 * (long_horizon.MAX_NO_PROGRESS + 1))
        self.assertLess(result["budget"]["provider_calls"], result["budget"]["max_provider_calls"])
        self.assertIn("no new evidence", result["note"])

    def test_distinct_public_discussion_continues_without_artificial_file_edits(self):
        goal = self.create("useful-design-conversation")
        discussion = [
            "We should persist reward balance so purchases survive reload.",
            "The inventory should also survive reload; equipment refers to owned item IDs.",
            "Purchase validation must reject already owned items before charging balance.",
            "Agreed. Failed purchases should retain the existing inventory and balance.",
            "Equipment must be validated against ownership before applying an effect.",
            "Effects should derive from the equipped item to avoid stacking on reload.",
            "An unequip action should restore base movement and score multipliers.",
            "A changed catalog needs to tolerate saved item IDs that are no longer present.",
            "We can retain ownership history while ignoring unavailable effects.",
            "The UI should state the missing item clearly when restoring equipment.",
            "The purchase button can show the exact missing balance without disabling inspection.",
            "The selected architecture now covers persistence and catalog migration.",
        ]
        result, seen = self.run_replies(goal, [
            *(reply("work", message) for message in discussion), reply(), reply(),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(seen), len(discussion) + 2)
        self.assertFalse(any(one.get("kind") == "file_transaction" for one in result["artifacts"]))

    def test_new_tool_observations_extend_work_but_new_envelopes_do_not(self):
        for distinct in (True, False):
            with self.subTest(distinct=distinct):
                goal = self.create("observations-" + str(distinct))
                observed = []
                fake_tools = mock.Mock()
                def tool_result(_agent, call, *, execution_scope=""):
                    observed.append(call["call_id"])
                    number = len(observed)
                    return {
                        "call_id": call["call_id"], "span_id": "span-" + str(number),
                        "elapsed_ms": number * 11, "notice": "Tool call " + str(number),
                        "content_bytes": number * 10, "content_sha256": "envelope-" + str(number),
                        "content": json.dumps({"finding": "item-" + str(number) if distinct else "same-result"}),
                    }
                fake_tools.execute.side_effect = tool_result
                def responses(number, _route, _kwargs):
                    if number == 19:
                        return reply()
                    if number % 3 == 1:
                        return reply("work", "I will inspect the next relevant source.", tool_calls=[{
                            "call_id": "inspect-" + str(number), "name": "search_workspace",
                            "arguments": {"query": "input", "max_results": 8},
                        }])
                    if number % 3 == 2:
                        return reply("work", "I inspected the requested source; please check my observation.")
                    return reply()
                with mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools", return_value=fake_tools):
                    result, seen = self.run_replies(goal, responses)
                if distinct:
                    self.assertEqual(result["status"], "complete", result["note"])
                    self.assertEqual(len(seen), 19)
                else:
                    self.assertEqual(result["status"], "paused")
                    self.assertLess(len(seen), 19)
                    self.assertIn("no new evidence", result["note"])

    def test_verifier_timing_changes_are_not_progress_but_changed_results_are(self):
        def result(milliseconds, count=1):
            return {"status": "passed", "commands": [{
                "exit_code": 0, "duration_ms": milliseconds,
                "stdout": f"Ran {count} test in 0.{milliseconds:03d}s\nOK\n# duration_ms {milliseconds}\n",
            }]}
        normalize = lambda value: long_horizon._semantic_tool_result(value, verification=True)
        self.assertEqual(normalize(result(14)), normalize(result(31)))
        self.assertNotEqual(normalize(result(14)), normalize(result(14, count=2)))
        self.assertNotEqual(
            long_horizon._semantic_tool_result({"stdout": "Ran 1 test in 0.014s\n"}),
            long_horizon._semantic_tool_result({"stdout": "Ran 1 test in 0.031s\n"}),
        )

    def test_restart_preserves_transcript_and_next_participant(self):
        goal = self.create()
        claimed = self.runtime.store.claim_ready(goal["goal_id"], "fixture-first-turn")[0]
        first = reply("work", "Lin, inspect the saved state after this restart.")
        self.runtime.store.record_dispatch(goal["goal_id"], claimed, "fixture-digest")
        self.runtime.store.record_provider_reply(goal["goal_id"], claimed, phase="initial")
        self.runtime.store.record_action(goal["goal_id"], claimed, first)
        self.runtime.store.apply_action(goal["goal_id"], claimed, first)
        self.runtime.store.release_scheduler(goal["goal_id"], "fixture-first-turn")
        reopened = long_horizon.GoalStore(self.config)
        saved = reopened.get(goal["goal_id"])
        self.assertEqual(saved["dialogue"]["messages"][0]["summary"], first["summary"])
        next_task = reopened.claim_ready(goal["goal_id"], "fixture-after-restart")[0]
        self.assertEqual(next_task["assigned_agent_id"], "peer")
        self.assertIn(first["summary"], self.runtime._agent_context(saved, next_task))

    def test_changed_dialogue_contract_is_inspectable_but_never_dispatched(self):
        goal = self.create()
        self.runtime.store._mutate(goal["goal_id"], lambda document, _db: document["dialogue"].update({
            "contract_fingerprint_sha256": "0" * 64,
        }))
        reopened = long_horizon.GoalStore(self.config)
        self.assertTrue(reopened.public(reopened.get(goal["goal_id"]))["collaboration_contract_changed"])
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as dispatch:
            with self.assertRaisesRegex(HarnessError, "collaboration|conversation|scheduler"):
                self.runtime.run(goal["goal_id"])
            dispatch.assert_not_called()

    def test_steering_reopens_both_agreements_and_discards_old_provider_reply(self):
        goal = self.create()
        def responses(number, _route, _kwargs):
            if number == 1:
                return reply(summary="Lin, the initial result is ready.")
            if number == 2:
                self.runtime.store.control(goal["goal_id"], "steer", {"text": "Use a blue theme and never write stale.js"})
                return reply(changes=[change("stale.js", "old unwanted proposal")])
            return reply(summary="The shared result respects the blue theme and steering.")
        result, seen = self.run_replies(goal, responses)
        self.assertEqual(result["status"], "complete")
        self.assertFalse((self.project / "stale.js").exists())
        self.assertTrue(all("Use a blue theme" in context for _, context in seen[2:]))
        self.assertEqual(result["objective_epoch"], 2)
        self.assertTrue(all(one["agreed_artifact_generation"] == 1 for one in result["tasks"]))
        self.assertFalse(any("old unwanted proposal" in one["summary"] for one in result["dialogue"]["messages"]))


if __name__ == "__main__":
    unittest.main()
