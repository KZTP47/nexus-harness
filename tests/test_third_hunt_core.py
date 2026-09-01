"""Bugs an independent reader found in the parts that keep a project safe.

Each of these was reproduced against the real code before it was fixed, and the
input here is the one that reproduced it.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness.changes import FileTransaction, atomic_write
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.execution import CommandRunner
from our_harness.models import ChangePlan, HarnessError
from our_harness.redaction import CredentialRedactor
from our_harness.safety import confined_path


class WindowsShortNameTests(unittest.TestCase):
    """GIT~1 opens .git, and a rule that knows one spelling has a door beside it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        (self.root / ".git").mkdir()

    def test_a_short_name_cannot_reach_the_git_folder(self) -> None:
        for spelled in ("GIT~1/hooks/pre-commit", "git~1/config", "GIT~1"):
            with self.subTest(spelled=spelled), self.assertRaises(HarnessError) as caught:
                confined_path(self.root, spelled)
            self.assertIn("short name", str(caught.exception))

    def test_a_short_name_cannot_reach_the_harness_folder(self) -> None:
        for spelled in ("HARNES~1/config.local.json", "harnes~1/memory.db"):
            with self.subTest(spelled=spelled), self.assertRaises(HarnessError):
                confined_path(self.root, spelled)

    def test_a_change_written_through_a_short_name_is_refused(self) -> None:
        # This installed a Git hook, which runs on the next commit, and it
        # rewrote the settings file the whole trust system rests on.
        for spelled in ("GIT~1/hooks/pre-commit", "HARNES~1/config.local.json"):
            with self.subTest(spelled=spelled), self.assertRaises(HarnessError):
                FileTransaction(self.root).apply(
                    [ChangePlan(path=spelled, content="owned", baseline_sha256=None)]
                )
        self.assertFalse((self.root / ".git" / "hooks").exists())

    def test_an_ordinary_name_with_a_squiggle_still_works(self) -> None:
        confined_path(self.root, "notes~draft.txt")
        confined_path(self.root, "a-file~with-a-dash.txt")


class ConsoleDeviceTests(unittest.TestCase):
    """Writing to the console throws the words away and says it worked."""

    def test_the_console_is_refused_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for spelled in ("conout$", "conin$", "CONOUT$", "folder/conin$"):
                with self.subTest(spelled=spelled), self.assertRaises(HarnessError):
                    confined_path(root, spelled)

    def test_a_change_written_to_the_console_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(HarnessError):
                FileTransaction(root).apply(
                    [ChangePlan(path="conout$", content="LOST DATA", baseline_sha256=None)]
                )


class LongFileNameTests(unittest.TestCase):
    def test_a_name_the_system_accepts_can_be_written(self) -> None:
        # Building the temporary name out of the real one made it too long, so
        # a file somebody could create by hand could not be written at all.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for length in (200, 240, 244, 250):
                name = "L" * (length - 4) + ".txt"
                with self.subTest(length=length):
                    atomic_write(root / name, b"written")
                    self.assertEqual((root / name).read_bytes(), b"written")
            self.assertEqual([p for p in root.iterdir() if p.suffix == ".tmp"], [])

    def test_a_long_name_goes_through_a_transaction_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            name = "L" * 240 + ".txt"
            FileTransaction(root).apply(
                [ChangePlan(path=name, content="hello", baseline_sha256=None)]
            )
            self.assertEqual((root / name).read_text(encoding="utf-8"), "hello")


class DeniedProgramTests(unittest.TestCase):
    """A denied program is denied however the command line packs it."""

    def runner(self, denied: list[str] | None = None) -> CommandRunner:
        data = copy.deepcopy(DEFAULT_CONFIG)
        if denied is not None:
            data["execution"]["deny_executables"] = denied
        return CommandRunner(LoadedConfig(data, Path.cwd(), [], {}))

    def test_a_name_packed_onto_its_own_switch_is_still_that_name(self) -> None:
        runner = self.runner(["danger", "whoami"])
        for argv in (
            ["python", "-cimport subprocess;subprocess.run(['danger.bat'])"],
            ["bash", "-cwhoami"],
            ["perl", "-ewhoami"],
            ["cmd", "/c danger.bat now"],
            ["powershell", "/c shutdown /r"],
        ):
            with self.subTest(argv=argv[1][:28]), self.assertRaises(HarnessError):
                runner._check(argv)

    def test_a_letter_hidden_behind_a_caret_is_still_that_letter(self) -> None:
        # Windows takes the caret out before it runs the line, so dan^ger is
        # danger by the time anything happens.
        with self.assertRaises(HarnessError):
            self.runner(["danger"])._check(["cmd", "/c", "dan^ger.bat", "now"])

    def test_ordinary_commands_still_run(self) -> None:
        runner = self.runner(["danger"])
        for argv in (
            ["python", "-m", "pytest", "-q"],
            ["python", "-c", "print('{}'.format(1))"],
            ["python", "-cprint(1)"],
            ["node", "build.js"],
            ["npm", "run", "format"],
            ["cmd", "/c", "echo hello"],
            ["bash", "-c", "ls -la"],
        ):
            with self.subTest(argv=argv[:2]):
                runner._check(argv)


class DeniedCommandShapeTests(unittest.TestCase):
    """A rule about a command means that command, however it was typed."""

    def runner(self) -> CommandRunner:
        return CommandRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}))

    def test_a_flag_in_the_middle_does_not_get_past_the_rule(self) -> None:
        # This one really destroyed uncommitted work in the reader's test repo.
        for argv in (
            ["git", "reset", "--hard"],
            ["git", "reset", "-q", "--hard"],
            ["git", "reset", "--quiet", "--hard", "HEAD"],
        ):
            with self.subTest(argv=argv), self.assertRaises(HarnessError):
                self.runner()._check(argv)

    def test_flags_written_apart_or_together_are_the_same_flags(self) -> None:
        for argv in (
            ["git", "clean", "-fd"],
            ["git", "clean", "-f", "-d"],
            ["git", "clean", "-d", "-f"],
        ):
            with self.subTest(argv=argv), self.assertRaises(HarnessError):
                self.runner()._check(argv)

    def test_the_short_way_of_writing_a_switch_counts_too(self) -> None:
        for argv in (["git", "push", "-f"], ["git", "push", "--force"]):
            with self.subTest(argv=argv), self.assertRaises(HarnessError):
                self.runner()._check(argv)

    def test_powershell_named_switches_are_not_mistaken_for_short_force(self) -> None:
        for argv in (
            ["powershell.exe", "-NoProfile", "-File", "script.ps1"],
            ["pwsh", "-NoProfile", "-InputFormat", "Text", "-File", "script.ps1"],
            [r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile", "-File", "script.ps1"],
        ):
            with self.subTest(argv=argv):
                self.runner()._check(argv)

    def test_destructive_git_clean_bundles_cannot_hide_force_or_directory(self) -> None:
        for switch in ("-xfd", "-xdf", "-nfd"):
            with self.subTest(switch=switch), self.assertRaises(HarnessError):
                self.runner()._check(["git", "clean", switch])

    def test_powershell_command_payload_returns_to_short_bundle_parsing(self) -> None:
        for argv in (
            ["powershell.exe", "-NoProfile", "-Command", "git clean -xfd"],
            ["pwsh", "-NoProfile", "-c", "git clean -xdf"],
            ["powershell.exe", "-NoProfile", "git clean -nfd"],
        ):
            with self.subTest(argv=argv), self.assertRaises(HarnessError):
                self.runner()._check(argv)

    def test_force_and_short_bundles_still_match_the_denied_switch(self) -> None:
        for switch in ("-Force", "-f", "-fd"):
            with self.subTest(switch=switch), self.assertRaises(HarnessError):
                self.runner()._check(["git", "push", switch, "origin", "main"])

    def test_ordinary_work_is_not_refused(self) -> None:
        for argv in (
            ["git", "status"],
            ["git", "log", "--oneline"],
            ["git", "clean", "-n"],
            ["git", "clean", "-nd"],
            ["git", "clean", "-nxd"],
            ["git", "commit", "-m", "reset the counter"],
            ["npm", "run", "build"],
            ["python", "-m", "pytest", "-q"],
        ):
            with self.subTest(argv=argv):
                self.runner()._check(argv)


class CredentialInSettingsFileTests(unittest.TestCase):
    """A value written on the lines below its name is still that value."""

    def setUp(self) -> None:
        self.remover = CredentialRedactor(None)

    def test_a_credential_under_its_name_is_hidden(self) -> None:
        for text, secret in (
            ("password: |\n  hunter2\n", "hunter2"),
            ("secret: |-\n  TOP-SECRET-1\n  second-line\n", "TOP-SECRET-1"),
            ("api_key: >\n  abcdefghij\n", "abcdefghij"),
            ("database:\n  password: |\n    SUPERSECRET-PW-123\n", "SUPERSECRET-PW-123"),
        ):
            with self.subTest(text=text[:24]):
                cleaned = self.remover.text(text)
                self.assertNotIn(secret, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_every_line_of_the_value_goes_not_just_the_first(self) -> None:
        cleaned = self.remover.text("password: |\n  one\n  two\n  three\n")
        for word in ("one", "two", "three"):
            self.assertNotIn(word, cleaned)

    def test_what_comes_after_the_value_is_kept(self) -> None:
        cleaned = self.remover.text("password: |\n  hunter2\nhost: localhost\n")
        self.assertIn("host: localhost", cleaned)

    def test_an_ordinary_block_of_words_is_left_alone(self) -> None:
        text = "notes: |\n  nothing secret here\n"
        self.assertEqual(self.remover.text(text), text)

    def test_it_reaches_what_is_written_down(self) -> None:
        from our_harness.memory import MemoryStore

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
            leaky = "database:\n  password: |\n    SUPERSECRET-PW-123\n"
            with MemoryStore(config) as memory:
                memory.add_episode("ns", "creds", leaky, {"note": leaky})
            written = json.dumps(
                [row for row in (root / ".harness").rglob("*") if row.is_file()], default=str
            )
            self.assertNotIn("SUPERSECRET-PW-123", written)
            for spot in (root / ".harness").rglob("*"):
                if spot.is_file():
                    with self.subTest(file=spot.name):
                        self.assertNotIn(
                            b"SUPERSECRET-PW-123", spot.read_bytes()
                        )


if __name__ == "__main__":
    unittest.main()
