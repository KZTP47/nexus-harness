"""Put a launcher for the harness on your desktop, with its own icon.

Everything else in this project is a command you have to know to type. This is
the one thing that is not: run it once and there is an icon on your desktop that
opens the panel, the way every other program on the machine works.

What the icon starts, in this order:

  1. The desktop app, if it is installed. That is the whole thing in its own
     window.
  2. The desktop app built in this folder but not installed, which is what you
     have after `npm run build` in `desktop`.
  3. The panel itself, started by Python straight out of this folder. That needs
     nothing but Python, so it always works, and it is what most people get.

All three end up at the same panel. The third opens it in your browser instead of
its own window, and says so.

    python scripts/put_it_on_your_desktop.py

On Windows there is a file in the top of this project you can double-click
instead, so nobody has to open a terminal to get started.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THE_ICON = ROOT / "desktop" / "nexus-harness.ico"
WHAT_IT_IS_CALLED = "Nexus Harness"
WHAT_IT_IS_FOR = "The Nexus Harness control panel: checks, automations, and your agents"


@dataclass(frozen=True)
class Launcher:
    """What the icon on the desktop will start."""

    program: Path
    arguments: list[str]
    working_folder: Path
    # One line saying which of the three this is, said out loud when it is made
    # so nobody has to guess which one they got.
    what_it_is: str
    # Whether the panel opens in a window of its own or in a browser.
    in_its_own_window: bool
    # Where the icon is taken from. The app carries its own once it is built, so
    # a shortcut to it does not point back into this folder for a picture - and
    # keeps its icon if the folder is ever moved. The one started by Python
    # points here, which is fine, because that one needs this folder anyway.
    icon: Path


def _installed_app() -> Path | None:
    """Where the desktop app puts itself when somebody installs it."""

    if os.name != "nt":
        return None
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "Programs" / "our-harness-desktop" / "Nexus Harness.exe"


def _built_app(root: Path) -> Path | None:
    """The desktop app built here and not installed."""

    if os.name != "nt":
        return None
    return root / "desktop" / "build-output" / "win-unpacked" / "Nexus Harness.exe"


def _python_that_shows_no_terminal() -> Path:
    """The Python that opens no black window behind the panel.

    Windows ships a second one next to the usual one for exactly this. Somewhere
    without it, the ordinary one is used and there is a terminal window as well,
    which is untidy and works.
    """

    here = Path(sys.executable)
    if os.name == "nt":
        quiet = here.with_name("pythonw.exe")
        if quiet.is_file():
            return quiet
    return here


def what_to_launch(root: Path = ROOT, is_there=None) -> Launcher:
    """Which of the three the icon should start, best first.

    `is_there` is only for tests, so this can be asked what it would do on a
    machine that is not this one.
    """

    is_there = is_there or (lambda where: Path(where).is_file())
    for where, what in (
        (_installed_app(), "the desktop app, already installed on this machine"),
        (_built_app(root), "the desktop app built in this folder"),
    ):
        if where is not None and is_there(where):
            return Launcher(
                program=Path(where), arguments=[], working_folder=Path(where).parent,
                what_it_is=what, in_its_own_window=True, icon=Path(where),
            )
    return Launcher(
        program=_python_that_shows_no_terminal(),
        arguments=[str(root / "scripts" / "harness.py"), "--project", str(root), "ui"],
        working_folder=root,
        what_it_is="the panel, started by Python out of this folder",
        in_its_own_window=False,
        icon=root / "desktop" / "nexus-harness.ico",
    )


# ---- putting it where somebody will find it -------------------------------


def as_powershell_text(said: object) -> str:
    """One value, written so PowerShell reads it as the text it is.

    Inside single quotes PowerShell takes everything literally, which is what is
    wanted, and the one character that ends the quoting is a single quote itself.
    Doubling it is how PowerShell says "one of these, not the end" - so a folder
    called "Karo's Folder" stays a folder name instead of ending the string and
    turning the rest of the line into nonsense.
    """

    return str(said).replace("'", "''")


def _ask_windows(script: str) -> str:
    """Run one small piece of PowerShell and hand back what it printed."""

    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=120,
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip() or "PowerShell would not run")
    return done.stdout.strip()


def where_the_desktop_is() -> Path:
    """The desktop folder, asked for rather than guessed at.

    Guessed at as the home folder with Desktop on the end, it is wrong for
    everybody whose desktop is kept in OneDrive - which is most people at work,
    and the shortcut lands somewhere they never look.
    """

    if os.name == "nt":
        said = _ask_windows("[Environment]::GetFolderPath('Desktop')")
        if said:
            return Path(said)
    return Path.home() / "Desktop"


def _windows_shortcut(where: Path, launcher: Launcher, icon: Path) -> None:
    arguments = " ".join(f'"{one}"' for one in launcher.arguments)
    quoted = as_powershell_text
    script = (
        "$made = (New-Object -ComObject WScript.Shell)"
        f".CreateShortcut('{quoted(where)}');"
        f"$made.TargetPath = '{quoted(launcher.program)}';"
        f"$made.Arguments = '{quoted(arguments)}';"
        f"$made.WorkingDirectory = '{quoted(launcher.working_folder)}';"
        f"$made.IconLocation = '{quoted(icon)},0';"
        f"$made.Description = '{quoted(WHAT_IT_IS_FOR)}';"
        "$made.Save()"
    )
    _ask_windows(script)


def _linux_launcher(where: Path, launcher: Launcher, icon: Path) -> None:
    parts = " ".join([str(launcher.program), *launcher.arguments])
    where.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={WHAT_IT_IS_CALLED}\n"
        f"Comment={WHAT_IT_IS_FOR}\n"
        f"Exec={parts}\n"
        f"Path={launcher.working_folder}\n"
        f"Icon={icon}\n"
        "Terminal=false\n",
        encoding="utf-8",
    )
    where.chmod(0o755)


def _mac_launcher(where: Path, launcher: Launcher, _icon: Path) -> None:
    parts = " ".join(f'"{one}"' for one in [str(launcher.program), *launcher.arguments])
    where.write_text(
        "#!/bin/sh\n"
        f"# Opens {WHAT_IT_IS_CALLED}. Made by scripts/put_it_on_your_desktop.py.\n"
        f'cd "{launcher.working_folder}" || exit 1\n'
        f"exec {parts}\n",
        encoding="utf-8",
    )
    where.chmod(0o755)


def what_the_launcher_is_called() -> str:
    """What the thing on the desktop is named, on this kind of machine."""

    if os.name == "nt":
        return f"{WHAT_IT_IS_CALLED}.lnk"
    if sys.platform == "darwin":
        return f"{WHAT_IT_IS_CALLED}.command"
    return f"{WHAT_IT_IS_CALLED}.desktop"


def put_it_there(desktop: Path, launcher: Launcher, icon: Path | None = None) -> Path:
    """Write the launcher onto the desktop and hand back where it went."""

    icon = icon or launcher.icon
    desktop.mkdir(parents=True, exist_ok=True)
    where = desktop / what_the_launcher_is_called()
    if os.name == "nt":
        _windows_shortcut(where, launcher, icon)
    elif sys.platform == "darwin":
        _mac_launcher(where, launcher, icon)
    else:
        _linux_launcher(where, launcher, icon)
    if not where.exists():
        raise RuntimeError(f"Nothing was written to {where}")
    return where


def is_the_settings_file_trusted(root: Path = ROOT) -> bool | None:
    """Whether this machine has been told the project's settings are its own.

    Nothing, when there is no answer to be had - no Python to ask with, or the
    question itself went wrong. Nothing is not a no: saying "your settings are
    not trusted" to somebody whose settings are fine would send them off to fix
    what is not broken.

    Asked by running the harness rather than by reading the file, because the
    harness is what decides it and there is only one place that rule should
    live.
    """

    try:
        done = subprocess.run(
            [sys.executable, str(root / "scripts" / "harness.py"),
             "--project", str(root), "trust", "--show"],
            capture_output=True, text=True, timeout=120, cwd=str(root),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    said = f"{done.stdout}\n{done.stderr}".lower()
    if "not trusted" in said:
        return False
    if "is trusted" in said:
        return True
    return None


def what_to_say_about_trust(root: Path = ROOT) -> list[str]:
    """What to tell somebody about the settings file, if anything.

    Said here rather than left for the icon to fail at, because the icon starts
    the panel with the quiet Python and a panel that refuses there says it to
    nobody: the icon is double-clicked and nothing happens at all.
    """

    if is_the_settings_file_trusted(root) is not False:
        return []
    return [
        "",
        "One thing first, or the icon will open nothing.",
        "",
        "This project has a settings file, and a settings file can name commands",
        "to run - so nothing reads one until you say the file is yours. Read it,",
        "then run this once:",
        "",
        f"    python scripts/harness.py --project \"{root}\" trust",
        "",
        "After that the icon works. Until then the panel stops before it starts,",
        "and it stops quietly, because the icon opens it without a window.",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desktop", default="",
        help="Put it somewhere other than the desktop. Only used by the checks.",
    )
    said = parser.parse_args(argv)

    if not THE_ICON.is_file():
        print(f"The icon is missing from {THE_ICON}.")
        print("Draw it again with: python scripts/draw_the_icon.py")
        return 1

    launcher = what_to_launch()
    # Finding the desktop asks Windows, and asking Windows can go wrong on a
    # machine locked down enough that PowerShell will not run. Outside this, the
    # answer to that was a Python traceback rather than the plain sentence every
    # other way of failing here is careful to give.
    try:
        desktop = Path(said.desktop) if said.desktop else where_the_desktop_is()
        where = put_it_there(desktop, launcher)
    except (OSError, RuntimeError) as exc:
        print(f"The launcher could not be put on your desktop: {exc}")
        return 1

    print(f"Done. There is now a {WHAT_IT_IS_CALLED} icon on your desktop.")
    print()
    print(f"  it opens   {launcher.what_it_is}")
    print(f"  it lives   {where}")
    if not launcher.in_its_own_window:
        print()
        print("That one opens the panel in your browser. For a window of its own,")
        print("build the desktop app once - in the desktop folder, run:")
        print("    npm install")
        print("    npm run build")
        print("then run this again and the icon will open that instead.")
    for line in what_to_say_about_trust():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
