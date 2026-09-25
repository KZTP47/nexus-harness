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

    def test_a_cli_configuration_error_does_not_tell_somebody_to_log_in(self) -> None:
        config = self.config({
            "codex": {
                "kind": "codex-cli", "model": "gpt", "command": ["codex.exe"],
            },
        })
        native = {
            "kind": "codex-cli", "installed": True,
            "authentication": "unknown", "state": "configuration-error",
            "can_login": True, "problem": "Error loading configuration: bad value",
        }
        with mock.patch.object(
            subscription_cli, "connection_status", return_value=native,
        ):
            found = connections.connection_status(config, "codex")
        self.assertEqual(found["state"], "configuration-error")
        self.assertIn("signing in again will not help", found["note"])
        self.assertNotIn("Not logged in", found["note"])

    def test_codex_isolation_capability_turns_a_config_error_into_deferred_readiness(self) -> None:
        config = self.config({
            "codex": {
                "kind": "codex-cli", "model": "gpt", "command": ["codex.exe"],
            },
        })
        native = {
            "kind": "codex-cli", "installed": True,
            "authentication": "unknown", "state": "isolated-ready",
            "can_login": False, "config_ignored_for_exec": True,
            "problem": "Error loading configuration: unknown variant ultra",
        }
        with mock.patch.object(
            subscription_cli, "connection_status", return_value=native,
        ):
            found = connections.connection_status(config, "codex")
        self.assertEqual(found["state"], "isolated-ready")
        self.assertIn("first isolated request", found["note"])
        self.assertIn("sign-in again is not required", found["note"])
        self.assertNotIn("unknown variant", found["note"])
        self.assertIn("not a current agent-turn failure", found["note"])
        self.assertFalse(found["can_login"])

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

    def test_codex_config_error_probes_and_reports_isolated_exec_readiness(self) -> None:
        exact = r"C:\exact\codex.exe"
        responses = [
            self.result(1, error="Error loading configuration: unknown variant ultra"),
            self.result(0, out="Usage: codex exec --ignore-user-config"),
        ]
        with mock.patch.object(subscription_cli, "available", return_value=exact), \
             mock.patch.object(
                 subscription_cli, "_run_bounded", side_effect=responses,
             ) as run:
            found = subscription_cli.connection_status(
                "codex-cli", command=[exact], use_cache=False,
            )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0], [exact, "exec", "--help"])
        self.assertEqual(found["state"], "isolated-ready")
        self.assertTrue(found["config_ignored_for_exec"])
        self.assertEqual(found["authentication"], "unknown")
        self.assertFalse(found["can_login"])

    def test_truncated_codex_help_cannot_claim_isolated_readiness(self) -> None:
        exact = r"C:\exact\codex.exe"
        responses = [
            self.result(1, error="Error loading config.toml: unknown variant ultra"),
            CommandResult(
                [exact, "exec", "--help"], ".", 0,
                "Usage: codex exec --ignore-user-config", "", 1,
                output_truncated=True,
            ),
        ]
        with mock.patch.object(subscription_cli, "available", return_value=exact), \
             mock.patch.object(subscription_cli, "_run_bounded", side_effect=responses):
            found = subscription_cli.connection_status(
                "codex-cli", command=[exact], use_cache=False,
            )
        self.assertEqual(found["state"], "configuration-error")
        self.assertNotEqual(found["state"], "isolated-ready")

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


class RememberedCliVersionTests(unittest.TestCase):
    """An unchanged CLI is not started again on every app start."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        patched = mock.patch.dict(os.environ, {"OUR_HARNESS_SWARM_RUN_DIR": str(self.root / "runtime")})
        patched.start()
        self.addCleanup(patched.stop)
        from our_harness.providers import version_memory
        self.memory = version_memory

    def launcher(self) -> tuple[Path, Path]:
        target = self.root / "npm" / "node_modules" / "tool" / "cli.js"
        target.parent.mkdir(parents=True)
        target.write_text("console.log('1.0.0')", encoding="utf-8")
        shim = self.root / "npm" / "tool.cmd"
        shim.write_text("@ECHO off\n" + r'"%dp0%\node_modules\tool\cli.js" %*' + "\n", encoding="utf-8")
        return shim, target

    def test_a_launcher_answer_is_reused_until_its_package_changes(self) -> None:
        shim, target = self.launcher()
        self.memory.remember("seen.json", ["key"], str(shim), [1, 0, 0])
        self.assertEqual(self.memory.load("seen.json"), [(["key"], [1, 0, 0])])
        # npm updating the package leaves the launcher itself untouched.
        target.write_text("console.log('2.0.0') // updated", encoding="utf-8")
        self.assertEqual(self.memory.load("seen.json"), [])

    def test_an_executable_is_identified_by_its_own_file(self) -> None:
        program = self.root / "tool.exe"
        program.write_bytes(b"MZ one")
        self.memory.remember("seen.json", ["exe"], str(program), {"state": "observed"})
        self.assertEqual(len(self.memory.load("seen.json")), 1)
        program.write_bytes(b"MZ two, a newer build")
        self.assertEqual(self.memory.load("seen.json"), [])

    def test_a_program_that_cannot_be_identified_is_never_remembered(self) -> None:
        script = self.root / "tool"
        script.write_text("#!/bin/sh\necho 1.0", encoding="utf-8")
        self.memory.remember("seen.json", ["sh"], str(script), [1, 0])
        empty = self.root / "empty.cmd"
        empty.write_text("@ECHO off\r\necho nothing\r\n", encoding="utf-8")
        self.memory.remember("seen.json", ["cmd"], str(empty), [1, 0])
        self.assertEqual(self.memory.load("seen.json"), [])
        (self.root / "runtime").mkdir(exist_ok=True)
        (self.root / "runtime" / "seen.json").write_text("{damaged", encoding="utf-8")
        self.assertEqual(self.memory.load("seen.json"), [])

    def test_a_restart_reuses_the_version_instead_of_starting_the_cli(self) -> None:
        shim, _target = self.launcher()
        answered = CommandResult(["tool", "--version"], str(self.root), 0, "tool 3.4.5\n", "", 1)
        with mock.patch.object(subscription_cli, "_run_bounded", return_value=answered) as ran:
            self.assertEqual(subscription_cli._the_version_of(str(shim)), (3, 4, 5))
            subscription_cli._tool_version_cache.clear()  # a new process
            self.assertEqual(subscription_cli._the_version_of(str(shim)), (3, 4, 5))
        self.assertEqual(ran.call_count, 1)
