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
        with mock.patch.object(installer, "_installed_app", lambda: installed):
            found = installer.what_to_launch(self.root, is_there=lambda where: True)
        self.assertEqual(found.program, installed)
        self.assertTrue(found.in_its_own_window)
        self.assertIn("installed", found.what_it_is)

    def test_one_built_here_is_next(self) -> None:
        built = self.root / "desktop" / "build-output" / "win-unpacked" / "Nexus Harness.exe"
        with mock.patch.object(installer, "_installed_app", lambda: None), \
             mock.patch.object(installer, "_built_app", lambda root: built):
            found = installer.what_to_launch(self.root, is_there=lambda where: where == built)
        self.assertEqual(found.program, built)
        self.assertTrue(found.in_its_own_window)

    def test_with_neither_of_those_python_starts_the_panel(self) -> None:
        """The one that always works. Nothing but Python is needed, which is
        what most people will have."""

        with mock.patch.object(installer, "_installed_app", lambda: None), \
             mock.patch.object(installer, "_built_app", lambda root: None):
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
        with mock.patch.object(installer, "_installed_app", lambda: installed):
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


if __name__ == "__main__":
    unittest.main()
