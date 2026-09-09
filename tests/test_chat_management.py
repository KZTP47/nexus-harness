import copy
import json
import unittest
import threading
from unittest import mock

from our_harness import chat, chat_management, swarm_chats, server
from our_harness.models import HarnessError
from tests import test_swarm_chats, test_long_horizon_verification_policy, test_team_server


class ChatManagementTests(unittest.TestCase):
    setUp = test_swarm_chats.PairScopedChatsTests.setUp
    ask = test_team_server.PanelTestCase.ask

    def test_http_preferences_work_during_turn_and_purge_waits_for_release(self):
        original = self.saved()
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        threading.Thread(target=self.panel.serve_forever, daemon=True).start()
        self.addCleanup(self.panel.shutdown)
        with mock.patch.object(self.panel, "swarm_standing", return_value={"board": self.board}):
            runs = self.panel.swarm_communication_runs
            snapshot = {"kind": "chat", "chat_key": original["id"], "prompt": "private prompt"}
            run, _ = runs.accept("purge-http-run", snapshot)
            runs.start(run["run_id"])
            runs.event(run["run_id"], "reply", {"text": "private answer"})
            runs.finish(run["run_id"], {"text": "private result"})
            sibling, _ = runs.accept("keep-http-run", {"kind": "chat", "chat_key": "other-chat", "prompt": "keep sibling"})
            runs.start(sibling["run_id"])
            with self.assertRaises(HarnessError):
                runs.purge_conversation("other-chat")
            with self.panel.swarm_communication_runs.conversation_turn("fixture-working", original["id"]):
                for action, data in [("rename", {"name": "Still working"}), ("pin", {"pinned": True})]:
                    status, value = self.ask("/api/swarm/chats/" + action,
                        {"agent": "agent-1", "chat": original["id"], **data})
                    self.assertEqual(status, 200, value)
                status, value = self.ask("/api/swarm/chats/purge", {"agent": "agent-1", "chat": original["id"]})
                self.assertEqual(status, 200, value)
                self.assertTrue(value["purge_pending"])
                self.assertEqual(self.saved()["id"], original["id"])
            status, value = self.ask("/api/swarm/chats/purge", {"agent": "agent-1", "chat": original["id"]})
            self.assertEqual(status, 200, value)
            self.assertNotIn(original["id"], [row["id"] for row in value["chats"]])
            remaining = runs.get(run["run_id"])
            self.assertEqual(remaining["snapshot"]["kind"], "purged_chat")
            self.assertIsNone(remaining["result"])
            self.assertEqual(remaining["event_count"], 0)
            self.assertNotIn("private", json.dumps(remaining))
            self.assertEqual(runs.get(sibling["run_id"])["snapshot"]["prompt"], "keep sibling")
            self.assertEqual(runs.get(sibling["run_id"])["status"], "running")
            with self.assertRaises(HarnessError):
                runs.accept("purge-http-run", snapshot)
            from our_harness.swarm_runs import SwarmRunStore
            self.assertEqual(SwarmRunStore.for_communication(self.config).get(run["run_id"])["status"], "stopped")

    def test_purge_keeps_chat_while_goal_is_draining(self):
        original = self.saved()
        chat._keep_it(self.config, "claude", [chat.Said("you", "Pending work", "now")], original["filed_as"])
        with mock.patch.object(chat_management, "_purge_goals", return_value=False):
            result = chat_management.update(self.config, self.board, "agent-1", original["id"], action="purge", runtime=object())
        self.assertTrue(result["purge_pending"])
        self.assertEqual(self.saved()["id"], original["id"])
        self.assertEqual(chat.read_it(self.config, "claude", original["filed_as"])[0].text, "Pending work")

    def saved(self):
        return next(row for row in swarm_chats.list_for_agent(self.config,self.board,"agent-1")["chats"] if len(row["pair"]) == 2)

    def test_rename_pin_restart_and_other_pair_rejection(self):
        original = self.saved()
        original_binding = copy.deepcopy(next(row for row in swarm_chats._read(self.config)["chats"] if row["id"]==original["id"])["binding"])
        for action, kwargs in [("rename", {"name":"My custom game"}), ("pin", {"pinned":True})]:
            chat_management.update(self.config,self.board,"agent-1",original["id"],action=action,**kwargs)
        updated = self.saved()
        for field in ("id","pair","filed_as","project"):
            self.assertEqual(updated[field],original[field])
        self.assertEqual(next(row for row in swarm_chats._read(self.config)["chats"] if row["id"]==original["id"])["binding"], original_binding)
        self.assertEqual(updated["name"],"My custom game")
        self.assertTrue(updated["pinned"])
        reverse=next(row for row in swarm_chats.list_for_agent(self.config,self.board,"agent-2")["chats"] if row["id"]==original["id"])
        self.assertTrue(reverse["pinned"])
        for name in ("", "x"*81, "invalid\nname"):
            with self.assertRaises(HarnessError):
                chat_management.update(self.config,self.board,"agent-1",original["id"],action="rename",name=name)
        with self.assertRaises(HarnessError):
            chat_management.update(self.config,self.board,"another-agent",original["id"],action="purge")
        chat_management.update(self.config,self.board,"agent-1",original["id"],action="pin",pinned=False)
        self.assertFalse(self.saved()["pinned"])

    def test_purge_removes_transcript_history_and_attachments_without_removing_project(self):
        original=self.saved()
        chat._keep_it(self.config,"claude",[chat.Said("you","private chat content","now")],original["filed_as"])
        where=chat.where_it_is_kept(self.config,"claude",original["filed_as"])
        attachments=chat._attachment_folder(self.config,"claude",original["filed_as"])
        attachments.mkdir(parents=True,exist_ok=True)
        (attachments/"owned.txt").write_text("attachment")
        delivered=self.first/"published.txt"; delivered.write_text("keep this")
        other=next(row for row in swarm_chats.list_for_agent(self.config,self.board,"agent-1")["chats"] if len(row["pair"])==1)
        chat._keep_it(self.config,"claude",[chat.Said("you","sibling content","now")],other["filed_as"])
        chat_management.update(self.config,self.board,"agent-1",original["id"],action="purge")
        self.assertFalse(where.exists()); self.assertFalse(where.with_suffix(".events.jsonl").exists())
        self.assertFalse(attachments.exists())
        self.assertEqual(delivered.read_text(),"keep this")
        self.assertEqual(chat.read_it(self.config,"claude",other["filed_as"])[0].text,"sibling content")
        for _ in range(2):
            self.assertNotIn(original["id"],[r["id"] for r in swarm_chats.list_for_agent(self.config,self.board,"agent-1")["chats"]])
        for snapshot in swarm_chats._history_where(self.config).glob("*.json"):
            value=json.loads(snapshot.read_text())
            self.assertNotIn(original["id"],[r["id"] for r in value.get("chats",[])])
        # Even recovery from older registry snapshots cannot resurrect it.
        swarm_chats._where(self.config).unlink()
        swarm_chats._backup_where(self.config).unlink()
        self.assertNotIn(original["id"],[r["id"] for r in swarm_chats.list_for_agent(self.config,self.board,"agent-1")["chats"]])


class GoalPurgeTests(unittest.TestCase):
    setUp = test_long_horizon_verification_policy.LongHorizonVerificationPolicyTests.setUp
    create = test_long_horizon_verification_policy.LongHorizonVerificationPolicyTests.create
    finish_tasks = test_long_horizon_verification_policy.LongHorizonVerificationPolicyTests.finish_tasks
    verify = test_long_horizon_verification_policy.LongHorizonVerificationPolicyTests.verify

    def test_goal_purge_retires_request_and_keeps_published_files(self):
        goal=self.verify(self.finish_tasks(self.create("purge-owned")))
        self.assertEqual(goal["status"],"complete")
        sibling=self.create("keep-sibling")
        self.assertTrue(chat_management._purge_goals(self.runtime,goal["conversation_id"]))
        with self.assertRaises(HarnessError): self.runtime.store.get(goal["goal_id"])
        self.assertTrue((self.project/"index.html").is_file())
        self.assertEqual(self.runtime.store.get(sibling["goal_id"])["conversation_id"],sibling["conversation_id"])
        with self.runtime.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM long_goal_dialogue_messages WHERE goal_id=?",(goal["goal_id"],)).fetchone()[0],0)
            tombstones=[json.loads(row[0]) for row in db.execute("SELECT tombstone_json FROM long_goal_request_tombstones")]
            self.assertEqual(sum(row["goal_id"]==goal["goal_id"] for row in tombstones),1)

    def test_queued_goal_is_cancelled_before_purge(self):
        goal=self.create("purge-queued")
        self.assertTrue(chat_management._purge_goals(self.runtime,goal["conversation_id"]))
        with self.assertRaises(HarnessError): self.runtime.store.get(goal["goal_id"])
