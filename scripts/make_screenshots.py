"""Take the pictures used in the README.

Everything is captured from a small demo project made here and thrown away
afterwards, so no picture shows anyone's own folders, files, or results.

    python scripts/make_screenshots.py

The pictures land in docs/images. Run it again whenever the panel changes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"
WIDE = (1280, 860)


def demo_project(folder: Path) -> None:
    """A small project with something to look at, and nothing personal in it."""

    (folder / ".harness" / "qa").mkdir(parents=True)
    (folder / "README.md").write_text(
        "# Shop\n\nA small example project.\n", encoding="utf-8"
    )
    (folder / "shop.py").write_text(
        textwrap.dedent(
            '''
            """The part of the shop that adds up a basket."""


            def total(prices):
                return sum(prices)
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (folder / "test_shop.py").write_text(
        textwrap.dedent(
            """
            import unittest

            from shop import total


            class BasketTests(unittest.TestCase):
                def test_an_empty_basket_costs_nothing(self):
                    self.assertEqual(total([]), 0)

                def test_a_basket_adds_up(self):
                    self.assertEqual(total([2, 3]), 5)


            if __name__ == "__main__":
                unittest.main()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    suite = {
        "schema_version": 1,
        "name": "shop",
        "cases": [
            {
                "id": "unit-tests",
                "title": "The unit tests finish without an error",
                "kind": "command",
                "tags": ["fast", "tests"],
                "command": [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-q"],
                "expect": {"exit_code": 0},
            },
            {
                "id": "readme-explains-the-project",
                "title": "The README says what this project is",
                "kind": "file",
                "tags": ["docs"],
                "path": "README.md",
                "expect": {"exists": True, "contains": ["Shop"]},
            },
            {
                "id": "no-keys-in-the-code",
                "title": "No credentials are left in the code",
                "kind": "secrets",
                "tags": ["safety"],
                "paths": ["shop.py", "test_shop.py", "README.md"],
            },
            {
                "id": "the-panel-opens",
                "title": "The control panel opens with no errors",
                "kind": "browser",
                "tags": ["ui"],
                "url": "http://127.0.0.1:8765/",
                "expect": {"max_console_errors": 0, "max_page_errors": 0},
            },
        ],
    }
    (folder / ".harness" / "qa" / "suite.json").write_text(
        json.dumps(suite, indent=2) + "\n", encoding="utf-8"
    )


def wait_for(url: str, seconds: float = 60.0) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"The panel never answered on {url}")


SHOTS = """
const { chromium } = require('playwright');

const wide = { width: WIDTH, height: HEIGHT };
const out = OUTPUT;
const address = ADDRESS;

async function settle(page, ms = 1200) {
  await page.waitForTimeout(ms);
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: wide, deviceScaleFactor: 2 });
  await page.goto(address, { waitUntil: 'load' });
  // Wait for the panel to finish looking at the project, or the picture shows
  // it still thinking.
  await page.waitForSelector('#checkupSteps li', { timeout: 60000 });
  await settle(page, 1500);
  // Fold away anything that names a path on the machine taking the picture.
  await page.evaluate(() => {
    document.querySelectorAll('#checkupSteps details').forEach((box) => { box.open = false; });
  });
  await settle(page, 400);

  await page.screenshot({ path: out + '/start.png' });

  await page.click('[data-view="checks"]');
  await settle(page);
  await page.click('#runChecks');
  for (let tries = 0; tries < 120; tries += 1) {
    const said = await page.textContent('#checkStatus');
    if (said && /passed|failed/.test(said)) break;
    await page.waitForTimeout(500);
  }
  await settle(page);
  await page.screenshot({ path: out + '/checks.png' });

  await page.evaluate(() => {
    renderCoverage({
      percent: 60,
      pages: [
        { address: 'http://127.0.0.1:8000/', state: 'checked', checked_by: ['home-opens'] },
        { address: 'http://127.0.0.1:8000/shop', state: 'checked', checked_by: ['shop-opens'] },
        { address: 'http://127.0.0.1:8000/basket', state: 'checked', checked_by: ['basket-opens'] },
        { address: 'http://127.0.0.1:8000/help', state: 'only walked over', checked_by: [], walked_by: ['a walk'] },
        { address: 'http://127.0.0.1:8000/checkout', state: 'nobody looks at it', checked_by: [] },
      ],
      checked: ['http://127.0.0.1:8000/', 'http://127.0.0.1:8000/shop', 'http://127.0.0.1:8000/basket'],
      walked_only: ['http://127.0.0.1:8000/help'],
      missing: ['http://127.0.0.1:8000/checkout'],
      more_pages: 0,
    });
  });
  await settle(page, 600);
  await page.screenshot({ path: out + '/coverage.png' });

  await page.click('[data-view="workflow"]');
  await settle(page);
  // Fit, then a little closer, so the boxes can be read.
  await page.click('#fitButton');
  for (let step = 0; step < 2; step += 1) await page.click('#zoomIn');
  await settle(page, 800);
  await page.screenshot({ path: out + '/workflow.png' });

  await browser.close();
})().catch((error) => { console.error(error); process.exit(1); });
"""


def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary).resolve() / "shop"
        folder.mkdir()
        demo_project(folder)
        panel = subprocess.Popen(
            [sys.executable, "-m", "our_harness", "--project", str(folder),
             "ui", "--port", "8765", "--no-open-browser"],
            cwd=ROOT,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for("http://127.0.0.1:8765/api/health")
            script = (
                SHOTS.replace("WIDTH", str(WIDE[0]))
                .replace("HEIGHT", str(WIDE[1]))
                .replace("OUTPUT", json.dumps(IMAGES.as_posix()))
                .replace("ADDRESS", json.dumps("http://127.0.0.1:8765/"))
            )
            # The script has to sit beside the project's own node modules,
            # or the browser library cannot be found from a temporary folder.
            spot = ROOT / ".harness" / "qa" / "tmp" / "shots.js"
            spot.parent.mkdir(parents=True, exist_ok=True)
            spot.write_text(script, encoding="utf-8")
            try:
                finished = subprocess.run(
                    ["node", str(spot)], cwd=ROOT, capture_output=True, text=True, timeout=600
                )
            finally:
                spot.unlink(missing_ok=True)
            if finished.returncode != 0:
                print(finished.stdout, finished.stderr)
                return 1
        finally:
            panel.terminate()
            try:
                panel.wait(timeout=15)
            except subprocess.TimeoutExpired:
                panel.kill()
    for picture in sorted(IMAGES.glob("*.png")):
        print(f"{picture.relative_to(ROOT).as_posix()}  {picture.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
