"""One file you can send to anyone."""

from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from our_harness import share
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def tiny_png(width: int = 2, height: int = 2) -> bytes:
    """A real, readable PNG, so nothing here leans on a made-up file."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            len(body).to_bytes(4, "big")
            + kind
            + body
            + zlib.crc32(kind + body).to_bytes(4, "big")
        )

    header = chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0]))
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def case(case_id: str, status: str, reasons=None, evidence: str = "") -> dict:
    return {
        "id": case_id,
        "title": f"The {case_id} check",
        "kind": "browser",
        "status": status,
        "duration_ms": 12,
        "reasons": reasons or ([] if status == "passed" else ["it did not work"]),
        "attempts": [{"number": 1, "status": status, "evidence": evidence}],
    }


class ShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runs = self.root / ".harness" / "qa" / "runs"
        self.runs.mkdir(parents=True)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def keep(self, name: str, cases: list[dict], pictures: list[str] = ()) -> Path:
        folder = self.runs / name
        folder.mkdir(exist_ok=True)
        (folder / "result.json").write_text(
            json.dumps({
                "run_id": name, "suite_name": "mine", "started_at": "2026-01-01T00:00:00Z",
                "cases": cases,
            }),
            encoding="utf-8",
        )
        for picture in pictures:
            spot = folder / picture
            spot.parent.mkdir(parents=True, exist_ok=True)
            spot.write_bytes(tiny_png())
        return folder

    def test_the_page_says_what_happened(self) -> None:
        self.keep("20260101-000001", [case("sign-in", "passed"), case("checkout", "failed")])
        page = share.build(self.config)
        self.assertIn("Some checks failed", page.html)
        self.assertIn("sign-in", page.html)
        self.assertIn("checkout", page.html)
        self.assertIn("1 passed, 1 failed", page.html)

    def test_a_run_where_everything_passed_says_so(self) -> None:
        self.keep("20260101-000001", [case("sign-in", "passed")])
        self.assertIn("All checks passed", share.build(self.config).html)

    def test_the_pictures_are_inside_the_file(self) -> None:
        self.keep(
            "20260101-000001",
            [case("sign-in", "failed")],
            ["sign-in/attempt-01-step-01-went-wrong.png"],
        )
        page = share.build(self.config)
        self.assertEqual(page.pictures, 1)
        self.assertIn("data:image/png;base64,", page.html)
        # The real bytes, not a link to a file that will not travel with it.
        packed = base64.b64encode(tiny_png()).decode("ascii")
        self.assertIn(packed, page.html)
        self.assertNotIn("<img src=\"sign-in/", page.html)

    def test_pictures_can_be_left_out_to_keep_it_small(self) -> None:
        self.keep("20260101-000001", [case("a", "failed")], ["a/one.png"])
        page = share.build(self.config, with_pictures=False)
        self.assertEqual(page.pictures, 0)
        self.assertNotIn("data:image/png", page.html)

    def test_a_picture_too_big_to_send_is_named_rather_than_dropped_in_silence(self) -> None:
        folder = self.keep("20260101-000001", [case("a", "failed")])
        big = folder / "a" / "huge.png"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (share.MAX_PICTURE_BYTES + 1))
        page = share.build(self.config)
        self.assertEqual(page.pictures, 0)
        self.assertTrue(any("huge.png" in item for item in page.left_out))
        self.assertIn("Left out of this file", page.html)

    def test_the_whole_page_stops_growing_at_some_point(self) -> None:
        folder = self.keep("20260101-000001", [case("a", "failed")])
        (folder / "a").mkdir(exist_ok=True)
        for number in range(share.MAX_PICTURES + 5):
            (folder / "a" / f"shot-{number:03d}.png").write_bytes(tiny_png())
        page = share.build(self.config)
        self.assertEqual(page.pictures, share.MAX_PICTURES)
        self.assertTrue(page.left_out)


class SafetyTests(ShareTests):
    def test_a_credential_in_the_evidence_never_reaches_the_page(self) -> None:
        self.keep(
            "20260101-000001",
            [case(
                "sign-in", "failed",
                reasons=['the answer said {"password": "hunter2hunter2"}'],
                evidence='api_key="sk-live-abcdefghij"\nAuthorization: Bearer eyJhbGciOiJIUzI1NiJ9abc',
            )],
        )
        page = share.build(self.config)
        for secret in ("hunter2hunter2", "sk-live-abcdefghij", "eyJhbGciOiJIUzI1NiJ9abc"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, page.html)
        self.assertIn("[REDACTED]", page.html)

    def test_the_persons_own_folder_name_is_not_written_down(self) -> None:
        self.keep(
            "20260101-000001",
            [case("a", "failed", evidence=f"it looked in {Path.home()}/projects/thing")],
        )
        page = share.build(self.config)
        self.assertNotIn(str(Path.home()), page.html)
        self.assertIn("~", page.html)

    def test_terminal_colour_codes_are_taken_out_so_a_person_can_read_it(self) -> None:
        self.keep(
            "20260101-000001",
            [case("a", "failed", evidence="Call log:\n\x1b[2m  - waiting for the button\x1b[22m")],
        )
        page = share.build(self.config)
        self.assertNotIn("\x1b[", page.html)
        self.assertNotIn("[2m", page.html)
        self.assertIn("waiting for the button", page.html)

    def test_colour_codes_spelled_out_in_letters_are_taken_out_too(self) -> None:
        self.keep(
            "20260101-000001",
            [case("a", "failed", evidence=r"Call log: [2m waiting [22m for it")],
        )
        page = share.build(self.config)
        self.assertNotIn("u001b", page.html)
        self.assertIn("waiting", page.html)

    def test_a_title_holding_html_cannot_change_the_page(self) -> None:
        self.keep(
            "20260101-000001",
            [{
                "id": "<script>alert(1)</script>",
                "title": "<img src=x onerror=alert(2)>",
                "kind": "browser", "status": "failed", "duration_ms": 1,
                "reasons": ["<b>bold</b>"], "attempts": [],
            }],
        )
        page = share.build(self.config)
        # The markup has to arrive as words on the page, never as markup.
        self.assertNotIn("<script>alert(1)</script>", page.html)
        self.assertNotIn("<img src=x", page.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page.html)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", page.html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", page.html)

    def test_a_run_name_cannot_reach_outside_the_project(self) -> None:
        for name in ("../../elsewhere", "..\\elsewhere", "a/b", "/etc"):
            with self.subTest(name=name), self.assertRaises(HarnessError):
                share.build(self.config, name)

    def test_writing_the_page_cannot_reach_outside_the_project(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        for where in ("../escaped.html", "..\\escaped.html", str(self.root.parent / "escaped.html")):
            with self.subTest(where=where), self.assertRaises(HarnessError):
                share.write(self.config, output=where)
        self.assertFalse((self.root.parent / "escaped.html").exists())


class ChoosingTests(ShareTests):
    def test_the_newest_kept_run_is_used_when_none_is_named(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        self.keep("20260101-000002", [case("b", "failed")])
        self.assertEqual(share.build(self.config).run_id, "20260101-000002")

    def test_a_named_run_is_used_when_one_is_given(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        self.keep("20260101-000002", [case("b", "failed")])
        self.assertEqual(share.build(self.config, "20260101-000001").run_id, "20260101-000001")

    def test_no_runs_yet_says_what_to_do(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            share.build(self.config)
        self.assertIn("harness qa run", str(caught.exception))

    def test_a_run_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(HarnessError):
            share.build(self.config, "20991231-235959")

    def test_a_report_that_is_not_a_run_is_refused(self) -> None:
        folder = self.runs / "20260101-000001"
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps({"cases": "none"}), encoding="utf-8")
        with self.assertRaises(HarnessError):
            share.build(self.config)


class WritingTests(ShareTests):
    def test_the_page_is_written_next_to_the_run_by_default(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        path, page = share.write(self.config)
        self.assertEqual(path, self.runs / "20260101-000001" / "report.html")
        self.assertIn("All checks passed", path.read_text(encoding="utf-8"))
        self.assertIn("Wrote", share.summary(path, page)[0])

    def test_it_can_be_written_where_you_ask(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        path, _page = share.write(self.config, output="report-for-the-team.html")
        self.assertEqual(path, self.root / "report-for-the-team.html")
        self.assertTrue(path.is_file())

    def test_writing_over_a_folder_says_so_rather_than_falling_over(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        (self.root / "somewhere").mkdir()
        with self.assertRaises(HarnessError):
            share.write(self.config, output="somewhere")

    def test_the_json_answer_holds_where_it_went(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        path, page = share.write(self.config)
        answer = json.loads(share.as_json(path, page))
        self.assertEqual(answer["run_id"], "20260101-000001")
        self.assertGreater(answer["bytes"], 0)

    def test_one_picture_is_said_in_the_singular(self) -> None:
        self.keep("20260101-000001", [case("a", "failed")], ["a/one.png"])
        path, page = share.write(self.config)
        said = " ".join(share.summary(path, page))
        self.assertIn("1 picture is inside", said)

    def test_no_pictures_says_there_were_none_rather_than_zero_pictures_are(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        path, page = share.write(self.config)
        self.assertIn("no pictures in this run", " ".join(share.summary(path, page)))


class PanelTests(ShareTests):
    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        super().setUp()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), LoadedConfig(data, self.root, [], {}))
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, body: dict) -> tuple[int, dict]:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        connection.request(
            "POST", "/api/qa/share", json.dumps(body),
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

    def test_the_panel_writes_the_page_and_says_where(self) -> None:
        self.keep("20260101-000001", [case("a", "failed")], ["a/one.png"])
        status, body = self.post({})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["pictures"], 1)
        self.assertEqual(body["path"], ".harness/qa/runs/20260101-000001/report.html")
        self.assertTrue((self.root / body["path"]).is_file())

    def test_the_panel_refuses_a_run_name_that_climbs_out(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        status, body = self.post({"run": "../../elsewhere"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_the_panel_says_when_there_is_nothing_to_share_yet(self) -> None:
        status, body = self.post({})
        self.assertEqual(status, 400)
        self.assertIn("harness qa run", body["error"])


if __name__ == "__main__":
    unittest.main()


class NothingOnThePageEscapesTheDoorTests(ShareTests):
    """A credential is a credential wherever somebody typed it.

    The id and the title of a check are free text. Somebody drafting a check
    can paste a token into either of them, and the page must not print it.
    """

    def test_a_credential_in_the_name_of_a_check_never_reaches_the_page(self) -> None:
        self.keep("20260101-000001", [{
            "id": "sign-in-with-api_key=sk-live-abcdefghijklmno",
            "title": 'Sign-in test (curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")',
            "kind": "browser", "status": "failed", "duration_ms": 5,
            "reasons": ["it did not work"], "attempts": [],
        }])
        page = share.build(self.config)
        for secret in ("sk-live-abcdefghijklmno", "eyJhbGciOiJIUzI1NiJ9.payload.sig"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, page.html)
        self.assertIn("[REDACTED]", page.html)

    def test_a_credential_in_the_name_of_the_suite_never_reaches_the_page(self) -> None:
        folder = self.keep("20260101-000001", [case("a", "passed")])
        (folder / "result.json").write_text(
            json.dumps({
                "run_id": "20260101-000001",
                "suite_name": 'the suite for password="hunter2hunter2"',
                "started_at": "2026-01-01T00:00:00Z",
                "cases": [case("a", "passed")],
            }),
            encoding="utf-8",
        )
        self.assertNotIn("hunter2hunter2", share.build(self.config).html)

    def test_a_credential_in_the_name_of_a_picture_never_reaches_the_page(self) -> None:
        self.keep(
            "20260101-000001",
            [case("a", "failed")],
            ["a/token=abcdefghijklmnop.png"],
        )
        page = share.build(self.config)
        self.assertEqual(page.pictures, 1)
        self.assertNotIn("abcdefghijklmnop", page.html)

    def test_a_credential_in_a_left_out_note_never_reaches_the_page(self) -> None:
        folder = self.keep("20260101-000001", [case("a", "failed")])
        big = folder / "a" / "password=hunter2hunter2.png"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (share.MAX_PICTURE_BYTES + 1))
        page = share.build(self.config)
        self.assertIn("Left out of this file", page.html)
        self.assertNotIn("hunter2hunter2", page.html)

    def test_markup_in_a_name_still_arrives_as_words(self) -> None:
        # Hiding credentials must not undo the escaping that was already right.
        self.keep("20260101-000001", [{
            "id": "<script>alert(1)</script>", "title": "<img src=x onerror=alert(2)>",
            "kind": "browser", "status": "failed", "duration_ms": 1,
            "reasons": [], "attempts": [],
        }])
        page = share.build(self.config)
        self.assertNotIn("<script>alert(1)</script>", page.html)
        self.assertNotIn("<img src=x", page.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page.html)

    def test_an_ordinary_name_is_left_exactly_as_it_was(self) -> None:
        self.keep("20260101-000001", [case("sign-in", "passed")])
        page = share.build(self.config)
        self.assertIn("sign-in", page.html)
        self.assertIn("The sign-in check", page.html)
        self.assertNotIn("[REDACTED]", page.html)


class WhereItIsWrittenTests(ShareTests):
    """The path comes from the folder on disk, never from the file's own words."""

    def test_a_run_naming_itself_something_odd_is_still_written_beside_its_folder(self) -> None:
        folder = self.runs / "20260101-000001"
        folder.mkdir()
        (folder / "result.json").write_text(
            json.dumps({
                "run_id": "../../../somewhere-else",
                "suite_name": "mine",
                "cases": [case("a", "passed")],
            }),
            encoding="utf-8",
        )
        path, page = share.write(self.config)
        self.assertEqual(path, folder / "report.html")
        self.assertTrue(path.is_file())
        self.assertFalse((self.root.parent / "somewhere-else" / "report.html").exists())
        self.assertEqual(page.folder, "20260101-000001")

    def test_the_run_folder_is_used_even_when_the_file_names_no_run_at_all(self) -> None:
        folder = self.runs / "20260101-000002"
        folder.mkdir()
        (folder / "result.json").write_text(
            json.dumps({"cases": [case("a", "passed")]}), encoding="utf-8"
        )
        path, _page = share.write(self.config)
        self.assertEqual(path, folder / "report.html")


class TheAnswerNobodyLooksAtTests(ShareTests):
    """The JSON answer carries the same notes the page does, cleaned the same way.

    The page was right and the answer behind it was not, which is the worst way
    round: the thing a person reads looked safe while the thing a program reads
    held the secret.
    """

    def big_picture(self, folder: Path, name: str) -> None:
        spot = folder / name
        spot.parent.mkdir(parents=True, exist_ok=True)
        spot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (share.MAX_PICTURE_BYTES + 1))

    def test_a_left_out_note_is_clean_in_the_answer_as_well_as_on_the_page(self) -> None:
        folder = self.keep("20260101-000001", [case("api-key-sk-live-abcdefghijklmno", "failed")])
        self.big_picture(folder, "api-key-sk-live-abcdefghijklmno/attempt-01-went-wrong.png")
        page = share.build(self.config)
        self.assertTrue(page.left_out)
        for note in page.left_out:
            with self.subTest(note=note[:40]):
                self.assertNotIn("sk-live-abcdefghijklmno", note)
        self.assertNotIn("sk-live-abcdefghijklmno", page.html)
        self.assertNotIn("sk-live-abcdefghijklmno", json.dumps(page.to_dict()))

    def test_the_command_line_answer_is_clean_too(self) -> None:
        folder = self.keep("20260101-000001", [case("api-key-sk-live-abcdefghijklmno", "failed")])
        self.big_picture(folder, "api-key-sk-live-abcdefghijklmno/one.png")
        path, page = share.write(self.config)
        self.assertNotIn("sk-live-abcdefghijklmno", share.as_json(path, page))
        self.assertNotIn("sk-live-abcdefghijklmno", " ".join(share.summary(path, page)))

    def test_the_panel_answer_is_clean_too(self) -> None:
        import http.client
        import threading

        from our_harness.server import HarnessHTTPServer

        folder = self.keep("20260101-000001", [case("api-key-sk-live-abcdefghijklmno", "failed")])
        self.big_picture(folder, "api-key-sk-live-abcdefghijklmno/one.png")
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        server = HarnessHTTPServer(("127.0.0.1", 0), LoadedConfig(data, self.root, [], {}))
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
            connection.request(
                "POST", "/api/qa/share", "{}",
                {
                    "Host": f"127.0.0.1:{server.server_port}",
                    "Content-Type": "application/json",
                    "X-Harness-Token": server.token,
                },
            )
            answer = connection.getresponse()
            body = answer.read().decode("utf-8")
            connection.close()
            self.assertEqual(answer.status, 200, body)
            self.assertIn("left_out", body)
            self.assertNotIn("sk-live-abcdefghijklmno", body)
        finally:
            server.shutdown()
            server.server_close()

    def test_an_ordinary_note_still_says_which_picture_it_was(self) -> None:
        folder = self.keep("20260101-000001", [case("sign-in", "failed")])
        self.big_picture(folder, "sign-in/attempt-01-went-wrong.png")
        page = share.build(self.config)
        self.assertIn("attempt-01-went-wrong.png", page.left_out[0])
        self.assertIn("too big", page.left_out[0])
