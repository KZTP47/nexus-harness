"""The one thing somebody who has just downloaded this does not have to be told.

Everything else in the project is a command you have to know to type. The
launcher is the exception: run one thing once, and there is an icon on the
desktop that opens the panel the way every other program on the machine opens.

So two things are held down here. The icon is drawn from code, so it can be
looked at and made again, and the one on disk has to be the one that code draws.
And the launcher has to point at the best thing on this machine, carry a picture,
and land where somebody will actually see it - which is not always the folder
called Desktop under their home.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import draw_the_icon  # noqa: E402
import put_it_on_your_desktop as installer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class TheIconTests(unittest.TestCase):
    def test_the_icon_is_in_the_project(self) -> None:
        """Committed, not made at install time. Somebody who has just downloaded
        this should not have to draw a picture before they can start."""

        self.assertTrue(draw_the_icon.WHERE_IT_GOES.is_file(), draw_the_icon.WHERE_IT_GOES)

    def test_the_one_on_disk_is_the_one_the_code_draws(self) -> None:
        """Or the code is a story about a picture rather than the picture."""

        self.assertEqual(
            draw_the_icon.WHERE_IT_GOES.read_bytes(),
            draw_the_icon.as_icon(),
            "the icon and the code that draws it have come apart. "
            "Run: python scripts/draw_the_icon.py",
        )

    def test_checking_it_says_so_without_changing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            where = Path(folder) / "made.ico"
            self.assertEqual(draw_the_icon.main(["--output", str(where)]), 0)
            self.assertEqual(draw_the_icon.main(["--check", "--output", str(where)]), 0)
            where.write_bytes(b"not an icon")
            self.assertEqual(draw_the_icon.main(["--check", "--output", str(where)]), 1)
            self.assertEqual(where.read_bytes(), b"not an icon", "checking changed it")

    def test_it_holds_every_size_windows_asks_for(self) -> None:
        """Sixteen is the one in the corner of the taskbar, and the size that
        decides whether a mark works at all. Two hundred and fifty six is what
        the installer and a large tile want."""

        raw = draw_the_icon.WHERE_IT_GOES.read_bytes()
        kind, is_icon, count = struct.unpack_from("<HHH", raw)
        self.assertEqual((kind, is_icon), (0, 1))
        self.assertEqual(count, len(draw_the_icon.SIZES))
        found = []
        for n in range(count):
            wide, _tall, _colours, _spare, _planes, deep, how_big, at = struct.unpack_from(
                "<BBBBHHII", raw, 6 + 16 * n)
            found.append(wide or 256)
            self.assertEqual(deep, 32, "an icon with fewer colours than that looks poor")
            body = raw[at:at + how_big]
            self.assertTrue(body.startswith(b"\x89PNG"), "each size is a whole picture")
        self.assertEqual(sorted(found), sorted(draw_the_icon.SIZES))
        self.assertIn(16, found)
        self.assertIn(256, found)

    def test_the_smallest_one_is_not_empty(self) -> None:
        """A mark that becomes a smudge at sixteen pixels is a mark nobody
        recognises in a taskbar."""

        pixels = draw_the_icon.draw_one(16)
        painted = [one for one in pixels if one[3] > 0]
        self.assertGreater(len(painted), 150, "almost nothing was drawn")
        colours = {one[:3] for one in painted}
        self.assertGreater(len(colours), 3, "it came out as one flat shape")

    def test_the_desktop_app_is_built_with_it(self) -> None:
        """So the program itself wears the same picture as the icon that starts
        it, rather than the one every Electron app ships with."""

        import json

        said = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        built = said["build"]
        for which in ("win", "mac", "linux"):
            with self.subTest(which=which):
                self.assertEqual(built[which].get("icon"), "nexus-harness.ico")
        self.assertIn("nexus-harness.ico", built["files"], "it has to be packed as well")


class WhichThingTheIconOpensTests(unittest.TestCase):
    """Best first: the installed app, then one built here, then Python."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_the_installed_app_wins(self) -> None:
        installed = Path("C:/somewhere/Programs/our-harness-desktop/Nexus Harness.exe")
        with mock.patch.object(installer, "_built_app", lambda root: None), \
             mock.patch.object(installer, "_installed_app", lambda: installed):
            found = installer.what_to_launch(self.root, is_there=lambda where: True)
        self.assertEqual(found.program, installed)
        self.assertTrue(found.in_its_own_window)
        self.assertIn("installed", found.what_it_is)

    def test_an_installed_app_that_cannot_open_this_project_is_passed_over(self) -> None:
        """The bug somebody actually hit: an icon that opens an error page.

        An installed app carries the harness from the day it was built. This
        folder moves on, its settings name something that copy has never heard
        of, and the app opens on a page about Python and folders - none of
        which is wrong with the machine. There is a working app right here, so
        the icon is pointed at that one instead.
        """

        installed = Path("C:/somewhere/Programs/our-harness-desktop/Nexus Harness.exe")
        built = self.root / "desktop" / "build-output" / "win-unpacked" / "Nexus Harness.exe"
        with mock.patch.object(installer, "_installed_app", lambda: installed),              mock.patch.object(installer, "_built_app", lambda root: built),              mock.patch.object(installer, "_this_copy_reads_this_project",
                               lambda where, root: True):
            found = installer.what_to_launch(
                self.root,
                is_there=lambda where: True,
                can_it_open=lambda where, root: where != installed,
            )
        self.assertEqual(found.program, built)
        self.assertEqual(found.passed_over, ())

    def test_settings_no_copy_can_read_are_not_held_against_the_app(self) -> None:
        """When every copy refuses, the settings are the problem.

        Taking somebody's app window away then would fix nothing and cost them
        the window, and the panel would say the same thing in a browser.
        """

        installed = Path("C:/somewhere/Programs/our-harness-desktop/Nexus Harness.exe")
        with mock.patch.object(installer, "_built_app", lambda root: None), \
             mock.patch.object(installer, "_installed_app", lambda: installed),              mock.patch.object(installer, "_this_copy_reads_this_project",
                               lambda where, root: False):
            found = installer.what_to_launch(
                self.root,
                is_there=lambda where: True,
                can_it_open=lambda where, root: False,
            )
        self.assertEqual(found.program, installed)
        self.assertEqual(found.passed_over, ())

    def test_an_app_that_cannot_be_asked_is_left_alone(self) -> None:
        """No answer is not an answer of no."""

        installed = Path("C:/somewhere/Programs/our-harness-desktop/Nexus Harness.exe")
        with mock.patch.object(installer, "_built_app", lambda root: None), \
             mock.patch.object(installer, "_installed_app", lambda: installed):
            found = installer.what_to_launch(
                self.root,
                is_there=lambda where: True,
                can_it_open=lambda where, root: None,
            )
        self.assertEqual(found.program, installed)
        self.assertEqual(found.passed_over, ())

    def test_what_is_said_about_the_app_that_was_passed_over(self) -> None:
        """It names the app, why, and the one thing to run about it."""

        setup = self.root / "desktop" / "build-output" / "Nexus Harness Setup 0.1.0.exe"
        setup.parent.mkdir(parents=True, exist_ok=True)
        setup.write_bytes(b"")
        installed = Path("C:/somewhere/Programs/our-harness-desktop/Nexus Harness.exe")
        launcher = installer.Launcher(
            program=Path(sys.executable), arguments=[], working_folder=self.root,
            what_it_is="something that works", in_its_own_window=True,
            icon=self.root, passed_over=(installed,),
        )
        lines = installer.what_to_say_about_an_app_left_behind(launcher, self.root)
        said = os.linesep.join(lines)
        self.assertIn(str(installed), said)
        self.assertIn("older than", said)
        self.assertIn(str(setup), said)
        self.assertIn("Start menu", said, "it opens from there too, and will still fail")

    def test_one_built_here_is_next(self) -> None:
        built = self.root / "desktop" / "build-output" / "win-unpacked" / "Nexus Harness.exe"
        with mock.patch.object(installer, "_installed_app", lambda: None), \
             mock.patch.object(installer, "_built_app", lambda root: built):
            found = installer.what_to_launch(self.root, is_there=lambda where: where == built)
        self.assertEqual(found.program, built)
        self.assertTrue(found.in_its_own_window)

    def test_with_no_desktop_app_it_still_opens_a_window(self) -> None:
        """Somebody pressed this icon on a company computer expecting an app and
        got a browser tab. A clone of this project has no desktop app in it,
        only the instructions for building one - and building one needs npm, a
        few minutes and a couple of hundred megabytes, any of which a company
        machine can block.

        Every Windows machine has a browser that will show one page as a window
        with no tabs and no address bar, which is what an app window is.
        """

        with mock.patch.object(installer, "_installed_app", lambda: None), \
             mock.patch.object(installer, "_built_app", lambda root: None), \
             mock.patch.object(
                 installer, "_a_browser_that_can_do_windows",
                 lambda: Path("somewhere") / "msedge.exe"):
            found = installer.what_to_launch(self.root, is_there=lambda where: False)
        self.assertIn("open_the_app.py", " ".join(found.arguments))
        self.assertTrue(found.in_its_own_window, "it promised a window and did not")

    def test_with_nothing_that_can_do_a_window_it_falls_back_to_a_tab(self) -> None:
        """A browser tab is the honest last answer, and the icon says which of
        the three you got rather than leaving somebody to guess."""

        with mock.patch.object(installer, "_installed_app", lambda: None), \
             mock.patch.object(installer, "_built_app", lambda root: None), \
             mock.patch.object(installer, "_a_browser_that_can_do_windows", lambda: None):
            found = installer.what_to_launch(self.root, is_there=lambda where: False)
        self.assertIn("harness.py", " ".join(found.arguments))
        self.assertIn("ui", found.arguments)
        self.assertIn(str(self.root), " ".join(found.arguments))
        self.assertFalse(found.in_its_own_window)

    def test_the_one_that_needs_this_folder_takes_its_picture_from_it(self) -> None:
        with mock.patch.object(installer, "_installed_app", lambda: None), \
             mock.patch.object(installer, "_built_app", lambda root: None):
            found = installer.what_to_launch(self.root, is_there=lambda where: False)
        self.assertEqual(found.icon, self.root / "desktop" / "nexus-harness.ico")

    def test_a_shortcut_to_the_app_takes_its_picture_from_the_app(self) -> None:
        """So it keeps its icon if this folder is ever moved or thrown away."""

        installed = Path("C:/somewhere/Nexus Harness.exe")
        with mock.patch.object(installer, "_built_app", lambda root: None), \
             mock.patch.object(installer, "_installed_app", lambda: installed):
            found = installer.what_to_launch(self.root, is_there=lambda where: True)
        self.assertEqual(found.icon, installed)


class WhereItLandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.desktop = Path(self.temporary.name) / "desktop"

    def a_launcher(self) -> installer.Launcher:
        return installer.Launcher(
            program=Path(sys.executable),
            arguments=["--version"],
            working_folder=Path(self.temporary.name),
            what_it_is="a stand-in, for the checks",
            in_its_own_window=False,
            icon=draw_the_icon.WHERE_IT_GOES,
        )

    def test_it_is_written_and_says_where_it_went(self) -> None:
        where = installer.put_it_there(self.desktop, self.a_launcher())
        self.assertTrue(where.exists(), where)
        self.assertEqual(where.parent, self.desktop)
        self.assertEqual(where.name, installer.what_the_launcher_is_called())

    def test_running_it_again_leaves_one_of_them(self) -> None:
        """Somebody who is not sure whether it worked runs it twice."""

        first = installer.put_it_there(self.desktop, self.a_launcher())
        second = installer.put_it_there(self.desktop, self.a_launcher())
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.desktop.iterdir())), 1)

    @unittest.skipUnless(os.name == "nt", "Windows shortcuts")
    def test_on_windows_it_really_points_where_it_says(self) -> None:
        """Read back through Windows itself, not by trusting what was written:
        a shortcut file that looks right and opens nothing is the whole failure
        this is here to catch."""

        launcher = self.a_launcher()
        where = installer.put_it_there(self.desktop, launcher)
        said = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"$one = (New-Object -ComObject WScript.Shell).CreateShortcut('{where}');"
             "Write-Output $one.TargetPath; Write-Output $one.IconLocation;"
             "Write-Output $one.WorkingDirectory"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(said.returncode, 0, said.stderr)
        target, icon, folder = [one.strip() for one in said.stdout.strip().splitlines()[:3]]
        self.assertEqual(Path(target), launcher.program)
        self.assertEqual(Path(icon.split(",")[0]), launcher.icon)
        self.assertEqual(Path(folder), launcher.working_folder)

    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "the Linux one")
    def test_on_linux_it_is_a_desktop_entry_that_names_the_icon(self) -> None:
        launcher = self.a_launcher()
        where = installer.put_it_there(self.desktop, launcher)
        said = where.read_text(encoding="utf-8")
        self.assertIn("[Desktop Entry]", said)
        self.assertIn(f"Icon={launcher.icon}", said)
        self.assertIn(str(launcher.program), said)

    def test_a_missing_icon_is_said_plainly_rather_than_half_done(self) -> None:
        with mock.patch.object(installer, "THE_ICON", self.desktop / "not-here.ico"):
            self.assertEqual(installer.main(["--desktop", str(self.desktop)]), 1)


class TheOneThingThatStopsTheIconWorkingTests(unittest.TestCase):
    """A freshly downloaded project has a settings file nobody has trusted, and
    the panel will not read one until they do. The icon starts the panel with
    the quiet Python - the one with no window - so it says that to nobody: the
    icon is double-clicked and nothing happens at all. The installer is the
    moment to say it, while somebody is looking.
    """

    def answering(self, said: str, code: int = 0):
        def instead(argv, **rest):
            return subprocess.CompletedProcess(argv, code, said, "")

        return instead

    def test_a_project_nobody_has_trusted_is_said_out_loud(self) -> None:
        with mock.patch.object(
            installer.subprocess, "run", self.answering("This file is not trusted yet.", 1)
        ):
            said = installer.what_to_say_about_trust(ROOT)
        self.assertTrue(said)
        together = " ".join(said)
        self.assertIn("the icon will open nothing", together)
        self.assertIn("trust", together)
        self.assertIn("quietly", together, "and why nothing seems to happen")

    def test_a_project_that_is_trusted_is_left_alone(self) -> None:
        with mock.patch.object(
            installer.subprocess, "run", self.answering("This file is trusted.", 0)
        ):
            self.assertEqual(installer.what_to_say_about_trust(ROOT), [])

    def test_no_answer_is_not_taken_for_a_no(self) -> None:
        """Saying "your settings are not trusted" to somebody whose settings are
        fine sends them off to fix what is not broken."""

        with mock.patch.object(
            installer.subprocess, "run", self.answering("something else entirely", 3)
        ):
            self.assertIsNone(installer.is_the_settings_file_trusted(ROOT))
            self.assertEqual(installer.what_to_say_about_trust(ROOT), [])

    def test_a_question_that_cannot_be_asked_is_not_a_no_either(self) -> None:
        def falls_over(argv, **rest):
            raise OSError("no python here")

        with mock.patch.object(installer.subprocess, "run", falls_over):
            self.assertIsNone(installer.is_the_settings_file_trusted(ROOT))
            self.assertEqual(installer.what_to_say_about_trust(ROOT), [])

    def test_it_names_the_folder_so_the_command_can_be_pasted(self) -> None:
        with mock.patch.object(
            installer.subprocess, "run", self.answering("This file is not trusted yet.", 1)
        ):
            said = " ".join(installer.what_to_say_about_trust(ROOT))
        self.assertIn(str(ROOT), said)
        self.assertIn("harness.py", said)

    def test_the_installer_really_prints_it(self) -> None:
        """The tests above are about the words. This is about them reaching
        somebody: worked out and never said is the same as never worked out."""

        import contextlib
        import io

        with tempfile.TemporaryDirectory() as folder:
            desktop = Path(folder)
            with mock.patch.object(
                installer, "is_the_settings_file_trusted", lambda root=ROOT: False
            ), mock.patch.object(
                installer, "put_it_there",
                lambda where, launcher, icon=None: where / "Nexus Harness.lnk",
            ):
                held = io.StringIO()
                with contextlib.redirect_stdout(held):
                    self.assertEqual(installer.main(["--desktop", str(desktop)]), 0)
        said = held.getvalue()
        self.assertIn("the icon will open nothing", said)
        self.assertIn("trust", said)

    def test_the_installer_says_nothing_about_it_when_there_is_nothing_to_say(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as folder:
            desktop = Path(folder)
            with mock.patch.object(
                installer, "is_the_settings_file_trusted", lambda root=ROOT: True
            ), mock.patch.object(
                installer, "put_it_there",
                lambda where, launcher, icon=None: where / "Nexus Harness.lnk",
            ):
                held = io.StringIO()
                with contextlib.redirect_stdout(held):
                    installer.main(["--desktop", str(desktop)])
        self.assertNotIn("open nothing", held.getvalue())

    def test_the_harness_itself_is_what_decides_it(self) -> None:
        """Asked by running the harness rather than by reading the file, so the
        rule lives in one place."""

        seen: list[list[str]] = []

        def watching(argv, **rest):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "This file is trusted.", "")

        with mock.patch.object(installer.subprocess, "run", watching):
            installer.is_the_settings_file_trusted(ROOT)
        self.assertTrue(seen)
        self.assertIn("trust", seen[0])
        self.assertIn("--show", seen[0])


class PathsWithAnApostropheInThemTests(unittest.TestCase):
    """Somebody's folder can have an apostrophe in it, and often does.

    Every path went straight into a single-quoted piece of PowerShell, so a
    folder called "Karo's Folder" - or a company OneDrive with an apostrophe in
    its name, which is most of the ones that have one - ended the string early
    and handed somebody a parser dump instead of an icon.
    """

    def test_a_quote_is_doubled_rather_than_left_to_end_the_string(self) -> None:
        self.assertEqual(installer.as_powershell_text("Karo's Folder"), "Karo''s Folder")
        self.assertEqual(installer.as_powershell_text("nothing to do"), "nothing to do")

    def test_every_path_in_the_shortcut_goes_through_it(self) -> None:
        seen: list[str] = []

        def watching(script):
            seen.append(script)
            return ""

        # Inside a folder of this test's own. Pointed at a real place on the
        # machine, a test that goes further than expected leaves something
        # behind on somebody's C drive - which is exactly what happened.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        here = Path(folder.name) / "O'Brien"
        awkward = here / "Karo's Folder" / "Nexus Harness.lnk"
        launcher = installer.Launcher(
            program=here / "python.exe",
            arguments=[str(here / "harness.py")],
            working_folder=here,
            what_it_is="the panel",
            in_its_own_window=False,
            icon=here / "nexus-harness.ico",
        )
        # With Windows' own shortcut maker out of reach, which is the whole
        # reason there is a second way at all.
        def no_maker(*args, **rest):
            raise OSError("no shortcut maker here")

        with mock.patch.object(installer, "_ask_windows", watching), \
             mock.patch.object(installer, "_shortcut_the_direct_way", no_maker), \
             self.assertRaises(RuntimeError):
            installer._windows_shortcut(awkward, launcher, launcher.icon)
        self.assertTrue(seen)
        script = seen[0]
        self.assertNotIn("O'Brien", script, "a lone quote is what ends the string early")
        self.assertIn("O''Brien", script)
        self.assertIn("Karo''s Folder", script)

    @unittest.skipUnless(os.name == "nt", "the Windows one")
    def test_a_shortcut_really_lands_in_a_folder_with_one(self) -> None:
        """Written for real, by Windows, into a folder named the awkward way."""

        with tempfile.TemporaryDirectory() as folder:
            desktop = Path(folder) / "Karo's Folder"
            desktop.mkdir()
            launcher = installer.what_to_launch()
            where = installer.put_it_there(desktop, launcher)
            self.assertTrue(where.is_file(), str(where))


class NothingIsLeftInTheProjectTests(unittest.TestCase):
    """A launcher belongs on somebody's desktop, not in the repository.

    One got as far as being committed: a check that took the desktop-finding out
    to see whether anything noticed wrote the shortcut into the project instead,
    and it went in with the next commit. Ignoring the name is the fix; this is
    the check that says so out loud.
    """

    def test_no_launcher_is_sitting_in_the_project(self) -> None:
        left = [
            one.name for one in ROOT.iterdir()
            if one.name.startswith(installer.WHAT_IT_IS_CALLED)
            and one.suffix.lower() in (".lnk", ".command", ".desktop")
        ]
        self.assertEqual(left, [], f"these belong on a desktop: {left}")

    def test_every_name_it_can_write_is_ignored(self) -> None:
        """Every name, not the one this machine happens to use. Ignoring only
        the Windows one, the same accident on Linux writes a file nothing stops
        going in - and only the check above would find it, after the fact."""

        said = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for ending in (".lnk", ".command", ".desktop"):
            with self.subTest(ending=ending):
                self.assertIn(f"{installer.WHAT_IT_IS_CALLED}{ending}", said)

    def test_the_name_this_machine_writes_is_one_of_them(self) -> None:
        """So the list above cannot drift away from what the code really does."""

        said = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(installer.what_the_launcher_is_called(), said)


class WhenAskingWindowsGoesWrongTests(unittest.TestCase):
    def test_a_desktop_that_cannot_be_found_is_said_plainly(self) -> None:
        """Finding the desktop asks Windows, and that can fail on a machine
        locked down enough. Outside the part that catches things going wrong,
        the answer was a Python traceback."""

        def falls_over():
            raise RuntimeError("PowerShell would not run")

        with mock.patch.object(installer, "where_the_desktop_is", falls_over):
            self.assertEqual(installer.main([]), 1)


class SomebodyCanFindItTests(unittest.TestCase):
    """The thing to run has to be findable by somebody who knows nothing."""

    def test_there_is_something_at_the_top_to_double_click(self) -> None:
        found = [
            one.name for one in ROOT.iterdir()
            if one.is_file() and one.suffix.lower() == ".cmd"
        ]
        self.assertTrue(found, "nothing at the top of the project can be double-clicked")
        self.assertTrue(
            any("install" in one.lower() for one in found),
            f"none of these says what it is for: {found}",
        )

    def test_it_runs_the_script_that_does_the_work(self) -> None:
        said = (ROOT / "Install Nexus Harness.cmd").read_text(encoding="utf-8")
        self.assertIn("put_it_on_your_desktop.py", said)
        # It has to work for somebody who has Python under either name.
        self.assertIn("where python", said)
        self.assertIn("where py", said)
        # And stay open long enough to be read when something goes wrong.
        self.assertIn("pause", said)

    def test_it_says_what_to_do_when_python_is_missing(self) -> None:
        """The one thing it cannot do for somebody, so it has to be said."""

        said = (ROOT / "Install Nexus Harness.cmd").read_text(encoding="utf-8")
        self.assertIn("python.org", said)
        self.assertIn("PATH", said)

    def test_the_readme_tells_somebody_to_run_it(self) -> None:
        said = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Install Nexus Harness.cmd", said)


class TheUninstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = ROOT / "Uninstall Nexus Harness.bat"
        self.said = self.script.read_text(encoding="utf-8")

    def test_it_is_a_top_level_file_somebody_can_download_and_double_click(self) -> None:
        self.assertTrue(self.script.is_file())
        self.assertEqual(self.script.parent, ROOT)
        self.assertIn("choice /C YN", self.said)
        self.assertIn("pause", self.said)

    def test_it_has_no_person_or_version_baked_into_its_paths(self) -> None:
        self.assertNotRegex(self.said, r"(?i)C:\\Users\\[^%\"]+")
        self.assertNotRegex(self.said, r"Nexus Harness Setup \d")
        self.assertIn("%LOCALAPPDATA%", self.said)
        self.assertIn("%APPDATA%", self.said)
        self.assertIn("%USERPROFILE%", self.said)
        self.assertIn("GetFolderPath('Desktop')", self.said)

    def test_it_covers_every_supported_windows_install_shape(self) -> None:
        self.assertIn(r"Programs\our-harness-desktop", self.said)
        self.assertIn("Uninstall*.exe", self.said)
        self.assertIn(r"Programs\OurHarness", self.said)
        self.assertIn("Nexus Harness.lnk", self.said)
        self.assertIn("Start Menu", self.said)

    def test_it_preserves_the_things_that_belong_to_the_person(self) -> None:
        lowered = self.said.lower()
        self.assertIn("projects, settings, transcripts, and evidence were preserved", lowered)
        self.assertNotIn(r"rmdir /s /q \"%appdata%", lowered)
        self.assertNotIn(r"rmdir /s /q \"%userprofile%", lowered)

    def test_it_has_silent_and_non_destructive_preview_modes(self) -> None:
        self.assertIn('"/S"', self.said)
        self.assertIn('"/DRY-RUN"', self.said)
        self.assertIn("DRY RUN: nothing will be changed", self.said)

    @unittest.skipUnless(os.name == "nt", "Windows batch file")
    def test_dry_run_succeeds_without_changing_an_isolated_fake_install(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "O'Brien & Team!"
            root.mkdir()
            local = root / "local"
            profile = root / "profile"
            roaming = root / "roaming"
            desktop = profile / "Desktop"
            cli = local / "Programs" / "OurHarness"
            shortcut = desktop / "Nexus Harness.lnk"
            (cli / "bin").mkdir(parents=True)
            desktop.mkdir(parents=True)
            roaming.mkdir(parents=True)
            (cli / "bin" / "harness.cmd").write_text("@echo off\n", encoding="ascii")
            shortcut.write_bytes(b"stand-in shortcut")
            # A dry run against a fake account must not probe this machine's
            # real Known Folder through PowerShell.  Besides escaping the
            # isolation boundary, that external process can stall under CI
            # load.  cmd built-ins still exercise every fake path below.
            empty_path = root / "empty-path"
            empty_path.mkdir()
            environment = {
                **os.environ,
                "PATH": str(empty_path),
                "LOCALAPPDATA": str(local),
                "APPDATA": str(roaming),
                "USERPROFILE": str(profile),
                "OneDrive": str(root / "one-drive"),
                "OneDriveCommercial": "",
                "OneDriveConsumer": "",
            }
            command = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
            done = subprocess.run(
                [command, "/d", "/c", str(self.script), "/S", "/DRY-RUN"],
                cwd=root, env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("DRY RUN", done.stdout)
            self.assertTrue(shortcut.is_file(), "dry-run removed the shortcut")
            self.assertTrue(cli.is_dir(), "dry-run removed the CLI installation")

    @unittest.skipUnless(os.name == "nt", "Windows batch file")
    def test_it_really_removes_only_the_isolated_default_cli_and_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "O'Brien & Team!"
            root.mkdir()
            local = root / "local"
            profile = root / "profile"
            roaming = root / "roaming"
            one_drive = root / "one drive"
            cli = local / "Programs" / "OurHarness"
            shortcuts = [
                profile / "Desktop" / "Nexus Harness.lnk",
                one_drive / "Desktop" / "Nexus Harness.lnk",
                roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                / "Nexus Harness.lnk",
            ]
            (cli / "bin").mkdir(parents=True)
            (cli / "app").mkdir(parents=True)
            (cli / "bin" / "harness.cmd").write_text("@echo off\n", encoding="ascii")
            (cli / "app" / "harness.pyz").write_bytes(b"stand-in")
            for shortcut in shortcuts:
                shortcut.parent.mkdir(parents=True, exist_ok=True)
                shortcut.write_bytes(b"stand-in shortcut")
            unrelated = root / "project" / "keep-me.txt"
            unrelated.parent.mkdir()
            unrelated.write_text("person's work", encoding="utf-8")

            # With PowerShell absent from PATH the Known Folder probe has no
            # answer, which keeps this destructive integration check entirely
            # inside its fake account tree. All cmd built-ins still work.
            empty_path = root / "empty-path"
            empty_path.mkdir()
            environment = {
                **os.environ,
                "PATH": str(empty_path),
                "LOCALAPPDATA": str(local),
                "APPDATA": str(roaming),
                "USERPROFILE": str(profile),
                "OneDrive": str(one_drive),
                "OneDriveCommercial": "",
                "OneDriveConsumer": "",
            }
            command = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
            done = subprocess.run(
                [command, "/d", "/c", str(self.script), "/S"],
                cwd=root, env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertFalse(cli.exists(), "default CLI installation remained")
            self.assertTrue(all(not one.exists() for one in shortcuts))
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "person's work")

    def test_the_readme_explains_uninstall_and_data_preservation(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Uninstall Nexus Harness.bat", readme)
        self.assertIn("Project folders", readme)


class MakingTheShortcutWithoutPowerShellTests(unittest.TestCase):
    """On plenty of company machines PowerShell is gone - scripts turned off, or
    held in a mode where it cannot make one of these at all - and none of that
    is anything the person holding the installer can change. Windows' own
    shortcut maker is not PowerShell and is always there."""

    def a_launcher(self, where: Path):
        return installer.Launcher(
            program=Path(r"C:\Windows\System32\notepad.exe"),
            arguments=["--one", "a b"],
            working_folder=where,
            what_it_is="a test",
            in_its_own_window=True,
            icon=Path(r"C:\Windows\System32\notepad.exe"),
        )

    def test_powershell_is_not_asked_when_it_does_not_have_to_be(self) -> None:
        asked = []
        made = []
        with tempfile.TemporaryDirectory() as folder:
            where = Path(folder) / "one.lnk"

            def pretend(target, launcher, icon):
                made.append(target)
                target.write_bytes(b"a shortcut")

            with mock.patch.object(installer, "_shortcut_the_direct_way", pretend), \
                 mock.patch.object(
                     installer, "_ask_windows", lambda script: asked.append(script) or ""):
                installer._windows_shortcut(where, self.a_launcher(Path(folder)), where)
        self.assertEqual(asked, [], "PowerShell was asked when it did not need to be")
        self.assertEqual(len(made), 1)

    def test_it_falls_back_when_the_maker_will_not(self) -> None:
        asked = []
        with tempfile.TemporaryDirectory() as folder:
            where = Path(folder) / "one.lnk"

            def no_maker(*args, **rest):
                raise OSError("no shortcut maker here")

            def pretend_powershell(script):
                asked.append(script)
                where.write_bytes(b"a shortcut")
                return ""

            with mock.patch.object(installer, "_shortcut_the_direct_way", no_maker), \
                 mock.patch.object(installer, "_ask_windows", pretend_powershell):
                installer._windows_shortcut(where, self.a_launcher(Path(folder)), where)
        self.assertEqual(len(asked), 1)

    def test_neither_way_working_says_so_rather_than_saying_nothing(self) -> None:
        """PowerShell can say nothing and do nothing, in a mode that quietly
        refuses. Believed, somebody is left with no icon and a message saying it
        worked, which is the worst of both."""

        with tempfile.TemporaryDirectory() as folder:
            where = Path(folder) / "one.lnk"

            def no_maker(*args, **rest):
                raise OSError("no shortcut maker here")

            with mock.patch.object(installer, "_shortcut_the_direct_way", no_maker), \
                 mock.patch.object(installer, "_ask_windows", lambda script: ""), \
                 self.assertRaises(RuntimeError) as caught:
                installer._windows_shortcut(where, self.a_launcher(Path(folder)), where)
        self.assertIn("no shortcut", str(caught.exception).lower())

    @unittest.skipUnless(os.name == "nt", "the Windows one")
    def test_it_really_makes_a_shortcut_windows_can_read(self) -> None:
        """The only test here that proves anything: Windows reading back what
        was written. Written by hand rather than made, everything but where it
        points comes back right - and where it points is the whole file."""

        with tempfile.TemporaryDirectory() as folder:
            where = Path(folder) / "made.lnk"
            installer._shortcut_the_direct_way(
                where, self.a_launcher(Path(folder)),
                Path(r"C:\Windows\System32\notepad.exe"))
            self.assertTrue(where.is_file())
            held = where.read_bytes()
        # What every shortcut starts with, and long enough to hold the part that
        # says where it points rather than only the odds and ends.
        self.assertEqual(held[:4], bytes([0x4C, 0, 0, 0]))
        self.assertIn("notepad.exe".encode("utf-16-le"), held)
        self.assertGreater(len(held), 600, "too short to be carrying a target")

    @unittest.skipUnless(os.name == "nt", "the Windows one")
    def test_windows_saying_no_about_the_picture_is_not_ignored(self) -> None:
        """Windows does not refuse this in practice, which is exactly why the
        answer got thrown away. Ignored, the day it does refuse somebody is told
        an icon was made and finds one with the wrong picture on it - or none."""

        def says_no(thing, which, *takes):
            return lambda *args: -2147024809      # Windows for "no"

        with mock.patch.object(installer, "_call", says_no),              self.assertRaises(OSError) as caught:
            installer._set_the_picture(object(), "somewhere.ico")
        self.assertIn("picture", str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "the Windows one")
    def test_the_desktop_is_found_without_asking_powershell(self) -> None:
        """Guessed at as the home folder with Desktop on the end, it is wrong for
        everybody whose desktop is in OneDrive - which at work is most people."""

        asked = []

        def refuse(script):
            asked.append(script)
            raise RuntimeError("PowerShell is turned off on this machine")

        with mock.patch.object(installer, "_ask_windows", refuse):
            held = installer.where_the_desktop_is()
        self.assertTrue(held.is_dir(), f"{held} is not a folder")
        # Counted, not merely survived. Falling back to the home folder with
        # Desktop on the end also survives, and is wrong for everybody whose
        # desktop is in OneDrive - so what is checked is that PowerShell was
        # never asked at all.
        self.assertEqual(asked, [], "PowerShell was asked when it did not need to be")
        self.assertEqual(installer._desktop_out_of_the_registry(), held)


class TheRealBrowserFinderTests(unittest.TestCase):
    """Tested only through a stand-in, the real one could return nothing at all
    and every test still passed - and then the icon opens a browser tab, which
    is the thing somebody complained about."""

    @unittest.skipUnless(os.name == "nt", "the Windows one")
    def test_it_finds_a_browser_on_a_windows_machine(self) -> None:
        import sys as sys_lab

        sys_lab.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import open_the_app

        found = open_the_app.a_browser_that_can_do_windows()
        self.assertIsNotNone(
            found, "no browser found, so the icon can only open a tab")
        self.assertTrue(found.is_file())

    def test_it_looks_for_edge_before_anything_else(self) -> None:
        """Edge is on every Windows machine and cannot be removed, which makes
        it the one that is really there when it matters."""

        import sys as sys_lab

        sys_lab.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import open_the_app

        looked = [str(one).lower() for one in open_the_app._where_browsers_live()]
        self.assertTrue(looked, "it looks nowhere at all")
        self.assertIn("msedge.exe", looked[0])

    def test_it_finds_nothing_when_there_is_nothing(self) -> None:
        import sys as sys_lab

        sys_lab.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import open_the_app

        with mock.patch.object(open_the_app, "_where_browsers_live", lambda: ()):
            self.assertIsNone(open_the_app.a_browser_that_can_do_windows())


if __name__ == "__main__":
    unittest.main()
