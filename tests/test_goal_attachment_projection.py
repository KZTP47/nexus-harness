"""Follow-up metadata remains public, immutable and bound to its saved chat."""
import copy
import json
import sqlite3
import unittest

from our_harness import chat, goal_dialogue
from our_harness.models import HarnessError
from tests import test_goal_chat_projection as fixtures


class GoalAttachmentProjectionTests(unittest.TestCase):
    setUp = fixtures.GoalChatProjectionTests.setUp
    message = fixtures.GoalChatProjectionTests.message
    page = fixtures.GoalChatProjectionTests.page
    project = fixtures.GoalChatProjectionTests.project
    speech = fixtures.GoalChatProjectionTests.speech

    def attachment(self):
        return {"name": "private/root/screen.png", "type": "image/png", "size": 123,
                "image": True, "sha256": "a" * 64, "width": 20, "height": 10,
                "path": "private/root/original.png", "data": "PRIVATE_BYTES", "id": "private-id"}

    def test_archive_projection_keeps_only_metadata_and_exact_replay(self):
        message = self.message(1, user=True)
        message["attachments"] = [self.attachment()]
        self.project(self.page([message]))
        self.project(self.page([message]))
        rows = self.speech()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].attachments, [{"name": "screen.png", "type": "image/png",
            "size": 123, "image": True, "sha256": "a" * 64, "width": 20, "height": 10}])
        encoded = json.dumps(rows[0].attachments)
        self.assertNotIn("PRIVATE_BYTES", encoded)
        self.assertNotIn("private-id", encoded)

    def test_changed_attachment_cannot_reuse_projected_message(self):
        message = self.message(1, user=True)
        message["attachments"] = [self.attachment()]
        self.project(self.page([message]))
        changed = copy.deepcopy(message)
        changed["attachments"][0]["sha256"] = "b" * 64
        with self.assertRaisesRegex(chat.ChatError, "conflicting saved attachments"):
            self.project(self.page([changed]))

    def test_attachment_page_cannot_cross_chat_binding(self):
        message = self.message(1, user=True)
        message["attachments"] = [self.attachment()]
        other_goal = {**self.goal, "conversation_id": "other-chat"}
        with self.assertRaises(chat.ChatError):
            self.project(self.page([message]), goal=other_goal)
        self.assertEqual(self.speech(), [])

    def test_answer_event_freezes_safe_metadata_in_archive(self):
        event = {"event_id": "event-a", "type": "interrupt_resolved", "task_id": "task-a",
                 "agent_id": "builder", "seq": 1, "at_ms": 123}
        message = goal_dialogue._user_message(self.goal, event,
            {"answer": "Inspect this", "answer_audience": "team", "attachments": [self.attachment()]},
            current_recipient=True)
        self.assertEqual(message["visibility"], "team")
        self.assertEqual(message["attachments"], goal_dialogue.public_attachments([self.attachment()]))

    def test_display_metadata_does_not_invent_visual_support(self):
        value = goal_dialogue.public_attachments([{"name": r"some\folder\drawing.svg",
            "type": "image/svg+xml", "image": False, "size": 10, "width": True}])[0]
        self.assertEqual(value["name"], "drawing.svg")
        self.assertFalse(value["image"])
        self.assertNotIn("width", value)

    def test_legacy_event_projection_preserves_metadata(self):
        chat.keep_long_horizon_events(self.config, "route-blue", self.goal, [{
            "event_id": "e" * 32, "seq": 1, "goal_id": "goal-a", "type": "goal_steered",
            "payload": {"text": "See this screenshot", "attachments": [self.attachment()]},
        }], filed_as=self.filed_as)
        rows = [one for one in chat.read_it(self.config, "route-blue", self.filed_as)
                if one.correlation.get("source_goal_event_id") == "e" * 32]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].attachments, goal_dialogue.public_attachments([self.attachment()]))

    def test_archive_identity_binds_attachment_content(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        goal_dialogue.ensure_schema(db)
        document = copy.deepcopy(self.goal)
        document["dialogue_archive"] = goal_dialogue.empty(document)
        message = self.message(1, user=True)
        message["attachments"] = [self.attachment()]
        goal_dialogue.append(db, document, message)
        goal_dialogue.append(db, document, message)
        stored = db.execute("SELECT message_json FROM long_goal_dialogue_messages").fetchone()[0]
        self.assertNotIn("PRIVATE_BYTES", stored)
        self.assertNotIn("private-id", stored)
        self.assertNotIn("private/root", stored)
        changed = copy.deepcopy(message)
        changed["attachments"][0]["sha256"] = "b" * 64
        with self.assertRaisesRegex(HarnessError, "different attachments"):
            goal_dialogue.append(db, document, changed)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM long_goal_dialogue_messages").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
