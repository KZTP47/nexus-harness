from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
import unittest

from our_harness import chat, goal_access, long_horizon, provider_reconnect, swarm_chats
from our_harness.models import HarnessError
from tests import test_long_horizon_dialogue as fixtures


class ProviderReconnectTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    run_replies = fixtures.LongHorizonDialogueTests.run_replies
    provider = fixtures.LongHorizonDialogueTests.provider

    def prepare(self, *, interrupted=False, goal=True, kind="local"):
        self.executable = self.base / "portable-assistant.cmd"
        self.executable.write_text("first executable")
        self.executable.chmod(0o755)
        self.config.data["providers"]["builder-route"] = {
            "kind": kind, "model": "any-model", "command": [str(self.executable), "--arbitrary"],
        }
        self.board.update({"schema_version": 1, "binding_schema_version": 1,
                           "workspace_id": "workspace-" + "a" * 32,
                           "talks_to": [{"one": "builder", "other": "peer"}]})
        chats = swarm_chats.list_for_agent(self.config, self.board, "builder")
        self.conversation = next(one for one in chats["chats"] if one["pair"] == ["builder", "peer"])
        self.chat_id = self.conversation["id"]
        chat.keep_exchange(self.config, "builder-route", "saved question", "saved answer",
                           filed_as=self.conversation["filed_as"])
        if goal:
            self.goal = self.runtime.store.create(self.board, "game", ["Finish existing work"], "reconnect-fixture",
                lead_id="builder", participant_ids=["builder", "peer"], conversation_id=self.chat_id,
                policy={"agent_access_mode": "full"})
            if interrupted:
                store = self.runtime.store
                self.old_task = store.claim_ready(self.goal["goal_id"], "dead-worker")[0]
                store.record_dispatch(self.goal["goal_id"], self.old_task, "old request")
                store.release_scheduler(self.goal["goal_id"], "dead-worker")
                store.recover_dead(self.goal["goal_id"])
            else:
                self.runtime.store.control(self.goal["goal_id"], "pause")

    def replace_executable(self):
        self.executable.rename(self.base / "retired-assistant")
        self.executable.write_text("updated executable with new identity")
        self.executable.chmod(0o755)

    def review(self):
        return provider_reconnect.reconnect(self.config, self.board, "builder", self.chat_id, self.runtime)

    def apply(self, review):
        return provider_reconnect.reconnect(self.config, self.board, "builder", self.chat_id, self.runtime,
            confirmation={**review, "decision": "reconnect"})

    def test_executable_update_reconnect_restart_and_real_resume_preserve_saved_work(self):
        self.prepare(interrupted=True)
        store = self.runtime.store
        before = store.get(self.goal["goal_id"])
        self.replace_executable()
        protected = swarm_chats.resolve(self.config, self.board, "builder", self.chat_id, allow_binding_drift=True)
        self.assertTrue(protected["binding_problem"]["can_review_reconnect"])
        with self.assertRaises(HarnessError):
            swarm_chats.resolve(self.config, self.board, "builder", self.chat_id)
        with self.assertRaises(HarnessError):
            self.runtime.resume(self.goal["goal_id"])
        review = self.review()
        self.assertNotIn(str(self.executable), str(review))
        self.assertEqual(store.get(self.goal["goal_id"])["revision"], before["revision"])
        self.apply(review)
        after = store.get(self.goal["goal_id"])
        for field in ("tasks", "budget", "admission_digest", "dialogue", "policy"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(goal_access.state(after)["mode"], "full")
        self.assertEqual(after["agent_access"]["grants"], before["agent_access"]["grants"])
        self.assertEqual(after["agent_access"]["binding"], goal_access.binding(after))
        self.assertEqual(after["status"], "paused")
        self.assertFalse(store.public(after)["provider_setup_changed"])
        with self.assertRaises(HarnessError):
            self.apply(review)
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        store = self.runtime.store
        current = store.public(store.get(after["goal_id"]))
        self.assertTrue(current["resume_recovery"]["can_retry"])
        self.assertFalse(current["resume_recovery"]["resume_safe"])
        resolved = swarm_chats.resolve(self.config, self.board, "builder", self.chat_id)
        self.assertFalse(resolved.get("binding_problem"))
        self.assertEqual(chat.read_it(self.config, "builder-route", resolved["filed_as"])[-1].text, "saved answer")
        resumed = store.control(after["goal_id"], "resume", {
            "expected_revision": current["revision"], "recovery": {
                "schema_version": 1, "decision": "retry_provider",
                "fingerprint": current["resume_recovery"]["fingerprint"],
            }})
        with self.assertRaises(HarnessError):
            store.record_provider_reply(after["goal_id"], self.old_task, phase="initial")
        result, seen = self.run_replies(resumed, [fixtures.reply(), fixtures.reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(seen), 2)
        self.assertTrue(any(one["type"] == "provider_setup_reconnected"
                            for one in store.events(after["goal_id"])["events"]))

    def test_ordinary_sign_in_without_executable_change_keeps_binding(self):
        self.prepare()
        with mock.patch.dict("os.environ", {"ARBITRARY_LOGIN_TOKEN": "rotated"}):
            self.assertFalse(swarm_chats.resolve(self.config, self.board, "builder", self.chat_id).get("binding_problem"))
            self.assertFalse(self.runtime.store.public(self.runtime.store.get(self.goal["goal_id"]))["provider_setup_changed"])

    def test_reviewed_reconnect_preserves_ask_grants_and_consumed_once(self):
        self.prepare()
        store = self.runtime.store
        def access(document, _db):
            held = goal_access.state(document)
            held.update({"mode": "ask", "grants": {
                "a" * 64: {"decision": "always", "remaining": 0},
                "b" * 64: {"decision": "deny", "remaining": 0},
                "c" * 64: {"decision": "once", "remaining": 0},
                "d" * 64: {"decision": "once", "remaining": 1},
            }})
            document["agent_access"] = held
        before = store._mutate(self.goal["goal_id"], access)[0]
        self.replace_executable()
        self.apply(self.review())
        saved = long_horizon.GoalStore(self.config).get(self.goal["goal_id"])
        self.assertEqual(goal_access.state(saved)["mode"], "ask")
        self.assertEqual(goal_access.state(saved)["grants"], before["agent_access"]["grants"])
        drifted = copy.deepcopy(saved)
        drifted["project_authority_id"] = "different-project"
        self.assertEqual(goal_access.state(drifted)["mode"], "read_only")
        self.assertEqual(goal_access.state(drifted)["grants"], {})

    def test_claude_adapter_update_keeps_goal_and_exposes_interrupted_call_recovery(self):
        from our_harness.providers.subscription_cli import SubscriptionCLIProvider
        with mock.patch.object(SubscriptionCLIProvider, "_effective_dispatch_version",
                               return_value={"state": "observed", "version_output_sha256": "c" * 64}):
            self.prepare(interrupted=True, kind="claude-cli")
            self.replace_executable()
            self.apply(self.review())
            current = self.runtime.store.public(self.runtime.store.get(self.goal["goal_id"]))
            self.assertFalse(current["provider_setup_changed"])
            self.assertEqual(current["status"], "paused")
            self.assertTrue(current["resume_recovery"]["can_retry"])
            self.assertFalse(current["resume_recovery"]["resume_safe"])

    def test_stale_review_and_changed_config_are_rejected_without_mutation(self):
        self.prepare()
        self.replace_executable()
        review = self.review()
        before = self.runtime.store.get(self.goal["goal_id"])
        self.executable.write_text("another update")
        with self.assertRaisesRegex(HarnessError, "review changed"):
            self.apply(review)
        self.config.data["providers"]["builder-route"]["model"] = "different-model"
        with self.assertRaisesRegex(HarnessError, "settings"):
            self.review()
        self.assertEqual(self.runtime.store.get(self.goal["goal_id"])["agents"], before["agents"])

    def test_project_change_and_live_goal_and_missing_executable_are_rejected(self):
        self.prepare()
        self.replace_executable()
        old = self.board["projects"][0]["path"]
        self.board["projects"][0]["path"] = str(self.base)
        with self.assertRaises(HarnessError):
            self.review()
        self.board["projects"][0]["path"] = old
        self.runtime.store._mutate(self.goal["goal_id"], lambda doc, db: doc.update(status="running"))
        with self.assertRaisesRegex(HarnessError, "Pause the team"):
            self.review()
        self.runtime.store._mutate(self.goal["goal_id"], lambda doc, db: doc.update(status="paused"))
        self.executable.unlink()
        with self.assertRaisesRegex(HarnessError, "installing"):
            self.review()

    def test_chat_only_reconnect_does_not_create_or_dispatch_a_goal(self):
        self.prepare(goal=False)
        self.replace_executable()
        self.apply(self.review())
        self.assertEqual(self.runtime.store.active_authority_goals(), [])
        self.assertFalse(swarm_chats.resolve(self.config, self.board, "builder", self.chat_id).get("binding_problem"))

    def test_wrong_pair_and_workspace_and_changed_contract_are_rejected(self):
        self.prepare()
        self.replace_executable()
        with self.assertRaises(HarnessError):
            provider_reconnect.reconnect(self.config, self.board, "unrelated", self.chat_id, self.runtime)
        other = copy.deepcopy(self.board)
        other["workspace_id"] = "workspace-" + "b" * 32
        with self.assertRaises(HarnessError):
            provider_reconnect.reconnect(self.config, other, "builder", self.chat_id, self.runtime)
        with mock.patch.dict(chat.PROVIDER_TRANSPORT_CONTRACT_REVISIONS, {"local": "different/v9"}):
            with self.assertRaisesRegex(HarnessError, "contract"):
                self.review()

    def test_partial_registry_write_failure_is_recoverable_and_never_resumes(self):
        self.prepare()
        self.replace_executable()
        review = self.review()
        with mock.patch.object(swarm_chats, "_write", side_effect=OSError("simulated power loss")):
            with self.assertRaises(OSError):
                self.apply(review)
        self.assertEqual(self.runtime.store.get(self.goal["goal_id"])["status"], "paused")
        with self.assertRaises(HarnessError):
            swarm_chats.resolve(self.config, self.board, "builder", self.chat_id)
        self.apply(self.review())
        self.assertFalse(swarm_chats.resolve(self.config, self.board, "builder", self.chat_id).get("binding_problem"))

    def test_http_reconnect_is_authenticated_scoped_and_keeps_goal_paused(self):
        from our_harness.server import HarnessHTTPServer
        self.prepare(interrupted=True)
        self.replace_executable()
        panel = HarnessHTTPServer(("127.0.0.1", 0), self.config)
        panel._long_horizon = self.runtime
        self.addCleanup(panel.server_close)
        threading.Thread(target=panel.serve_forever, daemon=True).start()
        self.addCleanup(panel.shutdown)

        def post(value, authenticated=True):
            request = urllib.request.Request(
                f"http://127.0.0.1:{panel.server_address[1]}/api/swarm/chats/reconnect",
                data=json.dumps(value).encode(), headers={"Content-Type": "application/json",
                    **({"X-Harness-Token": panel.token} if authenticated else {})})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        body = {"agent": "builder", "chat": self.chat_id}
        with mock.patch.object(panel, "swarm_standing", return_value={"board": self.board}):
            self.assertNotEqual(post(body, False)[0], 200)
            self.assertEqual(post({**body, "agent": "unrelated"})[0], 400)
            status, review = post(body)
            self.assertEqual(status, 200, review)
            self.assertNotIn("candidate", review)
            status, result = post({**body, "confirmation": {**review, "decision": "reconnect"}})
            self.assertEqual(status, 200, result)
            self.assertTrue(result["reconnected"])
        self.assertEqual(self.runtime.store.get(self.goal["goal_id"])["status"], "paused")

    def test_goal_revision_change_invalidates_review_and_pending_file_effects_survive(self):
        self.prepare(interrupted=True)
        self.replace_executable()
        review = self.review()
        store = self.runtime.store
        def change(document, db):
            document["tasks"][0]["pending_transaction"] = {"state": "prepared", "transaction_id": "saved-work"}
        store._mutate(self.goal["goal_id"], change)
        before = store.get(self.goal["goal_id"])
        with self.assertRaisesRegex(HarnessError, "review changed"):
            self.apply(review)
        self.apply(self.review())
        after = store.get(self.goal["goal_id"])
        self.assertEqual(before["tasks"], after["tasks"])
        self.assertFalse(store.public(after)["resume_recovery"]["can_retry"])


if __name__ == "__main__":
    unittest.main()
