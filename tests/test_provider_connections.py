"""Connection checks follow configured agent routes all the way to their engine."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import CommandResult, HarnessError
from our_harness.providers import connection as connections
from our_harness.providers import subscription_cli


class RouteAwareConnectionChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def config(self, providers: dict) -> LoadedConfig:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = providers
        return LoadedConfig(data, self.root, [], {})

    def test_a_route_name_resolves_to_its_engine_and_exact_command(self) -> None:
        exact = r"C:\Program Files\Claude\claude.exe"
        config = self.config({
            "claude": {
                "kind": "claude-cli", "model": "sonnet", "command": [exact],
            },
        })
        native = {
            "kind": "claude-cli", "installed": True,
            "authentication": "signed-in", "state": "authenticated",
            "can_login": True,
        }
        with mock.patch.object(
            subscription_cli, "connection_status", return_value=native,
        ) as checked:
            found = connections.connection_status(config, "claude")

        checked.assert_called_once_with(
            "claude-cli", timeout_seconds=10.0, use_cache=False,
            probe=True, command=[exact],
        )
        self.assertEqual(found["route"], "claude")
        self.assertEqual(found["kind"], "claude-cli")
        self.assertEqual(found["authentication"], "signed-in")
        self.assertIn("actually uses", found["note"])

    def test_two_routes_using_one_engine_check_their_own_commands(self) -> None:
        config = self.config({
            "claude_work": {
                "kind": "claude-cli", "model": "sonnet", "command": ["work.exe"],
            },
            "claude_personal": {
                "kind": "claude-cli", "model": "sonnet", "command": ["personal.exe"],
            },
        })
        native = {
            "kind": "claude-cli", "installed": True,
            "authentication": "unknown", "state": "installed", "can_login": True,
        }
        with mock.patch.object(
            subscription_cli, "connection_status", return_value=native,
        ) as checked:
            connections.connection_status(config, "claude_work")
            connections.connection_status(config, "claude_personal")
        self.assertEqual(
            [one.kwargs["command"] for one in checked.call_args_list],
            [["work.exe"], ["personal.exe"]],
        )

    def test_web_routes_report_the_live_electron_connection(self) -> None:
        config = self.config({})
        out = connections.connection_status(config, "web:claude-1")
        self.assertEqual(out["authentication"], "signed-out")
        live = connections.connection_status(
            config, "web:claude-1", web_connection={"provider": "Claude"},
        )
        self.assertEqual(live["authentication"], "signed-in")
        self.assertEqual(live["checked_by"], "live-electron-web-chat")

    def test_key_routes_only_check_presence_and_never_return_the_value(self) -> None:
        config = self.config({
            "api_agent": {
                "kind": "openai", "model": "model", "api_key_env": "TEST_AGENT_KEY",
            },
        })
        with mock.patch.dict(os.environ, {"TEST_AGENT_KEY": "do-not-return-this"}):
            found = connections.connection_status(config, "api_agent")
        self.assertEqual(found["authentication"], "credential-configured")
        self.assertNotIn("do-not-return-this", repr(found))

    def test_a_missing_route_fails_with_a_route_specific_explanation(self) -> None:
        with self.assertRaisesRegex(HarnessError, "configured provider route"):
            connections.connection_status(self.config({}), "claude")

    @unittest.skipUnless(os.name == "nt", "Windows login window")
    def test_manual_login_uses_the_same_exact_route_command(self) -> None:
        config = self.config({
            "claude": {
                "kind": "claude-cli", "model": "sonnet", "command": ["exact.exe"],
            },
        })
        with mock.patch.object(
            subscription_cli, "start_interactive_login",
            return_value={"opened": True, "kind": "claude-cli", "note": "opened"},
        ) as opened:
            found = connections.start_interactive_login(config, "claude")
        opened.assert_called_once_with("claude-cli", command=["exact.exe"])
        self.assertEqual(found["route"], "claude")


class ExactConfiguredCliChecks(unittest.TestCase):
    @staticmethod
    def result(code: int, out: str = "", error: str = "") -> CommandResult:
        return CommandResult(["tool"], ".", code, out, error, 1)

    def test_status_runs_the_configured_command_and_prefix_arguments(self) -> None:
        exact = r"C:\exact\claude.exe"
        command = [exact, "--profile", "work"]
        with mock.patch.object(subscription_cli, "available", return_value=exact) as available, \
             mock.patch.object(
                 subscription_cli, "_run_bounded",
                 return_value=self.result(0, '{"loggedIn": true}'),
             ) as run:
            found = subscription_cli.connection_status("claude-cli", command=command)
        available.assert_called_once_with("claude-cli", command)
        self.assertEqual(
            run.call_args.args[0],
            [exact, "--profile", "work", "auth", "status"],
        )
        self.assertEqual(found["authentication"], "signed-in")

    def test_an_explicit_missing_command_does_not_fall_back_to_another_build(self) -> None:
        with mock.patch.object(subscription_cli.shutil, "which", return_value=None), \
             mock.patch.object(subscription_cli, "_every_build_of") as desktop_builds:
            found = subscription_cli.available(
                "claude-cli", [r"C:\missing\claude.exe"],
            )
        self.assertEqual(found, "")
        desktop_builds.assert_not_called()

    def test_copilot_has_a_documented_manual_login_but_no_fake_status_probe(self) -> None:
        recipe = subscription_cli.recipe_for("copilot-cli")
        self.assertEqual(recipe.interactive_login_arguments, ("login",))
        self.assertEqual(recipe.signed_in_arguments, ())
