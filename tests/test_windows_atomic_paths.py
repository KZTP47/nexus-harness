"""Exercise real Windows I/O while emulating a machine without long-path policy."""
from __future__ import annotations

import json
import io
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from our_harness import changes, goal_workspaces as gw, agent_workspaces as aw
from our_harness.filesystem_paths import filesystem_path


@unittest.skipUnless(os.name == "nt", "Windows filesystem namespace")
class WindowsAtomicPaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.real_open = os.open

    def legacy_open(self, path, *args, **kwargs):
        text = os.fspath(path)
        if not text.startswith("\\\\?\\") and len(text) >= 260:
            raise FileNotFoundError(2, "Legacy Windows path limit", text)
        return self.real_open(path, *args, **kwargs)

    def test_long_temp_name_reproduces_and_atomic_write_survives_replace_and_failure(self):
        parent = self.base / ("nested-" + "x" * 90)
        parent /= "more-" + "y" * (235 - len(str(parent)) - 6)
        parent.mkdir(parents=True)
        target = parent / "build-deploy.yml"
        self.assertLess(len(str(target)), 260)
        with patch.object(os, "open", side_effect=self.legacy_open):
            with self.assertRaises(FileNotFoundError):
                tempfile.mkstemp(prefix=".build-and-deploy-command.", suffix=".tmp", dir=parent)
            changes.atomic_write(target, b"first")
            changes.atomic_write(target, b"second")
            with patch.object(os, "replace", side_effect=PermissionError("write denied")):
                with self.assertRaises(PermissionError):
                    changes.atomic_write(target, b"must not replace")
        self.assertEqual(target.read_bytes(), b"second")
        self.assertEqual([p.name for p in parent.iterdir()], [target.name])

    def test_goal_copy_with_nested_project_recovers_after_interrupted_admission(self):
        source = self.base / "selected-project"
        source.mkdir()
        runtime = self.base / "runtime-for-another-account"
        goal = {"goal_id": "portable-goal-" + "a" * 24, "project": {"path": str(source)}}
        candidate = runtime / "goal-workspaces" / goal["goal_id"] / "project"
        relative = Path("repositories") / ("nested-" + "z" * (218 - len(str(candidate)) - 21)) / "build-and-deploy.yml"
        target = candidate / relative
        # Put the final file below MAX_PATH and its normal temporary name above it.
        relative = relative.parent / ("c" * (254 - len(str(target.parent)) - 5) + ".yml")
        original = source / relative
        original.parent.mkdir(parents=True)
        original.write_bytes(b"portable source bytes")
        with patch.object(gw, "_copy_file", side_effect=OSError("interrupted copy")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                gw.create(goal, runtime)
        with patch.object(os, "open", side_effect=self.legacy_open):
            goal["execution_workspace"] = gw.create(json.loads(json.dumps(goal)), runtime)
        self.assertEqual((gw.root(goal, runtime) / relative).read_bytes(), original.read_bytes())
        restored = json.loads(json.dumps(goal))
        self.assertEqual(gw.create(restored, runtime), goal["execution_workspace"])
        gw.validate(restored, runtime, full=True)
        self.assertEqual(original.read_bytes(), b"portable source bytes")

    def test_deep_files_reach_agents_publish_and_roll_back_without_machine_policy(self):
        source = self.base / "project"
        source.mkdir()
        runtime = self.base / "runtime"
        relative = "/".join(["nested-" + "x" * 75] * 3 + ["workflow.yml"])
        original = source / relative
        changes.atomic_write(original, b"original")
        self.assertGreater(len(str(original)), 260)
        agent = {"id": "arbitrary-agent", "who": "arbitrary-route"}
        goal = {"goal_id": "deep-goal", "project": {"path": str(source)}, "agents": [agent]}
        def legacy(operation):
            def call(path, *args, **kwargs):
                if not isinstance(path, int):
                    text = os.fspath(path)
                    if not text.startswith("\\\\?\\") and len(text) >= 260:
                        raise FileNotFoundError(2, "Legacy Windows path limit", text)
                return operation(path, *args, **kwargs)
            return call
        with ExitStack() as stack:
            for module, name in [(os, "open"), (io, "open"), (os, "stat"), (os, "scandir"), (os, "unlink")]:
                stack.enter_context(patch.object(module, name, side_effect=legacy(getattr(module, name))))
            goal["execution_workspace"] = gw.create(goal, runtime)
            with aw.workspace(goal, agent, runtime) as work:
                self.assertEqual(aw.inspect(goal, runtime, agent["id"], relative)["content"], "original")
                changes.atomic_write(work.root / relative, b"agent result")
                delta = work.changes()
                self.assertEqual(delta[0]["content"], "agent result")
            team = gw.root(goal, runtime)
            changes.atomic_write(team / relative, b"agent result")
            with gw.publication(goal, runtime):
                receipt = gw.prepare_publish(goal, runtime)
                result = gw.publish(goal, runtime, receipt)
            self.assertEqual(filesystem_path(original).read_bytes(), b"agent result")
            changes.FileTransaction(source).rollback(result["transaction_id"])
            self.assertEqual(filesystem_path(original).read_bytes(), b"original")
            gw.validate(json.loads(json.dumps(goal)), runtime, full=True)
