"""Every kind of news the server sends must be one the page listens for.

This is the quietest way a button can break. The button works, the server does
the work, the answer arrives at the page, and the page throws it away because
its list of names to listen for was never updated. Nothing fails, nothing is
logged, and the person pressing the button just sees nothing happen.

That is exactly what happened to the "Find pages nobody checks" button: the
server sent coverage_started and coverage_result, and the page only listened
for names beginning qa_, pick_ or record_.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "src" / "our_harness"

# Every event the checks view is supposed to draw something for.
_CHECKS_EVENT = re.compile(
    r'events\.add\(\{\s*"kind":\s*"([a-z_]+)"\s*,\s*"node":\s*"checks"', re.MULTILINE
)
# The same, written over more than one line.
_CHECKS_EVENT_SPREAD = re.compile(
    r'\{\s*"kind":\s*"([a-z_]+)"\s*,\s*"node":\s*"checks"', re.MULTILINE
)
_DISPATCH = re.compile(r'\[([^\]]*)\]\.some\(\(start\) => kind\.startsWith\(start\)\)')
_HANDLED = re.compile(r'event\.kind === "([a-z_]+)"')


def server_text() -> str:
    return (HERE / "server.py").read_text(encoding="utf-8")


def panel_text() -> str:
    return (HERE / "ui" / "app.js").read_text(encoding="utf-8")


def kinds_the_server_sends() -> set[str]:
    text = server_text()
    return set(_CHECKS_EVENT.findall(text)) | set(_CHECKS_EVENT_SPREAD.findall(text))


def prefixes_the_page_listens_for() -> list[str]:
    found = _DISPATCH.search(panel_text())
    if not found:
        raise AssertionError(
            "Could not find the list of event names the panel listens for. "
            "If it was rewritten, this test has to be rewritten with it."
        )
    return re.findall(r'"([a-z_]+)"', found.group(1))


class EveryKindIsListenedForTests(unittest.TestCase):
    def test_the_server_really_does_send_checks_events(self) -> None:
        # If this finds nothing, the test below would pass while proving
        # nothing at all.
        sent = kinds_the_server_sends()
        self.assertGreaterEqual(len(sent), 8, sorted(sent))
        for expected in ("qa_result", "coverage_result", "pick_result", "record_result"):
            with self.subTest(kind=expected):
                self.assertIn(expected, sent)

    def test_every_kind_the_server_sends_is_one_the_page_listens_for(self) -> None:
        prefixes = prefixes_the_page_listens_for()
        for kind in sorted(kinds_the_server_sends()):
            with self.subTest(kind=kind):
                self.assertTrue(
                    any(kind.startswith(start) for start in prefixes),
                    f"The server sends {kind} and the page listens for {prefixes}. "
                    "The button would work and the page would show nothing.",
                )

    def test_every_kind_the_page_draws_is_one_the_server_can_send(self) -> None:
        # The other way round: a handler for a name nothing sends is dead code
        # that reads like working code.
        drawn = set(_HANDLED.findall(panel_text()))
        checks_kinds = kinds_the_server_sends()
        for kind in sorted(drawn):
            if not any(
                kind.startswith(start) for start in ("qa_", "pick_", "record_", "coverage_")
            ):
                continue
            with self.subTest(kind=kind):
                self.assertIn(kind, checks_kinds)


if __name__ == "__main__":
    unittest.main()
