"""What the panel warns about has to be what trusting a file really allows.

Before somebody trusts a settings file they did not write, the panel tells them
what trusting it would let happen. That warning is only worth having if it
covers everything trust unlocks. A hand-written list of dangers is right on the
day it is written and quietly wrong the first time the harness learns a new
trick, and the failure is silent in the worst direction: the panel telling
somebody there is nothing to worry about while handing over the very thing they
should have worried about.

So the list is held against the code that decides what needs trusting. The
config reader refuses a whole set of settings when they come from a file nobody
has trusted; every one of those sections has to appear in the warning.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from our_harness import seats

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "src" / "our_harness" / "config.py"

# The part of the config reader that decides what a file nobody trusts may not
# do. Everything named in there is a power that trusting hands over.
GATE = "_validate_capability_provenance"


def the_gate() -> str:
    text = CONFIG.read_text(encoding="utf-8")
    start = text.index(f"def {GATE}(")
    after = text.find("\ndef ", start + 1)
    return text[start:after if after > 0 else len(text)]


def sections_that_need_trusting() -> set[str]:
    """The top-level settings the config reader guards behind trust.

    Read out of the code rather than listed here: a dotted name like
    "git.allow_push" or a lookup like data["mcp"]["servers"] both name the
    section they belong to.
    """

    body = the_gate()
    found: set[str] = set()
    for match in re.finditer(r'"([a-z_]+)\.[a-z_]+"', body):
        found.add(match.group(1))
    for match in re.finditer(r'data\["([a-z_]+)"\]', body):
        found.add(match.group(1))
    for match in re.finditer(r'project_controls\("([a-z_]+)\.', body):
        found.add(match.group(1))
    # Sections a settings file cannot hold at all are not the warning's job.
    return {name for name in found if name not in {"trusted_floor", "provenance", "data"}}


class TheWarningCoversEverythingTests(unittest.TestCase):
    def test_the_reading_finds_the_gate(self) -> None:
        # Without this, renaming the config function would make the test below
        # pass by finding nothing at all.
        self.assertIn("requires trusted", the_gate())
        self.assertGreaterEqual(len(sections_that_need_trusting()), 6)

    def test_every_section_trust_unlocks_is_named_in_the_warning(self) -> None:
        named = {section for section, _means in seats.WHAT_TRUSTING_UNLOCKS}
        missing = sorted(sections_that_need_trusting() - named)
        self.assertEqual(
            missing,
            [],
            "The config reader guards these behind trust and the panel never mentions "
            f"them: {missing}. Add each to seats.WHAT_TRUSTING_UNLOCKS with a line "
            "saying what it means, or somebody will trust a file on a promise that "
            "was never checked.",
        )

    def test_a_file_that_starts_another_program_is_called_out(self) -> None:
        # The shape the schema and the config reader really use: a list of
        # servers, each naming itself. Written the wrong way round once, and
        # the warning fell over on the one case it exists for while every test
        # went on passing, because the tests used the wrong shape too.
        said = " ".join(seats.what_makes_it_risky({
            "mcp": {"servers": [
                {"name": "evil", "transport": "stdio",
                 "command": "cmd.exe", "args": ["/c", "calc"]},
            ]},
        }))
        self.assertIn("starts another program", said)
        self.assertIn("evil", said)
        self.assertIn("cmd.exe /c calc", said)

    def test_the_shape_it_reads_is_the_shape_the_schema_says(self) -> None:
        # Held against the schema rather than remembered, so the two cannot
        # drift apart again.
        import json

        schema = json.loads((ROOT / "harness.schema.json").read_text(encoding="utf-8"))
        servers = schema["properties"]["mcp"]["properties"]["servers"]
        self.assertEqual(servers["type"], "array")
        wanted = servers["items"]["properties"]
        for field in ("name", "command", "args", "url"):
            self.assertIn(field, wanted)

    def test_no_shape_at_all_makes_it_fall_over(self) -> None:
        # It reads a file nobody has checked, and the routes are already
        # written by the time it runs. Falling over here would leave somebody
        # with a changed file, no way back, and no warning ever shown.
        for odd in (
            {"mcp": {"servers": "nonsense"}},
            {"mcp": {"servers": [None, 7, "text"]}},
            {"providers": "text"},
            {"providers": {"one": "text"}},
            {"project": {"test_commands": 7}},
            {"project": {"test_commands": [None]}},
            {"git": []},
            {"plugins": None},
            {"qa": {"allow_hosts": 5}},
            {"execution": "on"},
        ):
            with self.subTest(odd=odd):
                said = seats.what_makes_it_risky(odd)
                self.assertIsInstance(said, list)
                for line in said:
                    self.assertIsInstance(line, str)

    def test_a_file_that_loads_somebody_elses_code_is_called_out(self) -> None:
        said = " ".join(seats.what_makes_it_risky({"plugins": {"enabled": ["theirs"]}}))
        self.assertIn("loads and runs code", said)

    def test_a_file_that_runs_commands_is_called_out(self) -> None:
        said = " ".join(seats.what_makes_it_risky({
            "project": {"test_commands": [["curl", "http://elsewhere.example"]]},
        }))
        self.assertIn("would run this", said)
        self.assertIn("curl", said)

    def test_a_file_that_may_push_is_called_out(self) -> None:
        said = " ".join(seats.what_makes_it_risky({"git": {"allow_push": True}}))
        self.assertIn("push to your repository", said)

    def test_every_section_it_knows_says_something_when_present(self) -> None:
        # Even with nothing recognisable inside, a section that only a trusted
        # file may hold is named, because a section nobody mentions is a
        # section nobody reads.
        for section, _means in seats.WHAT_TRUSTING_UNLOCKS:
            with self.subTest(section=section):
                said = seats.what_makes_it_risky({section: {"something": True}})
                self.assertTrue(said, f"{section} was passed over in silence")

    def test_nothing_in_it_says_nothing(self) -> None:
        self.assertEqual(seats.what_makes_it_risky({"memory": {"enabled": True}}), [
            "It sets memory, which decides sending pieces of your code away to be "
            "turned into numbers."
        ])

    def test_the_panel_never_calls_a_file_safe(self) -> None:
        # The other half of the same promise: with nothing found, the panel has
        # to say what it looked for, never that there is nothing to worry about.
        panel = (ROOT / "src" / "our_harness" / "ui" / "app.js").read_text(encoding="utf-8")
        spot = panel.index("function showTheChoiceAboutTrusting")
        said = panel[spot:spot + 2500]
        self.assertIn("That is not the same as safe", said)
        self.assertNotIn("Nothing in it starts a program", said)


if __name__ == "__main__":
    unittest.main()
