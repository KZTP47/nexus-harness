"""The vault through the panel, not through the library.

The library tests drive the notes directly. That left the part between the
button and the notes untested, and two of the worst bugs in this feature lived
exactly there: changing a note's title left the old file behind and the vault
quietly held two, and asking for a note that had been removed took the whole
view down instead of clearing one panel.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import server, vault
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class PanelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        threading.Thread(target=self.panel.serve_forever, daemon=True).start()
        self.addCleanup(self.panel.shutdown)
        self.config = config

    def ask(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "X-Harness-Token": self.panel.token},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ReadingTheVaultTests(PanelTestCase):
    def test_looking_at_the_vault_writes_nothing(self) -> None:
        # Opening a tab must never leave files behind in somebody's project.
        # It used to write two notes on the way past.
        status, said = self.ask("/api/vault")
        self.assertEqual(status, 200)
        self.assertEqual(said["notes"], [])
        self.assertFalse((self.root / ".harness" / "vault").exists())

    def test_it_writes_the_first_notes_when_asked_to(self) -> None:
        status, said = self.ask("/api/vault/start", {})
        self.assertEqual(status, 200)
        self.assertTrue(said["made"])
        self.assertTrue((self.root / ".harness" / "vault").is_dir())
        _status, again = self.ask("/api/vault/start", {})
        self.assertEqual(again["made"], [], "it only ever does that once")

    def test_a_note_that_has_gone_does_not_take_the_view_with_it(self) -> None:
        self.ask("/api/vault/write", {"title": "Here now", "kind": "lesson", "body": "x"})
        status, said = self.ask("/api/vault?name=gone-in-an-editor")
        self.assertEqual(status, 200, "the whole vault still comes back")
        self.assertIsNone(said["open"])
        self.assertEqual(said["gone"], "gone-in-an-editor")
        self.assertEqual(len(said["notes"]), 1)

    def test_a_large_vault_is_sifted_where_it_lives(self) -> None:
        self.ask("/api/vault/write", {"title": "Payment things", "kind": "lesson", "body": "About money."})
        self.ask("/api/vault/write", {"title": "Other things", "kind": "lesson", "body": "About nothing."})
        _status, said = self.ask("/api/vault?q=money")
        self.assertEqual([note["title"] for note in said["notes"]], ["Payment things"])
        self.assertEqual(said["searched_for"], "money")


class ReadingAndWritingAtOnceTests(PanelTestCase):
    """A read must wait for a write, and a write must wait for a read.

    Reading the vault is several passes over the same folder. A write landing
    between two of them answers with a vault that never existed: a note in the
    picture and not in the list, or - while a change of title is halfway done,
    with the new file written and the old one not yet taken away - the same
    note twice.
    """

    def test_a_write_waits_while_a_read_is_going_on(self) -> None:
        self.ask("/api/vault/write", {"title": "Already here", "kind": "lesson", "body": "x"})
        reading = threading.Event()
        let_go = threading.Event()
        real = vault.going_stale

        def slowly(config):
            reading.set()
            let_go.wait(timeout=10)
            return real(config)

        with mock.patch.object(vault, "going_stale", slowly):
            reader = threading.Thread(target=lambda: self.ask("/api/vault"))
            reader.start()
            self.addCleanup(reader.join)
            self.assertTrue(reading.wait(timeout=10), "the read never started")

            wrote = threading.Event()

            def write() -> None:
                self.ask("/api/vault/write", {"title": "New one", "kind": "lesson", "body": "y"})
                wrote.set()

            writer = threading.Thread(target=write)
            writer.start()
            self.addCleanup(writer.join)
            self.assertFalse(
                wrote.wait(timeout=1.5),
                "a note was written while the vault was being read, so the read "
                "could answer with a vault that never existed",
            )
            let_go.set()
            self.assertTrue(wrote.wait(timeout=10), "the write never finished")
        reader.join(timeout=10)
        writer.join(timeout=10)
        _status, whole = self.ask("/api/vault")
        self.assertEqual(len(whole["notes"]), 2)


class WritingThroughThePanelTests(PanelTestCase):
    def test_changing_a_title_moves_the_note_rather_than_copying_it(self) -> None:
        # The one a person hits first: open a note, fix a typo in the title,
        # save. It used to leave the old file behind and make a second note.
        _status, first = self.ask(
            "/api/vault/write", {"title": "Paymnet notes", "kind": "lesson", "body": "One."}
        )
        status, second = self.ask("/api/vault/write", {
            "was": first["note"]["name"],
            "title": "Payment notes", "kind": "lesson", "body": "One.",
        })
        self.assertEqual(status, 200)
        self.assertEqual(second["note"]["name"], "payment-notes")
        _status, whole = self.ask("/api/vault")
        self.assertEqual([note["name"] for note in whole["notes"]], ["payment-notes"])

    def test_two_titles_that_share_one_file_name_are_refused(self) -> None:
        self.ask("/api/vault/write", {"title": "Payment notes", "kind": "lesson", "body": "Mine."})
        status, said = self.ask(
            "/api/vault/write", {"title": "Payment  notes", "kind": "how-to", "body": "Theirs."}
        )
        self.assertEqual(status, 400)
        self.assertIn("already a note", said["error"])
        _status, whole = self.ask("/api/vault")
        self.assertEqual(len(whole["notes"]), 1)
        self.assertEqual(whole["notes"][0]["body"], "Mine.")

    def test_saving_the_same_note_again_is_not_a_collision(self) -> None:
        _status, first = self.ask(
            "/api/vault/write", {"title": "A note", "kind": "lesson", "body": "One."}
        )
        status, _said = self.ask("/api/vault/write", {
            "was": first["note"]["name"], "title": "A note", "kind": "lesson", "body": "Two.",
        })
        self.assertEqual(status, 200)
        _status, whole = self.ask("/api/vault")
        self.assertEqual(whole["notes"][0]["body"], "Two.")

    def test_a_title_that_would_climb_out_of_the_folder_is_refused(self) -> None:
        for bad in ("../escape", "/etc/passwd", "a/b", "CON", "..", "x" * 200):
            with self.subTest(bad=bad):
                status, _said = self.ask(
                    "/api/vault/write", {"title": bad, "kind": "lesson", "body": "x"}
                )
                self.assertEqual(status, 400)
        self.assertFalse(list((self.root / ".harness").glob("vault/*.md")))

    def test_saying_a_note_helped_is_counted(self) -> None:
        self.ask("/api/vault/write", {"title": "A way", "kind": "how-to", "body": "x"})
        _status, said = self.ask("/api/vault/used", {"name": "a-way", "went_well": True})
        self.assertEqual(said["note"]["uses"], 1)
        self.assertEqual(said["note"]["worked"], 1)

    def test_removing_a_note_removes_the_file(self) -> None:
        self.ask("/api/vault/write", {"title": "Going", "kind": "lesson", "body": "x"})
        status, _said = self.ask("/api/vault/remove", {"name": "going"})
        self.assertEqual(status, 200)
        self.assertFalse((self.root / ".harness" / "vault" / "going.md").exists())
        status, _said = self.ask("/api/vault/remove", {"name": "going"})
        self.assertEqual(status, 400, "and says so the second time")

    def test_everything_here_needs_the_token(self) -> None:
        for path, body in (
            ("/api/vault", None),
            ("/api/vault/write", {"title": "x", "kind": "lesson", "body": "x"}),
            ("/api/vault/remove", {"name": "x"}),
            ("/api/vault/used", {"name": "x", "went_well": True}),
            ("/api/vault/learn", {}),
            ("/api/vault/start", {}),
        ):
            with self.subTest(path=path):
                # Windows can occasionally abort one fresh loopback socket
                # itself (WinError 10053/10054), especially while the full
                # suite has several short-lived local servers.  That is a
                # transport interruption, not an authorization answer. Retry
                # that exact request once, but never retry an HTTP result.
                for attempt in range(2):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{self.port}{path}",
                        data=json.dumps(body).encode("utf-8") if body is not None else None,
                        headers={"Content-Type": "application/json", "Connection": "close"},
                        method="POST" if body is not None else "GET",
                    )
                    try:
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            urllib.request.urlopen(request, timeout=10)
                    except ConnectionError:
                        if attempt:
                            raise
                        continue
                    self.assertEqual(caught.exception.code, 400)
                    break


class LearningFromTheRunsThroughThePanelTests(PanelTestCase):
    def test_one_record_nobody_can_name_does_not_stop_the_others(self) -> None:
        # A run calls something "Bug: fixed a race", and a colon cannot be part
        # of a file name. That used to throw away every record after it and
        # report a bare failure, while the earlier ones were already written.
        found = {"records": [
            {"title": "A perfectly fine title", "summary": "One.", "trust": 0.6},
            {"title": "Bug: fixed a race condition", "summary": "Two.", "trust": 0.6},
            {"title": "Another fine title", "summary": "Three.", "trust": 0.6},
        ]}
        with mock.patch("our_harness.memory.MemoryStore") as store:
            store.return_value.__enter__.return_value.memory_graph.return_value = found
            status, said = self.ask("/api/vault/learn", {})
        self.assertEqual(status, 200)
        written = sorted(note.name for note in vault.all_notes(self.config))
        self.assertEqual(
            written, ["a-perfectly-fine-title", "another-fine-title", "bug-fixed-a-race-condition"]
        )
        self.assertEqual(len(said["made"]), 3)

    def test_a_title_nothing_can_be_made_of_is_passed_over(self) -> None:
        found = {"records": [
            {"title": "***", "summary": "One."},
            {"title": "A fine title", "summary": "Two."},
        ]}
        with mock.patch("our_harness.memory.MemoryStore") as store:
            store.return_value.__enter__.return_value.memory_graph.return_value = found
            status, said = self.ask("/api/vault/learn", {})
        self.assertEqual(status, 200)
        self.assertEqual(said["made"], ["a-fine-title"])
        self.assertEqual(len(said["passed_over"]), 1)
        self.assertIn("could not be turned into a note", said["note"])


if __name__ == "__main__":
    unittest.main()
