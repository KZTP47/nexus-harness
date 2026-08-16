"""Pointing at part of a page and getting a usable name for it."""

from __future__ import annotations

import json
import unittest

from our_harness import selectors
from our_harness.models import HarnessError


def one(selector: str, kind: str, matches: int = 1, **extra) -> selectors.Candidate:
    return selectors.Candidate(selector=selector, kind=kind, matches=matches, **extra)


class RankTests(unittest.TestCase):
    def test_a_test_attribute_beats_everything_else(self) -> None:
        found = selectors.rank([
            one("div.card", "class"),
            one("body > div > button", "path"),
            one("[data-testid=\"save\"]", "test-hook"),
            one("#save", "id"),
        ])
        self.assertEqual(
            [item.selector for item in found],
            ["[data-testid=\"save\"]", "#save", "div.card", "body > div > button"],
        )

    def test_a_name_that_matches_more_than_one_thing_is_never_offered(self) -> None:
        found = selectors.rank([
            one("button.primary", "class", matches=7),
            one("#save", "id", matches=1),
        ])
        self.assertEqual([item.selector for item in found], ["#save"])

    def test_a_name_that_matches_nothing_is_never_offered(self) -> None:
        self.assertEqual(selectors.rank([one("#gone", "id", matches=0)]), ())

    def test_a_name_the_browser_refused_is_never_offered(self) -> None:
        self.assertEqual(selectors.rank([one("#not a name", "id", matches=-1)]), ())

    def test_the_same_name_is_only_offered_once(self) -> None:
        found = selectors.rank([one("#save", "id"), one("#save", "id")])
        self.assertEqual(len(found), 1)

    def test_the_shorter_of_two_equally_good_names_comes_first(self) -> None:
        found = selectors.rank([one("button.a.b.c.d", "class"), one("button.a", "class")])
        self.assertEqual(found[0].selector, "button.a")

    def test_the_order_holds_for_every_kind(self) -> None:
        found = selectors.rank([one(f"x{index}", kind) for index, kind in enumerate(selectors.ORDER)])
        self.assertEqual([item.kind for item in found], list(selectors.ORDER))

    def test_the_thrown_away_names_are_kept_for_explaining(self) -> None:
        thrown = selectors.rejected([
            one("#save", "id", matches=1),
            one(".row", "class", matches=12),
            one(".gone", "class", matches=0),
        ])
        self.assertEqual([item.selector for item in thrown], [".row", ".gone"])

    def test_every_kind_has_words_a_person_can_read(self) -> None:
        for kind in selectors.ORDER:
            with self.subTest(kind=kind):
                self.assertTrue(selectors.KIND_REASON[kind].strip())
                self.assertEqual(one("x", kind).reason, selectors.KIND_REASON[kind])


class MadeUpNameTests(unittest.TestCase):
    def test_names_a_build_tool_invented_are_flagged(self) -> None:
        for name in (":r3:", "mui-12345", "app-9f8e7d6c5b4a", "field-1234567", "ember1042"):
            with self.subTest(name=name):
                self.assertTrue(selectors.made_up_name(name))

    def test_names_a_person_wrote_are_left_alone(self) -> None:
        for name in ("save-button", "login_form", "checkoutStep2", "nav", "item-3"):
            with self.subTest(name=name):
                self.assertEqual(selectors.made_up_name(name), "")

    def test_nothing_is_said_about_an_empty_name(self) -> None:
        self.assertEqual(selectors.made_up_name(""), "")


class ReadingWhatThePageSentTests(unittest.TestCase):
    def test_a_normal_answer_is_read(self) -> None:
        found = selectors.parse_candidates([
            {"selector": "#save", "kind": "id", "matches": 1, "detail": "save"},
        ])
        self.assertEqual(found[0].selector, "#save")
        self.assertEqual(found[0].detail, "save")

    def test_rubbish_from_the_page_is_refused(self) -> None:
        for value in (
            "not a list",
            [{"selector": "#a", "kind": "made-up", "matches": 1}],
            [{"selector": "#a", "kind": "id"}],
            [{"selector": "#a", "kind": "id", "matches": True}],
            [{"selector": "#a", "kind": "id", "matches": -2}],
            [{"selector": 5, "kind": "id", "matches": 1}],
            ["#a"],
            [{"selector": "#" + "a" * 900, "kind": "id", "matches": 1}],
            [{"selector": "#a", "kind": "id", "matches": 1}] * 500,
        ):
            with self.subTest(value=str(value)[:40]), self.assertRaises(HarnessError):
                selectors.parse_candidates(value)

    def test_an_empty_name_is_dropped_rather_than_refused(self) -> None:
        self.assertEqual(selectors.parse_candidates([{"selector": "  ", "kind": "id", "matches": 1}]), ())

    def test_a_count_below_zero_is_refused(self) -> None:
        # The page drops names the browser could not understand before sending,
        # so a count below zero means something is wrong with the answer itself.
        with self.assertRaises(HarnessError):
            selectors.parse_candidates([{"selector": "#a b", "kind": "id", "matches": -1}])


class StarterStepTests(unittest.TestCase):
    def test_a_pick_turns_into_a_step_that_can_be_pasted(self) -> None:
        candidate = one("[data-testid=\"save\"]", "test-hook", detail="Save")
        self.assertEqual(
            selectors.starter_step(candidate),
            {"do": "expect_visible", "target": "[data-testid=\"save\"]"},
        )
        self.assertEqual(selectors.starter_step(candidate, "type")["text"], "something")
        self.assertEqual(selectors.starter_step(candidate, "press")["key"], "Enter")
        self.assertEqual(selectors.starter_step(candidate, "expect_text")["text"], "Save")

    def test_every_step_it_makes_is_one_the_suite_understands(self) -> None:
        from our_harness import qa

        candidate = one("#save", "id", detail="Save")
        for action in qa.STEP_ACTIONS:
            if action == "wait":
                continue
            with self.subTest(action=action):
                step = selectors.starter_step(candidate, action)
                suite = qa.parse_suite({
                    "name": "d",
                    "cases": [{
                        "id": "c", "kind": "browser", "url": "http://127.0.0.1:1/",
                        "steps": [step],
                    }],
                })
                self.assertEqual(suite.cases[0].steps[0]["do"], action)

    def test_a_line_about_a_name_says_what_it_is(self) -> None:
        candidate = one("#save", "id", detail="save", warning="It looks made up")
        line = selectors.describe(candidate)
        self.assertIn("#save", line)
        self.assertIn("The thing's own id", line)
        self.assertIn("Careful: It looks made up", line)


class PageScriptTests(unittest.TestCase):
    def test_the_script_carries_the_plan_and_the_test_attributes(self) -> None:
        script = selectors.picker_script({"url": "http://127.0.0.1:1/", "viewport": {"width": 800, "height": 600}})
        self.assertIn("http://127.0.0.1:1/", script)
        self.assertIn("data-testid", script)
        self.assertNotIn("__PLAN__", script)
        self.assertNotIn("__TEST_ATTRIBUTES__", script)
        self.assertIn("<<<QA_REPORT>>>", script)

    def test_the_plan_is_written_as_data_not_as_code(self) -> None:
        nasty = "http://127.0.0.1:1/?x=</script><script>alert(1)</script>"
        script = selectors.picker_script({"url": nasty, "viewport": {"width": 1, "height": 1}})
        self.assertIn(json.dumps(nasty), script)

    def test_the_words_on_the_page_are_never_put_back_as_page_code(self) -> None:
        script = selectors.picker_script({"url": "http://127.0.0.1:1/"})
        # The old tool built its list with innerHTML, which let words on the page
        # run as code. Nothing here may use innerHTML at all.
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent = 'Click the thing", script)

    def test_the_script_counts_matches_before_offering_a_name(self) -> None:
        script = selectors.picker_script({"url": "http://127.0.0.1:1/"})
        self.assertIn("document.querySelectorAll(selector).length", script)
        self.assertIn("CSS.escape", script)


# Exactly what a real Chromium browser sent back when this picker was run on a
# page with two identical Buy buttons. Kept as it came, so the reading and
# ordering are tested against a real answer and not an invented one.
REAL_ANSWER = json.dumps({
    "gaveUp": False,
    "tag": "button",
    "text": "Buy",
    "names": [
        {"selector": "[data-testid=\"buy-now\"]", "kind": "test-hook", "detail": "data-testid", "matches": 1},
        {"selector": "#buy_1", "kind": "id", "detail": "buy_1", "matches": 1},
        {"selector": "[aria-label=\"Buy this now\"]", "kind": "role", "detail": "Buy this now", "matches": 1},
        {"selector": "button:text-is(\"Buy\")", "kind": "text", "detail": "Buy", "matches": 2},
        {"selector": "button.primary", "kind": "class", "detail": "primary", "matches": 2},
        {"selector": "button.big", "kind": "class", "detail": "big", "matches": 2},
        {"selector": "button.primary.big", "kind": "class", "detail": "primary big", "matches": 2},
        {"selector": "#buy_1", "kind": "path", "detail": "", "matches": 1},
    ],
})


class ReadingARealAnswerTests(unittest.TestCase):
    def test_a_real_browser_answer_gives_the_right_names_in_the_right_order(self) -> None:
        picked = selectors.read_report("noise before <<<QA_REPORT>>>" + REAL_ANSWER)
        self.assertFalse(picked.gave_up)
        self.assertEqual((picked.tag, picked.text), ("button", "Buy"))
        self.assertEqual(
            [item.selector for item in picked.offered],
            ["[data-testid=\"buy-now\"]", "#buy_1", "[aria-label=\"Buy this now\"]"],
        )
        # The page holds two identical buttons, so every name that cannot tell
        # them apart is thrown away instead of being offered.
        self.assertEqual(
            [item.selector for item in picked.thrown_away],
            ["button:text-is(\"Buy\")", "button.primary", "button.big", "button.primary.big"],
        )

    def test_giving_up_is_reported_rather_than_guessed_at(self) -> None:
        picked = selectors.read_report('<<<QA_REPORT>>>{"gaveUp": true, "names": []}')
        self.assertTrue(picked.gave_up)
        self.assertEqual(picked.offered, ())

    def test_an_id_the_page_made_up_carries_a_warning(self) -> None:
        answer = json.dumps({"names": [{"selector": "#mui-4821", "kind": "id", "detail": "mui-4821", "matches": 1}]})
        picked = selectors.read_report("<<<QA_REPORT>>>" + answer)
        self.assertIn("fresh every time", picked.offered[0].warning)

    def test_a_broken_answer_is_refused_with_a_sentence(self) -> None:
        for text in (
            "nothing at all",
            "<<<QA_REPORT>>>not json",
            "<<<QA_REPORT>>>[1, 2]",
            '<<<QA_REPORT>>>{"fatal": "the page never loaded"}',
        ):
            with self.subTest(text=text[:30]), self.assertRaises(HarnessError):
                selectors.read_report(text)


class PickTests(unittest.TestCase):
    def setUp(self) -> None:
        import copy
        import tempfile
        from pathlib import Path

        from our_harness.config import DEFAULT_CONFIG, LoadedConfig

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def runner(self, stdout: str, stderr: str = ""):
        class Fake:
            def __init__(self) -> None:
                self.argv: list[str] = []

            def run(self, argv, cwd=".", timeout=None):
                self.argv = list(argv)

                class Result:
                    pass

                answer = Result()
                answer.stdout = stdout
                answer.stderr = stderr
                answer.passed = True
                return answer

        return Fake()

    def test_a_pick_runs_the_script_and_reads_the_answer(self) -> None:
        fake = self.runner("<<<QA_REPORT>>>" + REAL_ANSWER)
        picked = selectors.pick(self.config, "http://127.0.0.1:8765/", commands=fake)
        self.assertEqual(picked.offered[0].kind, "test-hook")
        self.assertEqual(fake.argv[0], "node")
        self.assertTrue(fake.argv[1].endswith("picker.js"))

    def test_the_script_is_cleaned_up_afterwards(self) -> None:
        selectors.pick(self.config, "http://127.0.0.1:8765/", commands=self.runner("<<<QA_REPORT>>>" + REAL_ANSWER))
        leftovers = list((self.root / ".harness" / "qa" / "tmp").glob("*"))
        self.assertEqual(leftovers, [])

    def test_a_browser_that_never_starts_says_what_to_install(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            selectors.pick(
                self.config, "http://127.0.0.1:8765/",
                commands=self.runner("", "Cannot find module 'playwright'"),
            )
        self.assertIn("npm install playwright", str(caught.exception))

    def test_a_page_outside_the_allowed_hosts_is_refused(self) -> None:
        for url in ("http://example.com/", "ftp://127.0.0.1/", "http://user:pass@127.0.0.1/"):
            with self.subTest(url=url), self.assertRaises(HarnessError):
                selectors.check_url(self.config, url)

    def test_the_allowed_hosts_are_the_ones_the_checks_use(self) -> None:
        self.assertEqual(
            selectors.check_url(self.config, "http://127.0.0.1:8765/x"), "http://127.0.0.1:8765/x"
        )


if __name__ == "__main__":
    unittest.main()
