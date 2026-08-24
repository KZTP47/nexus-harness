"""More bugs found by hunting, each with the case that proves it stays fixed.

These cover the control panel's server, the desktop rules, and the commands.
"""

from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class ServerRequestTests(unittest.TestCase):
    """Odd requests must be answered with a sentence, never a crash."""

    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
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

    def call(self, method: str, path: str, body=None, headers=None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        sent = {
            "Host": f"127.0.0.1:{self.server.server_port}",
            "Content-Type": "application/json",
            "X-Harness-Token": self.server.token,
        }
        sent.update(headers or {})
        try:
            connection.request(method, path, body, sent)
            answer = connection.getresponse()
            raw = answer.read()
            try:
                return answer.status, json.loads(raw or b"{}")
            except ValueError:
                return answer.status, {}
        finally:
            connection.close()

    def test_the_session_key_is_only_given_to_the_panel_page(self) -> None:
        status, body = self.call("GET", "/api/bootstrap")
        self.assertEqual(status, 400)
        self.assertIn("control panel page", body["error"])
        status, body = self.call("GET", "/api/bootstrap", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertIn("token", body)

    def test_every_start_has_its_own_mark(self) -> None:
        _status, body = self.call("GET", "/api/bootstrap", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertTrue(body["started_id"])
        _status, events = self.call("GET", "/api/events?after=0&meta=1")
        self.assertEqual(events["started_id"], body["started_id"])

    def test_claude_repair_endpoint_opens_only_the_safe_provider_flow(self) -> None:
        opened = {"opened": True, "kind": "claude-cli", "note": "repair opened", "process": 42}
        with mock.patch(
                "our_harness.providers.subscription_cli.start_claude_repair",
                return_value=opened,
        ) as repair:
            status, body = self.call("POST", "/api/team/repair-claude", "{}")
        self.assertEqual(status, 200)
        self.assertEqual(body, opened)
        repair.assert_called_once_with()

    def test_json_nested_far_too_deep_is_refused_politely(self) -> None:
        deep = "[" * 60_000 + "]" * 60_000
        status, body = self.call("POST", "/api/validate", '{"graph": ' + deep + "}")
        self.assertEqual(status, 400)
        self.assertIn("nested far too deeply", body["error"])

    def test_a_state_that_is_not_an_object_is_refused_politely(self) -> None:
        status, body = self.call(
            "POST", "/api/simulate",
            json.dumps({"graph": self.server.template, "state": [1, 2, 3]}),
        )
        self.assertEqual(status, 400)
        self.assertIn("state must be an object", body["error"])

    def test_a_run_count_that_is_not_a_number_is_refused_politely(self) -> None:
        for value in ("abc", None, [1, 2], {"a": 1}, -5, 10_000):
            with self.subTest(value=value):
                status, body = self.call("POST", "/api/bundle", json.dumps({"runs": value}))
                self.assertEqual(status, 400)
                self.assertIn("runs", body["error"])

    def test_a_body_that_is_too_big_gets_an_answer(self) -> None:
        status, body = self.call("POST", "/api/validate", "x" * 10, {"Content-Length": "12000001"})
        self.assertEqual(status, 400)
        self.assertIn("12000000", body["error"])


class WorkflowNameCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        from importlib.resources import files

        from our_harness.graphs import migrate_graph

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)
        self.graph = migrate_graph(
            json.loads(
                files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8")
            )
        )

    def test_a_name_that_differs_only_by_capitals_cannot_overwrite_another(self) -> None:
        from our_harness import workflows

        workflows.save(self.config, "Nightly", self.graph)
        with self.assertRaises(HarnessError) as caught:
            workflows.save(self.config, "nightly", self.graph)
        self.assertIn("Nightly", str(caught.exception))
        self.assertEqual([item.name for item in workflows.listed(self.config)], ["Nightly"])

    def test_renaming_the_spelling_still_works(self) -> None:
        from our_harness import workflows

        workflows.save(self.config, "Nightly", self.graph)
        workflows.rename(self.config, "Nightly", "NIGHTLY")
        self.assertEqual([item.name for item in workflows.listed(self.config)], ["NIGHTLY"])


class StoredNotesTests(unittest.TestCase):
    def test_a_stored_note_is_cleaned_the_same_as_a_new_one(self) -> None:
        from our_harness.messaging import MESSAGE_SCHEMA_VERSION, MessageBoard

        def hide(value: str) -> str:
            return value.replace("sk-supersecretkey12345", "[REDACTED]")

        snapshot = {
            "schema_version": MESSAGE_SCHEMA_VERSION,
            "participants": ["ada", "bob"],
            "sequence": 1,
            "messages": [{
                "sequence": 1, "from": "ada", "to": "bob",
                "subject": "the key is sk-supersecretkey12345",
                "body": "here is sk-supersecretkey12345 for the service",
                "created_at": 1.0,
            }],
        }
        board = MessageBoard.restore(snapshot, redact=hide)
        note = board.inbox("bob")[0]
        self.assertNotIn("sk-supersecretkey12345", note.body)
        self.assertNotIn("sk-supersecretkey12345", note.subject)


class WatcherSurvivalTests(unittest.TestCase):
    def test_a_folder_that_disappears_does_not_end_the_watch(self) -> None:
        from our_harness.watcher import scan

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "one").mkdir()
            (root / "one" / "a.txt").write_text("a", encoding="utf-8")

            class Vanishing:
                def walk_files(self):
                    yield root / "one" / "a.txt"
                    raise HarnessError("Cannot inspect workspace directory: two")

            found = scan(root, Vanishing())
            self.assertEqual(list(found), ["one/a.txt"])


class AdviceCacheTests(unittest.TestCase):
    def test_changing_the_model_changes_the_advice(self) -> None:
        from unittest import mock

        from our_harness import provider_help

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["provider"].update({
                "name": "ollama", "model": "qwen2.5-coder:7b",
                "endpoint": "http://127.0.0.1:11434",
            })
            # Nothing listening, said so here rather than found out by asking
            # the machine. This test used to make a real call to the local
            # address, and passed or failed depending on whether the person
            # running it happened to have Ollama running.
            with mock.patch.object(provider_help, "_reachable", lambda *a, **k: False):
                first = repr(provider_help.setup_advice(LoadedConfig(data, root, [], {})))
                changed = copy.deepcopy(data)
                changed["provider"]["model"] = "llama3.1:70b"
                second = repr(provider_help.setup_advice(LoadedConfig(changed, root, [], {})))
            self.assertIn("qwen2.5-coder:7b", first)
            self.assertIn("llama3.1:70b", second)
            self.assertNotIn("qwen2.5-coder:7b", second)


class BundleOverwriteTests(unittest.TestCase):
    def test_an_existing_file_is_not_written_over_by_accident(self) -> None:
        from our_harness import bundle

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".harness").mkdir()
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
            keep = root / "notes.zip"
            keep.write_text("irreplaceable notes", encoding="utf-8")
            with self.assertRaises(HarnessError) as caught:
                bundle.build(config, parts=["machine"], output="notes.zip")
            self.assertIn("already there", str(caught.exception))
            self.assertEqual(keep.read_text(encoding="utf-8"), "irreplaceable notes")
            built = bundle.build(config, parts=["machine"], output="notes.zip", replace=True)
            self.assertTrue(built.path.is_file())


class DenyListTests(unittest.TestCase):
    """The denied list must not be walked around with a shell."""

    def runner(self):
        from our_harness.execution import CommandRunner

        return CommandRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}))

    def test_a_denied_program_is_refused_however_it_is_spelled(self) -> None:
        for argv in (
            ["format.com", "C:"],
            ["FORMAT.EXE", "C:"],
            ["cmd", "/c", "format", "C:", "/y"],
            ["cmd.exe", "/c", "shutdown", "/s", "/t", "0"],
            ["powershell", "-NoProfile", "-Command", "Format-Volume -DriveLetter C"],
            ["bash", "-c", "mkfs /dev/sda"],
            # A command inside another command is still that command.
            ["bash", "-c", "echo $(diskpart)"],
            ["bash", "-c", "echo `diskpart`"],
            ["sh", "-c", "true; shutdown -h now"],
            ["cmd", "/c", "echo hi & format C:"],
            ["powershell", "-Command", "$x = (Clear-Disk -Number 0)"],
        ):
            with self.subTest(argv=argv), self.assertRaises(HarnessError):
                self.runner()._check(argv)

    def test_a_switch_written_any_of_the_usual_ways_is_refused(self) -> None:
        for switch in ("--force", "-Force", "-force", "/Force", "/force"):
            with self.subTest(switch=switch), self.assertRaises(HarnessError):
                self.runner()._check(["git", "push", switch, "origin", "main"])

    def test_ordinary_commands_still_run(self) -> None:
        for argv in (
            ["python", "-m", "pytest", "-q"],
            ["node", "script.js"],
            ["git", "status"],
            ["cmd", "/c", "echo hello"],
            ["npm", "run", "build"],
        ):
            with self.subTest(argv=argv):
                self.runner()._check(argv)


class RedactionSpeedTests(unittest.TestCase):
    def test_text_that_looks_like_many_keys_is_still_quick(self) -> None:
        from our_harness.redaction import CredentialRedactor

        text = "-----BEGIN RSA PRIVATE KEY-----\nnot closed\n" * 6000
        started = time.monotonic()
        cleaned = CredentialRedactor(None).text(text)
        took = time.monotonic() - started
        self.assertIn("[REDACTED PRIVATE KEY]", cleaned)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", cleaned)
        # This much text used to take about ten seconds.
        self.assertLess(took, 3.0)

    def test_a_real_key_is_still_hidden(self) -> None:
        from our_harness.redaction import CredentialRedactor

        body = (
            "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n"
            "-----END RSA PRIVATE KEY-----\nafter"
        )
        cleaned = CredentialRedactor(None).text(body)
        self.assertNotIn("MIIBOgIBAAJBAK", cleaned)
        self.assertIn("before", cleaned)
        self.assertIn("after", cleaned)

    def test_two_keys_in_one_piece_of_text_are_both_hidden(self) -> None:
        from our_harness.redaction import CredentialRedactor

        body = (
            "one\n-----BEGIN A PRIVATE KEY-----\nAAAA\n-----END A PRIVATE KEY-----\n"
            "two\n-----BEGIN B PRIVATE KEY-----\nBBBB\n-----END B PRIVATE KEY-----\nthree"
        )
        cleaned = CredentialRedactor(None).text(body)
        self.assertNotIn("AAAA", cleaned)
        self.assertNotIn("BBBB", cleaned)
        for word in ("one", "two", "three"):
            self.assertIn(word, cleaned)

    def test_a_key_that_was_never_closed_is_still_hidden(self) -> None:
        from our_harness.redaction import CredentialRedactor

        cleaned = CredentialRedactor(None).text(
            "keep\n-----BEGIN RSA PRIVATE KEY-----\nSECRETBITS\nmore"
        )
        self.assertNotIn("SECRETBITS", cleaned)
        self.assertIn("keep", cleaned)


class WatchExitCodeTests(unittest.TestCase):
    """A watch session that saw a failure must not report success."""

    def test_a_failing_run_while_watching_ends_with_a_failing_code(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".harness" / "qa").mkdir(parents=True)
            (root / ".harness" / "qa" / "suite.json").write_text(
                json.dumps({
                    "schema_version": 1, "name": "d",
                    "cases": [{
                        "id": "always-fails", "kind": "command",
                        "command": [sys.executable, "-c", "raise SystemExit(3)"],
                        "expect": {"exit_code": 0},
                    }],
                }),
                encoding="utf-8",
            )
            finished = subprocess.run(
                [
                    sys.executable, "-m", "our_harness", "--project", str(root), "qa", "watch",
                    "--max-runs", "1", "--every", "0.2", "--interval", "0.1", "--no-artifacts",
                ],
                capture_output=True, text=True, timeout=180,
                env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd() / "src")},
                cwd=str(Path.cwd()),
            )
            self.assertEqual(finished.returncode, 1, finished.stdout + finished.stderr)
            self.assertIn("failed", finished.stdout)


if __name__ == "__main__":
    unittest.main()


class PanelScriptTests(unittest.TestCase):
    """One mistake in the panel script stops every browser check at once.

    It is worth a second of checking here, where the answer is one clear line,
    rather than forty failing checks that all say "not defined".
    """

    def test_the_panel_script_can_be_read_by_a_browser(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not on this machine")
        for name in ("app.js",):
            with self.subTest(file=name):
                path = Path(__file__).resolve().parents[1] / "src" / "our_harness" / "ui" / name
                finished = subprocess.run(
                    [node, "--check", str(path)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(finished.returncode, 0, finished.stderr)

    def test_the_panel_page_names_every_thing_the_script_looks_for(self) -> None:
        import re

        ui = Path(__file__).resolve().parents[1] / "src" / "our_harness" / "ui"
        page = (ui / "index.html").read_text(encoding="utf-8")
        script = (ui / "app.js").read_text(encoding="utf-8")
        known = set(re.findall(r'id="([A-Za-z0-9_-]+)"', page))
        wanted = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', script))
        missing = sorted(wanted - known)
        self.assertEqual(missing, [], f"the script looks for things the page does not have: {missing}")


class NewPanelEndpointTests(unittest.TestCase):
    """The shelf, adding, and removing, over the wire the panel really uses."""

    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({
                "schema_version": 1, "name": "d",
                "cases": [{"id": "readme", "kind": "file", "path": "README.md"}],
            }),
            encoding="utf-8",
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

    def call(self, method: str, path: str, body=None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {
            "Host": f"127.0.0.1:{self.server.server_port}",
            "Content-Type": "application/json",
            "X-Harness-Token": self.server.token,
        }
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            answer = connection.getresponse()
            return answer.status, json.loads(answer.read() or b"{}")
        finally:
            connection.close()

    def test_the_shelf_can_be_read(self) -> None:
        status, body = self.call("GET", "/api/qa/starters")
        self.assertEqual(status, 200)
        self.assertGreater(len(body["starters"]), 5)
        self.assertIn("phone", body["screens"])

    def test_a_check_can_be_added_and_taken_out_again(self) -> None:
        status, body = self.call(
            "POST", "/api/qa/add", {"starter": "no-keys-in-the-code", "name": "added-here"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["added"], "added-here")
        kept = json.loads((self.root / ".harness" / "qa" / "suite.json").read_text(encoding="utf-8"))
        self.assertIn("added-here", [case["id"] for case in kept["cases"]])

        status, body = self.call("POST", "/api/qa/remove", {"case": "added-here"})
        self.assertEqual(status, 200)
        kept = json.loads((self.root / ".harness" / "qa" / "suite.json").read_text(encoding="utf-8"))
        self.assertNotIn("added-here", [case["id"] for case in kept["cases"]])

    def test_adding_the_same_name_twice_is_refused(self) -> None:
        self.call("POST", "/api/qa/add", {"starter": "page-opens", "name": "twice"})
        status, body = self.call("POST", "/api/qa/add", {"starter": "page-opens", "name": "twice"})
        self.assertEqual(status, 400)
        self.assertIn("already holds", body["error"])

    def test_removing_something_that_is_not_there_says_so(self) -> None:
        status, body = self.call("POST", "/api/qa/remove", {"case": "never-existed"})
        self.assertEqual(status, 400)
        self.assertIn("never-existed", body["error"])

    def test_a_ready_made_check_that_does_not_exist_is_refused(self) -> None:
        status, body = self.call("POST", "/api/qa/add", {"starter": "not-a-real-one"})
        self.assertEqual(status, 400)
        self.assertIn("page-opens", body["error"])

    def test_recording_may_not_be_pointed_at_another_machine(self) -> None:
        status, body = self.call("POST", "/api/qa/record", {"url": "http://example.com/"})
        self.assertEqual(status, 400)
        self.assertIn("may not open", body["error"])


class RunPictureTests(unittest.TestCase):
    """Pictures a check kept can be looked at, and nothing else can."""

    def setUp(self) -> None:
        import threading

        from our_harness.server import HarnessHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        run = self.root / ".harness" / "qa" / "runs" / "20260101-000001" / "a-check"
        run.mkdir(parents=True)
        (run / "step-02-went-wrong.png").write_bytes(b"\x89PNG\r\n\x1a\nnot really a picture")
        (run / "attempt-1.txt").write_text("what the check saw", encoding="utf-8")
        (self.root / "secret.png").write_bytes(b"not for the panel")
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

    def fetch(self, path: str, token: bool = True):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if token:
            headers["X-Harness-Token"] = self.server.token
        try:
            connection.request("GET", path, None, headers)
            answer = connection.getresponse()
            return answer.status, answer.read()
        finally:
            connection.close()

    def test_a_picture_from_a_run_can_be_looked_at(self) -> None:
        status, raw = self.fetch(
            "/api/qa/picture?path=20260101-000001/a-check/step-02-went-wrong.png"
        )
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\x89PNG"))

    def test_nothing_else_on_the_machine_can_be_reached(self) -> None:
        for wanted in (
            "../../../secret.png",
            "..%2F..%2F..%2Fsecret.png",
            "/etc/passwd.png",
            "20260101-000001/a-check/attempt-1.txt",
            "20260101-000001/a-check/../../../../secret.png",
        ):
            with self.subTest(path=wanted):
                status, _raw = self.fetch(f"/api/qa/picture?path={wanted}")
                self.assertIn(status, (400, 404), wanted)

    def test_a_picture_that_is_not_there_says_so(self) -> None:
        status, _raw = self.fetch("/api/qa/picture?path=20260101-000001/a-check/never.png")
        self.assertEqual(status, 404)

    def test_the_session_key_is_needed(self) -> None:
        status, _raw = self.fetch(
            "/api/qa/picture?path=20260101-000001/a-check/step-02-went-wrong.png", token=False
        )
        self.assertEqual(status, 400)


class ThirdRoundFixTests(unittest.TestCase):
    """The last three the judge proved: a redirect, an interpreter, a secret."""

    def test_a_walk_refuses_a_redirect_off_the_allowed_hosts(self) -> None:
        from our_harness import qa

        case = qa.parse_suite({"name": "d", "cases": [{
            "id": "walk", "kind": "crawl", "url": "http://127.0.0.1:9921/",
        }]}).cases[0]
        found = qa.crawl_reasons(case, {
            "pages": [{"url": "http://127.0.0.1:9921/", "status": 200}],
            "refused": ["http://127.0.0.2:9922/secret"],
        })
        self.assertIn("may not open", found[0])
        self.assertIn("127.0.0.2", found[0])

    def test_the_page_is_told_to_fetch_nothing_from_another_host(self) -> None:
        from our_harness import qa

        script = qa.browser_script({
            "url": "http://127.0.0.1:1/", "routes": [], "steps": [],
            "crawl": {"maxPages": 3, "stayUnder": "http://127.0.0.1:1/", "allowedHosts": ["127.0.0.1"]},
        })
        # Every request is looked at, and where the browser landed is checked
        # as well, so a redirect cannot slip past a comparison of link text.
        self.assertIn("page.route('**/*'", script)
        self.assertIn("route.abort()", script)
        self.assertIn("const landed = page.url()", script)

    def test_a_one_liner_in_any_interpreter_cannot_run_a_denied_program(self) -> None:
        from our_harness.execution import CommandRunner

        runner = CommandRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}))
        for argv in (
            ["python", "-c", "import os; os.system('diskpart')"],
            ["python3", "-c", "__import__('os').system('fdisk')"],
            ["node", "-e", "require('child_process').execSync('diskpart')"],
            ["perl", "-e", "system('mkfs /dev/sda')"],
            ["ruby", "-e", "system('shutdown')"],
            ["php", "-r", "system('diskpart');"],
        ):
            with self.subTest(argv=argv[:2]), self.assertRaises(HarnessError):
                runner._check(argv)

    def test_ordinary_code_with_the_word_format_in_it_still_runs(self) -> None:
        from our_harness.execution import CommandRunner

        runner = CommandRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}))
        for argv in (
            ["python", "-c", "print('{}'.format(1))"],
            ["python", "-m", "pytest", "-q"],
            ["npm", "run", "format"],
            ["node", "build.js"],
            ["git", "commit", "-m", "format the code"],
        ):
            with self.subTest(argv=argv[:3]):
                runner._check(argv)

    def test_the_shapes_of_secret_that_used_to_slip_through_are_hidden(self) -> None:
        from our_harness.redaction import CredentialRedactor

        remover = CredentialRedactor(None)
        cases = {
            "token=abc123SECRETTOKENvalue999": "abc123SECRETTOKENvalue999",
            "auth_token: abc123SECRETTOKENvalue999": "abc123SECRETTOKENvalue999",
            'DB = "postgres://user:hunterhunter2@host/db"': "hunterhunter2",
            'aws_secret_access_key = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"': "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
            "X-Auth-Token: abcdefghijklmnop": "abcdefghijklmnop",
        }
        for line, secret in cases.items():
            with self.subTest(line=line):
                cleaned = remover.text(line)
                self.assertNotIn(secret, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_ordinary_words_are_not_mangled(self) -> None:
        from our_harness.redaction import CredentialRedactor

        remover = CredentialRedactor(None)
        for line in (
            "the token ring network was slow",
            "password rules are written in the docs",
            "https://example.com/docs/passwords",
            "See the authorisation policy for details",
        ):
            with self.subTest(line=line):
                self.assertEqual(remover.text(line), line)

    def test_nothing_a_check_saw_reaches_a_model_in_any_of_those_shapes(self) -> None:
        from our_harness import handover

        evidence = (
            "token=abc123SECRETTOKENvalue999\n"
            "DB = postgres://user:hunterhunter2@host/db\n"
            'aws_secret_access_key = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"'
        )
        question = handover.failure_question(
            {"id": "a", "title": "A check", "kind": "command", "reasons": ["boom"]}, evidence
        )
        for secret in ("abc123SECRETTOKENvalue999", "hunterhunter2", "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"):
            self.assertNotIn(secret, question)
        self.assertIn("boom", question)
