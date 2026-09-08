from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from our_harness import chat, goal_chat_projection, goal_chat_progress, long_horizon
from our_harness.config import LoadedConfig
from tests import test_goal_chat_projection as projection_fixture
from tests import test_long_horizon_dialogue as dialogue_fixture


def runner_fixture():
    """Real scheduler, journal, archive and saved chat; deterministic transport."""
    fixture = dialogue_fixture.LongHorizonDialogueTests(methodName="runTest")
    fixture.setUp()
    try:
        goal = fixture.create(request="visible-progress")
        route, filed_as = "builder-route", "portable-progress-chat"
        prompt = goal["objective"]
        chat.keep_long_horizon_prompt(
            fixture.config, route, prompt, filed_as=filed_as,
            request_id=goal["request_id"], chat_id=goal["conversation_id"],
            project_id="game", lead_id="builder",
            intent_sha256=chat.long_horizon_intent_sha256(goal["conversation_id"], "game", "builder", prompt, []),
        )
        stages = []

        def capture():
            current = fixture.runtime.store.public(fixture.runtime.store.get(goal["goal_id"]))
            goal_chat_projection.keep_page(fixture.config, route, current,
                fixture.runtime.store.dialogue_history(goal["goal_id"]), filed_as=filed_as)
            chat.keep_long_horizon_events(fixture.config, route, current,
                fixture.runtime.store.events(goal["goal_id"])["events"], filed_as=filed_as,
                public_dialogue_archived=True)
            rows = [one.to_dict() for one in chat.read_it(fixture.config, route, filed_as)]
            stages.append(rows)
            return rows

        calls = 0

        def provider(_config, _route, _text, **kwargs):
            nonlocal calls
            calls += 1
            kwargs["before_provider_dispatch"]("initial")
            capture()  # Visible while the provider has not answered yet.
            answer = dialogue_fixture.reply("work", "I will run the project checks.", tool_calls=[{
                "call_id": "verify-portable", "name": "run_selected_verification", "arguments": {},
            }]) if calls == 1 else dialogue_fixture.reply("blocked", "The checks failed. The game needs a correction.")
            kwargs["after_provider_response"]("initial")
            capture()
            return {"text": json.dumps(answer)}

        def execute(*_args, **_kwargs):
            capture()  # Real reservation and acknowledged request are saved.
            return {"status": "success", "content": json.dumps({
                "status": "failed", "reason": "The restart check failed.",
                "commands": [{"argv": ["npm", "run", "test"], "exit_code": 1}],
            })}

        tools = mock.Mock()
        tools.execute.side_effect = execute
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider), \
                mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools", return_value=tools):
            fixture.runtime.run(goal["goal_id"])
        final = capture()
        reopened = LoadedConfig(copy.deepcopy(fixture.config.data), fixture.config.project_root, [], {})
        restored = [one.to_dict() for one in chat.read_it(reopened, route, filed_as)]
        if restored != final:
            raise AssertionError("The saved progress changed on restart")
        return stages
    finally:
        fixture.doCleanups()


class GoalChatProgressTests(unittest.TestCase):
    def test_running_scheduler_publishes_intermediate_steps_before_final_reply(self):
        stages = runner_fixture()
        first = [one for one in stages[0] if one["phase"] == "nexus_progress"]
        self.assertEqual(len(first), 1)
        self.assertIn("Waiting for a reply", first[0]["text"])
        self.assertFalse(any(one["phase"] == "agent_discussion" for one in stages[0]))
        self.assertTrue(any(any("starting project checks" in one["text"] for one in stage)
                            and not any("restart check failed" in one["text"] for one in stage)
                            for stage in stages))
        final = stages[-1]
        progress = [one for one in final if one["phase"] == "nexus_progress"]
        texts = "\n".join(one["text"] for one in progress)
        for expected in ["sent a request", "returned a response", "requested project checks",
                         "starting project checks", "Project checks failed", "sent the tool results"]:
            self.assertIn(expected, texts)
        identities = [one["correlation"]["event_id"] for one in progress]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual([one["at"] for one in progress], sorted(one["at"] for one in progress))
        self.assertTrue(all(one["correlation"]["progress_contract"] == "engine-milestones/v1" for one in progress))
        tool = next(one for one in final if one["phase"] == "agent_tool")
        self.assertEqual(json.loads(tool["text"])["status"], "failed")
        start = next(i for i, one in enumerate(final) if "starting project checks" in one["text"])
        result = next(i for i, one in enumerate(final) if one["phase"] == "agent_tool")
        followup = next(i for i, one in enumerate(final) if "sent the tool results" in one["text"])
        self.assertLess(start, result)
        self.assertLess(result, followup)

    def test_each_tool_result_uses_its_own_time_with_legacy_batch_fallback(self):
        fixture = projection_fixture.GoalChatProjectionTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        step = fixture.tool_step(result=True)
        step["completed_ms"] = 1_700_000_009_000
        step["results"][0]["at_ms"] = 1_700_000_001_750
        fixture.goal["tasks"] = [{"id": "task-a", "context_steps": [step]}]
        fixture.project(fixture.page([fixture.message(1), fixture.message(2)]))
        rows = [one for one in chat.read_it(fixture.config, "route-blue", fixture.filed_as)
                if one.correlation.get("goal_id") == "goal-a"]
        self.assertEqual([one.phase for one in rows], ["agent_discussion", "agent_tool", "agent_discussion"])
        self.assertEqual(rows[1].at, goal_chat_projection._timestamp(1_700_000_001_750))

    def test_verification_report_outcomes_do_not_upgrade_transport_success(self):
        for report, expected in [
            ({"status": "passed"}, "passed"),
            ({"status": "failed"}, "failed"),
            ({"status": "unavailable", "basis": "discovered_command_approval_required"}, "approval_required"),
            ({"status": "unavailable", "reason": "No check configured"}, "unavailable"),
        ]:
            with self.subTest(report=report):
                envelope = {"status": "success", "content": json.dumps(report)}
                self.assertEqual(goal_chat_progress.tool_outcome("run_selected_verification", envelope)[0], expected)
                self.assertEqual(goal_chat_progress.tool_outcome("read_file", envelope)[0], "finished")
        self.assertEqual(goal_chat_progress.tool_outcome("run_selected_verification", {"content": "truncated JSON"})[0], "finished")

    def test_catchup_inserts_by_occurrence_and_rejects_another_chat(self):
        fixture = projection_fixture.GoalChatProjectionTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.project(fixture.page([fixture.message(1), fixture.message(2)]))
        events = [{"goal_id": "goal-a", "event_id": "f" * 32, "seq": 11,
                   "type": "provider_dispatched", "agent_id": "builder", "task_id": "task-a",
                   "at_ms": 1_700_000_001_500, "payload": {"phase": "initial", "prompt": "PRIVATE PROMPT"}}]
        def project(goal=fixture.goal):
            return chat.keep_long_horizon_events(fixture.config, "route-blue", goal, events,
                                                 filed_as=fixture.filed_as, public_dialogue_archived=True)
        project()
        rows = chat.read_it(fixture.config, "route-blue", fixture.filed_as)
        ordered = [one.phase for one in rows if one.correlation.get("goal_id") == "goal-a"]
        self.assertEqual(ordered, ["agent_discussion", "nexus_progress", "agent_discussion"])
        with self.assertRaises(chat.ChatError):
            project({**fixture.goal, "conversation_id": "other-chat"})
        project()  # Delayed duplicate poll.
        self.assertEqual([one.to_dict() for one in rows],
                         [one.to_dict() for one in chat.read_it(fixture.config, "route-blue", fixture.filed_as)])
        self.assertNotIn("PRIVATE PROMPT", json.dumps([one.to_dict() for one in rows]))


if __name__ == "__main__":
    unittest.main()
