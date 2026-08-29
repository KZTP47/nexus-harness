from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import agent_mailbox as mailbox
from our_harness.models import CommandResult
from our_harness.providers import subscription_cli


class DurableAgentMailbox(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.where = Path(self.temporary.name) / "mailbox.json"

    def queued(self, goal: str = "goal-one") -> mailbox.AgentMessage:
        return mailbox.enqueue(
            self.where,
            shared_goal_id=goal,
            sender="agent-1",
            sender_name="Reviewer",
            receiver="agent-2",
            receiver_name="Writer",
            project="project-1",
            project_name="Nexus",
            body="Please check the provider boundary.",
        )

    def test_a_failed_delivery_survives_for_the_next_run(self) -> None:
        message = self.queued()
        mailbox.attempted(self.where, [message.message_id], "provider unavailable")

        waiting = mailbox.pending(
            self.where,
            shared_goal_id="goal-one",
            receiver="agent-2",
            allowed_senders=["agent-1"],
        )

        self.assertEqual([one.message_id for one in waiting], [message.message_id])
        self.assertEqual(waiting[0].attempts, 1)
        self.assertEqual(mailbox.status(self.where)["retrying"], 1)

    def test_it_is_acknowledged_only_after_success(self) -> None:
        message = self.queued()
        mailbox.acknowledge(self.where, [message.message_id])
        self.assertEqual(mailbox.pending(
            self.where,
            shared_goal_id="goal-one",
            receiver="agent-2",
            allowed_senders=["agent-1"],
        ), [])
        self.assertEqual(mailbox.status(self.where), {
            "queued": 0, "acknowledged": 1, "retrying": 0,
        })

    def test_fan_out_reuses_one_verified_payload_until_every_copy_is_acknowledged(self) -> None:
        body = "same exact large answer\n" * 1_000
        first = mailbox.enqueue(
            self.where, shared_goal_id="goal-one", sender="agent-1",
            sender_name="Reviewer", receiver="agent-2", receiver_name="Writer",
            project="project-1", project_name="Nexus", body=body,
        )
        second = mailbox.enqueue(
            self.where, shared_goal_id="goal-one", sender="agent-1",
            sender_name="Reviewer", receiver="agent-3", receiver_name="Tester",
            project="project-1", project_name="Nexus", body=body,
        )
        messages = json.loads(self.where.read_text(encoding="utf-8"))["messages"]
        self.assertEqual(messages[0]["body_ref"], messages[1]["body_ref"])
        payloads = list((self.where.parent / f"{self.where.stem}-payloads").glob("*.txt"))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].read_text(encoding="utf-8"), body)

        mailbox.acknowledge(self.where, [first.message_id])
        self.assertTrue(payloads[0].is_file())
        waiting = mailbox.pending(
            self.where, shared_goal_id="goal-one", receiver="agent-3",
            allowed_senders=["agent-1"],
        )
        self.assertEqual(waiting[0].body, body)
        mailbox.acknowledge(self.where, [second.message_id])
        self.assertFalse(payloads[0].exists())

    def test_a_tampered_shared_payload_is_not_reused_for_new_fan_out(self) -> None:
        self.queued()
        stored = json.loads(self.where.read_text(encoding="utf-8"))["messages"][0]
        payload = self.where.parent / f"{self.where.stem}-payloads" / stored["body_ref"]
        payload.write_text("tampered", encoding="utf-8")
        index_before = self.where.read_bytes()

        with self.assertRaisesRegex(mailbox.MailboxError, "failed its SHA-256"):
            mailbox.enqueue(
                self.where, shared_goal_id="goal-one", sender="agent-1",
                sender_name="Reviewer", receiver="agent-3", receiver_name="Tester",
                project="project-1", project_name="Nexus",
                body="Please check the provider boundary.",
            )
        self.assertEqual(self.where.read_bytes(), index_before)
        self.assertEqual(payload.read_text(encoding="utf-8"), "tampered")

    def test_an_old_goal_is_never_replayed_into_a_new_goal(self) -> None:
        self.queued("old-goal")
        self.assertEqual(mailbox.pending(
            self.where,
            shared_goal_id="new-goal",
            receiver="agent-2",
            allowed_senders=["agent-1"],
        ), [])

    def test_a_removed_communication_line_cannot_receive_old_mail(self) -> None:
        self.queued()
        self.assertEqual(mailbox.pending(
            self.where,
            shared_goal_id="goal-one",
            receiver="agent-2",
            allowed_senders=[],
        ), [])

    def test_the_file_contains_no_provider_or_account_metadata(self) -> None:
        self.queued()
        written = json.loads(self.where.read_text(encoding="utf-8"))
        text = json.dumps(written).lower()
        for private in ("email", "account", "token", "credential", "executable"):
            self.assertNotIn(private, text)

    def test_the_disclosed_body_boundary_round_trips_exactly(self) -> None:
        body = "begin\n" + "x" * (mailbox.LONGEST_BODY - 10) + "\nend"
        self.assertEqual(len(body), mailbox.LONGEST_BODY)
        message = mailbox.enqueue(
            self.where,
            shared_goal_id="goal-one",
            sender="agent-1",
            sender_name="Reviewer",
            receiver="agent-2",
            receiver_name="Writer",
            project="project-1",
            project_name="Nexus",
            body=body,
        )
        index = json.loads(self.where.read_text(encoding="utf-8"))
        stored = index["messages"][0]
        self.assertNotIn("body", stored)
        self.assertEqual(stored["body_characters"], len(body))
        payload = self.where.parent / f"{self.where.stem}-payloads" / stored["body_ref"]
        self.assertEqual(payload.read_text(encoding="utf-8"), body)
        self.assertEqual(
            hashlib.sha256(payload.read_bytes()).hexdigest(), stored["body_sha256"]
        )
        waiting = mailbox.pending(
            self.where,
            shared_goal_id="goal-one",
            receiver="agent-2",
            allowed_senders=["agent-1"],
        )
        self.assertEqual(waiting[0].message_id, message.message_id)
        self.assertEqual(waiting[0].body, body)

    def test_leading_trailing_and_whitespace_only_lines_round_trip_exactly(self) -> None:
        body = "\n\t  indented first line  \n\n  \t\nlast line\t  \n\n"
        message = mailbox.enqueue(
            self.where,
            shared_goal_id="goal-one",
            sender="agent-1",
            sender_name="Reviewer",
            receiver="agent-2",
            receiver_name="Writer",
            project="project-1",
            project_name="Nexus",
            body=body,
        )

        waiting = mailbox.pending(
            self.where,
            shared_goal_id="goal-one",
            receiver="agent-2",
            allowed_senders=["agent-1"],
        )

        self.assertEqual(waiting[0].message_id, message.message_id)
        self.assertEqual(waiting[0].body, body)
        stored = json.loads(self.where.read_text(encoding="utf-8"))["messages"][0]
        payload = self.where.parent / f"{self.where.stem}-payloads" / stored["body_ref"]
        self.assertEqual(payload.read_text(encoding="utf-8"), body)

    def test_missing_or_forged_payload_digest_fails_closed_and_stays_queued(self) -> None:
        for replacement, error in (
            (None, "no valid SHA-256 authority"),
            ("0" * 64, "failed its SHA-256 check"),
        ):
            with self.subTest(replacement=replacement):
                where = self.where.with_name(
                    f"{self.where.stem}-{replacement is not None}.json"
                )
                message = mailbox.enqueue(
                    where,
                    shared_goal_id="goal-one",
                    sender="agent-1",
                    sender_name="Reviewer",
                    receiver="agent-2",
                    receiver_name="Writer",
                    project="project-1",
                    project_name="Nexus",
                    body="canonical payload text",
                )
                document = json.loads(where.read_text(encoding="utf-8"))
                if replacement is None:
                    document["messages"][0].pop("body_sha256")
                else:
                    document["messages"][0]["body_sha256"] = replacement
                where.write_text(json.dumps(document), encoding="utf-8")

                with self.assertRaisesRegex(mailbox.MailboxError, error):
                    mailbox.pending(
                        where,
                        shared_goal_id="goal-one",
                        receiver="agent-2",
                        allowed_senders=["agent-1"],
                    )

                stored = json.loads(where.read_text(encoding="utf-8"))["messages"][0]
                self.assertEqual(stored["message_id"], message.message_id)
                self.assertEqual(stored["state"], "queued")

    def test_a_missing_external_payload_fails_closed_and_stays_queued(self) -> None:
        message = self.queued()
        document = json.loads(self.where.read_text(encoding="utf-8"))
        stored = document["messages"][0]
        payload = self.where.parent / f"{self.where.stem}-payloads" / stored["body_ref"]
        payload.unlink()

        with self.assertRaisesRegex(mailbox.MailboxError, "cannot be read"):
            mailbox.pending(
                self.where,
                shared_goal_id="goal-one",
                receiver="agent-2",
                allowed_senders=["agent-1"],
            )

        still_stored = json.loads(self.where.read_text(encoding="utf-8"))["messages"][0]
        self.assertEqual(still_stored["message_id"], message.message_id)
        self.assertEqual(still_stored["state"], "queued")

    def test_an_oversized_handoff_is_refused_and_never_written_partly(self) -> None:
        with self.assertRaisesRegex(mailbox.MailboxError, "did not truncate"):
            mailbox.enqueue(
                self.where,
                shared_goal_id="goal-one",
                sender="agent-1",
                sender_name="Reviewer",
                receiver="agent-2",
                receiver_name="Writer",
                project="project-1",
                project_name="Nexus",
                body="x" * (mailbox.LONGEST_BODY + 1),
            )
        self.assertFalse(self.where.exists())

    def test_corrupt_mail_is_not_pretended_to_be_an_empty_mailbox(self) -> None:
        self.where.write_text("{ not json", encoding="utf-8")
        before = self.where.read_bytes()
        with self.assertRaisesRegex(mailbox.MailboxError, "did not pretend it was empty"):
            mailbox.pending(
                self.where,
                shared_goal_id="goal-one",
                receiver="agent-2",
                allowed_senders=["agent-1"],
            )
        self.assertEqual(self.where.read_bytes(), before)

    def test_a_legacy_oversized_queued_message_is_not_acknowledged_or_sliced(self) -> None:
        message = self.queued()
        document = json.loads(self.where.read_text(encoding="utf-8"))
        document["messages"][0].pop("body_ref", None)
        document["messages"][0].pop("body_sha256", None)
        document["messages"][0].pop("body_characters", None)
        document["messages"][0]["body"] = "x" * (mailbox.LONGEST_BODY + 1)
        self.where.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaisesRegex(mailbox.MailboxError, "did not truncate or acknowledge"):
            mailbox.pending(
                self.where,
                shared_goal_id="goal-one",
                receiver="agent-2",
                allowed_senders=["agent-1"],
            )
        stored = json.loads(self.where.read_text(encoding="utf-8"))["messages"][0]
        self.assertEqual(stored["message_id"], message.message_id)
        self.assertEqual(stored["state"], "queued")
        self.assertEqual(len(stored["body"]), mailbox.LONGEST_BODY + 1)

    def test_an_altered_external_payload_fails_its_hash_and_remains_queued(self) -> None:
        message = self.queued()
        document = json.loads(self.where.read_text(encoding="utf-8"))
        stored = document["messages"][0]
        payload = self.where.parent / f"{self.where.stem}-payloads" / stored["body_ref"]
        payload.write_text("altered after it was queued", encoding="utf-8")

        with self.assertRaisesRegex(mailbox.MailboxError, "failed its SHA-256 check"):
            mailbox.pending(
                self.where,
                shared_goal_id="goal-one",
                receiver="agent-2",
                allowed_senders=["agent-1"],
            )

        still_stored = json.loads(self.where.read_text(encoding="utf-8"))["messages"][0]
        self.assertEqual(still_stored["message_id"], message.message_id)
        self.assertEqual(still_stored["state"], "queued")


class SubscriptionConnectionState(unittest.TestCase):
    @staticmethod
    def result(code: int, out: str = "", error: str = "") -> CommandResult:
        return CommandResult(["tool"], ".", code, out, error, 1)

    def test_installed_is_not_mistaken_for_signed_in(self) -> None:
        with mock.patch.object(subscription_cli, "available", return_value="claude.exe"), \
             mock.patch.object(
                 subscription_cli, "_run_bounded",
                 return_value=self.result(1, error="Not logged in"),
             ):
            found = subscription_cli.connection_status("claude-cli")
        self.assertTrue(found["installed"])
        self.assertEqual(found["authentication"], "signed-out")
        self.assertEqual(found["state"], "needs-login")

    def test_account_details_from_auth_status_are_never_returned(self) -> None:
        private = '{"loggedIn": true, "email": "person@example.test"}'
        with mock.patch.object(subscription_cli, "available", return_value="claude.exe"), \
             mock.patch.object(
                 subscription_cli, "_run_bounded",
                 return_value=self.result(0, out=private),
             ):
            found = subscription_cli.connection_status("claude-cli")
        self.assertEqual(found["authentication"], "signed-in")
        self.assertNotIn("person", json.dumps(found))

    def test_a_provider_without_status_support_is_honestly_unknown(self) -> None:
        with mock.patch.object(subscription_cli, "available", return_value="gemini.exe"):
            found = subscription_cli.connection_status("gemini-cli")
        self.assertEqual(found["authentication"], "unknown")
        self.assertEqual(found["state"], "installed")

    @unittest.skipUnless(subscription_cli.os.name == "nt", "Windows login window")
    def test_interactive_login_never_captures_credentials_or_output(self) -> None:
        started = mock.Mock(pid=42)
        with mock.patch.object(
                subscription_cli, "available", return_value="claude.exe"), \
             mock.patch.object(subscription_cli.subprocess, "Popen", return_value=started) as popen:
            found = subscription_cli.start_interactive_login("claude-cli")
        call = popen.call_args
        self.assertEqual(call.args[0], ["claude.exe", "auth", "login"])
        self.assertIsNone(call.kwargs["stdout"])
        self.assertIsNone(call.kwargs["stderr"])
        self.assertNotIn("account", json.dumps(found).lower())

    @unittest.skipUnless(subscription_cli.os.name == "nt", "Windows repair window")
    def test_claude_repair_is_visible_fixed_and_captures_nothing(self) -> None:
        started = mock.Mock(pid=84)
        with mock.patch.object(
                subscription_cli, "available", return_value=r"C:\Claude\claude.exe"), \
             mock.patch.object(subscription_cli.subprocess, "Popen", return_value=started) as popen:
            found = subscription_cli.start_claude_repair()
        call = popen.call_args
        command = call.args[0]
        self.assertEqual(command[:4], [
            subscription_cli.os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/k",
        ])
        self.assertIn("update", command[4])
        self.assertIn("auth logout", command[4])
        self.assertIn("auth login", command[4])
        self.assertLess(command[4].index("update"), command[4].index("auth logout"))
        self.assertIsNone(call.kwargs["stdout"])
        self.assertIsNone(call.kwargs["stderr"])
        self.assertFalse(call.kwargs["shell"])
        self.assertNotIn("account", json.dumps(found).lower())


if __name__ == "__main__":
    unittest.main()
