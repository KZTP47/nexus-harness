"""No check may press a button that changes the machine it is running on.

The panel checks drive the real panel, against the real project, because that
is the only way to prove a button works. That is fine for a button that reads,
and not fine at all for "I don't care, just do it for me", which really starts
programs, fetches models, and writes to your settings.

It has gone wrong once already: a check pressed the Ollama card, which started
a server that was not running and wrote a route into a real settings file.

So the rule is written down here rather than left as care: the check that
presses that button may only press one that cannot get as far as writing. Which
ones those are is worked out from the code — the ways of connecting a model
that need a key, because nobody can make a key for you and the job stops at the
first step without one — not from a list somebody has to remember to update.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from our_harness import autosetup

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"
THE_BUTTON = "just do it for me"


def cannot_write_without_a_person() -> set[str]:
    """The ways of connecting a model that stop before writing anything.

    A service reached with a key: no key in the terminal, no route written, and
    nobody but the person can put one there.
    """

    return {option for option, plan in autosetup.PLANS.items() if plan.key_name}


def can_change_this_machine() -> set[str]:
    """The rest: pressing one of these really does something to the machine."""

    return set(autosetup.PLANS) - cannot_write_without_a_person()


def checks_that_press_it():
    cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        scripts = [step.get("script", "") for step in case.get("steps", [])]
        if any(THE_BUTTON in script for script in scripts):
            yield case["id"], "\n".join(scripts)


class NoCheckChangesTheMachineTests(unittest.TestCase):
    def test_there_is_a_check_that_presses_it(self) -> None:
        # Without this, deleting the check would make the rest of this file
        # pass by having nothing to look at.
        self.assertTrue(list(checks_that_press_it()))

    def test_the_ones_that_could_change_the_machine_are_named_and_kept_out(self) -> None:
        # Ollama and the signed-in tools start programs and write routes. A
        # check may name them only to stay away from them, never to press them.
        for case_id, script in checks_that_press_it():
            with self.subTest(case=case_id):
                safe = sorted(cannot_write_without_a_person())
                self.assertTrue(
                    all(option in script for option in safe),
                    f"{case_id} has to say which options it is willing to press: {safe}",
                )
                for risky in sorted(can_change_this_machine()):
                    self.assertNotIn(
                        f"'{risky}'", script,
                        f"{case_id} must not choose {risky}: pressing it really changes "
                        "this machine. Only a service waiting for a key is safe to press.",
                    )

    def test_the_safe_list_is_not_empty(self) -> None:
        # If every way of connecting a model became one that writes, the check
        # would have nothing safe to press and this rule would quietly pass by
        # being vacuous.
        self.assertTrue(cannot_write_without_a_person())

    def test_a_key_that_is_missing_really_does_stop_before_writing(self) -> None:
        # The rule above leans on this being true, so it is checked rather than
        # believed: a hosted plan with no key writes nothing at all.
        import copy
        import tempfile
        from unittest import mock

        from our_harness.config import DEFAULT_CONFIG, LoadedConfig

        for option in sorted(cannot_write_without_a_person()):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                (root / ".harness").mkdir()
                config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
                with mock.patch.dict(autosetup.os.environ, {}, clear=True):
                    job = autosetup.do_it(config, option)
                self.assertFalse(job.worked)
                self.assertFalse(
                    (root / ".harness" / "config.local.json").exists(),
                    f"{option} wrote something without a key",
                )


if __name__ == "__main__":
    unittest.main()
