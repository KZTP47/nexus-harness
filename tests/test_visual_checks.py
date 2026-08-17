"""Screenshot checks: reading PNG files, comparing them, and failing usefully."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from our_harness import images, qa
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def solid(width: int, height: int, color: tuple[int, int, int, int]) -> images.Image:
    return images.Image(width, height, bytes(color) * (width * height))


def build_png(
    width: int,
    height: int,
    depth: int,
    color: int,
    rows: list[bytes],
    palette: bytes = b"",
    see_through: bytes = b"",
    interlace: int = 0,
    filters: list[int] | None = None,
) -> bytes:
    """Hand-build a PNG so the reader can be tried on forms our writer never makes."""

    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((depth, color, 0, 0, interlace))
    )
    body = bytearray()
    for number, row in enumerate(rows):
        body.append((filters or [0] * len(rows))[number])
        body += row
    parts = [images._chunk(b"IHDR", header)]
    if palette:
        parts.append(images._chunk(b"PLTE", palette))
    if see_through:
        parts.append(images._chunk(b"tRNS", see_through))
    parts.append(images._chunk(b"IDAT", zlib.compress(bytes(body))))
    parts.append(images._chunk(b"IEND", b""))
    return images.SIGNATURE + b"".join(parts)


class PngReadingTests(unittest.TestCase):
    def test_a_written_picture_reads_back_the_same(self) -> None:
        first = images.Image(3, 2, bytes(range(24)))
        again = images.read_png(images.write_png(first))
        self.assertEqual((again.width, again.height), (3, 2))
        self.assertEqual(again.pixels, first.pixels)

    def test_plain_gray_and_gray_with_alpha_are_understood(self) -> None:
        gray = images.read_png(build_png(2, 1, 8, 0, [bytes((10, 200))]))
        self.assertEqual(gray.at(0, 0), (10, 10, 10, 255))
        self.assertEqual(gray.at(1, 0), (200, 200, 200, 255))
        faded = images.read_png(build_png(2, 1, 8, 4, [bytes((10, 0, 200, 128))]))
        self.assertEqual(faded.at(0, 0), (10, 10, 10, 0))
        self.assertEqual(faded.at(1, 0), (200, 200, 200, 128))

    def test_a_color_list_picture_is_understood(self) -> None:
        # Two colors, four pixels packed two bits each: 0, 1, 1, 0.
        data = build_png(
            4, 1, 2, 3, [bytes((0b00_01_01_00,))],
            palette=bytes((255, 0, 0, 0, 0, 255)),
            see_through=bytes((255, 64)),
        )
        picture = images.read_png(data)
        self.assertEqual(picture.at(0, 0), (255, 0, 0, 255))
        self.assertEqual(picture.at(1, 0), (0, 0, 255, 64))
        self.assertEqual(picture.at(3, 0), (255, 0, 0, 255))

    def test_sixteen_bit_colors_are_understood(self) -> None:
        row = bytes((1, 2, 3, 4, 5, 6))  # one pixel, two bytes per color
        picture = images.read_png(build_png(1, 1, 16, 2, [row]))
        self.assertEqual(picture.at(0, 0), (1, 3, 5, 255))

    def test_every_row_filter_is_undone(self) -> None:
        # Two pixels wide, five rows, one row per way of writing a row down.
        rows = [
            bytes((10, 20, 30, 40, 50, 60, 70, 80)),      # kept as it is
            bytes((5, 5, 5, 5, 1, 1, 1, 1)),              # from the pixel to the left
            bytes((1, 1, 1, 1, 2, 2, 2, 2)),              # from the row above
            bytes(8),                                     # from the average of both
            bytes(8),                                     # from whichever is nearest
        ]
        picture = images.read_png(build_png(2, 5, 8, 6, rows, filters=[0, 1, 2, 3, 4]))
        self.assertEqual(picture.at(0, 0), (10, 20, 30, 40))
        self.assertEqual(picture.at(1, 0), (50, 60, 70, 80))
        self.assertEqual((picture.at(0, 1), picture.at(1, 1)), ((5,) * 4, (6,) * 4))
        self.assertEqual((picture.at(0, 2), picture.at(1, 2)), ((6,) * 4, (8,) * 4))
        self.assertEqual((picture.at(0, 3), picture.at(1, 3)), ((3,) * 4, (5,) * 4))
        self.assertEqual((picture.at(0, 4), picture.at(1, 4)), ((3,) * 4, (5,) * 4))

    def test_broken_files_are_refused_with_a_sentence(self) -> None:
        good = images.write_png(solid(2, 2, (1, 2, 3, 4)))
        cases = {
            "not a PNG": b"hello there, this is not a picture",
            "stops in the middle": good[:-6],
            "interlaced": build_png(1, 1, 8, 6, [bytes((0, 0, 0, 0))], interlace=1),
            "cannot be right": build_png(0, 1, 8, 6, [b""]),
            "colour form": build_png(1, 1, 8, 5, [bytes((0, 0, 0, 0))]).replace(b"", b""),
        }
        for words, data in cases.items():
            with self.subTest(words=words), self.assertRaises(images.ImageError):
                images.read_png(data)

    def test_the_interlaced_message_says_what_to_do(self) -> None:
        data = build_png(1, 1, 8, 6, [bytes((0, 0, 0, 0))], interlace=1)
        with self.assertRaises(images.ImageError) as caught:
            images.read_png(data)
        self.assertIn("Save it again as a plain PNG", str(caught.exception))

    def test_a_damaged_file_is_refused_rather_than_trusted(self) -> None:
        good = images.write_png(solid(2, 2, (1, 2, 3, 4)))
        # Every part of a PNG carries a check number. Break one and the file
        # must be refused, because a damaged baseline would fail every run
        # afterwards for a reason nobody could work out.
        damaged = bytearray(good)
        damaged[29:33] = b"\xff\xff\xff\xff"
        with self.assertRaises(images.ImageError) as caught:
            images.read_png(bytes(damaged))
        self.assertIn("damaged", str(caught.exception))

    def test_damage_anywhere_in_the_file_is_found(self) -> None:
        good = images.write_png(solid(4, 4, (9, 8, 7, 255)))
        for at in range(len(images.SIGNATURE), len(good)):
            damaged = bytearray(good)
            damaged[at] ^= 0xFF
            with self.subTest(at=at), self.assertRaises(images.ImageError):
                images.read_png(bytes(damaged))

    def test_a_picture_that_claims_to_be_enormous_is_refused(self) -> None:
        header = (30000).to_bytes(4, "big") + (30000).to_bytes(4, "big") + bytes((8, 6, 0, 0, 0))
        data = images.SIGNATURE + images._chunk(b"IHDR", header) + images._chunk(b"IEND", b"")
        with self.assertRaises(images.ImageError):
            images.read_png(data)


class CompareTests(unittest.TestCase):
    def test_the_same_picture_twice_has_nothing_changed(self) -> None:
        picture = solid(20, 10, (12, 34, 56, 255))
        difference = images.compare(picture, picture)
        self.assertEqual(difference.changed, 0)
        self.assertEqual(difference.percent, 0.0)
        self.assertTrue(difference.same_size)

    def test_a_change_only_in_how_see_through_a_pixel_is_still_counts(self) -> None:
        before = solid(4, 4, (255, 255, 255, 255))
        after = images.Image(4, 4, bytes((255, 255, 255, 0)) + before.pixels[4:])
        difference = images.compare(before, after)
        self.assertEqual(difference.changed, 1)

    def test_a_different_size_is_a_difference(self) -> None:
        before = solid(10, 10, (0, 0, 0, 255))
        after = solid(10, 8, (0, 0, 0, 255))
        difference = images.compare(before, after)
        self.assertFalse(difference.same_size)
        self.assertEqual(difference.changed, 20)
        self.assertEqual(difference.after_size, (10, 8))
        self.assertIn("instead of", difference.summary())

    def test_a_wider_picture_counts_the_new_columns(self) -> None:
        difference = images.compare(solid(4, 3, (1, 1, 1, 255)), solid(6, 3, (1, 1, 1, 255)))
        self.assertEqual(difference.changed, 6)
        self.assertEqual(difference.width, 6)

    def test_allowed_drift_is_a_color_amount_not_a_share(self) -> None:
        before = solid(10, 10, (100, 100, 100, 255))
        after = solid(10, 10, (104, 100, 100, 255))
        self.assertEqual(images.compare(before, after, 3).changed, 100)
        self.assertEqual(images.compare(before, after, 4).changed, 0)
        self.assertEqual(images.compare(before, after, 4).biggest_channel_gap, 4)

    def test_drift_outside_zero_to_255_is_refused(self) -> None:
        picture = solid(2, 2, (0, 0, 0, 255))
        for bad in (-1, 256, 1.5, True, "3"):
            with self.subTest(bad=bad), self.assertRaises(images.ImageError):
                images.compare(picture, picture, bad)  # type: ignore[arg-type]

    def test_the_difference_picture_marks_what_moved(self) -> None:
        before = solid(3, 1, (255, 255, 255, 255))
        after = images.Image(3, 1, bytes((0, 0, 0, 255)) + before.pixels[4:])
        difference = images.compare(before, after)
        marked = images.read_png(images.write_png(difference.picture))
        self.assertEqual(marked.at(0, 0), (255, 48, 48, 255))
        self.assertEqual(marked.at(1, 0)[3], 255)
        self.assertNotEqual(marked.at(1, 0), (255, 48, 48, 255))

    def test_area_only_one_picture_has_is_marked_differently(self) -> None:
        difference = images.compare(solid(2, 2, (9, 9, 9, 255)), solid(2, 3, (9, 9, 9, 255)))
        marked = difference.picture
        self.assertEqual(marked.at(0, 2), (255, 0, 255, 255))

    def test_a_percentage_is_worked_out_once(self) -> None:
        before = solid(10, 10, (0, 0, 0, 255))
        changed = bytearray(before.pixels)
        for index in range(5):
            changed[index * 4 : index * 4 + 4] = b"\xff\xff\xff\xff"
        difference = images.compare(before, images.Image(10, 10, bytes(changed)))
        self.assertEqual(difference.changed, 5)
        self.assertAlmostEqual(difference.percent, 5.0)


class VisualCaseReadingTests(unittest.TestCase):
    def case(self, extra: dict) -> qa.QaCase:
        body = {"id": "look", "kind": "visual", "url": "http://127.0.0.1:8765/"}
        body.update(extra)
        return qa.parse_suite({"name": "d", "cases": [body]}).cases[0]

    def test_nothing_may_change_unless_the_case_says_so(self) -> None:
        case = self.case({})
        self.assertEqual(case.expect.max_changed_percent, 0.0)
        self.assertEqual(qa.baseline_file(case), ".harness/qa/baselines/look.png")

    def test_the_case_survives_a_round_trip(self) -> None:
        first = self.case({
            "selector": "#report", "viewport": {"width": 900, "height": 600},
            "baseline": "pictures/report.png", "expect": {"max_changed_percent": 1.5},
            "steps": [{"do": "click", "target": "#open"}],
        })
        again = qa.parse_suite(
            json.loads(json.dumps({"name": "d", "cases": [first.to_dict()]}))
        ).cases[0]
        self.assertEqual(again.to_dict(), first.to_dict())
        self.assertEqual(again.baseline, "pictures/report.png")

    def test_a_share_over_a_hundred_is_refused_and_explained(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.case({"expect": {"max_changed_percent": 101}})
        self.assertIn("already a percentage", str(caught.exception))

    def test_the_allowed_amounts_must_be_numbers_in_range(self) -> None:
        for expect in (
            {"max_changed_percent": "some"},
            {"max_changed_percent": -1},
            {"allowed_color_drift": 256},
            {"max_changed_pixels": -5},
        ):
            with self.subTest(expect=expect), self.assertRaises(HarnessError):
                self.case({"expect": expect})

    def test_a_picture_is_of_the_whole_page_or_one_part_of_it(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.case({"selector": "#report", "full_page": True})
        self.assertIn("not both", str(caught.exception))

    def test_one_picture_means_one_page(self) -> None:
        with self.assertRaises(HarnessError):
            self.case({"routes": ["/", "/about"]})

    def test_the_saved_picture_must_be_a_png_inside_the_project(self) -> None:
        for baseline in ("shot.jpg", "../outside/shot.png", "C:/shot.png"):
            with self.subTest(baseline=baseline), self.assertRaises(HarnessError):
                self.case({"baseline": baseline})

    def test_browser_only_fields_are_not_offered_here(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.case({"click_all": True})
        self.assertIn("click_all", str(caught.exception))

    def test_the_script_is_told_to_take_the_picture(self) -> None:
        script = qa.browser_script({
            "url": "http://127.0.0.1:1/", "routes": ["/"], "steps": [],
            "screenshot": {"path": ".harness/qa/tmp/a/shot.png", "selector": "#one", "fullPage": False},
        })
        self.assertIn("shot.png", script)
        self.assertIn("matches", script)
        self.assertIn("animations: 'disabled'", script)


class VisualRunTests(unittest.TestCase):
    """The whole check, with a stand-in for the browser."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def suite(self, extra: dict | None = None) -> qa.QaSuite:
        body = {"id": "look", "kind": "visual", "url": "http://127.0.0.1:8765/"}
        body.update(extra or {})
        return qa.parse_suite({"name": "shots", "cases": [body]})

    def runner(self, picture: images.Image, **kwargs) -> qa.QaRunner:
        made = qa.QaRunner(self.config, **kwargs)
        made.browser_available = lambda: True  # type: ignore[method-assign]

        def pretend(case, plan, timeout, keep=None):
            shot = plan.get("screenshot")
            if shot:
                path = self.root / shot["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(images.write_png(picture))
            return {"routes": [{"route": "/", "status": 200}], "steps": [], "fatal": "",
                    "consoleErrors": [], "pageErrors": [], "requestFailures": [],
                    "accessibility": [], "text": "", "screenshot": shot["path"] if shot else ""}

        made._drive_browser = pretend  # type: ignore[method-assign]
        return made

    def baseline(self) -> Path:
        return self.root / ".harness" / "qa" / "baselines" / "look.png"

    def save_baseline(self, picture: images.Image) -> None:
        path = self.baseline()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(images.write_png(picture))

    def test_with_no_saved_picture_the_check_is_skipped_and_says_what_to_run(self) -> None:
        result = self.runner(solid(4, 4, (1, 2, 3, 255))).run(self.suite(), run_id="r1")
        self.assertEqual(result.cases[0].status, "skipped")
        self.assertIn("harness qa baseline --case look", result.cases[0].reasons[0])
        self.assertFalse(self.baseline().exists())

    def test_saving_a_baseline_writes_the_file_and_passes(self) -> None:
        picture = solid(4, 4, (1, 2, 3, 255))
        result = self.runner(picture, update_baselines=True).run(
            self.suite(), run_id="r2", write_artifacts=False
        )
        self.assertEqual(result.cases[0].status, "passed")
        self.assertTrue(self.baseline().is_file())
        self.assertEqual(images.read_png(self.baseline().read_bytes()).pixels, picture.pixels)

    def test_an_unchanged_page_passes(self) -> None:
        picture = solid(6, 5, (30, 60, 90, 255))
        self.save_baseline(picture)
        result = self.runner(picture).run(self.suite(), run_id="r3")
        self.assertEqual(result.cases[0].status, "passed")

    def test_a_changed_page_fails_and_keeps_both_pictures(self) -> None:
        self.save_baseline(solid(10, 10, (255, 255, 255, 255)))
        result = self.runner(solid(10, 10, (0, 0, 0, 255))).run(self.suite(), run_id="r4")
        case = result.cases[0]
        self.assertEqual(case.status, "failed")
        self.assertIn("100 of 100 pixels look different", case.reasons[0])
        self.assertIn("look/attempt-1-now.png", case.artifacts)
        self.assertIn("look/attempt-1-difference.png", case.artifacts)
        marked = self.root / ".harness" / "qa" / "runs" / "r4" / "look" / "attempt-1-difference.png"
        self.assertEqual(images.read_png(marked.read_bytes()).at(0, 0), (255, 48, 48, 255))

    def test_a_small_change_passes_when_the_case_allows_it(self) -> None:
        before = solid(10, 10, (255, 255, 255, 255))
        self.save_baseline(before)
        changed = bytearray(before.pixels)
        changed[0:4] = b"\x00\x00\x00\xff"
        after = images.Image(10, 10, bytes(changed))
        allowed = self.suite({"expect": {"max_changed_percent": 2}})
        self.assertEqual(self.runner(after).run(allowed, run_id="r5").cases[0].status, "passed")
        strict = self.suite({"expect": {"max_changed_percent": 0}})
        self.assertEqual(self.runner(after).run(strict, run_id="r6").cases[0].status, "failed")

    def test_a_count_of_pixels_can_be_allowed_instead(self) -> None:
        before = solid(10, 10, (255, 255, 255, 255))
        self.save_baseline(before)
        changed = bytearray(before.pixels)
        for index in range(3):
            changed[index * 4 : index * 4 + 4] = b"\x00\x00\x00\xff"
        after = images.Image(10, 10, bytes(changed))
        loose = self.suite({"expect": {"max_changed_pixels": 5}})
        self.assertEqual(self.runner(after).run(loose, run_id="r7").cases[0].status, "passed")
        tight = self.suite({"expect": {"max_changed_pixels": 2}})
        failed = self.runner(after).run(tight, run_id="r8").cases[0]
        self.assertEqual(failed.status, "failed")
        self.assertIn("more than the 2 allowed", failed.reasons[0])

    def test_a_size_change_fails_even_with_a_generous_share(self) -> None:
        self.save_baseline(solid(100, 100, (255, 255, 255, 255)))
        after = solid(100, 99, (255, 255, 255, 255))
        suite = self.suite({"expect": {"max_changed_percent": 90}})
        case = self.runner(after).run(suite, run_id="r9").cases[0]
        self.assertEqual(case.status, "failed")
        self.assertIn("A page that changed size has changed", case.reasons[0])

    def test_a_color_drift_can_be_forgiven(self) -> None:
        self.save_baseline(solid(8, 8, (100, 100, 100, 255)))
        after = solid(8, 8, (102, 100, 100, 255))
        forgiving = self.suite({"expect": {"allowed_color_drift": 3}})
        self.assertEqual(self.runner(after).run(forgiving, run_id="r10").cases[0].status, "passed")
        strict = self.suite({"expect": {"allowed_color_drift": 1}})
        self.assertEqual(self.runner(after).run(strict, run_id="r11").cases[0].status, "failed")

    def test_saving_again_replaces_the_old_picture(self) -> None:
        self.save_baseline(solid(4, 4, (255, 255, 255, 255)))
        fresh = solid(4, 4, (10, 10, 10, 255))
        result = self.runner(fresh, update_baselines=True).run(
            self.suite(), run_id="r12", write_artifacts=False
        )
        self.assertEqual(result.cases[0].status, "passed")
        self.assertEqual(images.read_png(self.baseline().read_bytes()).pixels, fresh.pixels)

    def test_each_row_of_a_table_keeps_its_own_picture(self) -> None:
        suite = self.suite({"rows": [{"page": "one"}, {"page": "two"}]})
        expanded = qa.QaRunner(self.config).expand(suite.cases[0])
        self.assertEqual(
            [qa.baseline_file(case) for case in expanded],
            [".harness/qa/baselines/look-1.png", ".harness/qa/baselines/look-2.png"],
        )

    def test_a_named_setting_can_choose_the_saved_picture(self) -> None:
        from our_harness import datasets

        datasets.save_environments(self.config, {"dark": {"THEME": "dark"}})
        suite = self.suite({"baseline": "pictures/${env.THEME}.png"})
        runner = qa.QaRunner(self.config, environment="dark")
        self.assertEqual(qa.baseline_file(runner.expand(suite.cases[0])[0]), "pictures/dark.png")

    def test_a_step_that_fails_stops_the_picture_being_judged(self) -> None:
        self.save_baseline(solid(4, 4, (0, 0, 0, 255)))
        made = self.runner(solid(4, 4, (0, 0, 0, 255)))

        def broken(case, plan, timeout, keep=None):
            return {"routes": [], "steps": [{"label": "click #go", "ok": False, "text": "not found"}],
                    "fatal": "", "consoleErrors": [], "pageErrors": [], "requestFailures": [],
                    "accessibility": [], "text": "", "screenshot": ""}

        made._drive_browser = broken  # type: ignore[method-assign]
        suite = self.suite({"steps": [{"do": "click", "target": "#go"}]})
        case = made.run(suite, run_id="r13").cases[0]
        self.assertEqual(case.status, "failed")
        self.assertIn("The picture was never taken", case.reasons[0])

    def test_a_missing_picture_file_is_reported_plainly(self) -> None:
        self.save_baseline(solid(4, 4, (0, 0, 0, 255)))
        made = qa.QaRunner(self.config)
        made.browser_available = lambda: True  # type: ignore[method-assign]
        made._drive_browser = lambda case, plan, timeout, keep=None: {  # type: ignore[method-assign]
            "routes": [], "steps": [], "fatal": "", "consoleErrors": [], "pageErrors": [],
            "requestFailures": [], "accessibility": [], "text": "", "screenshot": "",
        }
        case = made.run(self.suite({"selector": "#gone"}), run_id="r14").cases[0]
        self.assertEqual(case.status, "failed")
        self.assertIn("#gone", case.reasons[0])

    def test_the_check_is_skipped_without_a_browser_driver(self) -> None:
        made = qa.QaRunner(self.config)
        made.browser_available = lambda: False  # type: ignore[method-assign]
        result = made.run(self.suite(), run_id="r15")
        self.assertEqual(result.cases[0].status, "skipped")
        self.assertTrue(result.passed)

    def test_nothing_is_left_behind_in_the_working_folder(self) -> None:
        self.save_baseline(solid(4, 4, (5, 5, 5, 255)))
        self.runner(solid(4, 4, (5, 5, 5, 255))).run(self.suite(), run_id="r16")
        leftovers = list((self.root / ".harness" / "qa" / "tmp").glob("*"))
        self.assertEqual(leftovers, [])


class PanelBaselineTests(unittest.TestCase):
    """The Save screenshots button in the control panel."""

    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        self.config = LoadedConfig(data, self.root, [], {})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(self.temporary.cleanup)
        # A started picture run works inside the temporary folder, so wait for it
        # to finish before that folder is removed.
        self.addCleanup(self.wait_for_the_run_to_end)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def wait_for_the_run_to_end(self) -> None:
        if self.server.qa_lock.acquire(timeout=60):
            self.server.qa_lock.release()

    def write_suite(self, cases: list[dict]) -> None:
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({"schema_version": 1, "name": "d", "cases": cases}), encoding="utf-8"
        )

    def call(self, path: str, body: dict, token: bool = True):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json"}
        if token:
            headers["X-Harness-Token"] = self.server.token
        try:
            connection.request("POST", path, json.dumps(body), headers)
            answer = connection.getresponse()
            return answer.status, json.loads(answer.read() or b"{}")
        finally:
            connection.close()

    def test_a_suite_with_no_screenshot_checks_says_so(self) -> None:
        self.write_suite([{"id": "readme", "kind": "file", "path": "README.md"}])
        status, body = self.call("/api/qa/baseline", {})
        self.assertEqual(status, 400)
        self.assertIn("no screenshot checks", body["error"])

    def test_the_button_starts_a_picture_run(self) -> None:
        self.write_suite([
            {"id": "look", "kind": "visual", "url": "http://127.0.0.1:8765/"},
            {"id": "readme", "kind": "file", "path": "README.md"},
        ])
        status, body = self.call("/api/qa/baseline", {})
        self.assertEqual(status, 202)
        self.assertEqual(body["cases"], 1)

    def test_a_check_that_is_not_a_screenshot_check_is_refused(self) -> None:
        self.write_suite([
            {"id": "look", "kind": "visual", "url": "http://127.0.0.1:8765/"},
            {"id": "readme", "kind": "file", "path": "README.md"},
        ])
        status, body = self.call("/api/qa/baseline", {"cases": ["readme"]})
        self.assertEqual(status, 400)
        self.assertIn("readme", body["error"])

    def test_the_call_needs_the_session_token(self) -> None:
        self.write_suite([{"id": "look", "kind": "visual", "url": "http://127.0.0.1:8765/"}])
        status, _body = self.call("/api/qa/baseline", {}, token=False)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
