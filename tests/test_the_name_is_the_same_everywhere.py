"""The harness calls itself one name, everywhere a person can read it.

It used to answer to two. The panel and the README said one name, while the
desktop window title, the help page, the benchmark heading, and the line
written into every evidence bundle still said the old one. Nobody notices that
while working on it, because both names read as the right one. Somebody who
downloads it notices immediately.

So the name is written down once, in our_harness.PRODUCT_NAME, and this reads
the files people actually see and insists they agree with it. It also refuses
any leftover of the old name, which is what a half-finished rename looks like.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from our_harness import PRODUCT_NAME

ROOT = Path(__file__).resolve().parents[1]
OLD_NAMES = ("Our Harness",)

# Folders that are not the shipped product: build output, other people's code,
# and anything a run wrote.
SKIP = {
    "build", "build-output", "dist", "node_modules", "__pycache__", ".git", ".harness",
    "reports", "benchmark-logs", "benchmark-archive", "venv", ".venv",
}
READABLE = {".py", ".js", ".mjs", ".html", ".css", ".json", ".md", ".yml", ".yaml"}


def shipped_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in READABLE:
            continue
        if any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        yield path


class TheNameIsTheSameEverywhereTests(unittest.TestCase):
    def test_no_shipped_file_still_says_an_old_name(self) -> None:
        # This test names the old one on purpose, so allow itself.
        mine = Path(__file__).resolve()
        found = []
        for path in shipped_files():
            if path == mine:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for old in OLD_NAMES:
                if old in text:
                    found.append(f"{path.relative_to(ROOT).as_posix()} says {old!r}")
        self.assertEqual(found, [], "a rename was left half done")

    def test_the_desktop_window_uses_the_same_name(self) -> None:
        package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["build"]["productName"], PRODUCT_NAME)
        main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
        self.assertIn(f'title: "{PRODUCT_NAME}"', main)

    def test_the_panel_heading_uses_the_same_name(self) -> None:
        page = (ROOT / "src" / "our_harness" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f"<title>{PRODUCT_NAME}</title>", page)
        self.assertRegex(page, rf"<h1[^>]*>\s*{re.escape(PRODUCT_NAME)}\s*<")

    def test_the_readme_opens_with_the_same_name(self) -> None:
        first = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first.strip(), f"# {PRODUCT_NAME}")

    def test_the_welcome_and_help_pages_use_the_same_name(self) -> None:
        for page in ("welcome.html", "help.html"):
            with self.subTest(page=page):
                text = (ROOT / "desktop" / "pages" / page).read_text(encoding="utf-8")
                self.assertIn(PRODUCT_NAME, text)


if __name__ == "__main__":
    unittest.main()
