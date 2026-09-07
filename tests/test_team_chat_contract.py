from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import chat, swarm_work
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.goal_verification import capture_verification_contract, verification_project
from our_harness.models import HarnessError
from our_harness.server import HarnessHTTPServer


class TeamChatContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nexus-team-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.project = self.root / "another user's game"
        self.project.mkdir()

    def goal(self, **project_settings):
        project = {"id": "world", "name": "World", "path": str(self.project), **project_settings}
        return {
            "goal_id": "team-goal", "request_id": "team-request",
            "conversation_id": "team-conversation", "lead_agent_id": "builder",
            "requested_agent_ids": ["builder", "tester"],
            "agents": [{"id": "builder", "name": "Builder", "who": "route-one"},
                       {"id": "tester", "name": "Tester", "who": "route-two"}],
            "project": {key: project[key] for key in ("id", "name", "path")},
            "objective": "Create a game", "status": "running", "tasks": [],
            "verification_contract": capture_verification_contract(self.config, project, self.project),
        }

    def test_selected_project_commands_survive_json_restart_and_win_over_host_commands(self):
        commands = [["node", "--test", "tests/game.test.js"]]
        goal = self.goal(test_commands=commands)
        restored = json.loads(json.dumps(goal))
        selected = verification_project(self.config, restored)
        self.assertEqual(swarm_work._verification_commands(self.config, self.project, selected),
                         (commands, "selected_project"))
        self.assertEqual(selected["tasks"], ["Create a game"])
        selected["test_commands"][0].append("unexpected")
        self.assertEqual(verification_project(self.config, restored)["test_commands"], commands)

    def test_discovered_approval_is_preserved_but_changed_manifest_invalidates_it(self):
        manifest = self.project / "package.json"
        manifest.write_text(json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8")
        selected = self.goal()["project"]
        approval = swarm_work.verification_command_approval(self.config, selected)
        digest = approval["approval_digest"]
        goal = self.goal(approved_test_command_digest=digest)
        restored = verification_project(self.config, json.loads(json.dumps(goal)))
        self.assertEqual(restored["approved_test_command_digest"], digest)
        self.assertEqual(swarm_work.verification_command_approval(self.config, restored)["approval_digest"], digest)
        manifest.write_text(json.dumps({"scripts": {"test": "node different-check.js"}}), encoding="utf-8")
        self.assertNotEqual(swarm_work.verification_command_approval(self.config, restored)["approval_digest"], digest)

    def test_verification_rejects_moved_project_tampered_contract_and_changed_same_root_config(self):
        goal = self.goal(test_commands=[["python", "-m", "unittest"]])
        moved = copy.deepcopy(goal)
        moved["project"]["path"] = str(self.root / "moved")
        with self.assertRaisesRegex(HarnessError, "contract changed"):
            verification_project(self.config, moved)
        corrupt = copy.deepcopy(goal)
        corrupt["verification_contract"]["test_commands"] = [["different-runner"]]
        with self.assertRaisesRegex(HarnessError, "contract changed"):
            verification_project(self.config, corrupt)
        future = copy.deepcopy(goal)
        future["verification_contract"]["schema_version"] = 99
        with self.assertRaisesRegex(HarnessError, "contract changed"):
            verification_project(self.config, future)
        same = {**goal, "project": {**goal["project"], "path": str(self.root)}}
        same["verification_contract"] = capture_verification_contract(self.config, same["project"], self.root)
        self.config.data["project"]["test_commands"] = [["python", "replacement.py"]]
        with self.assertRaisesRegex(HarnessError, "commands changed"):
            verification_project(self.config, same)

    def test_legacy_goal_does_not_invent_verification_authority(self):
        goal = self.goal()
        del goal["verification_contract"]
        self.assertNotIn("approved_test_command_digest", verification_project(self.config, goal))

    def test_chat_controls_require_exact_conversation_project_and_pair(self):
        goal = self.goal()
        binding = {"chat_id": "team-conversation", "project_id": "world",
                   "participant_ids": ["tester", "builder"]}
        HarnessHTTPServer.require_long_horizon_chat_binding(goal, binding)
        HarnessHTTPServer.require_long_horizon_chat_binding(goal, {"payload": binding})
        HarnessHTTPServer.require_long_horizon_chat_binding(goal, {"action": "pause"})
        for field, value in (("chat_id", "unrelated-chat"), ("project_id", "unrelated-project"),
                             ("participant_ids", ["builder", "new-agent"]), ("participant_ids", None),
                             ("participant_ids", ["builder", {}]), ("participant_ids", ["builder", 1])):
            with self.subTest(field=field):
                with self.assertRaises(HarnessError):
                    HarnessHTTPServer.require_long_horizon_chat_binding(goal, {**binding, field: value})
        with self.assertRaises(HarnessError):
            HarnessHTTPServer.require_long_horizon_chat_binding(goal, {"chat_id": "team-conversation"})

    def test_actual_steering_answers_and_agent_speech_remain_ordered_once_after_restart(self):
        goal = self.goal()
        filed_as = "team-history"
        intent = chat.long_horizon_intent_sha256("team-conversation", "world", "builder", "Create a game", [])
        chat.keep_long_horizon_prompt(
            self.config, "route-one", "Create a game", filed_as=filed_as,
            request_id="team-request", chat_id="team-conversation", project_id="world",
            lead_id="builder", intent_sha256=intent,
        )
        descriptions = [
            ("provider_acknowledged", "builder", {"summary": "Tester, I created the world.", "action": "work"}),
            ("goal_steered", "", {"text": "Please add keyboard controls."}),
            ("provider_acknowledged", "tester", {"summary": "Builder, keyboard controls now pass.", "action": "work"}),
            ("interrupt_resolved", "builder", {"answer": "Use arrow keys."}),
        ]
        events = [{"event_id": f"{seq:032x}", "seq": seq, "goal_id": goal["goal_id"],
                   "type": kind, "agent_id": agent, "payload": payload}
                  for seq, (kind, agent, payload) in enumerate(descriptions, 1)]
        chat.keep_long_horizon_events(self.config, "route-one", goal, events, filed_as=filed_as)
        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        chat.keep_long_horizon_events(reopened, "route-one", goal, list(reversed(events)), filed_as=filed_as)
        messages = [one for one in chat.read_it(reopened, "route-one", filed_as)
                    if one.correlation.get("source_goal_event_id")]
        self.assertEqual([one.text for one in messages], [
            "Tester, I created the world.", "Please add keyboard controls.",
            "Builder, keyboard controls now pass.", "Use arrow keys.",
        ])
        self.assertEqual([one.who for one in messages], ["them", "you", "them", "you"])
        self.assertEqual([one.speaker_id for one in messages], ["builder", "user", "tester", "user"])
        self.assertEqual(chat.long_horizon_event_cursor(reopened, "route-one", goal["goal_id"], filed_as=filed_as), 4)
        wrong = [{**events[0], "goal_id": "another-goal"}]
        with self.assertRaises(HarnessError):
            chat.keep_long_horizon_events(reopened, "route-one", goal, wrong, filed_as=filed_as)


if __name__ == "__main__":
    unittest.main()
