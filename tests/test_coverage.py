"""Which pages nobody checks."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import coverage
from our_harness import qa as qalab
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def suite(cases: list[dict]) -> qalab.QaSuite:
    return qalab.parse_suite({"schema_version": 1, "name": "d", "cases": cases})


def page(address: str, status: int = 200) -> dict:
    return {"url": address, "status": status}


def walk(pages: list[dict], more: int = 0) -> dict:
    return {"pages": pages, "morePages": more, "refused": [], "accessibility": []}


class TidyTests(unittest.TestCase):
    def test_the_same_page_written_two_ways_is_one_page(self) -> None:
        same = [
            "http://127.0.0.1:8000/shop/",
            "http://127.0.0.1:8000/shop",
            "HTTP://127.0.0.1:8000/shop",
            "http://127.0.0.1:8000/shop#buy",
            "  http://127.0.0.1:8000/shop//  ",
        ]
        tidied = {coverage.tidy(item) for item in same}
        self.assertEqual(len(tidied), 1, tidied)

    def test_a_default_port_means_the_same_place_as_no_port(self) -> None:
        self.assertEqual(
            coverage.tidy("http://example.test:80/a"), coverage.tidy("http://example.test/a")
        )
        self.assertEqual(
            coverage.tidy("https://example.test:443/a"), coverage.tidy("https://example.test/a")
        )

    def test_the_home_page_keeps_its_slash(self) -> None:
        self.assertEqual(coverage.tidy("http://a.test/"), "http://a.test/")
        self.assertEqual(coverage.tidy("http://a.test"), "http://a.test/")

    def test_a_question_mark_part_is_kept_because_it_is_a_different_page(self) -> None:
        self.assertNotEqual(coverage.tidy("http://a.test/s?q=1"), coverage.tidy("http://a.test/s"))

    def test_nothing_in_gives_nothing_out(self) -> None:
        for value in ("", "   ", None):
            self.assertEqual(coverage.tidy(value), "")


class NameTests(unittest.TestCase):
    def test_a_name_is_made_from_the_end_of_the_address(self) -> None:
        self.assertEqual(coverage.name_for("http://a.test/shop/checkout"), "shop-checkout-opens")

    def test_the_home_page_gets_a_name_too(self) -> None:
        self.assertEqual(coverage.name_for("http://a.test/"), "home-page-opens")

    def test_odd_letters_are_dropped_rather_than_written_into_the_suite(self) -> None:
        name = coverage.name_for("http://a.test/a b/%c!d")
        self.assertTrue(all(letter.isalnum() or letter == "-" for letter in name), name)
        self.assertNotIn("--", name)

    def test_a_very_long_address_gives_a_short_name(self) -> None:
        self.assertLessEqual(len(coverage.name_for("http://a.test/" + "x" * 300)), 46)

    def test_a_name_a_suite_will_take(self) -> None:
        from our_harness import starters

        built = starters.build(
            "page-opens", url="http://127.0.0.1:8000/shop/", case_id=coverage.name_for(
                "http://127.0.0.1:8000/shop/"
            )
        )
        suite([built])


class MeasureTests(unittest.TestCase):
    def test_a_page_with_a_check_of_its_own_is_checked(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/"), page("http://127.0.0.1:8000/shop")]),
            suite([
                {
                    "id": "home", "title": "Home", "kind": "browser",
                    "url": "http://127.0.0.1:8000/",
                }
            ]),
            start="http://127.0.0.1:8000/",
        )
        self.assertEqual([item.address for item in found.checked], ["http://127.0.0.1:8000/"])
        self.assertEqual([item.address for item in found.missing], ["http://127.0.0.1:8000/shop"])
        self.assertEqual(found.percent, 50)

    def test_a_page_only_a_walk_reaches_is_said_to_be_only_walked_over(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/shop")]),
            suite([
                {
                    "id": "walk", "title": "A walk", "kind": "crawl",
                    "url": "http://127.0.0.1:8000/",
                }
            ]),
        )
        self.assertEqual([item.address for item in found.walked_only], ["http://127.0.0.1:8000/shop"])
        self.assertEqual(found.missing, [])
        self.assertEqual(found.pages[0].state, "only walked over")

    def test_a_walk_of_another_folder_does_not_count(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/help/one")]),
            suite([
                {
                    "id": "walk", "title": "A walk", "kind": "crawl",
                    "url": "http://127.0.0.1:8000/shop/",
                }
            ]),
        )
        self.assertEqual([item.address for item in found.missing], ["http://127.0.0.1:8000/help/one"])

    def test_a_folder_name_does_not_swallow_a_page_that_starts_the_same(self) -> None:
        # /shopping is not inside /shop, however similar the two look.
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/shopping/cart")]),
            suite([
                {
                    "id": "walk", "title": "A walk", "kind": "crawl",
                    "url": "http://127.0.0.1:8000/shop/",
                    "stay_under": "http://127.0.0.1:8000/shop",
                }
            ]),
        )
        self.assertEqual(len(found.missing), 1)

    def test_the_extra_addresses_a_browser_check_visits_count_too(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/"), page("http://127.0.0.1:8000/about")]),
            suite([
                {
                    "id": "tour", "title": "A tour", "kind": "browser",
                    "url": "http://127.0.0.1:8000/", "routes": ["/about"],
                }
            ]),
        )
        self.assertEqual(found.missing, [])
        self.assertEqual(found.percent, 100)

    def test_a_command_check_holding_an_address_is_not_looking_at_that_page(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/shop")]),
            suite([
                {
                    "id": "curl", "title": "A command", "kind": "command",
                    "command": ["node", "--version"],
                }
            ]),
        )
        self.assertEqual(len(found.missing), 1)

    def test_the_same_page_twice_in_a_walk_is_counted_once(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/shop"), page("http://127.0.0.1:8000/shop/")]),
            suite([]),
        )
        self.assertEqual(len(found.pages), 1)

    def test_a_walk_that_found_nothing_says_nothing_rather_than_a_hundred_percent(self) -> None:
        found = coverage.measure(walk([]), suite([]))
        self.assertEqual(found.percent, 0)
        self.assertIn("Walked 0 pages", found.lines()[0])

    def test_a_report_that_is_not_a_walk_is_refused(self) -> None:
        for value in ({}, {"pages": "one"}, {"pages": 5}, "pages"):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                coverage.measure(value, suite([]))

    def test_a_page_entry_that_is_not_an_object_is_skipped(self) -> None:
        found = coverage.measure(
            {"pages": ["http://a.test/", page("http://127.0.0.1:8000/x")]}, suite([])
        )
        self.assertEqual(len(found.pages), 1)

    def test_a_walk_that_stopped_early_says_so(self) -> None:
        found = coverage.measure(
            {"pages": [page("http://127.0.0.1:8000/")], "fatal": "the browser closed"}, suite([])
        )
        self.assertIn("stopped early", found.note)
        self.assertIn("stopped early", " ".join(found.lines()))

    def test_pages_left_waiting_are_reported_with_what_to_do(self) -> None:
        found = coverage.measure(walk([page("http://127.0.0.1:8000/")], more=12), suite([]))
        self.assertEqual(found.more_pages, 12)
        self.assertIn("max-pages", " ".join(found.lines()))


class SuggestionTests(unittest.TestCase):
    def test_every_page_nobody_checks_comes_with_a_check_ready_to_add(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/shop"), page("http://127.0.0.1:8000/help")]),
            suite([]),
        )
        made = found.suggestions()
        self.assertEqual(len(made), 2)
        for item in made:
            with self.subTest(address=item["address"]):
                suite([item["case"]])
                self.assertEqual(item["case"]["url"], item["address"])

    def test_the_answer_the_panel_reads_holds_the_same_lists(self) -> None:
        found = coverage.measure(
            walk([page("http://127.0.0.1:8000/shop")]),
            suite([]),
        )
        shape = found.to_dict()
        self.assertEqual(shape["missing"], ["http://127.0.0.1:8000/shop"])
        self.assertEqual(shape["percent"], 0)
        self.assertEqual(shape["suggestions"][0]["starter"], "page-opens")
        json.dumps(shape)


class AddingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def written(self) -> dict:
        return json.loads(
            (self.root / ".harness" / "qa" / "suite.json").read_text(encoding="utf-8")
        )

    def test_a_check_is_written_for_each_page(self) -> None:
        added = coverage.add_missing(
            self.config, ["http://127.0.0.1:8000/shop", "http://127.0.0.1:8000/help"]
        )
        self.assertEqual(added, ["shop-opens", "help-opens"])
        self.assertEqual(len(self.written()["cases"]), 2)

    def test_the_same_page_twice_does_not_write_two_checks(self) -> None:
        coverage.add_missing(self.config, ["http://127.0.0.1:8000/shop"])
        with self.assertRaises(HarnessError):
            coverage.add_missing(self.config, ["http://127.0.0.1:8000/shop"])
        self.assertEqual(len(self.written()["cases"]), 1)

    def test_the_same_page_spelled_differently_is_still_the_same_page(self) -> None:
        coverage.add_missing(self.config, ["http://127.0.0.1:8000/shop"])
        with self.assertRaises(HarnessError):
            coverage.add_missing(self.config, ["http://127.0.0.1:8000/shop/"])
        self.assertEqual(len(self.written()["cases"]), 1)

    def test_two_pages_that_want_the_same_short_name_both_get_a_check(self) -> None:
        # /a/b and /x/a/b both read as "a-b". Neither may be quietly dropped.
        added = coverage.add_missing(
            self.config, ["http://127.0.0.1:8000/a/b", "http://127.0.0.1:8000/x/a/b"]
        )
        self.assertEqual(added, ["a-b-opens", "a-b-opens-2"])
        written = self.written()["cases"]
        self.assertEqual(len(written), 2)
        self.assertEqual(
            [case["url"] for case in written],
            ["http://127.0.0.1:8000/a/b", "http://127.0.0.1:8000/x/a/b"],
        )

    def test_a_name_a_check_already_uses_does_not_stop_the_page_being_added(self) -> None:
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({
                "schema_version": 1, "name": "mine",
                "cases": [{"id": "shop-opens", "title": "Something else", "kind": "command",
                           "command": ["node", "--version"]}],
            }),
            encoding="utf-8",
        )
        added = coverage.add_missing(self.config, ["http://127.0.0.1:8000/shop"])
        self.assertEqual(added, ["shop-opens-2"])

    def test_checks_that_were_there_before_are_kept(self) -> None:
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({
                "schema_version": 1, "name": "mine",
                "cases": [{"id": "old", "title": "Old", "kind": "command",
                           "command": ["node", "--version"]}],
            }),
            encoding="utf-8",
        )
        coverage.add_missing(self.config, ["http://127.0.0.1:8000/shop"])
        written = self.written()
        self.assertEqual(written["name"], "mine")
        self.assertEqual([case["id"] for case in written["cases"]], ["old", "shop-opens"])

    def test_no_pages_given_says_so(self) -> None:
        for value in ([], [""], ["  "]):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                coverage.add_missing(self.config, value)


class WalkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def test_no_address_says_what_to_type(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            coverage.walk_site(self.config, "")
        self.assertIn("--url", str(caught.exception))

    def test_a_silly_number_of_pages_is_refused(self) -> None:
        for limit in (0, -3, 5000):
            with self.subTest(limit=limit), self.assertRaises(HarnessError):
                coverage.walk_site(self.config, "http://127.0.0.1:8000/", max_pages=limit)

    def test_an_address_this_project_may_not_open_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            coverage.walk_site(self.config, "http://example.com/")

    def test_the_walk_asks_the_runner_and_reads_what_it_says(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.asked = None

            def walk_over(self, case):
                self.asked = case
                return ((), "one page", json.dumps(walk([page("http://127.0.0.1:8000/")])))

        engine = FakeRunner()
        report = coverage.walk_site(
            self.config, "http://127.0.0.1:8000/", max_pages=7, runner=engine
        )
        self.assertEqual(engine.asked.kind, "crawl")
        self.assertEqual(engine.asked.max_pages, 7)
        self.assertEqual(len(report["pages"]), 1)

    def test_a_walk_that_answers_with_rubbish_is_refused_with_a_sentence(self) -> None:
        class BrokenRunner:
            def walk_over(self, case):
                return ((), "", "not json at all")

        with self.assertRaises(HarnessError):
            coverage.walk_site(self.config, "http://127.0.0.1:8000/", runner=BrokenRunner())


if __name__ == "__main__":
    unittest.main()


class PanelTests(unittest.TestCase):
    """The panel's own way in to the same answers."""

    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), LoadedConfig(data, self.root, [], {}))
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        connection.request(
            "POST", path, json.dumps(body),
            {
                "Host": f"127.0.0.1:{self.server.server_port}",
                "Content-Type": "application/json",
                "X-Harness-Token": self.server.token,
            },
        )
        answer = connection.getresponse()
        found = json.loads(answer.read() or b"{}")
        connection.close()
        return answer.status, found

    def test_checks_are_written_for_the_pages_the_panel_sends(self) -> None:
        status, body = self.post(
            "/api/qa/coverage/add", {"addresses": ["http://127.0.0.1:8765/shop"]}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["added"], ["shop-opens"])
        written = json.loads(
            (self.root / ".harness" / "qa" / "suite.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(written["cases"]), 1)

    def test_an_address_this_project_may_not_open_is_refused(self) -> None:
        status, body = self.post("/api/qa/coverage/add", {"addresses": ["http://example.com/"]})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertFalse((self.root / ".harness" / "qa" / "suite.json").exists())

    def test_addresses_have_to_be_a_list(self) -> None:
        status, body = self.post("/api/qa/coverage/add", {"addresses": "http://127.0.0.1:8765/"})
        self.assertEqual(status, 400)
        self.assertIn("list", body["error"])

    def test_a_silly_number_of_pages_to_walk_is_refused_before_a_browser_opens(self) -> None:
        status, body = self.post(
            "/api/qa/coverage", {"url": "http://127.0.0.1:8765/", "max_pages": 9000}
        )
        self.assertEqual(status, 400)
        self.assertIn("500", body["error"])

    def test_a_walk_of_an_address_this_project_may_not_open_is_refused(self) -> None:
        status, body = self.post("/api/qa/coverage", {"url": "http://example.com/"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)


class NoSuiteYetTests(unittest.TestCase):
    """The first thing someone runs, in a project with nothing in it yet."""

    def test_a_project_with_no_checks_reads_as_every_page_unchecked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
            empty = coverage.read_suite(config)
            self.assertEqual(list(empty.cases), [])
            found = coverage.measure(walk([page("http://127.0.0.1:8000/")]), empty)
            self.assertEqual(len(found.missing), 1)

    def test_a_suite_that_is_there_is_still_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".harness" / "qa").mkdir(parents=True)
            (root / ".harness" / "qa" / "suite.json").write_text(
                json.dumps({
                    "schema_version": 1, "name": "mine",
                    "cases": [{"id": "home", "title": "Home", "kind": "browser",
                               "url": "http://127.0.0.1:8000/"}],
                }),
                encoding="utf-8",
            )
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
            self.assertEqual([case.id for case in coverage.read_suite(config).cases], ["home"])


class PrintedLinesTests(unittest.TestCase):
    def test_the_way_to_add_them_is_offered_when_they_are_not_written_yet(self) -> None:
        found = coverage.measure(walk([page("http://127.0.0.1:8000/shop")]), suite([]))
        self.assertIn("--write-missing", " ".join(found.lines()))

    def test_it_does_not_tell_you_to_add_checks_that_were_just_added(self) -> None:
        found = coverage.measure(walk([page("http://127.0.0.1:8000/shop")]), suite([]))
        self.assertNotIn("--write-missing", " ".join(found.lines(offer_help=False)))
        self.assertIn("Nobody looks at these:", found.lines(offer_help=False))


class SecondJudgeFindingTests(unittest.TestCase):
    """A page a check already visits on the way must not get a second check."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({
                "schema_version": 1, "name": "mine",
                "cases": [{
                    "id": "home-check", "title": "Home", "kind": "browser",
                    "url": "http://127.0.0.1:8791/", "routes": ["/", "/about"],
                }],
            }),
            encoding="utf-8",
        )

    def written(self) -> dict:
        return json.loads(
            (self.root / ".harness" / "qa" / "suite.json").read_text(encoding="utf-8")
        )

    def test_the_walk_and_the_writing_agree_on_what_is_already_checked(self) -> None:
        suite = coverage.read_suite(self.config)
        found = coverage.measure(walk([page("http://127.0.0.1:8791/about")]), suite)
        self.assertEqual([item.address for item in found.checked],
                         ["http://127.0.0.1:8791/about"])
        # The walk says this page is checked, so writing must not add another.
        with self.assertRaises(HarnessError):
            coverage.add_missing(self.config, ["http://127.0.0.1:8791/about"])
        self.assertEqual(len(self.written()["cases"]), 1)

    def test_a_page_that_really_is_not_checked_is_still_added(self) -> None:
        added = coverage.add_missing(self.config, ["http://127.0.0.1:8791/help"])
        self.assertEqual(added, ["help-opens"])
        self.assertEqual(len(self.written()["cases"]), 2)

    def test_a_page_a_walk_check_only_passes_over_still_gets_its_own_check(self) -> None:
        # Being walked over is not the same as being checked, so this one is
        # written. Otherwise the middle group could never be improved.
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({
                "schema_version": 1, "name": "mine",
                "cases": [{"id": "walk", "title": "A walk", "kind": "crawl",
                           "url": "http://127.0.0.1:8791/"}],
            }),
            encoding="utf-8",
        )
        added = coverage.add_missing(self.config, ["http://127.0.0.1:8791/shop"])
        self.assertEqual(added, ["shop-opens"])
