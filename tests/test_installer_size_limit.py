"""The byte ceiling changes independently of the release time budget."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 400 * 1024 * 1024


class InstallerSizeLimitTests(unittest.TestCase):
    def test_three_installer_surfaces_share_the_bounded_ceiling(self):
        for name in ("install_nexus_harness.ps1", "build_windows_offline_bundle.ps1"):
            source = (ROOT / "scripts" / name).read_text()
            self.assertEqual(int(re.search(r"\$maximumInstallerBytes = (\d+)", source)[1]), LIMIT)
        source = (ROOT / "scripts" / "install_nexus_harness.py").read_text()
        self.assertIn("MAX_INSTALLER_BYTES = 400 * 1024 * 1024", source)

    def test_powershell_installer_guards_accept_exact_limit_and_reject_overflow(self):
        host = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not host:
            self.skipTest("PowerShell required for actual source guard execution")
        for name, variable in (("install_nexus_harness.ps1", "$installerBytes"), ("build_windows_offline_bundle.ps1", "$installer.Length")):
            source = (ROOT / "scripts" / name).read_text()
            constant = re.search(r"\$maximumInstallerBytes = \d+", source)[0]
            guard = re.search(r"if \(" + re.escape(variable) + r" -le 0 -or " + re.escape(variable) + r" -gt \$maximumInstallerBytes\) \{[^}]+\}", source)[0]
            # Execute the actual product condition, with scalar metadata only.
            setup = "$installerBytes=$size; $installer=[pscustomobject]@{Length=$size}"
            script = constant + "; foreach($size in @(-1,0,1,367001601,419430400,419430401)) { " + setup + "; try { " + guard + "; Write-Output 'accept' } catch { Write-Output 'reject' } }"
            result = subprocess.run([host, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.split(), ["reject", "reject", "accept", "accept", "accept", "reject"], name)


if __name__ == "__main__":
    unittest.main()
