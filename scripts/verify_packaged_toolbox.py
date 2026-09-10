"""Execute toolbox/permission acceptance against the shipped Python sources."""
from pathlib import Path
import sys
import unittest


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: bundled-python verify_packaged_toolbox.py <packaged-harness-src>")
    packaged = Path(sys.argv[1]).resolve()
    repository = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(packaged), str(repository / "tests"), str(repository)]
    import our_harness
    if not Path(our_harness.__file__).resolve().is_relative_to(packaged):
        raise SystemExit("Acceptance loaded source-checkout code instead of packaged code")
    names = ["test_harness_tools", "test_full_access_tool_runtime"]
    suite = unittest.TestLoader().loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful() or result.testsRun < 24 or result.skipped:
        raise SystemExit("Packaged toolbox acceptance failed or skipped required checks")
    print("NEXUS_PACKAGED_TOOLBOX_ACCEPTANCE_PASS", result.testsRun, flush=True)


if __name__ == "__main__":
    main()
