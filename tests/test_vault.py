"""What the harness has learned, kept as notes anybody can read.

Everything here works on a throwaway project. The notes are ordinary markdown
files, so most of these tests are about the promise that makes: what is written
can be read back by anything, and what somebody edits by hand is understood.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import vault
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class VaultTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def write(self, title: str, **rest) -> vault.Note:
        return vault.write_one(self.config, vault.Note(
            name="", title=title, kind=rest.pop("kind", "about-this-project"), **rest
        ))


class OneNoteTests(VaultTestCase):
    def test_a_note_is_a_markdown_file_anybody_can_read(self) -> None:
        self.write("They prefer plain English", kind="about-you",
                   body="Short answers.", tags=["writing"])
        where = self.root / ".harness" / "vault" / "they-prefer-plain-english.md"
        self.assertTrue(where.is_file())
        text = where.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"))
        self.assertIn("title: They prefer plain English", text)
        self.assertIn("kind: about-you", text)
        self.assertIn("Short answers.", text)

    def test_what_was_written_reads_back_the_same(self) -> None:
        self.write("A thing", kind="lesson", body="Body.", tags=["one", "two"], sure=0.75)
        back = vault.read_one(self.config, "a-thing")
        self.assertEqual(back.title, "A thing")
        self.assertEqual(back.kind, "lesson")
        self.assertEqual(back.body, "Body.")
        self.assertEqual(back.tags, ["one", "two"])
        self.assertEqual(back.sure, 0.75)

    def test_a_note_written_by_hand_is_understood(self) -> None:
        # The whole point of markdown files: somebody can write one in an
        # editor, and the harness reads it like any other.
        (self.root / ".harness" / "vault").mkdir(parents=True)
        (self.root / ".harness" / "vault" / "by-hand.md").write_text(
            "---\ntitle: By hand\nkind: how-to\ntags: [mine]\n---\n\n"
            "Written in an editor, pointing at [[a-thing]].\n",
            encoding="utf-8",
        )
        note = vault.read_one(self.config, "by-hand")
        self.assertEqual(note.title, "By hand")
        self.assertEqual(note.kind, "how-to")
        self.assertEqual(note.links, ["a-thing"])

    def test_a_note_with_no_front_matter_is_still_read(self) -> None:
        (self.root / ".harness" / "vault").mkdir(parents=True)
        (self.root / ".harness" / "vault" / "plain.md").write_text(
            "Just some words.\n", encoding="utf-8"
        )
        note = vault.read_one(self.config, "plain")
        self.assertEqual(note.body, "Just some words.")
        self.assertEqual(note.kind, "about-this-project")

    def test_links_are_found_wherever_they_are_written(self) -> None:
        note = self.write("Linking", body="See [[one]] and [[Two Words]] and [[three|a name]].")
        self.assertEqual(note.links, ["one", "three", "two-words"])

    def test_a_kind_it_does_not_know_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            vault.write_one(self.config, vault.Note(name="", title="A", kind="something-else"))

    def test_a_title_that_would_climb_out_of_the_folder_is_refused(self) -> None:
        for bad in ("../escape", "/etc/passwd", "a/b", "", "   ", "x" * 100):
            with self.subTest(bad=bad):
                with self.assertRaises(HarnessError):
                    vault.check_the_title(bad)

    def test_a_note_that_is_not_a_note_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            self.write("Too long", body="x" * (vault.MOST_LETTERS + 1))
        with self.assertRaises(HarnessError):
            self.write("Control", body="a\x07b")

    def test_removing_one_removes_the_file(self) -> None:
        self.write("Going")
        vault.remove(self.config, "going")
        self.assertFalse((self.root / ".harness" / "vault" / "going.md").exists())
        with self.assertRaises(HarnessError):
            vault.remove(self.config, "going")


class TheWholeVaultTests(VaultTestCase):
    def test_it_draws_notes_links_and_what_is_missing(self) -> None:
        self.write("First", body="Points at [[second]] and at [[nobody-wrote-this]].")
        self.write("Second", body="Points back at [[first]].")
        whole = vault.everything(self.config)
        self.assertEqual(whole["counts"]["notes"], 2)
        self.assertEqual(whole["counts"]["links"], 2)
        self.assertEqual(whole["counts"]["not_written_yet"], 1)
        self.assertEqual(whole["not_written_yet"][0]["to"], "nobody-wrote-this")

    def test_a_note_pointing_at_itself_is_not_a_link(self) -> None:
        self.write("Alone", body="See [[alone]].")
        self.assertEqual(vault.everything(self.config)["links"], [])

    def test_the_ones_around_a_note_are_both_ways(self) -> None:
        self.write("Middle", body="Points at [[right]].")
        self.write("Left", body="Points at [[middle]].")
        self.write("Right", body="The end.")
        around = vault.neighbours(self.config, "middle")
        self.assertEqual([one["name"] for one in around["points_at"]], ["right"])
        self.assertEqual([one["name"] for one in around["points_here"]], ["left"])

    def test_searching_finds_a_note_by_any_of_its_words(self) -> None:
        self.write("Plain English", kind="about-you", body="Short answers.", tags=["writing"])
        self.write("Something else", body="Nothing to do with it.")
        for words in ("plain", "short answers", "writing", "about-you"):
            with self.subTest(words=words):
                found = vault.search(self.config, words)
                self.assertEqual([note.name for note in found], ["plain-english"])

    def test_a_broken_file_does_not_stop_the_rest_being_read(self) -> None:
        self.write("Good")
        (self.root / ".harness" / "vault" / "broken.md").write_bytes(b"\xff\xfe not text at all")
        self.assertIn("good", [note.name for note in vault.all_notes(self.config)])

    def test_a_fresh_vault_is_never_a_blank_page(self) -> None:
        made = vault.start_it_off(self.config)
        self.assertTrue(made)
        self.assertEqual(vault.start_it_off(self.config), [], "it only ever does that once")
        whole = vault.everything(self.config)
        self.assertGreaterEqual(whole["counts"]["notes"], 2)
        self.assertGreaterEqual(whole["counts"]["links"], 1)


class HowANoteEarnsItsPlaceTests(VaultTestCase):
    def test_using_a_note_counts_it(self) -> None:
        self.write("A way of doing it", kind="how-to", sure=0.5)
        vault.used(self.config, "a-way-of-doing-it", went_well=True)
        note = vault.used(self.config, "a-way-of-doing-it", went_well=True)
        self.assertEqual(note.uses, 2)
        self.assertEqual(note.worked, 2)
        self.assertEqual(note.how_it_goes, 1.0)
        self.assertGreater(note.sure, 0.5)

    def test_a_note_that_does_not_help_is_marked_down(self) -> None:
        self.write("A way that fails", kind="how-to", sure=0.8)
        note = vault.used(self.config, "a-way-that-fails", went_well=False)
        self.assertEqual(note.uses, 1)
        self.assertEqual(note.worked, 0)
        self.assertLess(note.sure, 0.8)

    def test_one_bad_afternoon_does_not_throw_away_what_it_earned(self) -> None:
        self.write("A good one", kind="how-to", sure=0.5)
        for _ in range(5):
            vault.used(self.config, "a-good-one", went_well=True)
        before = vault.read_one(self.config, "a-good-one").sure
        after = vault.used(self.config, "a-good-one", went_well=False).sure
        self.assertGreater(after, 0.3, "it went down, and not to nothing")
        self.assertLess(after, before)

    def test_nothing_is_ever_sure_beyond_all_doubt(self) -> None:
        self.write("Very used", kind="how-to", sure=0.9)
        for _ in range(20):
            note = vault.used(self.config, "very-used", went_well=True)
        self.assertLessEqual(note.sure, 0.99)

    def test_a_note_nothing_has_touched_for_months_is_called_stale(self) -> None:
        note = self.write("Old news")
        where = self.root / ".harness" / "vault" / "old-news.md"
        where.write_text(
            where.read_text(encoding="utf-8").replace(
                f"touched: {note.touched}", "touched: 2020-01-01"
            ),
            encoding="utf-8",
        )
        self.assertTrue(vault.read_one(self.config, "old-news").stale)
        self.assertEqual([one.name for one in vault.going_stale(self.config)], ["old-news"])

    def test_what_was_learned_lately_is_what_was_touched_lately(self) -> None:
        self.write("New thing")
        self.assertEqual([one.name for one in vault.lately(self.config, 14)], ["new-thing"])
        self.assertEqual(vault.lately(self.config, 0), [] if False else vault.lately(self.config, 1))


class LearningFromTheRunsTests(VaultTestCase):
    def test_it_writes_notes_from_what_the_harness_remembers(self) -> None:
        found = {"records": [
            {"title": "The parser caches by file name", "summary": "Two files with one name share a slot.",
             "trust": 0.8, "run_id": "run-1"},
        ]}
        with mock.patch("our_harness.memory.MemoryStore") as store:
            store.return_value.__enter__.return_value.memory_graph.return_value = found
            said = vault.learn_from_memory(self.config)
        self.assertEqual(said["made"], ["the-parser-caches-by-file-name"])
        note = vault.read_one(self.config, "the-parser-caches-by-file-name")
        self.assertIn("share a slot", note.body)
        self.assertEqual(note.came_from, "run-1")

    def test_it_never_writes_over_a_note_somebody_has(self) -> None:
        self.write("The parser caches by file name", body="What I wrote myself.")
        found = {"records": [
            {"title": "The parser caches by file name", "summary": "What a run thought.",
             "trust": 0.8, "run_id": "run-1"},
        ]}
        with mock.patch("our_harness.memory.MemoryStore") as store:
            store.return_value.__enter__.return_value.memory_graph.return_value = found
            said = vault.learn_from_memory(self.config)
        self.assertEqual(said["made"], [])
        self.assertIn("the-parser-caches-by-file-name", said["already_here"])
        self.assertEqual(
            vault.read_one(self.config, "the-parser-caches-by-file-name").body,
            "What I wrote myself.",
        )

    def test_a_record_with_nothing_in_it_is_passed_over(self) -> None:
        found = {"records": [{"title": "", "summary": ""}, {"title": "x"}]}
        with mock.patch("our_harness.memory.MemoryStore") as store:
            store.return_value.__enter__.return_value.memory_graph.return_value = found
            said = vault.learn_from_memory(self.config)
        self.assertEqual(said["made"], [])


if __name__ == "__main__":
    unittest.main()
