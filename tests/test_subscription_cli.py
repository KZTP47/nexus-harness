from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
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

# Says why it will not answer, in the place the recipe says to look, and then
# stops with a code anyway. Both real tools do this, and reading the code
# instead of the sentence is what put a page of JSON in front of somebody.
REFUSES_AND_STOPS = '''
import json, sys
arguments = sys.argv[1:]
if arguments[:2] == ["auth", "status"]:
    print(json.dumps({
        "loggedIn": True, "email": "somebody@example.test",
        "orgName": "A Company", "subscriptionType": "pro",
    }))
    raise SystemExit(0)
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "result": "Your organization does not have access to Claude.",
    "usage": {"input_tokens": 0, "output_tokens": 0},
}))
raise SystemExit(1)
'''

# Prints a line before the answer, the way a tool with a banner or a word of
# progress does. Read as one whole object and nothing else, the reason in here
# was never found.
REFUSES_AFTER_A_BANNER = '''
import json, sys
arguments = sys.argv[1:]
if arguments[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "orgName": "A Company"}))
    raise SystemExit(0)
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
print("Welcome to the fake tool")
print(json.dumps({"is_error": True, "result": "The banner reason"}))
print("Goodbye from the fake tool")
raise SystemExit(1)
'''

# One object per line, which is how these tools stream. The last word on it is
# the one that counts.
REFUSES_ONE_LINE_AT_A_TIME = '''
import json, sys
arguments = sys.argv[1:]
if arguments[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "orgName": "A Company"}))
    raise SystemExit(0)
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({"type": "progress", "is_error": False}))
print(json.dumps({"is_error": True, "result": "The first thing it said"}))
print(json.dumps({"is_error": True, "result": "The last word on it"}))
raise SystemExit(1)
'''

# Stops with a code and says nothing anybody can read, which is the one case
# where the code is all there is to say.
STOPS_AND_SAYS_NOTHING_READABLE = '''
import json, sys
arguments = sys.argv[1:]
if arguments[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "orgName": "A Company"}))
    raise SystemExit(0)
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
sys.stdout.write("x" * 9000)
raise SystemExit(4)
'''

# Takes its time answering about its own sign-in, so a caller with little time
# left is not made to wait for an explanation.
SLOW_ABOUT_ITSELF = '''
import json, sys, time
arguments = sys.argv[1:]
if arguments[:2] == ["auth", "status"]:
    time.sleep(30)
    raise SystemExit(0)
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({"is_error": True, "result": "No thank you"}))
raise SystemExit(1)
'''

# Turns the request down and says, in the same answer, that the service took no
# time at all. Nothing was asked: it decided here, out of what it has written
# down about the account. Claude does exactly this, and reading it as the
# service saying no sent somebody to their administrator for nothing.
REFUSES_WITHOUT_ASKING = '''
import json, sys
arguments = sys.argv[1:]
if arguments[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "orgName": "A Company"}))
    raise SystemExit(0)
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({
    "is_error": True, "duration_api_ms": 0,
    "result": "Your organization does not have access. Please login again.",
}))
raise SystemExit(1)
'''

# Asks, waits, and is turned down by the service - which is somebody else's
# answer and a different thing to do about it.
REFUSED_BY_THE_SERVICE = '''
import json, sys
arguments = sys.argv[1:]
if arguments[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "orgName": "A Company"}))
    raise SystemExit(0)
if "--version" in arguments:
    print("9.9.9 (Fake Claude)")
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({
    "is_error": True, "duration_api_ms": 412,
    "result": "That model is not available to you.",
}))
raise SystemExit(1)
'''

BROKEN_TOOL = '''
import sys
if "--version" in sys.argv[1:]:
    print("1.0")
    raise SystemExit(0)
sys.stderr.write("something went wrong\\n")
raise SystemExit(3)
'''

ARGUMENT_ECHO = '''
import json, sys
arguments = sys.argv[1:]
if "--version" in arguments and len(arguments) == 1:
    print("1.0")
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({"is_error": False, "result": json.dumps(arguments)}))
'''

SLOW_TOOL = '''
import sys, time
if "--version" in sys.argv[1:]:
    print("1.0")
    raise SystemExit(0)
time.sleep(30)
'''

VERSION_HANGS_BUT_REQUEST_WORKS = '''
import json, sys, time
if "--version" in sys.argv[1:]:
    time.sleep(30)
    raise SystemExit(0)
sys.stdin.read()
print(json.dumps({"is_error": False, "result": "the real request answered"}))
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

    def test_a_model_argument_with_no_flag_in_front_is_refused(self) -> None:
        """Dropping it would leave the tool with one fewer argument than expected."""

        recipe = CliRecipe(id="x", label="x", command=("tool",), arguments=("{model}",))
        with self.assertRaises(HarnessError) as caught:
            recipe.argv(["tool"], "")
        self.assertIn("straight after a flag", str(caught.exception))

    def test_a_flag_joined_to_the_model_by_an_equals_sign_works(self) -> None:
        recipe = CliRecipe(id="x", label="x", command=("tool",), arguments=("--model={model}",))
        self.assertEqual(recipe.argv(["tool"], "m"), ["tool", "--model=m"])
        self.assertEqual(recipe.argv(["tool"], ""), ["tool"])

    def test_every_shipped_recipe_has_a_workable_argument_list(self) -> None:
        for name in subscription_cli.SUBSCRIPTION_KINDS:
            with self.subTest(name=name):
                recipe_for(name).check()

    def test_copilot_ordinary_prompt_never_grants_all_tools(self) -> None:
        argv = COPILOT_RECIPE.argv(["copilot"], "gpt-5")
        self.assertNotIn("--allow-all-tools", argv)
        self.assertNotIn("-p", argv, "the dynamic prompt value is inserted at runtime")

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
        """Claude answers with is_error true while still saying success.

        And what is said about who refused is checked, not only that the reason
        came through. Checking the reason alone, this passed just the same while
        a tool that prints no timing was being told the service turned it down.
        """

        tool = fake_tool(self.folder, "faketool", CLAUDE_REFUSES)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("does not have access", message)
        self.assertNotIn("what turned this down was the service", message)

    def test_a_tool_that_prints_plain_text_is_read_too(self) -> None:
        tool = fake_tool(self.folder, "plaintool", PLAIN_TEXT_TOOL)
        answer = self.provider("copilot-cli", tool).complete(self.request())
        self.assertEqual(answer.text, '{"ok": true}', "a fenced block should be unwrapped")

    def test_one_large_outer_fence_preserves_nested_source_fences(self) -> None:
        source = "before\n```typescript\nconst result = `ok`;\n```\nafter"
        payload = json.dumps({"files": [{"path": "demo.md", "content": source}],
                              "padding": "x" * 35_700})
        wrapped = f"```json\n{payload}\n```"
        self.assertGreater(len(wrapped), 35_789)
        decoded = subscription_cli._plain_text(wrapped)
        self.assertEqual(decoded, payload)
        self.assertEqual(json.loads(decoded)["files"][0]["content"], source)

    def test_leading_prose_is_not_mistaken_for_an_outer_fence(self) -> None:
        raw = "Here is the answer:\n```json\n{\"ok\": true}\n```"
        self.assertEqual(subscription_cli._plain_text(raw), raw)

    def test_a_tool_that_never_asked_anybody_is_not_the_service_saying_no(self) -> None:
        """The one the person in front of it was right about. Claude answers with
        the service having taken no time at all, which means it never asked: it
        turned the job down here, out of what it has written down about the
        account. Read as the service saying no, the harness pointed at an
        administrator who has nothing to do with it, while the tool's own words
        said the useful thing - please login again."""

        tool = fake_tool(self.folder, "faketool", REFUSES_WITHOUT_ASKING)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("never asked anybody", message)
        self.assertIn("claude auth login", message)
        self.assertNotIn("what turned this down was the service", message)
        self.assertNotIn("setup-token", message)

    def test_a_tool_the_service_turned_down_says_that_instead(self) -> None:
        tool = fake_tool(self.folder, "faketool", REFUSED_BY_THE_SERVICE)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("what turned this down was the service", message)
        self.assertNotIn("never asked anybody", message)
        self.assertIn("claude auth login", message)
        self.assertIn("contact Anthropic support", message)

    def test_a_tool_that_does_not_say_either_way_gets_no_guess(self) -> None:
        """Nothing is a real answer. A tool that says nothing about how long the
        service took is not told what it meant."""

        tool = fake_tool(self.folder, "brokentool", BROKEN_TOOL)
        with self.assertRaises(HarnessError) as caught:
            self.provider("copilot-cli", tool).complete(self.request())
        self.assertNotIn("never asked anybody", str(caught.exception))

    def test_a_reason_is_found_after_a_banner(self) -> None:
        """A line of progress or a word of welcome, and reading it as one whole
        object found nothing - which dropped somebody into the message that says
        only what code it stopped with."""

        tool = fake_tool(self.folder, "faketool", REFUSES_AFTER_A_BANNER)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("The banner reason", message)
        self.assertNotIn("stopped with code", message)
        self.assertNotIn("Welcome to the fake tool", message)

    def test_a_reason_is_found_when_it_prints_one_object_a_line(self) -> None:
        """Which is how these tools stream, and the last word on it is the one
        that counts."""

        tool = fake_tool(self.folder, "faketool", REFUSES_ONE_LINE_AT_A_TIME)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("The last word on it", message)
        self.assertNotIn("The first thing it said", message)

    def test_what_it_printed_never_comes_before_the_words(self) -> None:
        """Machine output in front of the sentence is what this was written to
        stop. Put there, whatever reads the message afterwards looking for a
        sentence at the end finds the page instead."""

        tool = fake_tool(self.folder, "faketool", STOPS_AND_SAYS_NOTHING_READABLE)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("stopped with code 4", message)
        self.assertLess(
            message.index("stopped with code 4"), message.index("It printed:"),
            "the words come first and what it printed comes last",
        )
        # A glimpse, not the page. Nine thousand letters went in.
        self.assertLess(len(message), 1200, message[:200])

    def test_asking_it_about_itself_stays_inside_the_time_allowed(self) -> None:
        """Given time of its own, a call told to take no more than a few seconds
        took twenty - and the extra was spent explaining a failure that had
        already happened."""

        tool = fake_tool(self.folder, "slowtool", SLOW_ABOUT_ITSELF)
        started = time.monotonic()
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool, timeout_seconds=6).complete(self.request())
        took = time.monotonic() - started
        self.assertIn("No thank you", str(caught.exception))
        self.assertLess(took, 20, f"it took {took:.1f} seconds")

    def test_a_tool_that_says_why_and_stops_anyway_is_read_for_the_reason(self) -> None:
        """Both real tools do this: the sentence is in the answer and the exit
        code is not zero. Read for the code instead, what somebody saw was
        "stopped with code 1" and a page of JSON, which told them nothing."""

        tool = fake_tool(self.folder, "faketool", REFUSES_AND_STOPS)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("does not have access", message)
        self.assertNotIn("stopped with code", message)
        self.assertNotIn("is_error", message, "the raw answer should not be in a sentence")

    def test_a_refusal_says_what_the_harness_knows_as_well(self) -> None:
        """The tool's own sentence, read on its own, says the wrong thing: it
        tells somebody looking at a working Claude window that they have no
        Claude. What the harness knows and was not saying is that the tool is
        here, that it answered, and what it says about its own sign-in."""

        tool = fake_tool(self.folder, "faketool", REFUSES_AND_STOPS)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("is on this machine and did answer", message)
        self.assertIn("signed in", message)
        self.assertNotIn("somebody@example.test", message)
        self.assertNotIn("A Company", message)
        self.assertNotIn(", pro", message)
        # This one prints no timing, so it is not known whether it asked
        # anybody, and neither answer is claimed. What it does get is the order
        # to try things in, cheapest first.
        self.assertIn("neither is claimed", message)
        self.assertIn("claude auth login", message)

    def test_auth_status_keeps_only_the_fact_that_it_is_signed_in(self) -> None:
        said = subscription_cli._in_a_few_words(json.dumps({
            "loggedIn": True,
            "email": "somebody@example.test",
            "orgName": "A Company",
            "subscriptionType": "pro",
        }))
        self.assertEqual(said, "signed in")

    def test_a_tool_that_prints_no_timing_is_not_told_the_service_refused_it(self) -> None:
        """The one that came back. Whether it asked anybody has three answers,
        and folding "it did not say" in with "yes it asked" put the wrong
        sentence in front of every tool that prints no timing - which is the
        sentence this was all written to stop."""

        tool = fake_tool(self.folder, "faketool", REFUSES_AND_STOPS)
        with self.assertRaises(HarnessError) as caught:
            self.provider("claude-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertNotIn("what turned this down was the service", message)
        self.assertNotIn("never asked anybody", message)
        self.assertNotIn("setup-token", message, "which is only for a real refusal")

    def test_the_three_answers_each_say_something_different(self) -> None:
        """Said side by side, because the whole point of the three is that they
        send somebody to three different places."""

        seen = {}
        for what, source in (
            ("never asked", REFUSES_WITHOUT_ASKING),
            ("the service refused", REFUSED_BY_THE_SERVICE),
            ("it did not say", REFUSES_AND_STOPS),
        ):
            tool = fake_tool(self.folder, f"tool-{len(seen)}", source)
            with self.assertRaises(HarnessError) as caught:
                self.provider("claude-cli", tool).complete(self.request())
            seen[what] = str(caught.exception)
        self.assertEqual(len(set(seen.values())), 3, "two of them read the same")
        self.assertIn("never asked anybody", seen["never asked"])
        self.assertIn("was the service behind it", seen["the service refused"])
        self.assertIn("neither is claimed", seen["it did not say"])

    def test_a_tool_that_cannot_be_asked_about_itself_still_says_the_rest(self) -> None:
        """Anything that goes wrong while asking is left out rather than piled
        on top. This is already an error message."""

        tool = fake_tool(self.folder, "brokentool", BROKEN_TOOL)
        with self.assertRaises(HarnessError) as caught:
            self.provider("copilot-cli", tool).complete(self.request())
        message = str(caught.exception)
        self.assertIn("stopped with code 3", message)
        self.assertIn("is on this machine and did answer", message)

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

    def test_a_hung_version_banner_does_not_hide_the_real_request(self) -> None:
        tool = fake_tool(self.folder, "versionhang", VERSION_HANGS_BUT_REQUEST_WORKS)
        started = time.monotonic()
        answer = self.provider("claude-cli", tool, timeout_seconds=10).complete(self.request())
        self.assertEqual(answer.text, "the real request answered")
        self.assertLess(time.monotonic() - started, 8)

    def test_a_tool_that_is_not_installed_says_how_to_get_it(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({"name": "claude-cli", "model": "m", "endpoint": "", "api_key_env": ""})
        provider = SubscriptionCLIProvider(LoadedConfig(data, self.folder, [], {}), "claude-cli")
        with mock.patch.object(subscription_cli.shutil, "which", return_value=None), \
                mock.patch.object(subscription_cli, "_every_build_of", return_value=[]):
            with self.assertRaises(HarnessError) as caught:
                provider.complete(self.request())
        self.assertIn("Install Claude Code", str(caught.exception))

    def test_the_arguments_can_be_changed_without_touching_the_code(self) -> None:
        """The stand-in reports its own argument list, so this proves what ran."""

        tool = fake_tool(self.folder, "echotool", ARGUMENT_ECHO)
        provider = self.provider(
            "claude-cli", tool, arguments=["--set", "quiet", "-p", "--model", "{model}"]
        )
        answer = provider.complete(self.request())
        self.assertEqual(
            json.loads(answer.text),
            ["--set", "quiet", "-p", "--model", "fake-model"],
            "the configured arguments must be exactly what the child was given",
        )

    def test_the_built_in_arguments_are_what_the_child_really_gets(self) -> None:
        tool = fake_tool(self.folder, "echotool", ARGUMENT_ECHO)
        answer = self.provider("claude-cli", tool).complete(self.request())
        self.assertEqual(
            json.loads(answer.text),
            ["-p", "--output-format", "json", "--model", "fake-model", "--tools", ""],
        )

    def test_an_argument_shape_that_cannot_be_dropped_is_refused(self) -> None:
        tool = fake_tool(self.folder, "echotool", ARGUMENT_ECHO)
        provider = self.provider("claude-cli", tool, arguments=["out-{model}.txt"])
        with self.assertRaises(HarnessError) as caught:
            provider.complete(self.request())
        self.assertIn("shape the harness cannot drop", str(caught.exception))

    def test_native_tool_calls_are_refused_with_a_clear_reason(self) -> None:
        tool = fake_tool(self.folder, "faketool", CLAUDE_LIKE)
        provider = self.provider("claude-cli", tool)
        with self.assertRaises(HarnessError) as caught:
            provider.complete(self.request(tools=[{"name": "x"}]))
        self.assertIn("one prompt at a time", str(caught.exception))

    def test_the_prompt_carries_the_system_text_and_the_conversation(self) -> None:
        seen = self.folder / "seen.txt"
        recorder = (
            "import json, sys\n"
            "if '--version' in sys.argv[1:]:\n"
            "    print('1.0')\n"
            "    raise SystemExit(0)\n"
            f"open({str(seen)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
            "print('done')\n"
        )
        tool = fake_tool(self.folder, "recorder", recorder)
        self.provider("copilot-cli", tool).complete(self.request("Fix the parser"))
        argv = json.loads(seen.read_text(encoding="utf-8"))
        self.assertIn("-p", argv)
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("SYSTEM INSTRUCTIONS", prompt)
        self.assertIn("SYSTEM", prompt)
        self.assertIn("UNTRUSTED DATA", prompt)
        self.assertIn("Fix the parser", prompt)
        self.assertIn("-s", argv)
        self.assertIn("--available-tools=", argv)
        self.assertIn("--deny-tool=read,write,shell,url,memory", argv)
        self.assertIn("--disable-builtin-mcps", argv)

    def test_only_verified_answer_only_claude_is_schema_retry_safe(self) -> None:
        tool = fake_tool(self.folder, "plain", "print('done')\n")
        self.assertTrue(self.provider("claude-cli", tool).structured_retry_is_safe)
        self.assertFalse(self.provider("gemini-cli", tool).structured_retry_is_safe)
        self.assertFalse(self.provider("copilot-cli", tool).structured_retry_is_safe)

    def test_gemini_does_not_mislabel_an_empty_approval_list_as_no_tools(self) -> None:
        seen = self.folder / "gemini-argv.json"
        recorder = (
            "import json, sys\n"
            "if '--version' in sys.argv[1:]:\n"
            "    print('1.0')\n"
            "    raise SystemExit(0)\n"
            f"open({str(seen)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'response': 'done'}))\n"
        )
        tool = fake_tool(self.folder, "gemini-recorder", recorder)
        provider = self.provider("gemini-cli", tool)
        self.assertEqual(provider.complete(self.request()).text, "done")
        argv = json.loads(seen.read_text(encoding="utf-8"))
        self.assertNotIn("--allowed-tools", argv)
        self.assertFalse(provider.structured_retry_is_safe)


class WhatCountsAsTheToolTalkingTests(unittest.TestCase):
    """Which of the things a tool printed are the tool's own answer.

    Read as "everything from the first brace to the last one", a line of
    ordinary text with a brace in it was read as the tool's answer - so a line
    saying a thing had been rejected came back as the tool refusing, in those
    words. What goes into these tools is not always something anybody chose, so
    that was a way to put words in the tool's mouth.
    """

    def test_an_object_in_the_middle_of_a_line_of_words_is_not_an_answer(self) -> None:
        said = ('debug: raw candidate was {"is_error": true, '
                '"result": "SHOULD NOT LEAK"} but rejected')
        self.assertEqual(subscription_cli._every_object_in(said), [])

    def test_words_after_an_object_on_one_line_are_not_an_answer_either(self) -> None:
        said = '{"is_error": true, "result": "SHOULD NOT LEAK"} and then some words'
        self.assertEqual(subscription_cli._every_object_in(said), [])

    def test_one_object_a_line_is_read(self) -> None:
        said = "\n".join([
            "Welcome",
            '{"is_error": false}',
            '{"is_error": true, "result": "the real one"}',
            "Bye",
        ])
        found = subscription_cli._every_object_in(said)
        self.assertEqual([one.get("result") for one in found], [None, "the real one"])

    def test_one_object_written_across_several_lines_is_read(self) -> None:
        said = "\n".join([
            "Welcome",
            "{",
            '  "is_error": true,',
            '  "result": "across lines"',
            "}",
            "Bye",
        ])
        found = subscription_cli._every_object_in(said)
        self.assertEqual([one.get("result") for one in found], ["across lines"])

    def test_two_objects_with_nothing_between_them_are_both_read(self) -> None:
        said = '{"a": 1}{"is_error": true, "result": "joined up"}'
        found = subscription_cli._every_object_in(said)
        self.assertEqual([one.get("result") for one in found], [None, "joined up"])

    def test_something_nested_too_deep_to_read_is_not_an_answer(self) -> None:
        """A validly nested object a few thousand deep is what Python cannot
        read, and the error it raises is not the one about shapes. Let out of
        here it went past the one place that catches a route which will not
        answer, and took every other assistant's answer with it."""

        deep = '{"a":' * 28000 + "1" + "}" * 28000
        self.assertLessEqual(len(deep), subscription_cli.LONGEST_RUN)
        with self.assertRaises(RecursionError):
            json.loads(deep)
        self.assertEqual(subscription_cli._every_object_in(deep), [])

    def test_a_run_longer_than_worth_reading_is_left_alone(self) -> None:
        said = "{" + ('"a": 1, ' * 40_000) + '"b": 2}'
        self.assertGreater(len(said), subscription_cli.LONGEST_RUN)
        self.assertEqual(subscription_cli._every_object_in(said), [])

    def test_a_pile_of_broken_lines_is_read_quickly(self) -> None:
        """A line that opens a brace and never closes it, thousands of times.

        The lines here are the length a real tool prints, not the shortest thing
        that fits the shape. Measured with seven letters a line this looked
        cheap, while the same number of sixty-six letter lines took thirty-eight
        seconds: the cost was in walking the run again from the beginning for
        every line added, so the shortest possible line hid all of it.

        There is no clock on this at all - it happens after the program has
        already finished, so nothing else would have stopped it.
        """

        line = '{"attempt": 1, "tool": "claude", "note": "retrying the request now'
        self.assertGreater(len(line), 60)
        for how_many in (2_000, 5_000):
            with self.subTest(lines=how_many):
                said = chr(10).join([line] * how_many)
                started = time.monotonic()
                found = subscription_cli._every_object_in(said)
                took = time.monotonic() - started
                self.assertEqual(found, [])
                self.assertLess(took, 2, f"{how_many} lines took {took:.1f} seconds")

    def test_a_line_inside_an_answer_is_not_the_answer(self) -> None:
        """The one that matters most, because it was wrong rather than missing.

        An answer written out over several lines can hold a list whose last item
        is written compactly on one line - an ordinary mixed style, not a strange
        one. Taking that line as the answer said "transient network hiccup,
        retrying" while the real answer, two lines below it, was thrown away with
        the run it was in.
        """

        said = chr(10).join([
            "{",
            '  "type": "result",',
            '  "attempts": [',
            '    {"is_error": true, "result": "transient network hiccup, retrying"}',
            "  ],",
            '  "is_error": true,',
            '  "result": "Your organization does not have access.",',
            '  "session_id": "abc123"',
            "}",
        ])
        found = subscription_cli._every_object_in(said)
        self.assertEqual(
            [one.get("result") for one in found],
            ["Your organization does not have access."],
        )

    def test_the_reason_read_from_that_shape_is_the_real_one(self) -> None:
        """The same thing said through the part that reads a reason, because
        that is where a wrong sentence would have reached somebody."""

        said = chr(10).join([
            "{",
            '  "attempts": [',
            '    {"is_error": true, "result": "transient network hiccup, retrying"}',
            "  ],",
            '  "is_error": true,',
            '  "result": "Your organization does not have access."',
            "}",
        ])
        holder = SubscriptionCLIProvider(
            LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}), "claude-cli")
        self.assertEqual(
            holder._why_it_would_not(CLAUDE_RECIPE, said, ""),
            "Your organization does not have access.",
        )

    def test_a_run_that_closed_leaves_nothing_behind_for_the_next_one(self) -> None:
        """An answer holding a compact line of its own, and then a line that
        opens a brace and never closes it.

        The compact line was set aside while the first answer was being read, and
        the first answer being read means it was one of that answer's own parts.
        Left lying about, it came out as though it belonged to the second run -
        which is the same wrong sentence as before, arriving by a longer road.
        """

        said = chr(10).join([
            "{",
            '  "attempts": [',
            '    {"is_error": true, "result": "a part of the first"}',
            "  ],",
            '  "result": "the first answer"',
            "}",
            '{"b": 2, "note": "this line never closes',
        ])
        found = subscription_cli._every_object_in(said)
        self.assertEqual([one.get("result") for one in found], ["the first answer"])

    def test_a_whole_object_on_one_line_inside_a_good_answer_is_left_where_it_is(self) -> None:
        said = chr(10).join(["{", '  "a": {"b": 1},', '  "c": 2', "}"])
        found = subscription_cli._every_object_in(said)
        self.assertEqual(found, [{"a": {"b": 1}, "c": 2}])

    def test_a_whole_answer_after_an_unclosed_brace_is_still_read(self) -> None:
        """A line that opened a brace and never closed it was never going to
        close, and the answer after it is the thing somebody wants."""

        said = chr(10).join([
            '{"attempt": 1, "note": "this line never closes',
            '{"is_error": true, "result": "the one that matters"}',
        ])
        found = subscription_cli._every_object_in(said)
        self.assertEqual([one.get("result") for one in found], ["the one that matters"])

    def test_an_object_with_real_detail_in_it_is_still_read(self) -> None:
        """A refusal can carry a breakdown - so many tokens against each of
        eighty files - and written out one line at a time that is three hundred
        lines. Read after every line, a run that long cost enough that how far
        an object could be spread had to be kept shorter than a real answer."""

        body = {
            "is_error": True,
            "result": "Your organization does not have access.",
            "usage": {
                f"file-{n}.py": {"input_tokens": n, "output_tokens": n}
                for n in range(80)
            },
        }
        said = "Welcome" + chr(10) + json.dumps(body, indent=2) + chr(10) + "Goodbye"
        self.assertGreater(len(said.splitlines()), 300)
        found = subscription_cli._every_object_in(said)
        self.assertEqual(
            [one.get("result") for one in found],
            ["Your organization does not have access."],
        )

    def test_an_object_spread_past_what_is_read_is_not_read(self) -> None:
        """Only so much of what a tool printed is looked at, so an object spread
        past that is not there to find. Held down because it is the bound that
        stops any of this taking as long as somebody has patience for."""

        lines = ["{"] + [f'  "n{n}": {n},' for n in range(3_000)] + ['  "last": 1', "}"]
        self.assertGreater(len(lines), subscription_cli.MOST_LINES_READ)
        self.assertEqual(subscription_cli._every_object_in(chr(10).join(lines)), [])

    def test_a_brace_inside_a_string_is_a_letter(self) -> None:
        """Counted as a brace, a run looks closed while it is not, or never
        looks closed at all - and the reason inside it is lost either way."""

        said = chr(10).join([
            "{",
            '  "a": "} not a brace {",',
            '  "is_error": true, "result": "tricky"',
            "}",
        ])
        found = subscription_cli._every_object_in(said)
        self.assertEqual([one.get("result") for one in found], ["tricky"])

    def test_a_backslash_before_a_quote_does_not_end_the_string(self) -> None:
        body = {"a": 'a quote " and a brace }', "is_error": True, "result": "escaped"}
        said = json.dumps(body, indent=2)
        found = subscription_cli._every_object_in(said)
        self.assertEqual([one.get("result") for one in found], ["escaped"])

    def test_nothing_that_looks_like_an_object_is_nothing(self) -> None:
        self.assertEqual(subscription_cli._every_object_in("no braces here"), [])
        self.assertEqual(subscription_cli._every_object_in(""), [])


class TheNewestBuildWinsTests(unittest.TestCase):
    """Which copy of a tool gets run, when the machine has more than one.

    Found the hard way. Two builds of Claude Code were on one machine: 2.1.101
    put there by npm months ago and first on the path, and 2.1.234 kept up to
    date by the Claude desktop app. The old one refused without asking anybody
    and said "your organization does not have access to Claude, please login
    again" - which was not true, and sends somebody to their administrator about
    the wrong thing. The new one asked, and came back with a plain four hundred
    and three about command-line subscription access while the interactive app
    still worked. Same account, same minute; only the copy of the program was
    different. The status identifies the rejected OAuth request, not its cause.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()

    def a_build(self, version: str, name: str = "claude.exe") -> Path:
        where = self.folder / "kept" / "claude-code" / version
        where.mkdir(parents=True, exist_ok=True)
        made = where / name
        made.write_text("", encoding="utf-8")
        return made

    def patterns(self) -> tuple[str, ...]:
        return ("HARNESS_TEST_HOME/kept/claude-code/*/claude.exe",)

    def test_every_build_it_can_find_is_listed_with_its_version(self) -> None:
        self.a_build("2.1.229")
        self.a_build("2.1.234")
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}):
            found = subscription_cli._every_build_of(self.patterns())
        self.assertEqual(
            sorted(version for _where, version in found),
            [(2, 1, 229), (2, 1, 234)],
        )

    def test_a_folder_that_is_not_a_version_is_passed_over(self) -> None:
        self.a_build("nightly")
        self.a_build("2.1.234")
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}):
            found = subscription_cli._every_build_of(self.patterns())
        self.assertEqual([version for _where, version in found], [(2, 1, 234)])

    def test_versions_are_compared_as_numbers_not_as_words(self) -> None:
        """Written down, 2.1.9 comes after 2.1.101 and the old one wins."""

        self.assertGreater(
            subscription_cli._as_numbers("2.1.101"), subscription_cli._as_numbers("2.1.9"))
        self.assertEqual(subscription_cli._as_numbers("not a version"), ())

    def test_a_newer_build_is_taken_over_the_one_on_the_path(self) -> None:
        newer = self.a_build("2.1.234")
        holder = self.provider_reading(self.patterns())
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}), \
             mock.patch.object(subscription_cli, "_the_version_of", lambda where: (2, 1, 101)):
            self.assertEqual(holder._a_newer_build_than("C:/npm/claude.CMD"), newer)

    def test_an_older_build_elsewhere_is_left_alone(self) -> None:
        self.a_build("2.0.1")
        holder = self.provider_reading(self.patterns())
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}), \
             mock.patch.object(subscription_cli, "_the_version_of", lambda where: (2, 1, 101)):
            self.assertIsNone(holder._a_newer_build_than("C:/npm/claude.CMD"))

    def test_a_command_somebody_wrote_down_is_the_one_that_runs(self) -> None:
        """Somebody who said which program to run meant that one."""

        self.a_build("9.9.9")
        holder = self.provider_reading(self.patterns(), command=["C:/mine/claude.exe"])
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}), \
             mock.patch.object(subscription_cli, "_the_version_of", lambda where: (1, 0, 0)):
            self.assertIsNone(holder._a_newer_build_than("C:/mine/claude.exe"))

    def test_the_program_is_not_started_when_there_is_nothing_to_compare(self) -> None:
        """Asking a program its version means starting it.

        This is on the way to every single message, and on a machine with only
        the one copy - which is most of them - the answer cannot change anything.
        So the other builds are looked for first, and on a machine with none the
        program is never started at all.
        """

        asked = []
        holder = self.provider_reading(self.patterns())
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}), \
             mock.patch.object(subscription_cli, "_the_version_of",
                               lambda where: asked.append(where) or (2, 1, 101)):
            self.assertIsNone(holder._a_newer_build_than("C:/npm/claude.CMD"))
        self.assertEqual(asked, [])

    def test_a_tool_that_keeps_no_other_builds_is_left_alone(self) -> None:
        holder = self.provider_reading(())
        self.assertIsNone(holder._a_newer_build_than("C:/npm/copilot.CMD"))

    def provider_reading(self, patterns, command=None):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": "claude-cli", "model": "m", "endpoint": "", "api_key_env": "",
        })
        if command:
            data["provider"]["command"] = command
        holder = SubscriptionCLIProvider(
            LoadedConfig(data, self.folder, [], {}), "claude-cli")
        holder.recipe = CliRecipe(**{**CLAUDE_RECIPE.__dict__, "kept_under": patterns})
        return holder

    def test_the_command_that_really_runs_is_the_newer_one(self) -> None:
        """The one that matters. Finding a newer build and then running the old
        one anyway is the whole bug, kept."""

        newer = self.a_build("2.1.234")
        onpath = self.folder / "npm" / "claude.CMD"
        onpath.parent.mkdir(parents=True, exist_ok=True)
        onpath.write_text("", encoding="utf-8")
        holder = self.provider_reading(self.patterns())
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}),              mock.patch.object(subscription_cli.shutil, "which", lambda name: str(onpath)),              mock.patch.object(subscription_cli, "_the_version_of", lambda where: (2, 1, 101)):
            self.assertEqual(holder._command()[0], str(newer))

    def test_the_desktop_build_runs_even_when_nothing_is_on_the_path(self) -> None:
        desktop = self.a_build("2.1.234")
        holder = self.provider_reading(self.patterns())
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}), \
             mock.patch.object(subscription_cli.shutil, "which", return_value=None):
            self.assertEqual(holder._command()[0], str(desktop))

    def test_discovery_finds_a_desktop_build_without_a_path_launcher(self) -> None:
        desktop = self.folder / "claude.exe"
        with mock.patch.object(subscription_cli.shutil, "which", return_value=None), \
             mock.patch.object(
                 subscription_cli, "_every_build_of",
                 return_value=[(desktop, (2, 1, 234))],
             ):
            self.assertEqual(subscription_cli.available("claude-cli"), str(desktop))

    def test_with_nothing_newer_the_one_on_the_path_runs(self) -> None:
        onpath = self.folder / "npm" / "claude.CMD"
        onpath.parent.mkdir(parents=True, exist_ok=True)
        onpath.write_text("", encoding="utf-8")
        holder = self.provider_reading(self.patterns())
        with mock.patch.dict(os.environ, {"HARNESS_TEST_HOME": str(self.folder)}),              mock.patch.object(subscription_cli.shutil, "which", lambda name: str(onpath)),              mock.patch.object(subscription_cli, "_the_version_of", lambda where: (9, 9, 9)):
            self.assertEqual(holder._command()[0], str(onpath))

    def test_the_shipped_recipe_looks_where_the_desktop_app_keeps_them(self) -> None:
        where = " ".join(CLAUDE_RECIPE.kept_under)
        self.assertIn("claude-code", where)
        self.assertIn("LOCALAPPDATA", where)


class WhenTheAnswerAlreadySaysWhatToDoTests(unittest.TestCase):
    """A provider error must not be promoted into a cause it did not prove.

    The subscription-access 403 can coexist with a working interactive app.
    The harness therefore describes the rejected command-line OAuth request,
    offers a clean sign-in and support path, and never silently selects a paid
    API-key route.
    """

    def holder(self):
        return SubscriptionCLIProvider(
            LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}), "claude-cli")

    def said_about(self, reason: str) -> str:
        with mock.patch.object(
                SubscriptionCLIProvider, "_how_it_describes_its_sign_in", lambda *a, **k: ""):
            return self.holder()._and_what_it_says_about_itself(
                CLAUDE_RECIPE, None, True, reason)

    def test_the_service_naming_the_fix_is_the_fix_somebody_is_told(self) -> None:
        said = self.said_about(
            "Your organization has disabled Claude subscription access for Claude "
            "Code - Use an Anthropic API key instead, or ask your admin to enable access")
        self.assertIn("does not prove", said)
        self.assertIn("claude auth logout", said)
        self.assertNotIn("setup-token", said)

    def test_a_refusal_that_names_nothing_still_gets_something_to_try(self) -> None:
        said = self.said_about("that model is not available on your plan")
        self.assertIn("claude auth login", said)

    def test_it_reads_the_words_whatever_case_they_came_in(self) -> None:
        self.assertIn(
            "does not prove",
            self.said_about("DISABLED CLAUDE SUBSCRIPTION ACCESS for Claude Code"))

    def test_a_wait_that_mentions_an_administrator_still_gets_something_to_try(self) -> None:
        """Plenty of refusals say to ask an administrator without meaning that
        anything is turned off. Told "there is nothing to try again here", the
        one person who only had to wait a minute goes and asks for a meeting."""

        said = self.said_about(
            "Too many requests. If this keeps happening, ask your admin about "
            "raising the limit for the team.")
        self.assertNotIn("nothing to try again", said)
        self.assertIn("claude auth login", said)


class WhetherItReallyAskedTests(unittest.TestCase):
    """Two very different sentences hang on this, so it has to be read right."""

    def holder(self):
        return SubscriptionCLIProvider(
            LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}), "claude-cli")

    def test_a_status_from_the_service_is_proof_it_asked(self) -> None:
        """Even when the tool says it spent no time there, which is what this
        machine really reports for a refusal that did come back from the
        service. Read from the timing alone, somebody was told the request never
        left their machine and to sign in again, while the answer was four
        hundred and three and the thing to do was turn Claude Code on for the
        organisation."""

        said = json.dumps({
            "is_error": True, "api_error_status": 403, "duration_api_ms": 0,
            "result": "Your organization has disabled Claude subscription access",
        })
        self.assertIs(self.holder()._did_it_ask_anybody(CLAUDE_RECIPE, said, ""), True)

    def test_no_status_and_no_time_at_the_service_means_it_never_asked(self) -> None:
        said = json.dumps({"is_error": True, "duration_api_ms": 0, "result": "no"})
        self.assertIs(self.holder()._did_it_ask_anybody(CLAUDE_RECIPE, said, ""), False)

    def test_time_at_the_service_still_counts_when_there_is_no_status(self) -> None:
        said = json.dumps({"is_error": True, "duration_api_ms": 812, "result": "no"})
        self.assertIs(self.holder()._did_it_ask_anybody(CLAUDE_RECIPE, said, ""), True)

    def test_only_the_answer_counts_and_not_the_lines_around_it(self) -> None:
        """These tools print progress and counts next to the answer.

        Any of those can carry a number under one of these names without being
        about this request. Read as the answer, a refusal the tool decided on its
        own becomes "the service turned you down" and somebody goes to their
        administrator about something that never happened - which is the whole of
        what this was written to stop.
        """

        said = "\n".join([
            json.dumps({"is_error": True, "duration_api_ms": 0, "result": "no"}),
            json.dumps({"type": "how it went", "api_error_status": 200}),
        ])
        self.assertIs(self.holder()._did_it_ask_anybody(CLAUDE_RECIPE, said, ""), False)

    def test_the_answer_is_read_even_when_it_came_first(self) -> None:
        said = "\n".join([
            json.dumps({"is_error": True, "api_error_status": 403, "result": "no"}),
            json.dumps({"type": "how it went", "duration_api_ms": 0}),
        ])
        self.assertIs(self.holder()._did_it_ask_anybody(CLAUDE_RECIPE, said, ""), True)

    def test_a_tool_that_says_neither_gets_neither_claim(self) -> None:
        said = json.dumps({"is_error": True, "result": "no"})
        self.assertIsNone(self.holder()._did_it_ask_anybody(CLAUDE_RECIPE, said, ""))


class ConfigTests(unittest.TestCase):
    def config(self, **provider: object) -> dict:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update(provider)
        return data

    def test_a_signed_in_assistant_is_a_valid_provider(self) -> None:
        for kind in ("claude-cli", "copilot-cli", "assistant-cli"):
            with self.subTest(kind=kind):
                validate_config(self.config(name=kind, model="m", endpoint="", api_key_env=""))

    def test_a_signed_in_assistant_may_not_carry_an_endpoint(self) -> None:
        """It has no address to call. It runs a program that knows where to go."""

        with self.assertRaises(HarnessError):
            validate_config(self.config(
                name="claude-cli", model="m", endpoint="https://api.example.com",
                api_key_env=""))

    def test_a_signed_in_assistant_may_be_given_a_key_instead(self) -> None:
        """It used to be refused outright, which was right when the only reason
        to name a key was by mistake. Somebody who has a key and would rather
        spend that than a seat is not making a mistake, and the tool's own
        command line reads one out of an environment variable."""

        for kind in ("claude-cli", "copilot-cli", "gemini-cli"):
            with self.subTest(kind=kind):
                validate_config(self.config(
                    name=kind, model="m", endpoint="", api_key_env="A_KEY_OF_MINE"))

    def test_the_ones_whose_service_allows_no_key_still_refuse_one(self) -> None:
        """Microsoft 365 Copilot is the plain case: a person signing in is the
        only way in that exists, so a key written down there is somebody
        expecting something that cannot happen."""

        with self.assertRaises(HarnessError) as caught:
            validate_config(self.config(
                name="m365-copilot", model="", endpoint="", api_key_env="A_KEY"))
        self.assertIn("cannot be given a key", str(caught.exception))

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

    def test_digest_bound_cli_refuses_bytes_changed_after_native_review(self) -> None:
        reviewed = b'{"provider":{"name":"claude-cli"}}'
        self.local.write_bytes(reviewed)
        store = self.root / "user" / "trusted-projects.json"
        digest = hashlib.sha256(reviewed).hexdigest()
        self.local.write_bytes(b'{"provider":{"name":"gemini-cli"}}')
        with mock.patch("our_harness.config.project_trust_store_path", return_value=store):
            code, _output, errors = self.run_cli(
                "trust", "--yes", "--reviewed-config", str(self.local),
                "--expected-sha256", digest,
            )
        self.assertEqual(code, 2)
        self.assertIn("changed after review", errors)
        self.assertFalse(store.exists())

    def test_saying_no_changes_nothing(self) -> None:
        self.local.write_text("{}", encoding="utf-8")
        with mock.patch("builtins.input", return_value="n"), \
             mock.patch("our_harness.cli.trust_project_local_config") as recorder:
            code, output, _ = self.run_cli("trust")
        self.assertEqual(code, 1)
        self.assertIn("Left as it was", output)
        recorder.assert_not_called()
