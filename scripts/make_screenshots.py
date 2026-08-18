"""Take the pictures used in the README.

Everything is captured from a small demo project made here and thrown away
afterwards, so no picture shows anyone's own folders, files, or results.

    python scripts/make_screenshots.py

The pictures land in docs/images. Run it again whenever the panel changes.
"""

from __future__ import annotations

import json
import shutil
import socket
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
PORT = 8765


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
                "url": f"http://127.0.0.1:{PORT}/",
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


def already_answering(port: int) -> bool:
    """Is something else already sitting on this port?"""

    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


SHOTS = r"""
const { chromium } = require('playwright');

const wide = { width: WIDTH, height: HEIGHT };
const out = OUTPUT;
const address = ADDRESS;
const DEMO = DEMONAME;

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
  // Last line of defence: the panel names the project it is looking at, and it
  // has to be the demo one, or somebody's own work ends up in a picture.
  const looking = await page.textContent('#checkupSummary');
  if (!looking.includes(DEMO)) {
    throw new Error('The panel is looking at the wrong project: ' + looking);
  }
  // Fold away anything that names a path on the machine taking the picture.
  await page.evaluate(() => {
    document.querySelectorAll('#checkupSteps details').forEach((box) => { box.open = false; });
  });
  await settle(page, 400);

  await page.screenshot({ path: out + '/start.png' });

  // The seat setup, after looking. Nothing is written by looking.
  await page.click('#findSeats');
  for (let tries = 0; tries < 200; tries += 1) {
    if (await page.locator('#seatList li').count()) break;
    await page.waitForTimeout(250);
  }
  await settle(page, 600);
  // Where a tool was found is useful on your own machine and nobody else's
  // business in a picture. A path can also turn up in the line that says why a
  // tool would not start, so anything path-shaped goes, not only the words
  // "found at": a Windows path carries the account name in it.
  await page.evaluate(() => {
    // Built fresh for each line: a regular expression with /g remembers where
    // it stopped, so reusing one here would skip every other line.
    const stand_in = 'the usual place for this machine';
    // Only the lines of text. Writing over a whole row would throw away the
    // parts inside it and leave every word run together.
    document.querySelectorAll('#seatList p').forEach((line) => {
      line.textContent = line.textContent.replace(
        // A drive letter, then everything up to the next space. The letter has
        // to stand on its own, or the middle of "http://..." would count.
        /(?<![A-Za-z])[A-Za-z][:][^\s,;"']+|\/(?:home|Users)\/[^\s,;"']+/g, stand_in);
    });
  });
  await settle(page, 300);
  await page.locator('#seatSteps').screenshot({ path: out + '/seats.png' });

  // The picture of the workflow, part way through the walk through, so the
  // steps show as done, working, and still to come all at once.
  await page.click('#howDemo');
  await page.waitForTimeout(2600);
  await page.locator('#howStages').screenshot({ path: out + '/how-it-works.png' });
  for (let tries = 0; tries < 60; tries += 1) {
    if (!(await page.locator('#howDemo').isDisabled())) break;
    await page.waitForTimeout(250);
  }

  // "I don't care, just do it for me", on a service still waiting for a key:
  // it writes nothing, and shows what it did and what is left for a person.
  await page.evaluate(() => {
    const box = document.querySelector('.model-setup');
    if (box) box.open = true;
  });
  await settle(page, 400);
  const doItCard = page.locator('.model-option').filter({ hasText: 'ANTHROPIC_API_KEY' }).first();
  if (await doItCard.count()) {
    await doItCard.locator('button', { hasText: 'just do it for me' }).click();
    for (let tries = 0; tries < 80; tries += 1) {
      if (await doItCard.locator('.do-it-cannot').count()) break;
      await page.waitForTimeout(250);
    }
    await settle(page, 400);
    await doItCard.screenshot({ path: out + '/just-do-it.png' });
  }

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

  // The pipelines board, part way through a run, so the boxes show green,
  // blue and grey at once rather than all one colour.
  await page.click('[data-view="pipelines"]');
  for (let tries = 0; tries < 60; tries += 1) {
    if (await page.locator('#pipelineNodes .pipeline-node').count()) break;
    await page.waitForTimeout(250);
  }
  await settle(page, 500);
  await page.evaluate(() => {
    // The demo project has no test command and no git, so a real run would
    // show mostly failures. These are the states a run really writes, put on
    // the boxes so the picture shows what a run looks like.
    const shown = {start: 'passed', scan: 'passed', checks: 'passed',
                   gate: 'running', tests: 'waiting', evidence: 'waiting'};
    for (const [id, state] of Object.entries(shown)) {
      const box = document.querySelector(`[data-node="${id}"]`);
      if (box && state !== 'waiting') box.dataset.state = state;
    }
  });
  // Open the list saying what this tab is for, which is the part somebody
  // looking at the picture is trying to work out.
  await page.evaluate(() => {
    const box = document.getElementById('pipelineWhatFor');
    if (box) box.open = true;
  });
  await settle(page, 400);
  await page.screenshot({ path: out + '/pipelines.png' });

  // The timeline: one bar per step of a run, laid out in time. A short run is
  // started here first, so there is something real to draw.
  await page.evaluate(async () => {
    pipeline = {name: 'A short one', nodes: [
      {id: 'start', kind: 'start', label: 'Start', settings: {}, at: {x: 40, y: 120}},
      {id: 'repo', kind: 'git_repo', label: 'Read the repo', settings: {}, at: {x: 320, y: 120}},
      {id: 'kept', kind: 'artifact', label: 'Keep the evidence', settings: {}, at: {x: 600, y: 120}},
    ], edges: [{from: 'start', to: 'repo'}, {from: 'repo', to: 'kept'}]};
    renderPipeline();
    await runPipeline();
  });
  for (let tries = 0; tries < 80; tries += 1) {
    const done = await page.evaluate(() => document.getElementById('pipelineStop').disabled);
    if (done) break;
    await page.waitForTimeout(250);
  }
  await page.click('[data-pipeline-tab="timeline"]');
  await settle(page, 600);
  await page.screenshot({ path: out + '/pipeline-timeline.png' });
  await page.click('[data-pipeline-tab="board"]');

  // What the harness has learned, as a picture. A few notes are written first,
  // in the throwaway project, so the picture has something to show.
  await page.click('[data-view="vault"]');
  for (let tries = 0; tries < 60; tries += 1) {
    if (await page.locator('.vault-dot').count()) break;
    await page.waitForTimeout(250);
  }
  await page.evaluate(async () => {
    const notes = [
      {title: 'They want plain English', kind: 'about-you', tags: ['writing'], sure: 0.9,
       body: 'Short answers, no jargon. See [[how-to-answer-them]].'},
      {title: 'How to answer them', kind: 'how-to', tags: ['writing'], sure: 0.8,
       body: 'Lead with what changed. One table beats three paragraphs. Related: [[they-want-plain-english]].'},
      {title: 'How to run the checks here', kind: 'how-to', tags: ['checks'], sure: 0.7,
       body: 'Start the panel first. See [[the-panel-must-be-running]].'},
      {title: 'The panel must be running', kind: 'lesson', tags: ['checks'], sure: 1.0,
       body: 'Browser checks talk to a panel that is already up. Without one they all fail at once.'},
      {title: 'This project keeps its checks in one file', kind: 'about-this-project',
       tags: ['checks'], sure: 0.9,
       body: 'They live beside the settings. See [[how-to-run-the-checks-here]].'},
    ];
    for (const note of notes) {
      await request('/api/vault/write', {method: 'POST', body: JSON.stringify(note)});
    }
    await refreshVault();
  });
  await settle(page, 900);
  await page.screenshot({ path: out + '/what-it-knows.png' });

  // The team: who is on this machine and how they work together. Whatever is
  // really installed here is what shows, so this picture is honest about a
  // machine with one assistant as much as one with two.
  await page.click('[data-view="team"]');
  for (let tries = 0; tries < 80; tries += 1) {
    if (await page.locator('.team-node').count()
        && await page.locator('#teamPlain li').count()) break;
    await page.waitForTimeout(250);
  }
  await settle(page, 600);
  await page.screenshot({ path: out + '/your-team.png' });

  // Talking to them. The demo project has whatever is really on this machine,
  // so this picture is honest about a machine with one assistant as much as
  // one with three.
  await page.click('[data-view="talk"]');
  for (let tries = 0; tries < 80; tries += 1) {
    if (await page.locator('#talkWho li').count()) break;
    await page.waitForTimeout(250);
  }
  await page.fill('#talkBox', 'Is the old parser still used anywhere?');
  // Where a tool was found is a path on this machine, and a path carries an
  // account name in it.
  await page.evaluate(() => {
    document.querySelectorAll('#talkWho .hint').forEach((line) => {
      if (/[A-Za-z]:[\/]/.test(line.textContent)) {
        line.textContent = 'the usual place for this machine';
      }
    });
  });
  await settle(page, 500);
  await page.screenshot({ path: out + '/talk-to-them.png' });

  // Looking something up in the code. The demo project is tiny, so the answer
  // here is the honest guess, which is the half of this feature worth showing:
  // it says out loud how sure it is.
  await page.click('[data-view="lookup"]');
  for (let tries = 0; tries < 80; tries += 1) {
    if (await page.locator('#lookupTools li').count()) break;
    await page.waitForTimeout(250);
  }
  await page.fill('#lookupName', 'total');
  await page.click('#lookupWhere');
  for (let tries = 0; tries < 120; tries += 1) {
    if (await page.locator('#lookupPlaces .lookup-mark').count()) break;
    await page.waitForTimeout(250);
  }
  // Where a tool was found is a path on this machine, and a path carries an
  // account name in it.
  await page.evaluate(() => {
    document.querySelectorAll('#lookupTools .hint').forEach((line) => {
      line.textContent = 'the usual place for this machine';
    });
  });
  await settle(page, 500);
  await page.screenshot({ path: out + '/look-it-up.png' });

  await browser.close();
})().catch((error) => { console.error(error); process.exit(1); });
"""


def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    # A panel left running from earlier answers on this port first, and every
    # picture then shows that project instead of the demo one. That is somebody
    # else's work in a published picture, so stop rather than guess.
    if already_answering(PORT):
        raise SystemExit(
            f"Something is already answering on port {PORT}. Stop it first, "
            "or the pictures will show that project instead of the demo one."
        )
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary).resolve() / "shop"
        folder.mkdir()
        demo_project(folder)
        panel = subprocess.Popen(
            [sys.executable, "-m", "our_harness", "--project", str(folder),
             "ui", "--port", str(PORT), "--no-open-browser"],
            cwd=ROOT,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for(f"http://127.0.0.1:{PORT}/api/health")
            script = (
                SHOTS.replace("WIDTH", str(WIDE[0]))
                .replace("HEIGHT", str(WIDE[1]))
                .replace("OUTPUT", json.dumps(IMAGES.as_posix()))
                .replace("ADDRESS", json.dumps(f"http://127.0.0.1:{PORT}/"))
                .replace("DEMONAME", json.dumps(folder.name))
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
