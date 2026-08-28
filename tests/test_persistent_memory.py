from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.context import ContextCompiler
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.persistent_memory import (
    DEPLOYMENT_LOCK_OWNER,
    PersistentMemoryHooks,
    _checkout_deployment_lock,
    _is_owned_build_lock_failure,
    initialize_vault,
)
from our_harness.workflow import HarnessApplication


class PersistentMemoryHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        parent = Path(self.temporary.name)
        self.project = parent / "project"
        self.vault = parent / "vault"
        self.project.mkdir()
        self.vault.mkdir()
        (self.project / ".harness").mkdir()
        initialize_vault(self.project, self.vault)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["persistent_memory"].update(
            {"enabled": True, "vault_path": str(self.vault), "max_context_chars": 20_000}
        )
        self.config = LoadedConfig(data, self.project.resolve(), [], {})

    def test_langgraph_consults_before_and_writes_after(self) -> None:
        (self.vault / "Architecture.md").write_text(
            "The scheduler must keep mutation serialized.", encoding="utf-8"
        )
        hooks = PersistentMemoryHooks(self.config)
        nodes = set(hooks.graph.get_graph().nodes)
        self.assertIn("consult_vault_before_work", nodes)
        self.assertIn("rebuild_app_and_installer", nodes)
        self.assertIn("write_vault_after_work", nodes)

        context, consulted = hooks.before_session("change scheduler mutation")
        self.assertIn("mutation serialized", context)
        self.assertIn("Architecture.md", consulted)

        written = hooks.after_session(
            "change scheduler mutation",
            {
                "run_id": "run-1",
                "state": "complete",
                "changes": [{"path": "secret.py", "content": "SOURCE MUST NOT BE STORED"}],
                "summary": "kept bounded",
            },
        )
        note = (self.vault / written).read_text(encoding="utf-8")
        self.assertIn("kept bounded", note)
        self.assertNotIn("SOURCE MUST NOT BE STORED", note)

    def test_langgraph_refuses_closeout_until_desktop_deployment_succeeds(self) -> None:
        self.config.data["persistent_memory"]["enforce_desktop_deployment"] = True
        deployed: list[Path] = []

        def deploy(project: Path) -> dict[str, str]:
            deployed.append(project)
            return {
                "state": "deployed",
                "application": "desktop/build-output/win-unpacked/Nexus Harness.exe",
                "installer": "desktop/build-output/Nexus Harness Setup 0.1.0.exe",
                "desktop_shortcut": "Desktop/Nexus Harness.lnk",
                "icon_source": "Nexus Harness.exe",
            }

        hooks = PersistentMemoryHooks(self.config, deploy_desktop=deploy)
        written = hooks.after_session("ship it", {"run_id": "deploy", "state": "complete"})
        self.assertEqual(deployed, [self.project.resolve()])
        note = (self.vault / written).read_text(encoding="utf-8")
        self.assertIn('"closeout_deployment"', note)
        self.assertIn('"desktop_shortcut": "Desktop/Nexus Harness.lnk"', note)

        def fail(_project: Path) -> dict[str, str]:
            raise HarnessError("build failed")

        failing = PersistentMemoryHooks(self.config, deploy_desktop=fail)
        with self.assertRaisesRegex(HarnessError, "build failed"):
            failing.after_session("do not close", {"run_id": "blocked", "state": "complete"})
        self.assertEqual(list((self.vault / "Sessions").glob("*-blocked.md")), [])

    def test_closeout_retries_all_windows_owned_artifact_lock_wordings(self) -> None:
        owned = r"C:\project\desktop\build-output\win-unpacked\resources\runtime\locked.pyc"
        self.assertTrue(
            _is_owned_build_lock_failure(
                f"remove {owned}: The process cannot access the file because it is being used by another process."
            )
        )
        self.assertTrue(_is_owned_build_lock_failure(f"remove {owned}: Access is denied."))
        self.assertFalse(
            _is_owned_build_lock_failure(
                r"remove C:\some-other-app\locked.pyc: being used by another process"
            )
        )

    def test_checkout_deployment_lock_serializes_processes_and_recovers_dead_owner(self) -> None:
        marker = self.project / "deployment-order.txt"
        source = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source) + os.pathsep + environment.get("PYTHONPATH", "")
        worker = (
            "import os,sys,time; from pathlib import Path; "
            "from our_harness.persistent_memory import _checkout_deployment_lock; "
            "root=Path(sys.argv[1]); marker=Path(sys.argv[2]); label=sys.argv[3]; delay=float(sys.argv[4]); "
            "lock=_checkout_deployment_lock(root,5,purpose=label); lock.__enter__(); "
            "marker.open('a',encoding='utf-8').write(label+':start\\n'); "
            "time.sleep(delay); marker.open('a',encoding='utf-8').write(label+':end\\n'); lock.__exit__(None,None,None)"
        )
        first = subprocess.Popen(
            [sys.executable, "-c", worker, str(self.project), str(marker), "one", "0.45"],
            env=environment,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.is_file() and "one:start" in marker.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            first.kill()
            self.fail("first deployment-lock process did not acquire the lock")
        owner = json.loads((self.project / DEPLOYMENT_LOCK_OWNER).read_text(encoding="utf-8"))
        self.assertEqual(owner["state"], "owning")
        self.assertEqual(owner["pid"], first.pid)
        self.assertEqual(owner["project_root"], str(self.project.resolve()))

        second = subprocess.Popen(
            [sys.executable, "-c", worker, str(self.project), str(marker), "two", "0.02"],
            env=environment,
        )
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertEqual(
            marker.read_text(encoding="utf-8").splitlines(),
            ["one:start", "one:end", "two:start", "two:end"],
        )

        crashed = (
            "import os,sys; from pathlib import Path; "
            "from our_harness.persistent_memory import _checkout_deployment_lock; "
            "lock=_checkout_deployment_lock(Path(sys.argv[1]),5,purpose='crashed'); lock.__enter__(); "
            "Path(sys.argv[2]).write_text('owned',encoding='utf-8'); os._exit(0)"
        )
        crash_marker = self.project / "crashed-owner.txt"
        dead = subprocess.Popen(
            [sys.executable, "-c", crashed, str(self.project), str(crash_marker)],
            env=environment,
        )
        self.assertEqual(dead.wait(timeout=10), 0)
        self.assertTrue(crash_marker.is_file())
        started = time.monotonic()
        with _checkout_deployment_lock(self.project, 2, purpose="recovered") as recovered:
            self.assertEqual(recovered["pid"], os.getpid())
        self.assertLess(time.monotonic() - started, 1)

    def test_binding_rejects_every_other_project(self) -> None:
        other = self.project.parent / "other"
        other.mkdir()
        data = copy.deepcopy(self.config.data)
        config = LoadedConfig(data, other.resolve(), [], {})
        with self.assertRaisesRegex(HarnessError, "does not match this project"):
            PersistentMemoryHooks(config)

    def test_vault_inside_git_tree_is_rejected(self) -> None:
        (self.vault / ".git").mkdir()
        with self.assertRaisesRegex(HarnessError, "Git worktree"):
            PersistentMemoryHooks(self.config)

    def test_consulted_vault_is_in_compiled_agent_context(self) -> None:
        (self.vault / "Decision.md").write_text("Use a bounded queue.", encoding="utf-8")
        hooks = PersistentMemoryHooks(self.config)
        context, consulted = hooks.before_session("queue")
        with MemoryStore(self.config) as memory:
            compiled = ContextCompiler(
                self.config,
                memory,
                persistent_memory_context=context,
                persistent_memory_consulted=consulted,
            ).compile("queue", [])
        self.assertIn("Use a bounded queue", compiled.dynamic)
        self.assertIn("Decision.md", compiled.manifest["persistent_memory"]["consulted"])

    def test_harness_application_enforces_hooks_around_a_run(self) -> None:
        with HarnessApplication(self.config) as app:
            app._run_task_locked = lambda *_args: {"run_id": "integration", "state": "complete"}
            result = app.run_task("integration boundary")
        self.assertEqual(result["state"], "complete")
        notes = list((self.vault / "Sessions").glob("*-integration.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("integration boundary", notes[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
