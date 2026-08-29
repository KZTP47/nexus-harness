"""Prove bundled Playwright performs UI interaction inside AppContainer."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from our_harness.playwright_runtime import (
    discover_bundled_playwright_runtime,
    run_brokered_playwright_appcontainer,
)
from scripts.prepare_windows_runtime import selected_runtime


def main() -> int:
    if os.name != "nt":
        raise SystemExit("Windows AppContainer is required for this smoke")
    os.environ["NEXUS_PLAYWRIGHT_RUNTIME"] = str(selected_runtime() / "playwright")
    runtime = discover_bundled_playwright_runtime(required=True)
    assert runtime is not None
    with tempfile.TemporaryDirectory(prefix="nexus-playwright-smoke-") as temporary:
        base = Path(temporary)
        node_snapshot = base / "snapshot"
        node_snapshot.mkdir()
        denied = base / "must-not-be-written.txt"
        result = node_snapshot / "result.json"
        runner = node_snapshot / "smoke.cjs"
        runner.write_text(r'''
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.join(process.env.NEXUS_BUNDLED_PLAYWRIGHT_ROOT, 'node_modules', 'playwright'));
(async () => {
  let externalWriteDenied = false;
  try { fs.writeFileSync(process.env.NEXUS_DENIED_WRITE, 'escaped'); }
  catch (error) { externalWriteDenied = ['EACCES', 'EPERM'].includes(error.code); }
  if (!externalWriteDenied) throw new Error('AppContainer allowed an external write');
  const browser = await chromium.connectOverCDP(process.env.NEXUS_CDP_ENDPOINT);
  const context = browser.contexts()[0];
  if (!context) throw new Error('brokered Chromium did not expose a default context');
  const page = await context.newPage();
  await page.setContent('<label>Name <input id="name"></label><button id="save" onclick="document.body.dataset.saved=document.querySelector(\'#name\').value">Save</button>');
  await page.locator('#name').fill('Loop16');
  await page.locator('#save').click();
  const saved = await page.locator('body').getAttribute('data-saved');
  if (saved !== 'Loop16') throw new Error('real browser interaction did not produce the expected state');
  fs.writeFileSync(process.env.NEXUS_SMOKE_RESULT, JSON.stringify({externalWriteDenied, saved}));
  await browser.close(); // disconnect this client; the engine-owned closer terminates Chromium
})().catch(error => { console.error(error); process.exit(1); });
''', encoding="utf-8")
        broker = run_brokered_playwright_appcontainer(
            node_snapshot, runner, runtime=runtime, timeout=45.0,
            environment={
                "NEXUS_DENIED_WRITE": str(denied),
                "NEXUS_SMOKE_RESULT": str(result),
            },
        )
        if not broker["passed"]:
            raise SystemExit("Bundled Playwright AppContainer smoke failed: " + str(broker))
        if denied.exists():
            raise SystemExit("Bundled Playwright smoke escaped its disposable snapshot")
        receipt = json.loads(result.read_text(encoding="utf-8"))
        if receipt != {"externalWriteDenied": True, "saved": "Loop16"}:
            raise SystemExit("Bundled Playwright smoke receipt was invalid: " + repr(receipt))
        print(json.dumps({
            "runtime": str(runtime.root),
            "node": runtime.node_version,
            "playwright": runtime.playwright_version,
            "chromium_revision": runtime.chromium_revision,
            "containment": broker["runner"]["containment_profile"],
            "browser_containment": broker["browser"]["containment_profile"],
            **receipt,
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
