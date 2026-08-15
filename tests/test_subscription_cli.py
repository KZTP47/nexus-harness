from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig, validate_config
from our_harness.models import HarnessError, ProviderRequest, ResponseFormat
from our_harness.providers import subscription_cli
from our_harness.providers.subscription_cli import (
    CLAUDE_RECIPE,
    COPILOT_RECIPE,
    CliRecipe,
    SubscriptionCLIProvider,
    recipe_for,
)


def fake_tool(folder: Path, name: str, body: str) -> Path:
    """A small program that stands in for a real assistant's command line."""

    script = folder / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        launcher = folder / f"{name}.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = folder / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


# Answers the way Claude Code really does with --output-format json.
CLAUDE_LIKE = '''
import json, sys
arguments = sys.argv[1:]
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
prompt = sys.stdin.read()
answer = "SAW_SCHEMA" if "ANSWER FORMAT" in prompt else prompt.strip().splitlines()[-1]
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": answer, "session_id": "fake",
    "usage": {"input_tokens": 11, "output_tokens": 22},
    "model_asked": [a for a in arguments if not a.startswith("-")],
}))
'''

CLAUDE_REFUSES = '''
import json, sys
if "--version" in sys.argv[1:]:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "result": "Your organization does not have access to Claude.",
    "usage": {"input_tokens": 0, "output_tokens": 0},
}))
'''

PLAIN_TEXT_TOOL = '''
import sys
if "--version" in sys.argv[1:]:
    print("1.0 (Fake plain tool)")
    raise SystemExit(0)
sys.stdin.read()
sys.stdout.write("```json\\n{\\"ok\\": true}\\n```\\n")
'''

BROKEN_TOOL = '''
import sys
if "--version" in sys.argv[1:]:
    print("1.0")
    raise SystemExit(0)
sys.stderr.write("something went wrong\\n")
raise SystemExit(3)
'''

SLOW_TOOL = '''
import sys, time
if "--version" in sys.argv[1:]:
    print("1.0")
    raise SystemExit(0)
time.sleep(30)
'''


class RecipeTests(unittest.TestCase):
    def test_the_model_is_passed_through(self) -> None:
        self.assertEqual(
            CLAUDE_RECIPE.argv(["claude"], "claude-sonnet-4-5"),
            ["claude", "-p", "--output-format", "json", "--model", "claude-sonnet-4-5"],
        )

    def test_a_missing_model_drops_its_flag_as_well(self) -> None:
        """A bare --model with nothing after it would confuse the tool."""

        self.assertEqual(
            CLAUDE_RECIPE.argv(["claude"], ""),
            ["claude", "-p", "--output-format", "json"],
        )

    def test_the_command_itself_is_never_dropped(self) -> None:
        recipe = CliRecipe(id="x", label="x", command=("tool",), arguments=("{model}",))
        self.assertEqual(recipe.argv(["tool"], ""), ["tool"])

    def test_every_shipped_recipe_says_how_to_install_its_tool(self) -> None:
        for name in subscription_cli.SUBSCRIPTION_KINDS:
            with self.subTest(name=name):
                recipe = recipe_for(name)
                self.assertTrue(recipe.install_hint.strip(), f"{name} says nothing about installing")
                self.assertTrue(recipe.label.strip())

    def test_an_unknown_recipe_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            recipe_for("nonsense-cli")


class RunningTests(unittest.TestCase):
    """These drive a real child process, not a stand-in inside this one."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def provider(self, kind: str, tool: Path, **settings: object) -> SubscriptionCLIProvider:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": kind, "model": "fake-model", "endpoint": "", "api_key_env": "",
            "timeout_seconds": 60,
        })
        # Name the stand-in as the command, so this never reaches a real tool.
        data["provider"]["command"] = [tool.stem]
        data["provider"].update(settings)
        config = LoadedConfig(data, self.folder, [], {})
        provider = SubscriptionCLIProvider(config, kind)
        # The stand-in lives in a temporary folder, so point the lookup at it.
        real_which = subscription_cli.shutil.which
        patch = mock.patch.object(
            subscription_cli.shutil, "which",
            lambda name, *rest, **kw: str(tool) if Path(name).stem == tool.stem else real_which(name),
        )
        patch.start()
        self.addCleanup(patch.stop)
        return provider

    def request(self, text: str = "Say hello", **extra: object) -> ProviderRequest:
        return ProviderRequest(
            "SYSTEM", "CONTEXT", [{"role": "user", "content": text}], "fake-model", **extra
        )

    def test_a_real_child_process_answers_and_its_tokens_are_recorded(self) -> None:
        tool = fake_tool(self.folder, "faketool", CLAUDE_LIKE)
        answer = self.provider("claude-cli", tool).complete(self.request("Say hello"))
        self.assertEqual(answer.text, "Say hello")
        self.assertEqual(answer.input_tokens, 11)
        self.assertEqual(answer.output_tokens, 22)
        self.assertEqual(answer.raw["price_status"], "subscription-unpriced")

    def test_a_response_schema_is_put_in_front_of_the_tool(self) -> None:
        tool = fake_tool(self.folder, "faketool", CLAUDE_LIKE)
        wanted = ResponseFormat("demo", {"type": "object", "properties": {"a": {"type": "string"}}})
        answer = self.provider("claude-cli", tool).complete(self.request(response_format=wanted))
        self.assertEqual(answer.text, "SAW_SCHEMA")

    def test_a_refusal_is_reported_even_when_the_tool_exits_cleanly(self) -> None:
        """Claude answers with is_error true while still saying success."""

        tool = fake_tool(self.folder, "faketool", CLAUDE_REFUSES)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        self.assertIn("does not have access", str(caught.exception))

    def test_a_tool_that_prints_plain_text_is_read_too(self) -> None:
        tool = fake_tool(self.folder, "plaintool", PLAIN_TEXT_TOOL)
        answer = self.provider("copilot-cli", tool).complete(self.request())
        self.assertEqual(answer.text, '{"ok": true}', "a fenced block should be unwrapped")

    def test_a_tool_that_fails_reports_what_it_said(self) -> None:
        tool = fake_tool(self.folder, "brokentool", BROKEN_TOOL)
        with self.assertRaises(HarnessError) as caught:
            self.provider("copilot-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("stopped with code 3", message)
        self.assertIn("something went wrong", message)

    def test_a_tool_that_hangs_is_stopped_at_the_limit(self) -> None:
        tool = fake_tool(self.folder, "slowtool", SLOW_TOOL)
        with self.assertRaises(HarnessError) as caught:
            self.provider("copilot-cli", tool, timeout_seconds=2).complete(self.request())
        self.assertIn("ran past its", str(caught.exception))

    def test_a_tool_that_is_not_installed_says_how_to_get_it(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({"name": "claude-cli", "model": "m", "endpoint": "", "api_key_env": ""})
        provider = SubscriptionCLIProvider(LoadedConfig(data, self.folder, [], {}), "claude-cli")
        with mock.patch.object(subscription_cli.shutil, "which", return_value=None):
            with self.assertRaises(HarnessError) as caught:
                provider.complete(self.request())
        self.assertIn("Install Claude Code", str(caught.exception))

    def test_the_arguments_can_be_changed_without_touching_the_code(self) -> None:
        tool = fake_tool(self.folder, "faketool", CLAUDE_LIKE)
        provider = self.provider("claude-cli", tool, arguments=["--version-check", "off", "-p"])
        # The stand-in echoes back the words that are not flags, so the changed
        # argument list really reached the child process.
        answer = provider.complete(self.request("last line"))
        self.assertEqual(answer.text, "last line")

    def test_native_tool_calls_are_refused_with_a_clear_reason(self) -> None:
        tool = fake_tool(self.folder, "faketool", CLAUDE_LIKE)
        provider = self.provider("claude-cli", tool)
        with self.assertRaises(HarnessError) as caught:
            provider.complete(self.request(tools=[{"name": "x"}]))
        self.assertIn("one prompt at a time", str(caught.exception))

    def test_the_prompt_carries_the_system_text_and_the_conversation(self) -> None:
        seen = self.folder / "seen.txt"
        recorder = (
            "import sys\n"
            "if '--version' in sys.argv[1:]:\n"
            "    print('1.0')\n"
            "    raise SystemExit(0)\n"
            f"open({str(seen)!r}, 'w', encoding='utf-8').write(sys.stdin.read())\n"
            "print('done')\n"
        )
        tool = fake_tool(self.folder, "recorder", recorder)
        self.provider("copilot-cli", tool).complete(self.request("Fix the parser"))
        prompt = seen.read_text(encoding="utf-8")
        self.assertIn("SYSTEM INSTRUCTIONS", prompt)
        self.assertIn("SYSTEM", prompt)
        self.assertIn("UNTRUSTED DATA", prompt)
        self.assertIn("Fix the parser", prompt)


class ConfigTests(unittest.TestCase):
    def config(self, **provider: object) -> dict:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update(provider)
        return data

    def test_a_signed_in_assistant_is_a_valid_provider(self) -> None:
        for kind in ("claude-cli", "copilot-cli", "assistant-cli"):
            with self.subTest(kind=kind):
                validate_config(self.config(name=kind, model="m", endpoint="", api_key_env=""))

    def test_a_signed_in_assistant_may_not_carry_an_endpoint_or_a_key(self) -> None:
        for change in ({"endpoint": "https://api.example.com"}, {"api_key_env": "SOME_KEY"}):
            with self.subTest(change=change), self.assertRaises(HarnessError):
                validate_config(self.config(**{
                    "name": "claude-cli", "model": "m", "endpoint": "", "api_key_env": "", **change,
                }))

    def test_a_signed_in_assistant_may_leave_the_model_empty(self) -> None:
        validate_config(self.config(name="claude-cli", model="", endpoint="", api_key_env=""))

    def test_an_ordinary_provider_still_needs_its_endpoint(self) -> None:
        with self.assertRaises(HarnessError):
            validate_config(self.config(name="ollama", model="m", endpoint=""))

    def test_named_routes_may_mix_subscriptions_and_ordinary_providers(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "claude": {"kind": "claude-cli", "model": "claude-sonnet-4-5", "endpoint": ""},
            "copilot": {"kind": "copilot-cli", "model": "gpt-5", "endpoint": ""},
            "local": {"kind": "ollama", "model": "qwen2.5-coder:7b", "endpoint": "http://127.0.0.1:11434"},
        }
        validate_config(data)

    def test_the_provider_factory_builds_the_right_thing(self) -> None:
        from our_harness.providers import create_provider

        with tempfile.TemporaryDirectory() as temporary:
            for kind in ("claude-cli", "copilot-cli", "assistant-cli"):
                with self.subTest(kind=kind):
                    data = self.config(name=kind, model="m", endpoint="", api_key_env="")
                    built = create_provider(LoadedConfig(data, Path(temporary), [], {}))
                    self.assertIsInstance(built, SubscriptionCLIProvider)
                    self.assertEqual(built.recipe.id, kind)


if __name__ == "__main__":
    unittest.main()


class TrustCommandTests(unittest.TestCase):
    """Editing the local config by hand must have a way back to trusted."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.local = self.root / ".harness" / "config.local.json"
        self.addCleanup(self.temporary.cleanup)

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        from our_harness import cli

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--project", str(self.root), *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_a_missing_file_says_what_to_do(self) -> None:
        code, _out, errors = self.run_cli("trust", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("harness init", errors)

    def test_show_reports_untrusted_then_trusted(self) -> None:
        self.local.write_text('{"provider": {"name": "claude-cli"}}', encoding="utf-8")
        with mock.patch("our_harness.cli.is_project_local_config_trusted", return_value=False):
            code, output, _ = self.run_cli("trust", "--show")
        self.assertEqual(code, 1)
        self.assertIn("not trusted yet", output)
        with mock.patch("our_harness.cli.is_project_local_config_trusted", return_value=True):
            code, output, _ = self.run_cli("trust", "--show")
        self.assertEqual(code, 0)
        self.assertIn("This file is trusted", output)

    def test_trusting_shows_the_file_first_and_records_it(self) -> None:
        self.local.write_text('{"provider": {"name": "claude-cli"}}', encoding="utf-8")
        recorded: list = []
        with mock.patch("our_harness.cli.trust_project_local_config", side_effect=lambda *a: recorded.append(a) or Path("store.json")):
            code, output, _ = self.run_cli("trust", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("claude-cli", output, "the file should be shown before it is trusted")
        self.assertIn("Trusted.", output)
        self.assertEqual(len(recorded), 1)

    def test_saying_no_changes_nothing(self) -> None:
        self.local.write_text("{}", encoding="utf-8")
        with mock.patch("builtins.input", return_value="n"), \
             mock.patch("our_harness.cli.trust_project_local_config") as recorder:
            code, output, _ = self.run_cli("trust")
        self.assertEqual(code, 1)
        self.assertIn("Left as it was", output)
        recorder.assert_not_called()
