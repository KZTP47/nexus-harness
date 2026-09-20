"""Startup must not depend on Windows' machine-wide long-path policy."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from our_harness.filesystem_paths import filesystem_path


def test_engine_bootstrap_imports_modules_beyond_legacy_path_limit():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary) / ("portable-engine-" * 10)
        package = filesystem_path(root / "our_harness")
        package.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "src/our_harness/__init__.py"
        shutil.copyfile(source, package / "__init__.py")
        module = "module_" + "deep_" * 12
        target = package / (module + ".py")
        target.write_text("VALUE = 42\n", encoding="utf-8")
        assert len(str(target)) > 260
        code = (f"import sys; sys.path.insert(0, {str(root)!r}); "
                f"from our_harness.{module} import VALUE; assert VALUE == 42")
        result = subprocess.run([sys.executable, "-B", "-c", code],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        if os.name == "nt":
            shutil.rmtree(filesystem_path(root))
