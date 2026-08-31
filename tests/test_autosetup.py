"""Doing somebody's setting up for them.

Nothing here touches a real tool or the network: the machine is stood in for,
so the same answers come back on a machine with everything installed and on one
with nothing. The one exception is the settings each plan writes, which are
handed to the real config reader, because a button that writes a file the
harness then refuses is worse than no button at all.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import autosetup, provider_help
from our_harness.config import (
    DEFAULT_CONFIG,
    LoadedConfig,
    is_project_local_config_trusted,
    load_config,
    trust_project_local_config,
)
from our_harness.models import HarnessError


class SetupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        # Setup writes a user-scoped trust record. Keep every test process in
        # its own disposable user-config root so parallel CI shards never
        # mutate the desktop account or manufacture contention between tests.
        isolated_user_config = self.root / "user-config"
        environment = mock.patch.dict(os.environ, {
            "APPDATA": str(isolated_user_config),
            "XDG_CONFIG_HOME": str(isolated_user_config),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    @property
    def local(self) -> Path:
        return self.root / ".harness" / "config.local.json"

    def written(self) -> dict:
        return json.loads(self.local.read_text(encoding="utf-8"))


class EveryWayOfConnectingHasOneTests(unittest.TestCase):
    def test_every_option_on_the_first_screen_can_be_done_for_you(self) -> None:
        # The button list is not written by hand: it is held against the list
        # the first screen really shows. A new way of connecting a model then
        # cannot quietly ship without a button, which is exactly how the last
        # one would have been missed.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
        with mock.patch.object(provider_help, "_reachable", lambda *a, **k: False):
            shown = {option.id for option in provider_help.provider_options(config)}
        self.assertEqual(shown - set(autosetup.PLANS), set(), "an option with no button")

    def test_every_plan_knows_how_it_is_done(self) -> None:
        self.assertEqual(set(autosetup.PLANS), set(autosetup.HOW))


class WhatItWritesTests(SetupTestCase):
    def test_every_plan_writes_settings_the_harness_accepts(self) -> None:
        # The real gate. Each plan writes its route into a fresh project and
        # the real config reader is asked to read it back. A route the reader
        # refuses would leave somebody worse off than before they pressed.
        for option, plan in autosetup.PLANS.items():
            with self.subTest(option=option):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name).resolve()
                (root / ".harness").mkdir()
                config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
                written = autosetup.write_the_route(config, plan)
                self.assertTrue(written.trusted, written.trouble)
                read_back = load_config(root)
                route = (read_back.get("providers") or {})[plan.route_name]
                self.assertEqual(route["kind"], plan.kind)

    def test_it_keeps_settings_that_were_already_there(self) -> None:
        self.local.write_text(json.dumps({"memory": {"enabled": True}}), encoding="utf-8")
        trust_project_local_config(self.root, self.local)
        autosetup.write_the_route(self.config, autosetup.PLANS["ollama"])
        written = self.written()
        self.assertEqual(written["memory"], {"enabled": True})
        self.assertIn("ollama", written["providers"])

    def test_it_will_not_trust_a_file_somebody_else_left_behind(self) -> None:
        # Same rule as seat setup, for the same reason: a settings file can
        # start programs, and trusting one nobody has read is not this tool's
        # decision to make.
        self.local.write_text(json.dumps({
            "providers": {"theirs": {"kind": "openai-compatible", "model": "x",
                                     "endpoint": "http://somewhere.example/v1"}},
        }), encoding="utf-8")
        written = autosetup.write_the_route(self.config, autosetup.PLANS["ollama"])
        trusted, trouble = written.trusted, written.trouble
        self.assertFalse(trusted)
        self.assertFalse(is_project_local_config_trusted(self.root, self.local))
        self.assertIn("harness trust", trouble)

    def test_a_kind_that_may_not_be_the_default_is_not_made_the_default(self) -> None:
        autosetup.write_the_route(self.config, autosetup.PLANS["codex-cli"])
        written = self.written()
        self.assertIn("codex", written["providers"])
        self.assertNotIn("provider", written, "codex-cli may be a route, never the default")

    def test_a_settings_file_that_cannot_be_read_is_left_alone(self) -> None:
        self.local.write_text("{ not json at all", encoding="utf-8")
        with self.assertRaises(HarnessError):
            autosetup.write_the_route(self.config, autosetup.PLANS["ollama"])
        self.assertEqual(self.local.read_text(encoding="utf-8"), "{ not json at all")


class ASignedInToolTests(SetupTestCase):
    def test_a_tool_that_is_there_and_answers_is_set_up(self) -> None:
        with mock.patch.object(
                autosetup, "available", lambda kind, command=None: "/usr/bin/claude"), \
                mock.patch.object(autosetup, "_run", lambda parts, seconds: (0, "2.1.101")):
            job = autosetup.do_it(self.config, "claude-cli")
        self.assertTrue(job.worked, job.said)
        self.assertEqual(job.left_for_you, [])
        self.assertIn("claude", self.written()["providers"])
        self.assertTrue(is_project_local_config_trusted(self.root, self.local))

    def test_a_tool_that_is_not_there_says_so_and_writes_nothing(self) -> None:
        with mock.patch.object(autosetup, "available", lambda kind, command=None: ""):
            job = autosetup.do_it(self.config, "copilot-cli")
        self.assertFalse(job.worked)
        self.assertTrue(job.left_for_you)
        self.assertIn("Install", job.left_for_you[0])
        self.assertFalse(self.local.exists(), "nothing was written")

    def test_a_tool_that_is_there_but_not_signed_in_writes_nothing(self) -> None:
        with mock.patch.object(
                autosetup, "available", lambda kind, command=None: "/usr/bin/claude"), \
                mock.patch.object(autosetup, "_run", lambda parts, seconds: (1, "Please sign in")):
            job = autosetup.do_it(self.config, "claude-cli")
        self.assertFalse(job.worked)
        self.assertIn("sign in", " ".join(job.left_for_you).lower())
        self.assertFalse(self.local.exists())

    def test_built_in_setup_uses_full_desktop_discovery_not_an_exact_bare_hint(self) -> None:
        calls: list[tuple[str, object]] = []

        def found(kind, command=None):
            calls.append((kind, command))
            return "C:/OpenAI/Codex/bin/build/codex.exe"

        with mock.patch.object(autosetup, "available", found), \
                mock.patch.object(autosetup, "_run", lambda parts, seconds: (0, "1.2.3")), \
                mock.patch.object(autosetup, "_codex_model", return_value="gpt-current"):
            job = autosetup.do_it(self.config, "codex-cli")

        self.assertTrue(job.worked, job.said)
        self.assertEqual(calls, [("codex-cli", None)])
        self.assertEqual(
            self.written()["providers"]["codex"]["command"],
            ["codex"],
        )
        self.assertEqual(self.written()["providers"]["codex"]["model"], "gpt-current")

    def test_codex_setup_does_not_claim_connected_without_a_real_model_catalog(self) -> None:
        with mock.patch.object(
                autosetup, "available", return_value="C:/OpenAI/Codex/bin/build/codex.exe"), \
                mock.patch.object(autosetup, "_run", return_value=(0, "1.2.3")), \
                mock.patch.object(autosetup, "_codex_model", return_value=""):
            job = autosetup.do_it(self.config, "codex-cli")

        self.assertFalse(job.worked)
        self.assertIn("model catalog", job.said.lower())
        self.assertFalse(self.local.exists(), "an unverified model must not create a route")


class AHostedServiceTests(SetupTestCase):
    def test_a_key_that_is_set_is_enough(self) -> None:
        with mock.patch.dict(autosetup.os.environ, {"OPENAI_API_KEY": "not-a-real-key"}):
            job = autosetup.do_it(self.config, "openai")
        self.assertTrue(job.worked, job.said)
        self.assertIn("openai", self.written()["providers"])

    def test_it_never_writes_the_key_itself_anywhere(self) -> None:
        secret = "sk-this-must-never-be-written-down"
        with mock.patch.dict(autosetup.os.environ, {"ANTHROPIC_API_KEY": secret}):
            job = autosetup.do_it(self.config, "anthropic")
        written = self.local.read_text(encoding="utf-8")
        self.assertNotIn(secret, written)
        self.assertNotIn(secret, json.dumps(job.to_dict()))
        self.assertEqual(written.count("ANTHROPIC_API_KEY"), 2, "the name, never the value")

    def test_no_key_says_what_only_a_person_can_do(self) -> None:
        with mock.patch.dict(autosetup.os.environ, {}, clear=True):
            job = autosetup.do_it(self.config, "gemini")
        self.assertFalse(job.worked)
        self.assertIn("aistudio.google.com", " ".join(job.left_for_you))
        self.assertFalse(self.local.exists())


class OllamaTests(SetupTestCase):
    def test_a_server_already_answering_only_needs_the_route(self) -> None:
        with mock.patch.object(autosetup, "_answering", lambda *a, **k: True), \
                mock.patch.object(autosetup.shutil, "which", lambda name: ""):
            job = autosetup.do_it(self.config, "ollama")
        self.assertTrue(job.worked, job.said)
        self.assertIn("ollama", self.written()["providers"])

    def test_it_starts_a_server_that_is_installed_and_not_running(self) -> None:
        answers = iter([False, False, True, True, True, True])
        started: list[list[str]] = []

        def answering(*_args, **_kwargs):
            return next(answers, True)

        with mock.patch.object(autosetup, "_answering", answering), \
                mock.patch.object(autosetup.shutil, "which", lambda name: "/usr/bin/ollama"), \
                mock.patch.object(autosetup.time, "sleep", lambda seconds: None), \
                mock.patch.object(autosetup.subprocess, "Popen",
                                  lambda parts, **kwargs: started.append(parts)), \
                mock.patch.object(autosetup, "_run", lambda parts, seconds: (0, "pulled")):
            job = autosetup.do_it(self.config, "ollama")
        self.assertTrue(job.worked, job.said)
        self.assertEqual(started, [["/usr/bin/ollama", "serve"]])

    def test_it_will_not_install_anything(self) -> None:
        with mock.patch.object(autosetup, "_answering", lambda *a, **k: False), \
                mock.patch.object(autosetup.shutil, "which", lambda name: ""):
            job = autosetup.do_it(self.config, "ollama")
        self.assertFalse(job.worked)
        self.assertIn("ollama.com", " ".join(job.left_for_you))
        self.assertIn("not something this will do for you", " ".join(job.left_for_you))
        self.assertFalse(self.local.exists())

    def test_a_model_that_will_not_come_down_is_said_plainly(self) -> None:
        with mock.patch.object(autosetup, "_answering", lambda *a, **k: True), \
                mock.patch.object(autosetup.shutil, "which", lambda name: "/usr/bin/ollama"), \
                mock.patch.object(autosetup, "_run", lambda parts, seconds: (1, "no space left")):
            job = autosetup.do_it(self.config, "ollama")
        self.assertIn("ollama pull", " ".join(job.left_for_you))
        self.assertIn("ollama", self.written()["providers"], "the route is still written")


class OneAtATimeTests(SetupTestCase):
    def test_a_second_press_while_one_is_running_is_refused(self) -> None:
        runner = autosetup.Runner()
        holding = autosetup.threading.Event()

        def slow(config, option, job=None):
            holding.wait(5)
            return job or autosetup.Job(option=option, label="x", running=False, finished=True)

        with mock.patch.object(autosetup, "do_it", slow):
            runner.start(self.config, "ollama")
            with self.assertRaises(HarnessError):
                runner.start(self.config, "openai")
            holding.set()
            runner.wait()

    def test_a_job_that_falls_over_does_not_wedge_the_button_for_ever(self) -> None:
        # The worst kind of bug: a write that fails with an OSError killed the
        # thread, nothing was ever shown, and every later press was refused
        # with "wait for that to finish" until the panel was restarted.
        runner = autosetup.Runner()

        def full_disk(config, plan, **kwargs):
            raise OSError(28, "No space left on device")

        with mock.patch.object(autosetup, "write_the_route", full_disk), \
                mock.patch.object(autosetup.shutil, "which", lambda name: "/usr/bin/claude"), \
                mock.patch.object(autosetup, "_run", lambda parts, seconds: (0, "2.1.101")):
            runner.start(self.config, "claude-cli")
            runner.wait()
        self.assertFalse(runner.busy, "it has to let go of the button")
        latest = runner.latest()
        self.assertTrue(latest["finished"])
        self.assertFalse(latest["worked"])
        self.assertIn("No space left", latest["said"])
        self.assertTrue(latest["steps"], "and say something, rather than nothing at all")
        # And the next press works.
        with mock.patch.object(autosetup, "_answering", lambda *a, **k: True), \
                mock.patch.object(autosetup.shutil, "which", lambda name: ""):
            runner.start(self.config, "ollama")
            runner.wait()
        self.assertTrue(runner.latest()["worked"])

    def test_the_page_can_watch_a_job_while_it_is_still_going(self) -> None:
        # The whole point of doing this on a thread is showing each step as it
        # happens. The runner used to fill in one job and show a different,
        # empty one, so the page said nothing at all until the work finished.
        runner = autosetup.Runner()
        reached = autosetup.threading.Event()
        carry_on = autosetup.threading.Event()

        def slow(job, config, plan):
            job.steps.append(autosetup.Step("Look for it", state=autosetup.DONE))
            reached.set()
            carry_on.wait(5)
            job.steps.append(autosetup.Step("Write it", state=autosetup.DONE))
            job.worked = True
            job.said = "done"

        with mock.patch.dict(autosetup.HOW, {"ollama": slow}):
            runner.start(self.config, "ollama")
            self.assertTrue(reached.wait(5))
            part_way = runner.latest()
            self.assertTrue(part_way["running"], "it is still going")
            self.assertEqual([step["text"] for step in part_way["steps"]], ["Look for it"])
            carry_on.set()
            runner.wait()
        self.assertEqual(len(runner.latest()["steps"]), 2)

    def test_an_option_it_does_not_know_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            autosetup.Runner().start(self.config, "something-else")

    def test_the_answer_says_which_job_and_how_far_it_got(self) -> None:
        runner = autosetup.Runner()
        with mock.patch.object(autosetup, "_answering", lambda *a, **k: True), \
                mock.patch.object(autosetup.shutil, "which", lambda name: ""):
            runner.start(self.config, "ollama")
            runner.wait()
        latest = runner.latest()
        self.assertEqual(latest["option"], "ollama")
        self.assertFalse(latest["running"])
        self.assertTrue(latest["finished"])
        self.assertTrue(latest["steps"])


if __name__ == "__main__":
    unittest.main()
