from __future__ import annotations

import copy
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
        self.assertIn("A Company", message, "it should say what the tool says of itself")
        self.assertIn("signed in", message)
        self.assertIn("setup-token", message, "and the one thing to try")

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

    def test_a_tool_that_is_not_installed_says_how_to_get_it(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({"name": "claude-cli", "model": "m", "endpoint": "", "api_key_env": ""})
        provider = SubscriptionCLIProvider(LoadedConfig(data, self.folder, [], {}), "claude-cli")
        with mock.patch.object(subscription_cli.shutil, "which", return_value=None):
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
            ["-p", "--output-format", "json", "--model", "fake-model"],
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
        """Given the whole of what was printed to grow into, this walked to the
        end from every line that opened a brace: sixteen hundred lines took nine
        seconds, with no clock on it at all."""

        said = "\n".join(['{"a": 1' for _ in range(3_000)])
        started = time.monotonic()
        found = subscription_cli._every_object_in(said)
        took = time.monotonic() - started
        self.assertEqual(found, [])
        self.assertLess(took, 4, f"it took {took:.1f} seconds")

    def test_the_reason_is_found_at_either_end_of_a_torrent(self) -> None:
        """Reading only the first so many lines, a tool with a great deal to say
        before it says why lost the reason. Reading only the last so many loses
        it the other way round, from a tool that answers and then talks."""

        noise = [f"chatter {n}" for n in range(4_000)]
        for what, lines in (
            ("at the end", noise + ['{"is_error": true, "result": "at the end"}']),
            ("at the front", ['{"is_error": true, "result": "at the front"}'] + noise),
        ):
            with self.subTest(what=what):
                joined = chr(10).join(lines)
                found = subscription_cli._every_object_in(joined)
                self.assertEqual([one.get("result") for one in found], [what])

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
