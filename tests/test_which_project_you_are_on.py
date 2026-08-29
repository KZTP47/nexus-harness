"""Which project the panel is showing, and getting to another one.

The harness has always worked on one project at a time, and everything it keeps
- the automations, the timers, the checks, the team, what it has learnt - lives
inside that project's own folder. That part was right from the start. What was
missing was anywhere in the panel that said *which* project you were looking
at, and any way to another without stopping the harness and starting it again
with a different folder.

Two things are held apart here, and most of these tests are about that line.
The list of projects is about this machine, so it lives beside your own
settings and never inside a project. What a project is called is about the
project, so it lives in the project and travels with it.

And one thing matters more than all of it: nothing here ever deletes anybody's
work. Taking a project off the list is forgetting it, not removing it.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from our_harness import chat, pipelines, projects, server
from our_harness.config import DEFAULT_CONFIG, LoadedConfig, load_config


class ProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # The list is kept beside somebody's own settings. Sent somewhere
        # throwaway, so a test never touches the real one.
        self.somewhere_else = self.root / "settings"
        self.somewhere_else.mkdir()
        patched = mock.patch.dict(os.environ, {
            "APPDATA": str(self.somewhere_else),
            "XDG_CONFIG_HOME": str(self.somewhere_else),
            "OUR_HARNESS_PIPELINE_RUN_DIR": str(self.root / "pipeline-runtime"),
            "OUR_HARNESS_SWARM_RUN_DIR": str(self.root / "swarm-runtime"),
        })
        patched.start()
        self.addCleanup(patched.stop)

    def a_project(self, name: str = "alpha") -> Path:
        where = self.root / name
        (where / ".harness").mkdir(parents=True)
        return where


class WhatAProjectIsCalled(ProjectTestCase):
    def test_with_no_name_it_is_the_folder(self) -> None:
        """Right often enough that most people never type one."""

        self.assertEqual(projects.name_of(self.a_project("my-thing")), "my-thing")

    def test_a_name_is_kept_inside_the_project(self) -> None:
        """So it travels. Clone it onto another machine and it is still called
        the same thing."""

        where = self.a_project()
        projects.rename(where, "The nightly one")
        self.assertEqual(projects.name_of(where), "The nightly one")
        self.assertTrue((where / ".harness" / "project.json").is_file())

    def test_an_empty_name_puts_the_folder_name_back(self) -> None:
        where = self.a_project("my-thing")
        projects.rename(where, "Something else")
        self.assertEqual(projects.rename(where, "   "), "my-thing")
        self.assertEqual(projects.name_of(where), "my-thing")

    def test_a_name_written_as_an_essay_is_cut_short(self) -> None:
        where = self.a_project()
        said = projects.rename(where, "x" * 500)
        self.assertEqual(len(said), projects.LONGEST_NAME)

    def test_a_name_on_more_than_one_line_becomes_one(self) -> None:
        where = self.a_project()
        self.assertEqual(projects.rename(where, " The\n nightly \tone "), "The nightly one")

    def test_naming_a_folder_that_is_not_there(self) -> None:
        with self.assertRaises(projects.ProjectError) as caught:
            projects.rename(self.root / "nowhere", "x")
        self.assertIn("no folder", str(caught.exception))

    def test_a_file_nobody_can_read_falls_back_to_the_folder(self) -> None:
        where = self.a_project("my-thing")
        (where / ".harness" / "project.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(projects.name_of(where), "my-thing")


class TheListIsAboutThisMachine(ProjectTestCase):
    def test_it_is_kept_beside_your_own_settings(self) -> None:
        """Not inside any project. A list of the folders on your computer is
        nobody else's business, and would be nonsense to anybody who cloned
        your repository."""

        where = self.a_project()
        projects.add(where)
        kept = projects.where_the_list_lives()
        self.assertTrue(kept.is_file())
        self.assertIn(str(self.somewhere_else), str(kept))
        for one in (self.root / "alpha" / ".harness").iterdir():
            self.assertNotEqual(one.name, "projects.json")

    def test_one_that_is_added_is_on_it(self) -> None:
        where = self.a_project()
        one = projects.add(where)
        self.assertEqual(one.path, str(where))
        self.assertEqual([held.path for held in projects.every_one()], [str(where)])

    def test_adding_the_same_one_twice_keeps_one(self) -> None:
        where = self.a_project()
        projects.add(where)
        projects.add(where)
        self.assertEqual(len(projects.every_one()), 1)

    def test_a_folder_that_is_not_there_is_refused(self) -> None:
        """A path with nothing at it is almost always a typo, and a list full of
        those is a list nobody trusts."""

        with self.assertRaises(projects.ProjectError) as caught:
            projects.add(self.root / "nowhere")
        self.assertIn("no folder at", str(caught.exception))

    def test_the_one_being_worked_on_is_always_in_the_list(self) -> None:
        """It would be odd for the one in front of you to be missing from it."""

        where = self.a_project()
        found = projects.every_one(where)
        self.assertEqual([one.path for one in found], [str(where)])

    def test_the_newest_opened_comes_first(self) -> None:
        first, second = self.a_project("first"), self.a_project("second")
        projects.add(first)
        projects.add(second)
        projects.opened(first, datetime(2026, 1, 1, 9, 0))
        projects.opened(second, datetime(2026, 1, 2, 9, 0))
        self.assertEqual(
            [one.name for one in projects.every_one()], ["second", "first"]
        )

    def test_the_one_you_are_on_comes_first(self) -> None:
        """Sorted only by when each was last opened, the row saying "you are
        working on this one" turned up in the middle of the list. Somebody
        pressing the first Rename they saw then renamed a different project."""

        first, second = self.a_project("first"), self.a_project("second")
        projects.add(first)
        projects.add(second)
        projects.opened(second, datetime(2026, 1, 2, 9, 0))

        found = projects.every_one(first)
        self.assertEqual(found[0].path, str(first))
        self.assertEqual(
            [one.name for one in found], ["first", "second"],
            "and the rest are still newest first",
        )

    def test_the_rest_are_still_newest_first_under_it(self) -> None:
        here = self.a_project("here")
        older, newer = self.a_project("older"), self.a_project("newer")
        for one in (here, older, newer):
            projects.add(one)
        projects.opened(older, datetime(2026, 1, 1, 9, 0))
        projects.opened(newer, datetime(2026, 1, 3, 9, 0))
        self.assertEqual(
            [one.name for one in projects.every_one(here)],
            ["here", "newer", "older"],
        )

    def test_a_list_nobody_could_read_does_not_grow_for_ever(self) -> None:
        for number in range(projects.MOST_KEPT + 5):
            projects.add(self.a_project(f"one-{number}"))
        self.assertEqual(len(projects.every_one()), projects.MOST_KEPT)

    def test_a_list_file_nobody_can_read_is_not_the_end_of_it(self) -> None:
        projects.where_the_list_lives().parent.mkdir(parents=True, exist_ok=True)
        projects.where_the_list_lives().write_text("{ not json", encoding="utf-8")
        self.assertEqual(projects.every_one(), [])
        where = self.a_project()
        projects.add(where)
        self.assertEqual([one.path for one in projects.every_one()], [str(where)])


class TakingOneOffTheListDeletesNothing(ProjectTestCase):
    def test_the_folder_and_everything_in_it_stays(self) -> None:
        where = self.a_project()
        (where / "important.txt").write_text("somebody's work", encoding="utf-8")
        projects.add(where)

        said = projects.forget(where)

        self.assertEqual(projects.every_one(), [])
        self.assertTrue(where.is_dir(), "the folder is still there")
        self.assertEqual(
            (where / "important.txt").read_text(encoding="utf-8"), "somebody's work"
        )
        self.assertIn("Nothing was deleted", said)

    def test_forgetting_one_that_was_never_on_the_list(self) -> None:
        self.assertIn("off the list", projects.forget(self.a_project()))


class HowSomebodyLikesTheListShown(ProjectTestCase):
    def test_it_starts_as_the_one_that_stays_out_of_the_way(self) -> None:
        self.assertEqual(projects.how_it_looks(), "slide-out")

    def test_it_can_be_made_to_stay(self) -> None:
        self.assertEqual(projects.make_it_look("always"), "always")
        self.assertEqual(projects.how_it_looks(), "always")

    def test_a_way_nobody_offers_is_refused(self) -> None:
        with self.assertRaises(projects.ProjectError) as caught:
            projects.make_it_look("upside down")
        self.assertIn("slide-out", str(caught.exception))

    def test_it_is_remembered_on_this_machine_and_not_in_a_project(self) -> None:
        """Kept in a project, the panel would change shape every time you
        switched."""

        where = self.a_project()
        projects.add(where)
        projects.make_it_look("always")
        self.assertNotIn(
            "project.json", [one.name for one in (where / ".harness").iterdir()]
        )
        self.assertIn("always", projects.where_the_list_lives().read_text(encoding="utf-8"))


class WhereWeAre(ProjectTestCase):
    def test_it_says_the_name_and_the_path(self) -> None:
        where = self.a_project()
        projects.rename(where, "The nightly one")
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), where, [], {})
        said = projects.where_we_are(config)
        self.assertEqual(said["name"], "The nightly one")
        self.assertEqual(said["path"], str(where))
        self.assertEqual(said["folder"], "alpha")

    def test_a_path_inside_your_home_is_shortened_the_way_people_read_it(self) -> None:
        with mock.patch.object(Path, "home", staticmethod(lambda: self.root)):
            where = self.a_project()
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), where, [], {})
            self.assertTrue(
                projects.where_we_are(config)["shortened"].startswith("~"),
                projects.where_we_are(config)["shortened"],
            )


class MovingToAnotherOne(ProjectTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.a_project("alpha")
        self.second = self.a_project("beta")
        config = load_config(self.first)
        pipelines.save(config, {
            "name": "Only in alpha",
            "nodes": [{"id": "start", "kind": "start", "label": "Start"}],
            "edges": [],
        })
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        thread = threading.Thread(target=self.panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.panel.shutdown)

    def ask(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": self.panel.token,
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_the_panel_is_told_where_it_is(self) -> None:
        _status, said = self.ask("/api/projects")
        self.assertEqual(said["here"]["name"], "alpha")
        self.assertEqual(said["here"]["path"], str(self.first))
        self.assertEqual(said["sidebar"], "slide-out")

    def test_moving_really_changes_which_automations_are_there(self) -> None:
        """This is the whole point: each project keeps its own."""

        _status, before = self.ask("/api/pipelines")
        self.assertEqual(before["saved"], ["Only in alpha"])

        status, said = self.ask("/api/projects/open", {"path": str(self.second)})
        self.assertEqual(status, 200, said)
        self.assertEqual(said["here"]["name"], "beta")

        _status, after = self.ask("/api/pipelines")
        self.assertEqual(after["saved"], [], "beta has its own, and it is empty")

    def test_a_blocked_pipeline_store_cannot_republish_the_old_project_after_move(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        moved = threading.Event()
        stores: list[object] = []
        result: list[object] = []

        class BlockingStore:
            def __init__(_self, config: LoadedConfig) -> None:
                _self.project_root = config.project_root
                stores.append(_self)
                if len(stores) == 1:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release the store constructor")

        with mock.patch.object(server.pipeline_runtime, "PipelineRunStore", BlockingStore):
            constructor = threading.Thread(
                target=lambda: result.append(self.panel.pipeline_store), daemon=True
            )
            constructor.start()
            self.assertTrue(entered.wait(5))

            switch = threading.Thread(
                target=lambda: (self.panel.move_to(str(self.second)), moved.set()),
                daemon=True,
            )
            switch.start()
            self.assertFalse(
                moved.wait(0.1),
                "move must wait until the old-project cache publication is settled",
            )
            release.set()
            constructor.join(5)
            switch.join(5)

            self.assertFalse(constructor.is_alive())
            self.assertFalse(switch.is_alive())
            self.assertTrue(moved.is_set())
            self.assertEqual(result[0].project_root, self.first)
            self.assertEqual(self.panel.config.project_root, self.second)
            self.assertIsNone(self.panel._pipeline_store)
            self.assertEqual(self.panel.pipeline_store.project_root, self.second)

    def test_a_blocked_swarm_store_cannot_republish_the_old_project_after_move(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        moved = threading.Event()
        stores: list[object] = []
        result: list[object] = []

        class BlockingStore:
            def __init__(_self, config: LoadedConfig) -> None:
                _self.project_root = config.project_root
                stores.append(_self)
                if len(stores) == 1:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release the store constructor")

            def active(_self) -> None:
                return None

        with mock.patch.object(server.swarm_runs, "SwarmRunStore", BlockingStore):
            constructor = threading.Thread(
                target=lambda: result.append(self.panel.swarm_runs), daemon=True
            )
            constructor.start()
            self.assertTrue(entered.wait(5))

            switch = threading.Thread(
                target=lambda: (self.panel.move_to(str(self.second)), moved.set()),
                daemon=True,
            )
            switch.start()
            self.assertFalse(
                moved.wait(0.1),
                "move must wait until the old-project cache publication is settled",
            )
            release.set()
            constructor.join(5)
            switch.join(5)

            self.assertFalse(constructor.is_alive())
            self.assertFalse(switch.is_alive())
            self.assertTrue(moved.is_set())
            self.assertEqual(result[0].project_root, self.first)
            self.assertEqual(self.panel.config.project_root, self.second)
            self.assertIsNone(self.panel._swarm_runs)
            self.assertEqual(self.panel.swarm_runs.project_root, self.second)

    def test_move_is_refused_while_pipeline_acceptance_is_in_flight(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        response_started = threading.Event()
        release_response = threading.Event()
        self.addCleanup(release_response.set)

        class BlockingStore:
            project_root = self.first

            def accept(_self, *_args, **_kwargs):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release acceptance")
                return ({
                    "run_id": "alpha-replay",
                    "attempt_id": "alpha-attempt",
                    "name": "Only in alpha",
                    "definition_digest": "alpha-digest",
                }, False)

            def active(_self) -> None:
                return None

        self.panel._pipeline_store = BlockingStore()
        answer: list[tuple[int, dict]] = []
        original_json = server.HarnessHandler._json

        def block_replay_response(handler, value, status=200):
            if handler.path == "/api/pipelines/agent-run":
                response_started.set()
                if not release_response.wait(5):
                    raise RuntimeError("test did not release the replay response")
            return original_json(handler, value, status)

        with mock.patch.object(server.HarnessHandler, "_json", block_replay_response):
            request = threading.Thread(
                target=lambda: answer.append(self.ask(
                    "/api/pipelines/agent-run",
                    {"automation": "Only in alpha", "request_id": "same-alpha-request"},
                )),
                daemon=True,
            )
            request.start()
            self.assertTrue(entered.wait(5))

            with self.assertRaisesRegex(server.HarnessError, "being accepted"):
                self.panel.move_to(str(self.second))
            self.assertEqual(self.panel.config.project_root, self.first)

            release.set()
            self.assertTrue(response_started.wait(5))
            # The durable acceptance has returned, so slow response I/O must
            # not make the project appear busy after the work is really done.
            self.panel.move_to(str(self.second))
            self.assertEqual(self.panel.config.project_root, self.second)

            release_response.set()
            request.join(5)
            self.assertFalse(request.is_alive())
            self.assertEqual(answer[0][0], 202, answer)
            self.assertTrue(answer[0][1]["replayed"])

    def test_move_is_refused_for_each_whole_legacy_provider_turn(self) -> None:
        provider_roots: list[Path] = []
        cases = (
            ("say", "/api/chat/say", {"who": "codex", "text": "stay in alpha"},
             {"said": "alpha answer"}),
            ("ask_everyone", "/api/chat/ask-everyone", {"text": "stay in alpha"}, []),
        )

        for provider_method, path, body, returned in cases:
            with self.subTest(path=path):
                entered = threading.Event()
                release = threading.Event()

                def blocked(config: LoadedConfig, *_args) -> object:
                    provider_roots.append(config.project_root)
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release provider turn")
                    return returned

                answer: list[tuple[int, dict]] = []
                with mock.patch.object(chat, provider_method, side_effect=blocked):
                    request = threading.Thread(
                        target=lambda: answer.append(self.ask(path, body)), daemon=True
                    )
                    request.start()
                    self.assertTrue(entered.wait(5))

                    with self.assertRaisesRegex(server.HarnessError, "contacting a provider"):
                        self.panel.move_to(str(self.second))
                    self.assertEqual(self.panel.config.project_root, self.first)

                    release.set()
                    request.join(5)

                self.assertFalse(request.is_alive())
                self.assertEqual(answer[0][0], 200, answer)

        self.assertEqual(provider_roots, [self.first, self.first])
        self.panel.move_to(str(self.second))
        self.assertEqual(self.panel.config.project_root, self.second)

    def test_talk_authority_check_and_provider_call_stay_on_the_same_project(self) -> None:
        # Give alpha a valid local authority and make beta the copied project
        # whose execution must remain paused.
        self.panel.pipeline_store
        descriptor = self.first / ".harness" / "project-authority.json"
        copied = self.second / ".harness" / "project-authority.json"
        copied.write_bytes(descriptor.read_bytes())

        authority_entered = threading.Event()
        release_authority = threading.Event()
        provider_roots: list[Path] = []
        original_status = self.panel.project_authority_status

        def blocked_authority() -> dict:
            status = original_status()
            authority_entered.set()
            if not release_authority.wait(5):
                raise RuntimeError("test did not release authority inspection")
            return status

        def answer(config: LoadedConfig, _who: str, _text: str) -> dict:
            provider_roots.append(config.project_root)
            return {"said": "alpha answer"}

        result: list[tuple[int, dict]] = []
        with mock.patch.object(
            self.panel, "project_authority_status", side_effect=blocked_authority
        ), mock.patch.object(chat, "say", side_effect=answer):
            request = threading.Thread(
                target=lambda: result.append(self.ask(
                    "/api/chat/say", {"who": "codex", "text": "stay in alpha"}
                )),
                daemon=True,
            )
            request.start()
            self.assertTrue(authority_entered.wait(5))
            try:
                # The read-only fast precheck may overlap a move. Its answer
                # must never authorize the later provider call: the admitted
                # recheck sees beta's copied authority and refuses it.
                self.panel.move_to(str(self.second))
                self.assertEqual(self.panel.config.project_root, self.second)
            finally:
                release_authority.set()
            request.join(5)

        self.assertFalse(request.is_alive())
        self.assertEqual(result[0][0], 400, result)
        self.assertIn("copied or substituted", result[0][1]["error"])
        self.assertEqual(provider_roots, [])

        # The copied target remains visibly execution-paused instead of
        # inheriting alpha's earlier check.
        status, standing = self.ask("/api/chat")
        self.assertEqual(status, 200, standing)
        self.assertTrue(standing["cannot_run"], standing)
        self.assertFalse(standing["authority"]["can_run"], standing)

    def test_moving_says_the_page_will_be_read_again(self) -> None:
        _status, said = self.ask("/api/projects/open", {"path": str(self.second)})
        self.assertIn("reloads", said["note"])

    def test_moving_somewhere_that_is_not_there(self) -> None:
        status, said = self.ask(
            "/api/projects/open", {"path": str(self.root / "nowhere")}
        )
        self.assertGreaterEqual(status, 400)
        self.assertIn("no folder at", json.dumps(said))
        self.assertEqual(self.panel.config.project_root, self.first)

    def test_it_will_not_move_while_an_automation_is_running(self) -> None:
        """Halfway through, the run would be reading one project's settings and
        writing into another's."""

        self.panel.pipeline_running = True
        try:
            status, said = self.ask(
                "/api/projects/open", {"path": str(self.second)}
            )
        finally:
            self.panel.pipeline_running = False
        self.assertGreaterEqual(status, 400)
        self.assertIn("running", json.dumps(said).lower())
        self.assertEqual(self.panel.config.project_root, self.first)

    def test_it_will_not_move_while_the_checks_are_running(self) -> None:
        self.assertTrue(self.panel.reserve_qa())
        try:
            status, said = self.ask(
                "/api/projects/open", {"path": str(self.second)}
            )
        finally:
            self.panel.release_qa()
        self.assertGreaterEqual(status, 400)
        self.assertIn("checks are running", json.dumps(said).lower())

    def test_it_will_not_move_while_a_durable_board_command_is_active(self) -> None:
        store = self.panel.swarm_runs
        accepted, created = store.accept(
            "active-board-switch", {"kind": "board_order", "objective": "wait"}
        )
        self.assertTrue(created)
        run_id = str(accepted["run_id"])
        store.start(run_id)
        status, said = self.ask(
            "/api/projects/open", {"path": str(self.second)}
        )
        self.assertGreaterEqual(status, 400)
        self.assertIn("swarm board or chat command is active", json.dumps(said).lower())
        self.assertEqual(self.panel.config.project_root, self.first)
        # Refusing the switch keeps the old project's exact durable Stop
        # authority reachable.
        self.assertEqual(store.request_stop(run_id)["status"], "stopping")
        store.fail(run_id, "stopped for test", stopped=True)

    def test_it_will_not_move_while_a_durable_chat_command_is_active(self) -> None:
        store = self.panel.swarm_runs
        accepted, created = store.accept(
            "active-chat-switch", {"kind": "chat_order", "objective": "wait"}
        )
        self.assertTrue(created)
        run_id = str(accepted["run_id"])
        store.start(run_id)
        status, said = self.ask(
            "/api/projects/open", {"path": str(self.second)}
        )
        self.assertGreaterEqual(status, 400)
        self.assertIn("swarm board or chat command is active", json.dumps(said).lower())
        self.assertEqual(self.panel.config.project_root, self.first)
        self.assertEqual(store.request_stop(run_id)["status"], "stopping")
        store.fail(run_id, "stopped for test", stopped=True)

    def test_what_is_kept_out_of_the_news_moves_with_the_project(self) -> None:
        """It is worked out from a project's own settings, so a run in the new
        one must not be cleaned with the old one's rules."""

        before = self.panel.events.redactor
        self.ask("/api/projects/open", {"path": str(self.second)})
        self.assertIsNot(self.panel.events.redactor, before)

    def test_moving_remembers_it_was_opened(self) -> None:
        self.ask("/api/projects/open", {"path": str(self.second)})
        _status, said = self.ask("/api/projects")
        self.assertEqual(said["projects"][0]["path"], str(self.second))

    def test_the_list_can_be_added_to_renamed_and_forgotten(self) -> None:
        status, said = self.ask("/api/projects/add", {"path": str(self.second)})
        self.assertEqual(status, 200, said)

        _status, named = self.ask(
            "/api/projects/rename", {"path": str(self.second), "name": "The other one"}
        )
        self.assertEqual(named["name"], "The other one")

        _status, gone = self.ask("/api/projects/forget", {"path": str(self.second)})
        self.assertIn("Nothing was deleted", gone["note"])
        self.assertTrue(self.second.is_dir())

    def test_renaming_with_no_path_names_the_one_being_worked_on(self) -> None:
        _status, said = self.ask("/api/projects/rename", {"name": "This one"})
        self.assertEqual(said["name"], "This one")
        self.assertEqual(projects.name_of(self.first), "This one")

    def test_how_the_list_looks_can_be_changed_and_is_remembered(self) -> None:
        _status, said = self.ask("/api/projects/sidebar", {"how": "always"})
        self.assertEqual(said["sidebar"], "always")
        _status, again = self.ask("/api/projects")
        self.assertEqual(again["sidebar"], "always")

    def test_a_way_of_showing_it_that_nobody_offers(self) -> None:
        status, said = self.ask("/api/projects/sidebar", {"how": "upside down"})
        self.assertGreaterEqual(status, 400)
        self.assertIn("slide-out", json.dumps(said))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
