"""Open a project's web page the way people will, and report what they would see.

Agents that build a page cannot see it. This opens it in Nexus's bundled
Chromium twice: straight from disk (``file://``, what double-clicking
index.html does) and from a local server bound to 127.0.0.1. For each it
reports script errors, files that failed to load, whether the page looks
blank, and saves a screenshot under ``.harness/previews`` that agents can
look at with their own image-reading tools.

It is a view, never a gate: results are evidence for the agents and the user.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .models import HarnessError

CONTRACT = "nexus-web-preview/v1"
PREVIEW_DIRECTORY = Path(".harness") / "previews"
DEFAULT_WAIT_MS = 2500
MAX_WAIT_MS = 20_000
MAX_LINES = 20
# Whole-run ceiling: two page loads, their waits, screenshots and start-up.
RUN_SECONDS = 90

_SCRIPT = r"""
'use strict';
const fs = require('fs'), path = require('path'), http = require('http'), url = require('url');
const args = JSON.parse(process.argv[2]);
const {chromium} = require(args.module);
const root = path.resolve(args.root);
const TYPES = {'.html': 'text/html', '.htm': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp', '.ico': 'image/x-icon', '.wasm': 'application/wasm',
  '.glb': 'model/gltf-binary', '.gltf': 'model/gltf+json', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.txt': 'text/plain'};
const server = http.createServer((req, res) => {
  try {
    const wanted = decodeURIComponent(url.parse(req.url).pathname || '/');
    let file = path.resolve(root, '.' + wanted);
    if (file !== root && !file.startsWith(root + path.sep)) throw new Error('outside');
    if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
    const data = fs.readFileSync(file);
    res.writeHead(200, {'content-type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream'});
    res.end(data);
  } catch (_) { res.writeHead(404); res.end('not found'); }
});
const clip = (text, n = 400) => String(text || '').slice(0, n);
async function look(browser, mode, target, shot) {
  const context = await browser.newContext({viewport: {width: args.width, height: args.height}});
  const page = await context.newPage();
  const errors = [], failed = [], warnings = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(clip(m.text())); else if (m.type() === 'warning') warnings.push(clip(m.text())); });
  page.on('pageerror', e => errors.push('Uncaught: ' + clip(e.message)));
  page.on('requestfailed', r => failed.push(clip(r.url(), 240) + ' (' + clip(r.failure() && r.failure().errorText, 120) + ')'));
  page.on('response', r => { if (r.status() >= 400) failed.push(clip(r.url(), 240) + ' (HTTP ' + r.status() + ')'); });
  const result = {mode, url: target};
  try {
    await page.goto(target, {waitUntil: 'load', timeout: 20000});
    await page.waitForTimeout(args.wait_ms);
    result.title = clip(await page.title(), 200);
    Object.assign(result, await page.evaluate(() => ({
      visible_text: (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300),
      canvases: document.querySelectorAll('canvas').length,
      elements: document.querySelectorAll('body *').length,
      // The largest visible canvas is usually the game or chart; judge it on its own.
      canvas_box: [...document.querySelectorAll('canvas')].map(c => c.getBoundingClientRect())
        .filter(r => r.width > 40 && r.height > 40).sort((a, b) => b.width * b.height - a.width * a.height)
        .map(r => ({x: Math.max(0, r.x), y: Math.max(0, r.y), width: Math.min(r.width, innerWidth), height: Math.min(r.height, innerHeight)}))[0] || null,
    })));
    const png = await page.screenshot({path: shot});
    result.screenshot = shot;
    // How varied the picture is: a page that failed to draw is one or two colours.
    const probe = await context.newPage();
    const variety = async (b64) => probe.evaluate(async b64 => {
      const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
      const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
      const g = c.getContext('2d'); g.drawImage(img, 0, 0);
      const d = g.getImageData(0, 0, c.width, c.height).data, counts = new Map(); let total = 0;
      for (let i = 0; i < d.length; i += 16) { const k = (d[i] >> 3) + ',' + (d[i + 1] >> 3) + ',' + (d[i + 2] >> 3); counts.set(k, (counts.get(k) || 0) + 1); total++; }
      const top = Math.max(...counts.values());
      return {distinct_colours: counts.size, largest_colour_share: Math.round(top / total * 100) / 100};
    }, b64);
    // Nearly one colour. A simple drawing (a red square on white) has few colours
    // but is not blank; calling it blank taught agents to ignore the verdict.
    const plain = one => one.distinct_colours < 2 || one.largest_colour_share > 0.97;
    const colours = await variety(png.toString('base64'));
    Object.assign(result, colours);
    result.looks_blank = plain(colours);
    if (result.canvas_box) {
      // Hide the page's other layers so HUD text over the canvas does not count as drawing.
      await page.evaluate(() => {
        const main = [...document.querySelectorAll('canvas')].sort((a, b) => b.width * b.height - a.width * a.height)[0];
        main.setAttribute('data-nexus-main-canvas', '');
        const style = document.createElement('style'); style.id = 'nexus-preview-only-canvas';
        style.textContent = 'body * { visibility: hidden !important; } [data-nexus-main-canvas] { visibility: visible !important; }';
        document.head.append(style);
      });
      const only = await page.screenshot({clip: result.canvas_box});
      const drawn = await variety(only.toString('base64'));
      result.main_canvas = {...result.canvas_box, ...drawn, looks_blank: plain(drawn)};
      if (result.main_canvas.looks_blank) result.looks_blank = true;
    }
    delete result.canvas_box;
  } catch (e) { result.load_error = clip(e.message, 600); }
  result.errors = errors.slice(0, args.max_lines);
  result.error_count = errors.length;
  result.failed_requests = failed.slice(0, args.max_lines);
  result.warning_count = warnings.length;
  await context.close();
  return result;
}
server.listen(0, '127.0.0.1', async () => {
  const out = {pages: []};
  let browser;
  try {
    browser = await chromium.launch({headless: args.headless !== false, executablePath: args.chromium, args: ['--use-gl=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']});
    const fileUrl = url.pathToFileURL(path.join(root, args.page)).href;
    const httpUrl = 'http://127.0.0.1:' + server.address().port + '/' + args.page.split(path.sep).map(encodeURIComponent).join('/');
    out.pages.push(await look(browser, 'file', fileUrl, args.shots.file));
    out.pages.push(await look(browser, 'http', httpUrl, args.shots.http));
  } catch (e) { out.error = clip(e.message, 600); }
  finally { if (browser) await browser.close().catch(() => {}); server.close(); process.stdout.write(JSON.stringify(out)); }
});
"""


def verdict(pages: list[dict[str, Any]]) -> str:
    """One line an agent cannot skim past: what the user sees when they open the page."""
    words = []
    for one in pages:
        where = "Opened from disk (how the user opens it)" if one.get("mode") == "file" else "Served from a local server"
        problems = [f"{one['error_count']} script errors" if one.get("error_count") else "",
                    "the page looks blank" if one.get("looks_blank") else "",
                    "it did not load" if one.get("load_error") else ""]
        problems = [text for text in problems if text]
        words.append(f"{where}: " + ("PROBLEM - " + ", ".join(problems) if problems else "draws without script errors"))
    return ". ".join(words) + "."


def _advice(pages: list[dict[str, Any]]) -> list[str]:
    """Plain next steps for the common ways a page fails."""
    by_mode = {one.get("mode"): one for one in pages}
    disk, served = by_mode.get("file", {}), by_mode.get("http", {})
    text = " ".join(" ".join(one.get("errors", []) + one.get("failed_requests", [])) for one in pages).lower()
    advice = []
    disk_broken = bool(disk.get("error_count") or disk.get("load_error") or disk.get("looks_blank"))
    served_ok = bool(served) and not (served.get("error_count") or served.get("load_error") or served.get("looks_blank"))
    if disk_broken and served_ok:
        advice.append("The page works from a server but is broken when opened from disk, and double-clicking "
                      "index.html is how the user will open it: to them it is broken. Browsers block "
                      "<script type=\"module\" src=...> and fetch() of local files on file:// pages. Unless the user "
                      "asked for a server, make it work from disk, for example with classic scripts, or with the "
                      "module code inline in index.html and its imports from a CDN.")
    if "cors" in text and "file://" in text:
        advice.append("A file:// page cannot load local modules or files through fetch; see above.")
    if any(one.get("looks_blank") for one in pages):
        advice.append("The screenshot is nearly one colour: the page probably did not draw. Check the errors, "
                      "then open the screenshot to see what is actually on screen.")
    if any(one.get("error_count") for one in pages):
        advice.append("Fix the script errors first; later code often never runs after the first uncaught error.")
    return advice


def _plain_path(path) -> str:
    # Chromium cannot find its own graphics libraries when started through a
    # Windows long-path prefix, and then has no WebGL at all.
    text = str(path)
    return text[4:] if text.startswith("\\\\?\\") else text


def preview(root: Path, page: str = "index.html", *, wait_ms: int = DEFAULT_WAIT_MS,
            width: int = 1280, height: int = 720, timeout: float = RUN_SECONDS,
            runtime=None, headless: bool = True) -> dict[str, Any]:
    """``headless`` False shows the browser window while it checks (the user's choice)."""
    root = Path(_plain_path(Path(root).resolve()))
    relative = Path(str(page or "index.html").replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise HarnessError("preview_web_page: give a page path inside the project, like index.html")
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise HarnessError(f"preview_web_page: {relative.as_posix()} is not a file in the project")
    if target.suffix.lower() not in {".html", ".htm"}:
        raise HarnessError("preview_web_page: choose an .html page")
    if runtime is None:
        from .playwright_runtime import discover_bundled_playwright_runtime
        runtime = discover_bundled_playwright_runtime()
    if runtime is None:
        raise HarnessError("preview_web_page: Nexus's bundled browser is not available on this computer. "
                           "Run the page's own checks with your tools instead.")
    shots_dir = root / PREVIEW_DIRECTORY
    shots_dir.mkdir(parents=True, exist_ok=True)
    stem = "-".join(relative.with_suffix("").parts)[:80] or "page"
    shots = {mode: str(shots_dir / f"{stem}-{mode}.png") for mode in ("file", "http")}
    for one in shots.values():
        Path(one).unlink(missing_ok=True)
    arguments = {"module": _plain_path(runtime.playwright_module), "chromium": _plain_path(runtime.chromium),
                 "root": str(root), "page": str(relative), "shots": shots,
                 "wait_ms": max(0, min(int(wait_ms), MAX_WAIT_MS)), "width": width, "height": height,
                 "max_lines": MAX_LINES, "headless": bool(headless)}
    with tempfile.TemporaryDirectory(prefix="nexus-preview-") as scratch:
        script = Path(scratch) / "preview.cjs"
        script.write_text(_SCRIPT, encoding="utf-8")
        try:
            done = subprocess.run([_plain_path(runtime.node), str(script), json.dumps(arguments)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=runtime.environment(os.environ), cwd=scratch,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise HarnessError("preview_web_page: the page did not finish loading in time") from None
    try:
        report = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise HarnessError("preview_web_page: the browser did not report a result. " + done.stderr[-600:]) from None
    pages = report.get("pages") or []
    for one in pages:
        if one.get("screenshot"):
            one["screenshot"] = Path(one["screenshot"]).resolve().relative_to(root).as_posix()
    result = {"verdict": verdict(pages), "contract": CONTRACT, "page": relative.as_posix(), "pages": pages, "advice": _advice(pages),
              "how_to_look": "Open each screenshot with your image-reading tool to see the page as the user will."}
    if report.get("error"):
        result["error"] = report["error"]
    return result


LAUNCHER = PREVIEW_DIRECTORY.parent / "nexus-preview.py"
_LAUNCHER_TEXT = '''# Written by Nexus Harness so agents can look at a page with their own tools:
#   python .harness/nexus-preview.py [page.html] [--wait-ms N]
import sys
sys.path.insert(0, {source!r})
from our_harness.web_preview import main
raise SystemExit(main(sys.argv[1:]))
'''


def agent_command(root: Path) -> str:
    """A command an agent can run itself (in its own shell) to preview a page.

    Native agents cannot call Nexus tools mid-turn; they tried, failed and then
    declared a page finished without seeing it. A plain command keeps the
    preview inside their own turn. Returns "" when it cannot be prepared.
    """
    import sys
    try:
        root = Path(_plain_path(Path(root).resolve()))
        launcher = root / LAUNCHER
        launcher.parent.mkdir(parents=True, exist_ok=True)
        text = _LAUNCHER_TEXT.format(source=_plain_path(Path(__file__).resolve().parents[1]))
        if not launcher.is_file() or launcher.read_text(encoding="utf-8") != text:
            launcher.write_text(text, encoding="utf-8")
    except OSError:
        return ""
    python = _plain_path(sys.executable)
    return (f'"{python}" {LAUNCHER.as_posix()} index.html  (in PowerShell: '
            f'& "{python}" {LAUNCHER.as_posix()} index.html)')


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Open a project page like the user will and report what shows.")
    parser.add_argument("page", nargs="?", default="index.html")
    parser.add_argument("--wait-ms", type=int, default=DEFAULT_WAIT_MS)
    options = parser.parse_args(argv)
    root = Path.cwd()
    try:
        result = preview(root, options.page, wait_ms=options.wait_ms)
    except HarnessError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    for one in result["pages"]:
        if one.get("screenshot"):
            one["screenshot"] = str(Path(_plain_path(root.resolve())) / one["screenshot"])
    result["how_to_look"] = "Open each screenshot path with your image-reading tool (for example Read) to see the page."
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
