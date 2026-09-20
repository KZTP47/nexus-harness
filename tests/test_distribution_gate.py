from pathlib import Path
import tempfile
from unittest import mock

import pytest

from our_harness.distribution_gate import enforce_distribution_gate
from our_harness.models import HarnessError
from scripts import build_windows_desktop as builder


def fixture(root):
    source = root / "src" / "our_harness"
    source.mkdir(parents=True)
    return source / "engine.py"


def test_gate_accepts_engine_relocated_between_unrelated_roots():
    for name in ("first-install", "relocated install"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / name
            module = fixture(root)
            module.write_text("from pathlib import Path\nROOT = Path(__file__).resolve().parent\n")
            assert enforce_distribution_gate(root)["passed"] is True


@pytest.mark.parametrize("location", ["C:/Users/synthetic/mail", "/home/synthetic/mail", "/opt/synthetic/mail"])
def test_gate_rejects_machine_path_before_build(location):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture(root).write_text("MAIL = " + repr(location) + "\n")
        with mock.patch.object(builder, "ROOT", root), mock.patch.object(builder.shutil, "which", return_value="node"), mock.patch.object(builder.runtime, "runtime_build_lock") as build:
            with pytest.raises(HarnessError, match="portability gate failed"):
                builder.build()
            build.assert_not_called()


@pytest.mark.parametrize("name", [".nexus-memory", ".obsidian", "private-project-memory"])
def test_gate_rejects_memory_without_reading_it(name):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        module = fixture(root)
        module.write_text("VALUE = 1\n")
        private = module.parent / name
        private.mkdir()
        (private / "note.md").write_text("Do not read this private fixture")
        with mock.patch("our_harness.distribution_gate.audit_distribution") as audit:
            with pytest.raises(HarnessError, match="private or linked"):
                enforce_distribution_gate(root)
            audit.assert_not_called()


def test_gate_fails_closed_without_engine_source():
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(HarnessError, match="portability gate failed"):
            enforce_distribution_gate(Path(directory))
