"""Models running here, boards kept under a name, and finer control on a step.

Three things that were nearly possible and not quite. The settings had taken an
Ollama address for as long as there had been settings, and nothing ever went and
looked for one. The board was written down and came back, and there was only
ever the one. A step could be told to try again and could not be told to stop.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from our_harness import local_models, pipelines, swarm
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class StandingInForAModelServer(BaseHTTPRequestHandler):
    """Answers the way Ollama and LM Studio answer."""

    replies: dict[str, tuple[int, dict]] = {}

    def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
        number, said = type(self).replies.get(self.path, (404, {"error": "no"}))
        held = json.dumps(said).encode("utf-8")
        self.send_response(number)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(held)))
        self.end_headers()
        self.wfile.write(held)

    def log_message(self, *args) -> None:  # noqa: A003 - quiet in the test output
        return


class ModelsRunningHereTests(unittest.TestCase):
    """A model on your own machine needs no seat, no key and nobody's
    permission, and on a locked-down machine it is often the only one that will
    ever work. Somebody with Ollama running still had to know the port and the
    model's name and write both into a file by hand."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StandingInForAModelServer)
        cls.where = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        StandingInForAModelServer.replies = {}

    def answers(self, path: str, said: dict) -> None:
        StandingInForAModelServer.replies[path] = (200, said)

    def only_ours(self, held):
        """The stand-in, put where the two usual ones would be looked for."""

        return mock.patch.object(local_models, "THE_USUAL_ONES", tuple(held))

    def an_entry(self, **held):
        return local_models.WhereModelsRun(**{
            "id": "ours", "label": "Ours", "kind": "ollama",
            "endpoint": self.where, "asks_at": f"{self.where}/api/tags",
            "names_under": "models", "how_to_get_it": "Get it from somewhere.",
            **held,
        })

    def test_it_reads_the_shape_ollama_answers_with(self) -> None:
        self.answers("/api/tags", {"models": [
            {"name": "qwen2.5-coder:7b"}, {"name": "llama3:8b"}]})
        with self.only_ours([self.an_entry()]):
            found = local_models.look()
        self.assertTrue(found[0].running)
        self.assertEqual(found[0].models, ["llama3:8b", "qwen2.5-coder:7b"])

    def test_it_reads_the_shape_lm_studio_answers_with(self) -> None:
        """One says name under models and the other says id under data, so both
        are looked for rather than one being assumed."""

        self.answers("/v1/models", {"data": [{"id": "a-model"}]})
        with self.only_ours([self.an_entry(
                kind="openai-compatible", asks_at=f"{self.where}/v1/models",
                names_under="data")]):
            found = local_models.look()
        self.assertEqual(found[0].models, ["a-model"])

    def test_nothing_running_is_an_ordinary_answer_and_not_a_fault(self) -> None:
        """A server that is not running is the usual case, not a red line, and
        finding out must not take more than a moment."""

        with self.only_ours([self.an_entry(
                asks_at="http://127.0.0.1:9/nothing-is-here")]):
            found = local_models.look()
        self.assertFalse(found[0].running)
        self.assertIn("not running", found[0].why_not)

    def test_one_that_is_not_running_says_how_to_get_it(self) -> None:
        """Nothing found is a worse answer than here is what you could have."""

        with self.only_ours([self.an_entry(asks_at="http://127.0.0.1:9/nothing")]):
            self.assertTrue(local_models.look()[0].how_to_get_it)

    def test_running_with_nothing_in_it_says_that_rather_than_nothing(self) -> None:
        self.answers("/api/tags", {"models": []})
        with self.only_ours([self.an_entry()]):
            found = local_models.look()
        self.assertTrue(found[0].running)
        self.assertIn("no models in it yet", found[0].why_not)

    def test_a_long_list_of_models_is_cut_to_something_a_page_can_hold(self) -> None:
        self.answers("/api/tags", {"models": [
            {"name": f"model-{one:03}"} for one in range(200)]})
        with self.only_ours([self.an_entry()]):
            found = local_models.look()
        self.assertLessEqual(len(found[0].models), local_models.MOST_MODELS_SHOWN)

    def test_an_answer_that_is_nonsense_does_not_bring_the_page_down(self) -> None:
        StandingInForAModelServer.replies["/api/tags"] = (200, {"models": "not a list"})
        with self.only_ours([self.an_entry()]):
            found = local_models.look()
        self.assertEqual(found[0].models, [])

    def test_a_route_for_one_needs_no_key(self) -> None:
        """The whole point of running it here."""

        held = self.an_entry(models=["a-model"], running=True)
        route = local_models.a_route_for(held, "a-model")
        self.assertEqual(route["api_key_env"], "")
        self.assertEqual(route["model"], "a-model")
        self.assertEqual(route["endpoint"], self.where)

    def test_a_model_that_is_not_there_is_refused(self) -> None:
        held = self.an_entry(models=["a-model"], running=True)
        with self.assertRaises(ValueError):
            local_models.a_route_for(held, "a-different-model")

    def test_a_server_that_is_not_running_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            local_models.a_route_for(self.an_entry(models=["a-model"]), "a-model")

    def test_an_address_somebody_typed_in_is_asked_too(self) -> None:
        """Plenty of things answer the OpenAI shape and there is no finding
        those by guessing."""

        self.answers("/v1/models", {"data": [{"id": "from-elsewhere"}]})
        with self.only_ours([]):
            found = local_models.look(also=(f"{self.where}/v1",))
        self.assertEqual(found[0].models, ["from-elsewhere"])


class AskingThemAllAtOnceTests(unittest.TestCase):
    """This runs while somebody is looking at a page.

    One at a time, the wait is the sum of every place that is not running, and
    whoever opened the tab pays for each of them in turn to be told nothing. Two
    seconds each for two of them is four seconds of an app that feels broken.
    """

    def test_two_slow_ones_do_not_add_up(self) -> None:
        import time

        slowly = []

        def dawdle(where):
            slowly.append(where)
            time.sleep(0.4)
            return None

        held = [
            local_models.WhereModelsRun(
                id=f"slow-{one}", label=f"Slow {one}", kind="ollama",
                endpoint=f"http://127.0.0.1:{9000 + one}",
                asks_at=f"http://127.0.0.1:{9000 + one}/tags",
                names_under="models", how_to_get_it="From somewhere.")
            for one in range(4)
        ]
        with mock.patch.object(local_models, "THE_USUAL_ONES", tuple(held)),              mock.patch.object(local_models, "_ask_briefly", dawdle):
            started = time.monotonic()
            found = local_models.look()
            took = time.monotonic() - started
        self.assertEqual(len(slowly), 4, "not all of them were asked")
        self.assertEqual(len(found), 4)
        self.assertLess(took, 1.2, f"four at 0.4 seconds each took {took:.2f} seconds")

    def test_the_wait_is_short_enough_to_be_a_page_load(self) -> None:
        """A server on this machine answers in milliseconds or it is not there."""

        self.assertLessEqual(local_models.HOW_LONG_TO_WAIT, 1.0)


class OnlyThisMachineTests(unittest.TestCase):
    """Nothing hands an address to this today, and unreachable is not the same
    as safe. The moment somebody wires a box up to it, whatever is typed there
    is fetched by the panel, from wherever the panel can reach."""

    def test_an_address_on_this_machine_is_fine(self) -> None:
        for where in ("http://127.0.0.1:1234/v1", "http://localhost:8080",
                      "https://127.0.0.1:9/v1"):
            with self.subTest(where=where):
                self.assertEqual(local_models._only_this_machine(where), where)

    def test_somewhere_else_is_refused(self) -> None:
        for where in ("http://10.0.0.5/v1", "http://evil.example.com/v1",
                      "http://192.168.1.7:1234", "http://[2001:db8::1]/v1"):
            with self.subTest(where=where), self.assertRaises(ValueError):
                local_models._only_this_machine(where)

    def test_something_that_is_not_a_web_address_is_refused(self) -> None:
        """A file on the disk read by something that fetches addresses is how
        somebody reads a file they were never shown."""

        for where in ("file:///c:/secret", "ftp://127.0.0.1/x", "/etc/passwd", ""):
            with self.subTest(where=where), self.assertRaises(ValueError):
                local_models._only_this_machine(where)

    def test_an_address_that_is_refused_never_gets_asked(self) -> None:
        asked = []
        with mock.patch.object(
                local_models, "_ask_briefly", lambda where: asked.append(where)):
            with self.assertRaises(ValueError):
                local_models._one_somebody_pointed_at("http://evil.example.com")
        self.assertEqual(asked, [], "it was fetched before being checked")


class BoardsKeptUnderANameTests(unittest.TestCase):
    """One board came back on its own and only ever one, so a second
    arrangement meant taking the first apart and building it again from memory
    on Monday."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        (self.home / "project").mkdir()
        self.config = LoadedConfig(
            copy.deepcopy(DEFAULT_CONFIG), self.home / "project", [], {})
        runtime = mock.patch.dict(os.environ, {
            "OUR_HARNESS_SWARM_RUN_DIR": str(self.home / "runtime")
        })
        runtime.start()
        self.addCleanup(runtime.stop)
        held = mock.patch.object(
            swarm, "where_the_kept_ones_live", lambda: self.home / "swarms")
        held.start()
        self.addCleanup(held.stop)
        board = mock.patch.object(swarm, "where_it_lives", lambda: self.home / "swarm.json")
        board.start()
        self.addCleanup(board.stop)

    def a_board_with(self, *names: str) -> None:
        swarm.save({"agents": [{"name": one} for one in names],
                    "projects": [], "works_on": [], "talks_to": []}, self.config)

    def test_a_board_can_be_saved_and_listed(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("Friday work", self.config)
        listed = swarm.every_kept_board()
        self.assertEqual([one["name"] for one in listed], ["Friday work"])
        self.assertEqual(listed[0]["agents"], 1)

    def test_opening_one_puts_it_back_on_the_board(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("Friday work", self.config)
        self.a_board_with("Somebody else")
        self.assertEqual([one.name for one in swarm.load().agents], ["Somebody else"])
        swarm.open_this_board("Friday work", self.config)
        self.assertEqual([one.name for one in swarm.load().agents], ["The planner"])

    def test_the_last_opened_saved_board_is_remembered_across_a_restart(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("This week", self.config)
        self.a_board_with("The reviewer")
        swarm.keep_this_board("Fridays", self.config)

        swarm.open_this_board("This week", self.config)
        swarm.open_this_board("Fridays", self.config)

        restarted = swarm.load()
        self.assertEqual(restarted.active_saved_board, "Fridays")
        self.assertEqual([one.name for one in restarted.agents], ["The reviewer"])
        listed = {one["name"]: one["active"] for one in swarm.every_kept_board()}
        self.assertEqual(listed, {"This week": False, "Fridays": True})

    def test_edits_to_the_open_saved_board_survive_without_losing_its_identity(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("Friday work", self.config)
        opened = swarm.open_this_board("Friday work", self.config)
        changed = opened.to_dict()
        changed.pop("active_saved_board")  # as sent by a panel from before this feature
        changed["agents"].append({"name": "The reviewer"})
        swarm.save(changed, self.config)

        restarted = swarm.load()
        self.assertEqual(restarted.active_saved_board, "Friday work")
        self.assertEqual(
            [one.name for one in restarted.agents], ["The planner", "The reviewer"])

    def test_two_boards_are_kept_apart(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("This week", self.config)
        self.a_board_with("The reviewer", "The writer")
        swarm.keep_this_board("Fridays", self.config)
        self.assertEqual(
            sorted(one["name"] for one in swarm.every_kept_board()),
            ["Fridays", "This week"])
        swarm.open_this_board("This week", self.config)
        self.assertEqual([one.name for one in swarm.load().agents], ["The planner"])

    def test_names_that_differ_only_in_capitals_stay_apart(self) -> None:
        """A file name on Windows does not care about capitals, so two names
        somebody meant to keep apart would share one file and one of them would
        quietly become the other."""

        self.a_board_with("The planner")
        swarm.keep_this_board("Friday", self.config)
        self.a_board_with("The reviewer")
        swarm.keep_this_board("friday", self.config)
        self.assertEqual(len(swarm.every_kept_board()), 2)

    def test_a_name_cannot_reach_out_of_the_folder_it_belongs_in(self) -> None:
        self.a_board_with("The planner")
        for awkward in ("../elsewhere", "a/b", "..", "with\\backslash"):
            with self.subTest(name=awkward), self.assertRaises(swarm.SwarmError):
                swarm.keep_this_board(awkward, self.config)

    def test_an_empty_name_is_refused(self) -> None:
        with self.assertRaises(swarm.SwarmError):
            swarm.keep_this_board("   ", self.config)

    def test_one_can_be_deleted(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("Friday work", self.config)
        swarm.forget_this_board("Friday work", self.config)
        self.assertEqual(swarm.every_kept_board(), [])

    def test_deleting_the_open_saved_board_clears_its_identity(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("Friday work", self.config)
        swarm.open_this_board("Friday work", self.config)
        swarm.forget_this_board("Friday work", self.config)
        self.assertEqual(swarm.load().active_saved_board, "")

    def test_deleting_one_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(swarm.SwarmError):
            swarm.forget_this_board("Never existed", self.config)

    def test_opening_one_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(swarm.SwarmError):
            swarm.open_this_board("Never existed", self.config)

    def test_saving_over_one_replaces_it_rather_than_making_a_second(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("Friday work", self.config)
        self.a_board_with("The reviewer")
        swarm.keep_this_board("Friday work", self.config)
        listed = swarm.every_kept_board()
        self.assertEqual(len(listed), 1)
        swarm.open_this_board("Friday work", self.config)
        self.assertEqual([one.name for one in swarm.load().agents], ["The reviewer"])

    def test_there_is_a_lid_on_how_many_are_kept(self) -> None:
        """Remembered between sessions and never tidied, this is somewhere a
        folder quietly fills up."""

        self.a_board_with("The planner")
        for number in range(swarm.MOST_KEPT_BOARDS):
            swarm.keep_this_board(f"Board {number}", self.config)
        with self.assertRaises(swarm.SwarmError) as caught:
            swarm.keep_this_board("One too many", self.config)
        self.assertIn("which is the most", str(caught.exception))

    def test_a_saved_board_that_cannot_be_read_does_not_hide_the_others(self) -> None:
        self.a_board_with("The planner")
        swarm.keep_this_board("A good one", self.config)
        (swarm.where_the_kept_ones_live() / "rubbish.json").write_text(
            "not json at all", encoding="utf-8")
        self.assertEqual([one["name"] for one in swarm.every_kept_board()], ["A good one"])

    def test_what_is_saved_is_checked_the_way_any_board_is(self) -> None:
        """A board saved by an older version goes back in through the same door
        as anything else rather than being trusted."""

        self.a_board_with("The planner")
        swarm.keep_this_board("Friday work", self.config)
        where = swarm.where_the_kept_ones_live()
        one = next(iter(where.glob("*.json")))
        held = json.loads(one.read_text(encoding="utf-8"))
        held["board"]["agents"].append({"name": "The planner"})   # the same name twice
        one.write_text(json.dumps(held), encoding="utf-8")
        with self.assertRaises(swarm.SwarmError):
            swarm.open_this_board("Friday work", self.config)


class TheBoardSurvivesTheChecksTests(unittest.TestCase):
    """This cost a real person their board.

    A check that rearranges the board to see whether rearranging works timed out
    part way through, so the step that puts it back never ran, and four agents
    and two projects were gone. The checks were written carefully. Careful is not
    enough when a run is killed in the middle.
    """

    def setUp(self) -> None:
        from our_harness import qa

        self.qa = qa
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.where = Path(self.temporary.name) / "swarm.json"
        held = mock.patch.object(swarm, "where_it_lives", lambda: self.where)
        held.start()
        self.addCleanup(held.stop)

    def a_case(self, *touches: str):
        class Pretend:
            def __init__(self, touches):
                self.touches = touches
        return Pretend(touches)

    def test_a_check_that_wrecks_the_board_does_not_keep_it_wrecked(self) -> None:
        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")
        with self.qa._the_board_put_back_afterwards([self.a_case("the board of agents")]):
            self.where.write_text('{"agents": [{"name": "A test agent"}]}', encoding="utf-8")
        self.assertIn("Yours", self.where.read_text(encoding="utf-8"))

    def test_it_is_put_back_even_when_the_run_blows_up(self) -> None:
        """Which is the case that did the damage: nothing tidy runs after a
        check that was killed."""

        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            with self.qa._the_board_put_back_afterwards([self.a_case("the board of agents")]):
                self.where.write_text('{"agents": []}', encoding="utf-8")
                raise RuntimeError("the run was killed")
        self.assertIn("Yours", self.where.read_text(encoding="utf-8"))

    def test_a_board_a_check_made_out_of_nothing_is_not_left_behind(self) -> None:
        with self.qa._the_board_put_back_afterwards([self.a_case("the board of agents")]):
            self.where.write_text('{"agents": [{"name": "A test agent"}]}', encoding="utf-8")
        self.assertFalse(self.where.exists())

    def test_a_run_that_never_touches_the_board_leaves_it_entirely_alone(self) -> None:
        """Including not rewriting it for no reason, which is its own small way
        of losing something."""

        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")
        was = self.where.stat().st_mtime_ns
        with self.qa._the_board_put_back_afterwards([self.a_case("something else")]):
            pass
        self.assertEqual(self.where.stat().st_mtime_ns, was)

    def test_a_copy_is_written_before_anything_runs(self) -> None:
        """The part that holds when nothing else does.

        A run killed outright never reaches the putting-back - which is what did
        the damage in the first place - and a file something has open cannot be
        moved over. Neither of those can touch a copy already written.
        """

        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")
        with self.qa._the_board_put_back_afterwards(
                [self.a_case("the board of agents")], "a-run"):
            pass
        copies = list((self.where.parent / self.qa.WHERE_THE_BOARD_IS_COPIED).glob("*.json"))
        self.assertEqual(len(copies), 1)
        held = json.loads(copies[0].read_text(encoding="utf-8"))
        self.assertIn("Yours", held["board"])

    def test_the_copy_is_left_there_when_the_run_is_killed(self) -> None:
        """Killed outright there is no putting-back at all, so the copy is the
        only thing between somebody and losing the lot."""

        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")
        held = self.qa._the_board_put_back_afterwards(
            [self.a_case("the board of agents")], "a-run")
        held.__enter__()   # started, and then nothing tidy ever happens
        self.where.write_text('{"agents": []}', encoding="utf-8")
        copies = list((self.where.parent / self.qa.WHERE_THE_BOARD_IS_COPIED).glob("*.json"))
        self.assertEqual(len(copies), 1)
        self.assertIn("Yours", json.loads(copies[0].read_text(encoding="utf-8"))["board"])

    def test_a_board_something_is_holding_open_is_waited_for(self) -> None:
        """Windows will not move a file over one that anything has open, even
        only to read it, and a panel reading the board is exactly that. Left to
        a plain move this threw, the throw was swallowed, and the board stayed
        as the check left it."""

        patient = []
        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")
        real = self.qa.put_this_file_in_place

        def watching(path, written):
            patient.append(path.name)
            return real(path, written)

        with mock.patch.object(self.qa, "put_this_file_in_place", watching):
            with self.qa._the_board_put_back_afterwards(
                    [self.a_case("the board of agents")], "a-run"):
                self.where.write_text('{"agents": []}', encoding="utf-8")
        self.assertIn(self.where.name, patient, "it was put back with a plain move")
        self.assertIn("Yours", self.where.read_text(encoding="utf-8"))

    def test_a_board_that_cannot_be_put_back_is_said_out_loud(self) -> None:
        """Swallowed, somebody is left with a board that is not the one they
        arranged and no reason to think anything happened to it."""

        import io
        import contextlib as ctx

        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")

        def refuse(path, written):
            raise OSError("something has it open")

        said = io.StringIO()
        with mock.patch.object(self.qa, "put_this_file_in_place", refuse), \
             ctx.redirect_stderr(said):
            with self.qa._the_board_put_back_afterwards(
                    [self.a_case("the board of agents")], "a-run"):
                self.where.write_text('{"agents": []}', encoding="utf-8")
        self.assertIn("could not be put back", said.getvalue())

    def test_boards_saved_under_a_name_are_covered_too(self) -> None:
        """A check can delete one of these as easily as it can change the live
        board, and deleting somebody's saved arrangement is the worse of the
        two."""

        kept = self.where.parent / "swarms"
        kept.mkdir(parents=True, exist_ok=True)
        (kept / "friday-abc123.json").write_text(
            '{"name": "Friday", "board": {}}', encoding="utf-8")
        held = mock.patch.object(swarm, "where_the_kept_ones_live", lambda: kept)
        held.start()
        self.addCleanup(held.stop)

        with self.qa._the_board_put_back_afterwards(
                [self.a_case("the board of agents")], "a-run"):
            (kept / "friday-abc123.json").unlink()
        self.assertTrue(
            (kept / "friday-abc123.json").is_file(), "a saved board was left deleted")
        self.assertIn("Friday", (kept / "friday-abc123.json").read_text(encoding="utf-8"))

    def test_a_saved_board_a_check_changed_is_put_back_as_it_was(self) -> None:
        kept = self.where.parent / "swarms"
        kept.mkdir(parents=True, exist_ok=True)
        (kept / "friday-abc123.json").write_text('{"name": "Friday"}', encoding="utf-8")
        held = mock.patch.object(swarm, "where_the_kept_ones_live", lambda: kept)
        held.start()
        self.addCleanup(held.stop)

        with self.qa._the_board_put_back_afterwards(
                [self.a_case("the board of agents")], "a-run"):
            (kept / "friday-abc123.json").write_text('{"name": "Not Friday"}', encoding="utf-8")
        self.assertIn("\"Friday\"", (kept / "friday-abc123.json").read_text(encoding="utf-8"))

    def test_the_copies_do_not_pile_up_for_ever(self) -> None:
        self.where.write_text('{"agents": []}', encoding="utf-8")
        where = self.where.parent / self.qa.WHERE_THE_BOARD_IS_COPIED
        where.mkdir(parents=True, exist_ok=True)
        for number in range(self.qa.MOST_BOARD_COPIES + 8):
            (where / f"2020010{number:04}-old.json").write_text("{}", encoding="utf-8")
        with self.qa._the_board_put_back_afterwards(
                [self.a_case("the board of agents")], "a-run"):
            pass
        self.assertLessEqual(
            len(list(where.glob("*.json"))), self.qa.MOST_BOARD_COPIES)

    def test_the_runner_really_wraps_a_run_in_it(self) -> None:
        """Having the safety net and using it are two different things, and only
        the second one saved anybody's board."""

        import copy

        from our_harness.config import DEFAULT_CONFIG, LoadedConfig

        here = Path(self.temporary.name) / "project"
        (here / ".harness").mkdir(parents=True)
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), here, [], {})
        (here / "there.txt").write_text("here", encoding="utf-8")
        suite = self.qa.parse_suite({
            "name": "one",
            "cases": [{"id": "one", "kind": "file", "path": "there.txt",
                       "touches": ["the board of agents"]}],
        })

        used = []
        real = self.qa._the_board_put_back_afterwards

        def watching(cases, run_id=""):
            used.append(len(list(cases)))
            return real(cases, run_id)

        with mock.patch.object(self.qa, "_the_board_put_back_afterwards", watching):
            self.qa.QaRunner(config).run(suite, run_id="one", write_artifacts=False)
        self.assertEqual(used, [1], "the run did not go through the safety net")

    def test_a_board_that_did_not_change_is_not_written_over(self) -> None:
        self.where.write_text('{"agents": [{"name": "Yours"}]}', encoding="utf-8")
        was = self.where.stat().st_mtime_ns
        with self.qa._the_board_put_back_afterwards([self.a_case("the board of agents")]):
            pass
        self.assertEqual(self.where.stat().st_mtime_ns, was)


class FinerControlOnAStepTests(unittest.TestCase):
    """A step could be told to try again and could not be told to stop, and one
    nice-to-have failing threw away everything that had already passed."""

    def a_pipeline(self, **settings) -> dict:
        return {
            "name": "one",
            "nodes": [{"id": "a", "kind": "suite", "label": "Checks",
                       "settings": settings, "at": {"x": 0, "y": 0}}],
            "edges": [],
        }

    def test_a_step_can_be_given_a_time_limit(self) -> None:
        held = pipelines.read_it(self.a_pipeline(longest=90))
        self.assertEqual(held["nodes"][0]["settings"]["longest"], 90)

    def test_a_silly_time_limit_is_refused(self) -> None:
        for silly in (-1, pipelines.LONGEST_A_STEP_MAY_TAKE + 1, "soon", True):
            with self.subTest(longest=silly), self.assertRaises(pipelines.PipelineError):
                pipelines.read_it(self.a_pipeline(longest=silly))

    def test_no_time_limit_is_a_perfectly_good_answer(self) -> None:
        held = pipelines.read_it(self.a_pipeline(longest=0))
        self.assertEqual(held["nodes"][0]["settings"].get("longest", 0), 0)

    def test_a_step_can_be_let_off_failing(self) -> None:
        held = pipelines.read_it(self.a_pipeline(even_if_it_fails=True))
        self.assertIs(held["nodes"][0]["settings"]["even_if_it_fails"], True)

    def test_being_let_off_failing_is_yes_or_no(self) -> None:
        with self.assertRaises(pipelines.PipelineError):
            pipelines.read_it(self.a_pipeline(even_if_it_fails="sometimes"))

    def test_a_step_let_off_failing_does_not_fail_the_run(self) -> None:
        """Some steps are the point of the whole run and some are a
        nice-to-have. One nice-to-have failing should not throw away the work
        that already passed."""

        held = self.a_pipeline(even_if_it_fails=True)
        item = pipelines.NodeResult(id="a", kind="suite", label="Checks")
        item.state = pipelines.FAILED
        self.assertTrue(pipelines._was_allowed_to_fail(held, item))

    def test_an_ordinary_step_still_fails_the_run(self) -> None:
        held = self.a_pipeline()
        item = pipelines.NodeResult(id="a", kind="suite", label="Checks")
        item.state = pipelines.FAILED
        self.assertFalse(pipelines._was_allowed_to_fail(held, item))

    def test_the_time_limit_reaches_the_thing_that_asks_whether_to_stop(self) -> None:
        """A step is handed one thing to ask "should I stop yet". Handed two,
        the kind that forgot to ask both would be the one that hung."""

        import time

        should_stop = pipelines._also_stopping_after(None, time.monotonic() - 10, 5)
        self.assertTrue(should_stop())
        still_fine = pipelines._also_stopping_after(None, time.monotonic(), 60)
        self.assertFalse(still_fine())

    def test_the_run_s_own_stop_button_still_works_alongside_it(self) -> None:
        import time

        held = pipelines._also_stopping_after(lambda: True, time.monotonic(), 600)
        self.assertTrue(held())

    def test_with_no_limit_the_stop_button_is_handed_through_as_it_was(self) -> None:
        stopping = object()
        self.assertIs(pipelines._also_stopping_after(stopping, 0.0, 0), stopping)


if __name__ == "__main__":
    unittest.main()
