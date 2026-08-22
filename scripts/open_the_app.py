"""Open the panel in a window of its own, with nothing to install.

Somebody ran the installer on a company computer, got an icon on their desktop,
pressed it, and got a browser tab. They had asked for the app. Both things are
true: the icon worked exactly as written, and what they got was not an app.

The reason is that the desktop app is built rather than shipped - a clone of
this project has no app in it, only the instructions for making one - and making
one needs npm, a few minutes, and a download of a couple of hundred megabytes
from the internet. On a company machine any of those three can be blocked, and
none of them is a thing to ask of somebody who double-clicked an installer.

So this is the way in between. Every Windows machine already has Edge, and Edge
will open one page in a window with no tabs, no address bar and no bookmarks
bar - which is what an app window is. It gets its own button on the taskbar and
its own icon. It is not Electron and it does not pretend to be; it is the same
panel in a window that behaves like a program, and it costs nothing and needs
nobody's permission.

    python scripts/open_the_app.py

The panel is started here and stopped again when the window is closed, so
closing the window really does close the program - which is the other half of
what makes something feel like an app rather than a tab somebody left open.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Long enough for a slow machine to start Python and bind a port, short enough
# that somebody staring at nothing finds out rather than waits.
LONGEST_WAIT_FOR_THE_PANEL = 90.0
# Where the window keeps its own settings. Its own, and not shared with the
# browser somebody uses for everything else: a second window handed to a
# browser that is already running is not a process of its own, and then closing
# it tells this nothing and the panel is left running for ever.
WHERE_THE_WINDOW_KEEPS_ITSELF = "window"


def _where_browsers_live() -> tuple[Path, ...]:
    """Every place a browser that can do this is usually installed.

    Edge first because it is on every Windows machine and cannot be removed,
    which makes it the one that is actually there when it matters. The others
    are asked after, for anybody who has taken Edge off or prefers their own.
    """

    # Asked of Windows rather than typed out. Written down, they are right on
    # an English Windows and wrong on the ones where those folders are called
    # something else - and this is the code that decides whether somebody gets
    # an app window or a browser tab.
    program_files = [
        os.environ.get(name, "")
        for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LOCALAPPDATA")
    ]
    known = (
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
        r"Chromium\Application\chrome.exe",
    )
    # One browser at a time across every place it could be, rather than one
    # place at a time across every browser. The other way round, Edge being in
    # the second folder put it behind all three of the others - so the comment
    # above said Edge first and the code meant Edge fourth.
    found = []
    for one in known:
        for base in program_files:
            if base:
                found.append(Path(base) / one)
    return tuple(found)


def a_browser_that_can_do_windows() -> Path | None:
    """A browser on this machine that can open a page as a window.

    Any browser built on Chromium can, which is nearly all of them, and the
    switch has been the same for over a decade.
    """

    for where in _where_browsers_live():
        if where.is_file():
            return where
    return None


def start_the_panel(root: Path, port: int = 0) -> tuple[subprocess.Popen, str]:
    """Start the panel and wait until it says where it is.

    It says so itself, on the line beginning harness-ui-ready, which is there
    for exactly this. Waiting for that line rather than guessing a port means
    the window never opens on an address nothing is listening at yet - which
    looks to somebody pressing the icon like the app is broken.
    """

    started = subprocess.Popen(
        [sys.executable, str(root / "scripts" / "harness.py"),
         "--project", str(root), "ui", "--port", str(port), "--no-open-browser"],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        # No black window behind the app. Started from the desktop icon this is
        # already running without a console, and a child started from there gets
        # one of its own unless it is told not to - which is a console flashing
        # up behind an app that is supposed to look like a program.
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    # Everything the panel printed on its way to not starting. This is the whole
    # difference between "the icon does nothing" and knowing why: started from
    # an icon there is no console for any of this to land in, so unless it is
    # caught here and shown, it goes nowhere at all.
    printed: list[str] = []
    gives_up_at = time.monotonic() + LONGEST_WAIT_FOR_THE_PANEL
    while time.monotonic() < gives_up_at:
        if started.stdout is None:
            break
        line = started.stdout.readline()
        if not line:
            if started.poll() is not None:
                break
            continue
        printed.append(line.rstrip())
        if line.startswith("harness-ui-ready "):
            try:
                said = json.loads(line[len("harness-ui-ready "):])
                _keep_reading_what_it_prints(started)
                return started, str(said["url"])
            except (json.JSONDecodeError, KeyError):
                break
    _stop_it(started)
    raise TheAppWouldNotStart(
        "The panel did not start.", [one for one in printed if one.strip()][-12:])


def _keep_reading_what_it_prints(started: subprocess.Popen) -> None:
    """Go on emptying what the panel prints, for as long as it runs.

    The panel writes a line for every request it answers. Nothing read that
    after the window opened, so the pipe filled up, and the print inside the
    panel blocked - with it the thread answering that request, and then the
    whole app. Two hundred or so clicks in, which is a few minutes of somebody
    using it, everything simply stopped.

    Thrown away rather than kept: this is a log of requests, the app is running,
    and anything worth saying went past before the window opened.
    """

    def keep_it_empty() -> None:
        try:
            for _line in started.stdout or ():
                pass
        except (ValueError, OSError):
            return

    threading.Thread(target=keep_it_empty, daemon=True).start()


class TheAppWouldNotStart(RuntimeError):
    """The panel would not start, and what it said on the way.

    Carried rather than printed, because whoever is going to read it is looking
    at a desktop where nothing happened, not at a console.
    """

    def __init__(self, said: str, printed: list[str]) -> None:
        super().__init__(said)
        self.said = said
        self.printed = printed


def open_it_in_a_window(browser: Path, url: str, keeps_itself_in: Path) -> subprocess.Popen:
    """Open one address as a window, with none of the browser around it.

    The settings folder is the part that is easy to leave out and then spend an
    afternoon on. Without it, a browser that is already running takes the new
    window over and the program started here exits at once - so this would think
    the window had been closed the moment it opened, and shut the panel down
    underneath somebody.
    """

    keeps_itself_in.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            str(browser),
            f"--app={url}",
            f"--user-data-dir={keeps_itself_in}",
            "--no-first-run",
            "--no-default-browser-check",
            # Nothing here is a normal web page and none of it wants restoring
            # after a crash, which otherwise offers somebody a bar asking about
            # it every time they open their own tool.
            "--disable-session-crashed-bubble",
            "--disable-features=Translate,InfobarScreenshot",
        ],
        cwd=str(keeps_itself_in),
    )


def _stop_it(started: subprocess.Popen) -> None:
    """Ask the panel to stop, and insist if it will not.

    Left running, the next press of the icon finds the port taken and starts a
    second one somewhere else, and by the afternoon there are five.
    """

    if started.poll() is not None:
        return
    started.terminate()
    try:
        started.wait(timeout=10)
    except subprocess.TimeoutExpired:
        started.kill()
        try:
            started.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return


def where_the_window_keeps_itself(root: Path) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "NexusHarness" / WHERE_THE_WINDOW_KEEPS_ITSELF


def a_page_about_what_went_wrong(exc: "TheAppWouldNotStart") -> str:
    """One page saying what happened and what to do about it.

    Written rather than borrowed from the panel, because the panel is the thing
    that would not start. Plain, and no cleverness: whoever is reading this is
    already having a worse time than they expected.
    """

    def plain(said: str) -> str:
        return (said.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    printed = "\n".join(plain(one) for one in exc.printed) or "It said nothing at all."
    # The one that nearly everybody hits, said in words rather than left in the
    # machine output where it looks like a fault rather than a step.
    about_trust = ""
    if any("has not been told to trust" in one for one in exc.printed):
        about_trust = f"""
      <h2>What this one means</h2>
      <p>This project has a settings file, and a settings file can name commands
      to run. Nothing reads one until somebody says the file is theirs. That is
      a deliberate stop, not a fault.</p>
      <p>If this project is yours, run the installer again and say yes when it
      asks, or open a terminal in the project folder and run:</p>
      <pre>python scripts/harness.py trust</pre>
"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Nexus Harness could not start</title>
<style>
  body {{ margin: 0; padding: 40px; background: #0d1b24; color: #e8f1f5;
         font: 16px/1.6 "Segoe UI", system-ui, sans-serif; }}
  main {{ max-width: 46rem; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 1.8rem; }}
  pre {{ background: #06121a; border: 1px solid #1e3a49; border-radius: 10px;
         padding: 14px 16px; overflow-x: auto; white-space: pre-wrap;
         word-break: break-word; }}
  .quiet {{ opacity: .75; }}
</style></head>
<body><main>
  <h1>Nexus Harness could not start</h1>
  <p>The icon worked. What it opens did not, and this page is here so that
  something says so - pressing an icon and having nothing at all happen tells
  you nothing.</p>
  {about_trust}
  <h2>What it said</h2>
  <pre>{printed}</pre>
  <p class="quiet">You can close this window.</p>
</main></body></html>
"""


def show_what_went_wrong(browser: Path, exc: "TheAppWouldNotStart") -> None:
    """Put the reason in front of somebody, in the window they were expecting.

    Best effort all the way through: this is already the path where something
    went wrong, and failing to explain a failure must not itself throw.
    """

    try:
        where = where_the_window_keeps_itself(ROOT).parent / "could-not-start.html"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(a_page_about_what_went_wrong(exc), encoding="utf-8")
        open_it_in_a_window(
            browser, where.as_uri(), where_the_window_keeps_itself(ROOT)).wait()
    except (OSError, subprocess.SubprocessError):
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open the Nexus Harness panel in a window of its own.")
    parser.add_argument(
        "--port", type=int, default=0,
        help="Which port to listen on; 0 asks the system for a free one")
    parser.add_argument(
        "--in-a-tab", action="store_true",
        help="Open it as an ordinary browser tab instead of a window")
    said = parser.parse_args(argv)

    browser = None if said.in_a_tab else a_browser_that_can_do_windows()
    try:
        panel, url = start_the_panel(ROOT, said.port)
    except TheAppWouldNotStart as exc:
        print(exc.said)
        for line in exc.printed:
            print(f"  {line}")
        # Shown in a window, because there is nobody at a console to read the
        # lines above. Started from the desktop icon, the panel refusing to
        # start looked from the outside exactly like the icon being broken:
        # press it, nothing happens, no window, no message, nothing to go on.
        if browser is not None:
            show_what_went_wrong(browser, exc)
        return 1
    try:
        if browser is None:
            # No browser this can drive, so the ordinary one it is. The panel is
            # left running, because there is no window here to wait on and
            # closing a tab is not something this can be told about.
            print(f"Opening {url} in your browser.")
            print("Close this window when you are finished with the panel.")
            webbrowser.open(url)
            panel.wait()
            return 0
        window = open_it_in_a_window(browser, url, where_the_window_keeps_itself(ROOT))
        print(f"Nexus Harness is open. It is at {url} if you need the address.")
        window.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_it(panel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
