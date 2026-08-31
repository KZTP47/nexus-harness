"""Gemini and Codex, driven through the sign-in somebody already has.

Both were nearly here already and neither could be used. Gemini had no place in
the harness at all, though it has a command line and signs in with a Google
account exactly the way Claude and Copilot do. Codex had a whole provider,
older and better tested than most of this, and could not be found: its desktop
app keeps it in a folder of its own and nothing ever puts it on the path, so
the app said it was not on the machine while it sat there signed in.

And nothing could be handed a key on purpose. Everything a subscription tool
does not need is stripped before it runs, which is right for a subscription and
wrong for somebody who has a key and means to use it.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig, validate_config
from our_harness.models import CommandResult, HarnessError, ProviderRequest
from our_harness.providers import subscription_cli
from our_harness.providers.subscription_cli import (
    CODEX_RECIPE,
    GEMINI_RECIPE,
    RECIPES,
    SubscriptionCLIProvider,
    available,
    responding_command,
)


def a_provider(kind: str, **settings) -> SubscriptionCLIProvider:
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["provider"].update({
        "name": kind, "model": "", "endpoint": "", "api_key_env": "", **settings})
    return SubscriptionCLIProvider(LoadedConfig(data, Path.cwd(), [], {}), kind)


class HowGeminiSaysNoTests(unittest.TestCase):
    """Gemini says something went wrong by putting an object where the answer
    would be, and nothing else. Read for a flag set to true - which is how every
    other tool here says it - that is not a refusal at all, and a refusal came
    back as an empty answer with nothing to explain it."""

    def a_refusal(self) -> str:
        return json.dumps({
            "session_id": "s",
            "error": {
                "type": "ProjectIdRequiredError",
                "message": "This account requires setting the GOOGLE_CLOUD_PROJECT env var",
                "code": 41,
            },
        })

    def test_an_error_object_is_a_refusal(self) -> None:
        held = a_provider("gemini-cli")
        self.assertTrue(subscription_cli._that_went_wrong(
            GEMINI_RECIPE, json.loads(self.a_refusal())))

    def test_the_message_inside_it_is_what_somebody_is_told(self) -> None:
        said = a_provider("gemini-cli")._why_it_would_not(GEMINI_RECIPE, self.a_refusal())
        self.assertIn("GOOGLE_CLOUD_PROJECT", said)

    def test_an_answer_with_no_error_in_it_is_an_answer(self) -> None:
        body = {"session_id": "s", "response": "here you go", "stats": {}}
        self.assertFalse(subscription_cli._that_went_wrong(GEMINI_RECIPE, body))

    def test_the_answer_is_read_out_of_the_response(self) -> None:
        said = a_provider("gemini-cli")._read_answer(
            GEMINI_RECIPE,
            json.dumps({"session_id": "s", "response": "here you go"}),
            "", 0.0)
        self.assertEqual(said.text, "here you go")

    def test_the_cloud_project_message_says_what_to_do_about_it(self) -> None:
        """Google's own words are a link and a shrug. This is the one thing
        everybody with a work account hits, and the fix is a setting."""

        with mock.patch.object(
                SubscriptionCLIProvider, "_how_it_describes_its_sign_in", lambda *a, **k: ""):
            said = a_provider("gemini-cli")._and_what_it_says_about_itself(
                GEMINI_RECIPE, None, None,
                "This account requires setting the GOOGLE_CLOUD_PROJECT env var")
        self.assertIn("google_project", said)
        self.assertNotIn("signing in again will help", said)

    def test_the_answer_naming_its_own_cause_wins_however_vague_the_rest_is(self) -> None:
        """It was only read when the tool said plainly that it had asked
        somebody. Gemini says nothing either way, so the one message that spelt
        out what to do had a guess written over the top of it."""

        with mock.patch.object(
                SubscriptionCLIProvider, "_how_it_describes_its_sign_in", lambda *a, **k: ""):
            holder = a_provider("gemini-cli")
            for asked in (True, False, None):
                with self.subTest(asked_anybody=asked):
                    self.assertIn("google_project", holder._and_what_it_says_about_itself(
                        GEMINI_RECIPE, None, asked, "set the GOOGLE_CLOUD_PROJECT env var"))


class WhatTheToolIsHandedTests(unittest.TestCase):
    """Everything else is stripped before it runs. These are the ones somebody
    wrote down, and a key that arrives because it happened to be set on the
    machine is a key nobody decided to spend."""

    def test_the_cloud_project_is_handed_over_when_it_is_written_down(self) -> None:
        held = a_provider("gemini-cli", google_project="a-project")._what_it_is_handed(
            GEMINI_RECIPE)
        self.assertEqual(held, {"GOOGLE_CLOUD_PROJECT": "a-project"})

    def test_nothing_is_handed_over_when_nothing_is_written_down(self) -> None:
        self.assertEqual(a_provider("gemini-cli")._what_it_is_handed(GEMINI_RECIPE), {})

    def test_a_key_is_handed_over_when_a_route_asks_for_one(self) -> None:
        with mock.patch.dict(os.environ, {"A_KEY_OF_MINE": "sk-whatever"}):
            held = a_provider(
                "gemini-cli", api_key_env="A_KEY_OF_MINE")._what_it_is_handed(GEMINI_RECIPE)
        self.assertEqual(held, {"GEMINI_API_KEY": "sk-whatever"})

    def test_a_key_lying_about_in_the_environment_is_not_handed_over(self) -> None:
        """The whole point of stripping. A subscription tool handed a key nobody
        asked it to use starts spending money nobody decided to spend."""

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "sk-not-asked-for"}):
            self.assertEqual(a_provider("gemini-cli")._what_it_is_handed(GEMINI_RECIPE), {})

    def test_asking_for_a_key_that_is_not_set_says_so(self) -> None:
        """Falling back to the subscription without a word is how a route ends
        up doing something other than what it says on it."""

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("A_KEY_NOBODY_SET", None)
            with self.assertRaises(HarnessError) as caught:
                a_provider(
                    "gemini-cli", api_key_env="A_KEY_NOBODY_SET")._what_it_is_handed(
                        GEMINI_RECIPE)
        self.assertIn("is not set on this machine", str(caught.exception))

    def test_a_tool_that_cannot_take_a_key_says_so_rather_than_ignoring_it(self) -> None:
        recipe = RECIPES["assistant-cli"]
        with self.assertRaises(HarnessError) as caught:
            a_provider("assistant-cli", api_key_env="A_KEY")._what_it_is_handed(recipe)
        self.assertIn("cannot be given a key", str(caught.exception))

    def test_every_tool_whose_command_line_takes_a_key_says_which_one(self) -> None:
        self.assertEqual(RECIPES["claude-cli"].key_it_reads, "ANTHROPIC_API_KEY")
        self.assertEqual(RECIPES["copilot-cli"].key_it_reads, "GH_TOKEN")
        self.assertEqual(RECIPES["codex-cli"].key_it_reads, "OPENAI_API_KEY")
        self.assertEqual(RECIPES["gemini-cli"].key_it_reads, "GEMINI_API_KEY")

    def test_what_is_handed_over_really_reaches_the_program(self) -> None:
        """It is one thing to work out what to hand over and another to hand it."""

        handed = {}

        def watch(argv, **rest):
            # The tool is asked its version first, on the way in. That one is
            # let through; the one carrying the question is the one this is
            # about.
            if rest.get("stdin_text") is None:
                return CommandResult(
                    argv=argv, cwd="", exit_code=0, stdout="1.0.0", stderr="",
                    duration_ms=1, timed_out=False, output_truncated=False)
            handed.update(rest.get("also_in_the_environment") or {})
            raise HarnessError("far enough")

        # This is a transport-contract test, not an installation probe.  A
        # clean CI runner quite correctly has no Gemini executable, while a
        # developer machine usually does; make that prerequisite explicit so
        # the same code path is exercised on both.
        with mock.patch.object(subscription_cli.shutil, "which", return_value="gemini"), \
             mock.patch.object(subscription_cli, "_run_bounded", watch), \
             self.assertRaises(HarnessError):
            a_provider("gemini-cli", google_project="a-project").complete(
                ProviderRequest("", "", [{"role": "user", "content": "hi"}], "",
                                timeout_seconds=30))
        self.assertEqual(handed, {"GOOGLE_CLOUD_PROJECT": "a-project"})


class WhatTheProgramReallySeesTests(unittest.TestCase):
    """Working out what to hand over, handing it to the runner, and the runner
    putting it in front of the program are three separate things, and only the
    last one is the one that matters."""

    def test_it_ends_up_in_the_environment_the_program_is_started_with(self) -> None:
        from our_harness.providers import codex_cli

        held = codex_cli._minimal_codex_environment(
            {"GOOGLE_CLOUD_PROJECT": "a-project", "GEMINI_API_KEY": "sk-whatever"})
        self.assertEqual(held["GOOGLE_CLOUD_PROJECT"], "a-project")
        self.assertEqual(held["GEMINI_API_KEY"], "sk-whatever")

    def test_everything_else_is_still_left_outside(self) -> None:
        """The stripping is the point. A key that arrives because it happened to
        be set on the machine is a key nobody decided to spend."""

        from our_harness.providers import codex_cli

        with mock.patch.dict(os.environ, {"SOMEBODY_ELSES_KEY": "sk-not-mine"}):
            held = codex_cli._minimal_codex_environment({"GEMINI_API_KEY": "sk-mine"})
        self.assertNotIn("SOMEBODY_ELSES_KEY", held)

    def test_handing_over_nothing_changes_nothing(self) -> None:
        from our_harness.providers import codex_cli

        self.assertEqual(
            codex_cli._minimal_codex_environment(),
            codex_cli._minimal_codex_environment({}))

    def test_an_empty_value_is_not_handed_over_as_an_empty_one(self) -> None:
        """A tool handed an empty key tries to use it and fails oddly, where
        handed nothing at all it falls back to the sign-in and works."""

        from our_harness.providers import codex_cli

        self.assertNotIn(
            "GEMINI_API_KEY", codex_cli._minimal_codex_environment({"GEMINI_API_KEY": ""}))


class FindingAToolThatIsNotOnThePathTests(unittest.TestCase):
    """Codex is installed by its own desktop app, into a folder of its own, and
    nothing ever puts it on the path. So the app said it was not on this machine
    while it sat there signed in - and sent somebody off installing what they
    already had."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()

    def test_it_looks_where_the_desktop_app_puts_it(self) -> None:
        where = self.folder / "Packages" / "OpenAI.Codex_abc" / "LocalCache" / "Local"
        where = where / "OpenAI" / "Codex" / "bin"
        where.mkdir(parents=True)
        (where / "codex.exe").write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)}), \
             mock.patch.object(subscription_cli.shutil, "which", lambda one: None):
            self.assertEqual(available("codex-cli"), str(where / "codex.exe"))

    def test_it_finds_the_current_versioned_codex_desktop_location(self) -> None:
        where = self.folder / "OpenAI" / "Codex" / "bin" / "b99306303521e97e"
        where.mkdir(parents=True)
        (where / "codex.exe").write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)}), \
             mock.patch.object(subscription_cli.shutil, "which", lambda one: None):
            self.assertEqual(available("codex-cli"), str(where / "codex.exe"))

    def test_a_stable_codex_route_follows_a_desktop_update_to_the_new_build(self) -> None:
        first = self.folder / "OpenAI" / "Codex" / "bin" / "build-a" / "codex.exe"
        first.parent.mkdir(parents=True)
        first.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)}), \
             mock.patch.object(subscription_cli.shutil, "which", lambda one: None):
            self.assertEqual(available("codex-cli", ["codex"]), str(first))
            first.unlink()
            second = self.folder / "OpenAI" / "Codex" / "bin" / "build-b" / "codex.exe"
            second.parent.mkdir(parents=True)
            second.write_text("", encoding="utf-8")
            self.assertEqual(available("codex-cli", ["codex"]), str(second))

    def test_an_older_nexus_absolute_codex_route_migrates_after_update(self) -> None:
        vanished = self.folder / "OpenAI" / "Codex" / "bin" / "build-a" / "codex.exe"
        current = self.folder / "OpenAI" / "Codex" / "bin" / "build-b" / "codex.exe"
        current.parent.mkdir(parents=True)
        current.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)}), \
             mock.patch.object(subscription_cli.shutil, "which", lambda one: None):
            self.assertEqual(
                available("codex-cli", [str(vanished)]),
                str(current),
            )

    def test_an_arbitrary_missing_codex_command_remains_exact_authority(self) -> None:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)}), \
             mock.patch.object(subscription_cli.shutil, "which", lambda one: None):
            self.assertEqual(
                available("codex-cli", [str(self.folder / "custom" / "codex.exe")]),
                "",
            )

    def test_repair_discovery_requires_the_candidate_to_answer_its_version_probe(self) -> None:
        answered = CommandResult(["codex", "--version"], ".", 0, "codex 1", "", 1)
        refused = CommandResult(["codex", "--version"], ".", 1, "", "blocked", 1)
        with mock.patch.object(
            subscription_cli, "available", return_value="C:/tools/codex.exe",
        ), mock.patch.object(subscription_cli, "_run_bounded", return_value=answered):
            self.assertEqual(responding_command("codex-cli"), "C:/tools/codex.exe")
        with mock.patch.object(
            subscription_cli, "available", return_value="C:/tools/codex.exe",
        ), mock.patch.object(subscription_cli, "_run_bounded", return_value=refused):
            self.assertEqual(responding_command("codex-cli"), "")

    def test_a_copy_on_the_path_still_wins(self) -> None:
        """Somebody who put one there meant that one."""

        with mock.patch.object(
                subscription_cli.shutil, "which", lambda one: "C:/mine/codex.exe"):
            self.assertEqual(available("codex-cli"), "C:/mine/codex.exe")

    def test_a_tool_that_is_nowhere_is_still_nowhere(self) -> None:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)}), \
             mock.patch.object(subscription_cli.shutil, "which", lambda one: None):
            self.assertEqual(available("codex-cli"), "")

    def test_the_shipped_recipe_looks_where_the_codex_app_really_puts_it(self) -> None:
        where = " ".join(CODEX_RECIPE.also_found_at)
        self.assertIn("OpenAI.Codex", where)
        self.assertIn("codex.exe", where)


class BothCanBePickedInYourTeamTests(unittest.TestCase):
    def test_they_are_offered_alongside_the_others(self) -> None:
        from our_harness import seats

        for kind in ("claude-cli", "copilot-cli", "gemini-cli", "codex-cli"):
            with self.subTest(kind=kind):
                self.assertIn(kind, seats.KNOWN_SEATS)
                self.assertTrue(seats.ROUTE_NAMES[kind])

    def test_every_one_of_them_has_something_to_look_for(self) -> None:
        from our_harness import seats

        for kind in seats.KNOWN_SEATS:
            with self.subTest(kind=kind):
                recipe = RECIPES[kind]
                self.assertTrue(recipe.command, "nothing to look for")
                self.assertTrue(recipe.install_hint, "nothing to say when it is missing")


class TheSettingsForThemTests(unittest.TestCase):
    def a_route(self, **held) -> dict:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"one": {"kind": "gemini-cli", "model": "", **held}}
        return data

    def test_a_gemini_route_is_a_route_this_accepts(self) -> None:
        validate_config(self.a_route(google_project="a-project"))

    def test_a_gemini_route_may_be_given_a_key_instead(self) -> None:
        validate_config(self.a_route(api_key_env="GEMINI_API_KEY"))

    def test_a_route_that_can_only_sign_in_may_not_be_given_a_key(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"one": {
            "kind": "m365-copilot", "model": "", "api_key_env": "SOMETHING"}}
        with self.assertRaises(HarnessError) as caught:
            validate_config(data)
        self.assertIn("cannot be given a key", str(caught.exception))

    def test_the_one_this_project_uses_may_be_given_a_key_as_well(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": "gemini-cli", "model": "", "endpoint": "",
            "api_key_env": "GEMINI_API_KEY"})
        validate_config(data)

    def test_the_one_this_project_uses_can_be_gemini_too(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": "gemini-cli", "model": "", "endpoint": "", "api_key_env": "",
            "google_project": "a-project"})
        validate_config(data)

    def test_a_named_gemini_route_hands_its_cloud_project_to_the_cli(self) -> None:
        """The board talks through ProviderRegistry, not directly through the
        route dictionary.  Saving a project id is useless if that routing step
        drops it before the Gemini adapter builds its environment."""

        from our_harness.providers import ProviderRegistry

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"gemini": {
            "kind": "gemini-cli",
            "model": "gemini-2.5-pro",
            "endpoint": "",
            "google_project": "a-project",
        }}
        config = LoadedConfig(data, Path.cwd(), [], {})
        routed = ProviderRegistry(config).provider_config("gemini")
        provider = SubscriptionCLIProvider(routed, "gemini-cli")

        self.assertEqual(routed.get("provider.google_project"), "a-project")
        self.assertEqual(
            provider._what_it_is_handed(GEMINI_RECIPE),
            {"GOOGLE_CLOUD_PROJECT": "a-project"},
        )


if __name__ == "__main__":
    unittest.main()
