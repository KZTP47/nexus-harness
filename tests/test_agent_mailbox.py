from __future__ import annotations

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
