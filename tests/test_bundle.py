"""One zip holding the checks, the runs, the settings, and what this machine is."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from our_harness import bundle
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class PartNameTests(unittest.TestCase):
    def test_no_choice_means_everything(self) -> None:
        self.assertEqual(bundle.chosen_parts(None), tuple(bundle.PARTS))
        self.assertEqual(bundle.chosen_parts([]), tuple(bundle.PARTS))
        self.assertEqual(bundle.chosen_parts(["all"]), tuple(bundle.PARTS))

    def test_parts_may_be_named_one_at_a_time_or_in_a_list(self) -> None:
        self.assertEqual(bundle.chosen_parts(["checks", "runs"]), ("checks", "runs"))
        self.assertEqual(bundle.chosen_parts(["checks,runs"]), ("checks", "runs"))
        self.assertEqual(bundle.chosen_parts(["CHECKS", " runs "]), ("checks", "runs"))

    def test_the_same_part_twice_is_only_counted_once(self) -> None:
        self.assertEqual(bundle.chosen_parts(["runs", "runs"]), ("runs",))

    def test_a_part_that_does_not_exist_is_refused_with_the_list(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            bundle.chosen_parts(["logz"])
        message = str(caught.exception)
        self.assertIn("logz", message)
        for name in bundle.PARTS:
            self.assertIn(name, message)

    def test_every_part_has_words_explaining_it(self) -> None:
        for name, words in bundle.PARTS.items():
            with self.subTest(name=name):
                self.assertTrue(words.strip())
                self.assertNotIn("_", name)


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)
        qa = self.root / ".harness" / "qa"
        (qa / "runs" / "20260101-000001").mkdir(parents=True)
        (qa / "runs" / "20260101-000002" / "look").mkdir(parents=True)
        (qa / "suite.json").write_text(
            json.dumps({"schema_version": 1, "name": "d", "cases": []}), encoding="utf-8"
        )
        (qa / "history.json").write_text(json.dumps({"runs": []}), encoding="utf-8")
        (qa / "runs" / "20260101-000001" / "result.json").write_text("{}", encoding="utf-8")
        (qa / "runs" / "20260101-000002" / "look" / "attempt-1.txt").write_text(
            "what the check saw", encoding="utf-8"
        )
        (qa / "runs" / "20260101-000002" / "look" / "attempt-1-now.png").write_bytes(b"\x89PNG fake")
        (self.root / ".harness" / "config.json").write_text(
            json.dumps({"provider": {"api_key_env": "OPENAI_API_KEY"}}), encoding="utf-8"
        )

    def names_in(self, path: Path) -> list[str]:
        with zipfile.ZipFile(path) as archive:
            return sorted(archive.namelist())

    def read(self, path: Path, name: str) -> str:
        with zipfile.ZipFile(path) as archive:
            return archive.read(name).decode("utf-8")

    def test_a_whole_bundle_holds_every_part(self) -> None:
        result = bundle.build(self.config)
        names = self.names_in(result.path)
        self.assertIn("manifest.json", names)
        self.assertIn("checks/suite.json", names)
        self.assertIn("history.json", names)
        self.assertIn("settings/config.json", names)
        self.assertIn("machine.json", names)
        self.assertIn("runs/20260101-000002/look/attempt-1.txt", names)
        self.assertIn("runs/20260101-000002/look/attempt-1-now.png", names)

    def test_only_the_parts_asked_for_go_in(self) -> None:
        result = bundle.build(self.config, parts=["checks"])
        names = self.names_in(result.path)
        self.assertEqual(names, ["checks/suite.json", "manifest.json"])

    def test_the_number_of_runs_kept_is_obeyed(self) -> None:
        result = bundle.build(self.config, parts=["runs"], runs=1)
        names = self.names_in(result.path)
        self.assertTrue(all(name.startswith(("runs/20260101-000002", "manifest")) for name in names))

    def test_asking_for_no_runs_keeps_none(self) -> None:
        result = bundle.build(self.config, parts=["runs"], runs=0)
        self.assertEqual(self.names_in(result.path), ["manifest.json"])

    def test_a_silly_number_of_runs_is_refused(self) -> None:
        for runs in (-1, 101, 2.5, True, "five"):
            with self.subTest(runs=runs), self.assertRaises(HarnessError):
                bundle.build(self.config, runs=runs)  # type: ignore[arg-type]

    def test_the_manifest_says_what_is_inside(self) -> None:
        result = bundle.build(self.config, parts=["checks", "machine"])
        manifest = json.loads(self.read(result.path, "manifest.json"))
        self.assertEqual(manifest["parts"], ["checks", "machine"])
        self.assertIn("checks/suite.json", manifest["files"])
        self.assertEqual(manifest["schema_version"], 1)

    def test_what_the_manifest_lists_is_really_in_the_zip(self) -> None:
        # The old tool wrote one name and read another. Here the list and the
        # contents must agree, file for file.
        result = bundle.build(self.config)
        manifest = json.loads(self.read(result.path, "manifest.json"))
        inside = set(self.names_in(result.path)) - {"manifest.json"}
        self.assertEqual(set(manifest["files"]), inside)
        self.assertEqual(set(result.files), inside)
        self.assertNotIn("manifest.json", result.files)

    def test_the_manifest_can_be_read_back(self) -> None:
        result = bundle.build(self.config, parts=["machine"])
        again = bundle.read_manifest(result.path)
        self.assertEqual(again["parts"], ["machine"])

    def test_something_that_is_not_a_bundle_is_refused(self) -> None:
        loose = self.root / "notes.zip"
        loose.write_bytes(b"not a zip at all")
        with self.assertRaises(HarnessError):
            bundle.read_manifest(loose)
        with self.assertRaises(HarnessError):
            bundle.read_manifest(self.root / "missing.zip")

    def test_a_bundle_naming_an_unknown_part_is_refused_when_read(self) -> None:
        made = self.root / "odd.zip"
        with zipfile.ZipFile(made, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"parts": ["from-the-future"]}))
        with self.assertRaises(HarnessError) as caught:
            bundle.read_manifest(made)
        self.assertIn("from-the-future", str(caught.exception))

    def test_credentials_are_taken_out(self) -> None:
        (self.root / ".harness" / "config.local.json").write_text(
            json.dumps({"provider": {"api_key": "sk-abcdefghijklmnop1234"}}), encoding="utf-8"
        )
        result = bundle.build(self.config, parts=["settings"])
        body = self.read(result.path, "settings/config.local.json")
        self.assertNotIn("sk-abcdefghijklmnop1234", body)
        self.assertIn("REDACTED", body)

    def test_the_persons_own_folder_name_is_hidden(self) -> None:
        home = str(Path.home())
        (self.root / ".harness" / "qa" / "runs" / "20260101-000001" / "note.txt").write_text(
            f"it broke in {home}\\work", encoding="utf-8"
        )
        result = bundle.build(self.config, parts=["runs"])
        body = self.read(result.path, "runs/20260101-000001/note.txt")
        self.assertNotIn(home, body)
        self.assertIn("~", body)

    def test_a_file_that_is_too_big_is_left_out_and_said_so(self) -> None:
        big = self.root / ".harness" / "qa" / "runs" / "20260101-000001" / "huge.txt"
        big.write_text("x" * (bundle.MAX_FILE_BYTES + 10), encoding="utf-8")
        result = bundle.build(self.config, parts=["runs"])
        self.assertTrue(any("huge.txt" in item for item in result.left_out))
        self.assertNotIn("runs/20260101-000001/huge.txt", self.names_in(result.path))
        manifest = json.loads(self.read(result.path, "manifest.json"))
        self.assertTrue(manifest["left_out"])

    def test_nothing_from_the_git_folder_goes_in(self) -> None:
        git = self.root / ".git"
        git.mkdir()
        (git / "config").write_text("[remote]\n url = git@example.com:x.git\n", encoding="utf-8")
        result = bundle.build(self.config)
        self.assertFalse(any(name.startswith(".git") for name in self.names_in(result.path)))

    def test_the_zip_goes_where_it_was_asked_to(self) -> None:
        result = bundle.build(self.config, parts=["machine"], output="reports/support.zip")
        self.assertEqual(result.path, self.root / "reports" / "support.zip")
        self.assertTrue(result.path.is_file())

    def test_a_name_that_is_not_a_zip_is_refused(self) -> None:
        for name in ("report.txt", "report", "../outside.zip"):
            with self.subTest(name=name), self.assertRaises(HarnessError):
                bundle.build(self.config, output=name)

    def test_with_no_name_it_lands_in_the_project(self) -> None:
        result = bundle.build(self.config, parts=["machine"])
        self.assertTrue(result.path.is_relative_to(self.root))
        self.assertTrue(result.path.name.startswith("harness-bundle-"))

    def test_the_lines_it_prints_say_what_happened(self) -> None:
        result = bundle.build(self.config, parts=["checks"])
        lines = "\n".join(bundle.describe(result))
        self.assertIn("Wrote", lines)
        self.assertIn("checks:", lines)
        self.assertIn("Credentials were taken out", lines)


class PanelBundleTests(unittest.TestCase):
    """The Pack up the evidence button in the control panel."""

    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({"schema_version": 1, "name": "d", "cases": []}), encoding="utf-8"
        )
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        self.config = LoadedConfig(data, self.root, [], {})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), self.config)
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, body: dict, token: bool = True):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json"}
        if token:
            headers["X-Harness-Token"] = self.server.token
        try:
            connection.request("POST", "/api/bundle", json.dumps(body), headers)
            answer = connection.getresponse()
            return answer.status, json.loads(answer.read() or b"{}")
        finally:
            connection.close()

    def test_the_button_writes_a_bundle_and_says_where(self) -> None:
        status, body = self.call({})
        self.assertEqual(status, 200)
        made = Path(body["path"])
        self.assertTrue(made.is_file())
        self.assertTrue(made.is_relative_to(self.root))
        self.assertEqual(body["parts"], list(bundle.PARTS))
        self.assertGreater(body["files"], 0)

    def test_a_part_that_does_not_exist_is_refused(self) -> None:
        status, body = self.call({"parts": ["logz"]})
        self.assertEqual(status, 400)
        self.assertIn("logz", body["error"])

    def test_the_call_needs_the_session_token(self) -> None:
        status, _body = self.call({}, token=False)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
