"""Changing a setting without opening a file.

Every test here works in a throwaway project and then reads the settings back
with the real config reader, because the promise this makes is exactly that:
what you change is what the harness will read at the start of the next run.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import settings
from our_harness.config import (
    DEFAULT_CONFIG,
    LoadedConfig,
    is_project_local_config_trusted,
    load_config,
)
from our_harness.models import HarnessError


class SettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    @property
    def shared(self) -> Path:
        return self.root / ".harness" / "config.json"

    @property
    def yours(self) -> Path:
        return self.root / ".harness" / "config.local.json"

    def read_back(self) -> LoadedConfig:
        return load_config(self.root)


class EverySettingIsOfferedTests(SettingsTestCase):
    def test_nothing_the_harness_has_is_missing_from_the_view(self) -> None:
        # A hand-written list of settings goes stale the first time somebody
        # adds one, and a setting nobody can see is a setting nobody can
        # change. So the list is the harness's own settings, read out.
        offered = {one.key for one in settings.everything(self.config)}

        def leaves(held, above=""):
            for name, value in held.items():
                dotted = f"{above}.{name}" if above else name
                if isinstance(value, dict) and value:
                    yield from leaves(value, dotted)
                else:
                    yield dotted

        missing = sorted(set(leaves(DEFAULT_CONFIG)) - offered)
        self.assertEqual(missing, [], f"settings nobody can see: {missing}")

    def test_there_are_plenty_of_them(self) -> None:
        self.assertGreater(len(settings.everything(self.config)), 50)

    def test_each_one_says_what_it_is_and_where_it_came_from(self) -> None:
        for one in settings.everything(self.config):
            with self.subTest(key=one.key):
                self.assertTrue(one.label)
                self.assertTrue(one.group)
                self.assertTrue(one.came_from)
                self.assertIn(one.kind, ("yes or no", "number", "list", "text", "settings of its own"))

    def test_the_ones_people_reach_for_are_in_plain_words(self) -> None:
        said = {one.key: one.means for one in settings.everything(self.config)}
        for key in ("qa.workers", "provider.model", "project.test_commands", "git.allow_push"):
            with self.subTest(key=key):
                self.assertTrue(said.get(key), f"{key} has no plain words")

    def test_a_setting_that_is_not_as_it_shipped_is_marked(self) -> None:
        settings.change(self.config, "memory.enabled", "no")
        after = {one.key: one for one in settings.everything(self.read_back())}
        self.assertTrue(after["memory.enabled"].changed)
        self.assertFalse(after["qa.workers"].changed)
        self.assertIn("shared settings file", after["memory.enabled"].came_from)


class ChangingOneTests(SettingsTestCase):
    def test_a_change_is_what_the_harness_reads_next(self) -> None:
        settings.change(self.config, "qa.default_timeout_seconds", "45")
        self.assertEqual(self.read_back().get("qa.default_timeout_seconds"), 45)

    def test_yes_and_no_can_be_written_the_way_people_say_them(self) -> None:
        for said, meant in (("no", False), ("yes", True), ("false", False), ("on", True)):
            with self.subTest(said=said):
                settings.change(self.config, "memory.enabled", said)
                self.assertIs(self.read_back().get("memory.enabled"), meant)

    def test_a_command_can_be_typed_the_way_it_is_typed_in_a_terminal(self) -> None:
        # Nobody should have to know it is kept as a list of lists.
        settings.change(self.config, "project.test_commands", "pytest -q")
        self.assertEqual(self.read_back().get("project.test_commands"), [["pytest", "-q"]])

    def test_more_than_one_command_is_one_per_line(self) -> None:
        settings.change(self.config, "project.test_commands", "pytest -q\nnpm test")
        self.assertEqual(
            self.read_back().get("project.test_commands"),
            [["pytest", "-q"], ["npm", "test"]],
        )

    def test_something_that_is_not_a_number_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            settings.change(self.config, "qa.workers", "lots")
        self.assertIn("whole number", str(caught.exception))
        self.assertFalse(self.shared.exists())

    def test_a_setting_that_is_not_a_setting_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            settings.change(self.config, "qa.made_up", "1")

    def test_a_change_the_harness_refuses_is_put_straight_back(self) -> None:
        # The real gate: written, read back the way a run reads it, and undone
        # here and now if the harness will not have it.
        self.shared.write_text(json.dumps({"memory": {"enabled": True}}, indent=2), encoding="utf-8")
        before = self.shared.read_text(encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            settings.change(self.config, "provider.temperature", "9000")
        self.assertIn("put back", str(caught.exception))
        self.assertEqual(self.shared.read_text(encoding="utf-8"), before)
        self.read_back()  # still readable, which is the point

    def test_one_that_only_counts_from_your_own_file_goes_there(self) -> None:
        done = settings.change(self.config, "git.allow_push", "yes")
        self.assertTrue(done.file.endswith("config.local.json"))
        self.assertIs(self.read_back().get("git.allow_push"), True)
        self.assertTrue(is_project_local_config_trusted(self.root, self.yours))

    def test_one_that_can_only_be_raised_from_your_own_file_finds_its_way(self) -> None:
        # A limit the shared file may lower and only your own file may raise.
        # Nobody should have to know that: the change is tried in the shared
        # file, refused, and tried again in yours.
        done = settings.change(self.config, "qa.workers", "6")
        self.assertTrue(done.file.endswith("config.local.json"), done.note)
        self.assertIn("only counts from your own file", done.note)
        self.assertEqual(self.read_back().get("qa.workers"), 6)

    def test_it_will_not_quietly_trust_a_file_somebody_else_left(self) -> None:
        self.yours.write_text(json.dumps({"memory": {"enabled": True}}), encoding="utf-8")
        done = settings.change(self.config, "git.allow_commit", "yes")
        self.assertTrue(done.needs_trusting)
        self.assertIn("say the file is yours", done.note)
        self.assertFalse(is_project_local_config_trusted(self.root, self.yours))

    def test_a_settings_file_that_cannot_be_read_is_left_alone(self) -> None:
        self.shared.write_text("{ not json at all", encoding="utf-8")
        with self.assertRaises(HarnessError):
            settings.change(self.config, "memory.enabled", "no")
        self.assertEqual(self.shared.read_text(encoding="utf-8"), "{ not json at all")


class PuttingOneBackTests(SettingsTestCase):
    def test_it_goes_back_to_how_it_shipped(self) -> None:
        shipped = self.config.get("qa.default_timeout_seconds")
        settings.change(self.config, "qa.default_timeout_seconds", "45")
        said = settings.reset(self.config, "qa.default_timeout_seconds")
        self.assertIn("back to how it shipped", said.note)
        self.assertEqual(self.read_back().get("qa.default_timeout_seconds"), shipped)

    def test_putting_back_one_that_was_never_changed_says_so(self) -> None:
        said = settings.reset(self.config, "qa.workers")
        self.assertIn("already as it shipped", said.note)

    def test_it_leaves_everything_else_alone(self) -> None:
        settings.change(self.config, "memory.enabled", "no")
        settings.change(self.config, "qa.default_timeout_seconds", "45")
        settings.reset(self.config, "qa.default_timeout_seconds")
        self.assertIs(self.read_back().get("memory.enabled"), False)

    def test_changing_then_putting_back_leaves_the_file_as_it_was(self) -> None:
        # What the panel's own check leans on: a setting nobody has set can be
        # changed and put back, and the files end up exactly as they started.
        self.shared.write_text(
            json.dumps({"memory": {"enabled": True}}, indent=2) + "\n", encoding="utf-8"
        )
        before = self.shared.read_text(encoding="utf-8")
        settings.change(self.config, "qa.default_timeout_seconds", "45")
        settings.reset(self.config, "qa.default_timeout_seconds")
        self.assertEqual(json.loads(self.shared.read_text(encoding="utf-8")),
                         json.loads(before))


class WhichFileTests(SettingsTestCase):
    def test_the_list_of_yours_only_settings_matches_what_the_config_refuses(self) -> None:
        # Held against the config reader rather than remembered: it names the
        # settings it will not honour from a shared file, and this has to know
        # the same ones or somebody is sent to the wrong file.
        import re

        source = (
            Path(__file__).resolve().parents[1] / "src" / "our_harness" / "config.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _validate_capability_provenance(")
        after = source.find("\ndef ", start + 1)
        gate = source[start:after if after > 0 else len(source)]
        named = set()
        for match in re.finditer(r'project_controls\("([a-z_.]+)"\)', gate):
            named.add(match.group(1))
        known = set(settings.ONLY_FROM_YOUR_OWN_FILE)
        missing = sorted(
            key for key in named
            if not any(key == one or key.startswith(f"{one}.") for one in known)
        )
        # A few are about lowering a trusted floor rather than needing your own
        # file, and those find their way by being tried. Anything else has to
        # be named, so nobody is sent to a file the harness will ignore.
        allowed_to_find_their_own_way = {
            "execution.deny_executables", "execution.deny_argument_sequences",
            "git.protected_branches", "git.required_branch_prefix", "git.enabled",
            "workflow.reviewers", "workflow.review_parallelism", "workflow.name",
            "workflow.context_tool_execution_seconds",
            "provider.name", "provider.model",
        }
        self.assertEqual(sorted(set(missing) - allowed_to_find_their_own_way), [])


if __name__ == "__main__":
    unittest.main()
