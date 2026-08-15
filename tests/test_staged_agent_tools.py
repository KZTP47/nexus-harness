from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.agent_tools import AgentToolSession, parse_native_tool_calls, tool_loop_instructions
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.staged_coding import StagedCodingWorkspace, VerificationAction


class FixtureDeadline:
    def __init__(self, seconds: float = 30):
        self.end = time.monotonic() + seconds

    def check(self, operation: str) -> None:
        if time.monotonic() >= self.end:
            raise HarnessError(f"Workflow deadline exceeded while trying to {operation}")

    def remaining_seconds(self, operation: str, cap: float | None = None) -> float:
        self.check(operation)
        remaining = self.end - time.monotonic()
        return remaining if cap is None else min(remaining, cap)


def config_for(root: Path) -> LoadedConfig:
    return LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root.resolve(), [], {})


def check_action() -> VerificationAction:
    return VerificationAction(
        "unit",
        (
            sys.executable,
            "-c",
            "from pathlib import Path; raise SystemExit(0 if 'value = 2' in Path('module.py').read_text() else 1)",
        ),
    )


def content(result: dict[str, object]) -> dict[str, object]:
    value = json.loads(str(result["content"]))
    if not isinstance(value, dict):
        raise AssertionError("tool content was not an object")
    return value


class StagedAgentToolTests(unittest.TestCase):
    def test_failed_check_or_resubmit_clears_prior_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            config = config_for(root)
            with MemoryStore(config) as memory:
                session = AgentToolSession(config, memory, FixtureDeadline(), lambda *_: None)
                workspace = StagedCodingWorkspace(config, ["module.py"], [check_action()])
                session.attach_staged_workspace(workspace)
                digest = workspace.file_state("module.py")["sha256"]
                session.execute(
                    "coder", "replace", "stage_replace_file",
                    {"path": "module.py", "expected_sha256": digest, "content": "value = 2\n"},
                )
                session.execute("coder", "verify", "stage_run_verification", {"action": "unit"})
                session.execute("coder", "submit", "stage_finalize", {})
                self.assertEqual(session.staged_candidate().revision, 1)

                with patch.object(workspace, "run_verification", side_effect=HarnessError("check failed")):
                    failed = session.execute(
                        "coder", "verify-again", "stage_run_verification", {"action": "unit"}
                    )
                self.assertEqual(failed["status"], "error")
                with self.assertRaisesRegex(HarnessError, "No finalized staged candidate"):
                    session.staged_candidate()

                session.execute("coder", "submit-again", "stage_finalize", {})
                self.assertEqual(session.staged_candidate().revision, 1)
                with patch.object(workspace, "finalize", side_effect=HarnessError("submit failed")):
                    failed_submit = session.execute("coder", "submit-failed", "stage_finalize", {})
                self.assertEqual(failed_submit["status"], "error")
                with self.assertRaisesRegex(HarnessError, "No finalized staged candidate"):
                    session.staged_candidate()
                session.detach_staged_workspace()

    def test_fallback_envelope_tools_are_coder_only_and_submit_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "module.py"
            source.write_text("value = 1\n", encoding="utf-8")
            config = config_for(root)
            with MemoryStore(config) as memory:
                session = AgentToolSession(config, memory, FixtureDeadline(), lambda *_: None)
                self.assertNotIn("stage_finalize", {item["name"] for item in session.definitions("coder")})
                workspace = StagedCodingWorkspace(config, ["module.py"], [check_action()])
                stage_root = workspace.stage_root
                session.attach_staged_workspace(workspace)
                self.assertEqual(session.definitions("coder", set()), [])
                read_only = {item["name"] for item in session.definitions("coder", {"workspace.read"})}
                self.assertIn("read_file", read_only)
                self.assertNotIn("stage_replace_file", read_only)
                write_enabled = {item["name"] for item in session.definitions("coder", {"workspace.read", "workspace.write"})}
                self.assertIn("stage_replace_file", write_enabled)
                planner_names = {item["name"] for item in session.definitions("planner")}
                coder_definitions = session.definitions("coder")
                coder_names = {item["name"] for item in coder_definitions}
                self.assertNotIn("stage_replace_file", planner_names)
                self.assertIn("stage_replace_file", coder_names)
                instructions = tool_loop_instructions(coder_definitions)
                self.assertIn("CODER STAGED-EDIT LOOP", instructions)
                self.assertIn("stage_finalize", instructions)

                state = content(session.execute("coder", "state-1", "stage_file_state", {"path": "module.py"}))
                failed_check = session.execute(
                    "coder", "verify-round-1", "stage_run_verification", {"action": "unit"}
                )
                self.assertFalse(content(failed_check)["result"]["passed"])
                patched = session.execute(
                    "coder",
                    "patch-1",
                    "stage_apply_patch",
                    {
                        "path": "module.py",
                        "expected_sha256": state["sha256"],
                        "replacements": [{"old": "value = 1", "new": "value = 2", "count": 1}],
                        "reason": "correct fixture",
                    },
                )
                self.assertEqual(patched["status"], "ok")
                verified = session.execute(
                    "coder", "verify-round-2", "stage_run_verification", {"action": "unit"}
                )
                self.assertEqual(verified["status"], "ok")
                self.assertTrue(content(verified)["result"]["passed"])
                submitted = session.execute("coder", "submit-1", "stage_finalize", {})
                submitted_content = content(submitted)
                self.assertNotIn("content", submitted_content["files"][0])
                self.assertNotIn("value = 2", str(submitted_content))
                candidate = session.staged_candidate()
                self.assertEqual(candidate.changes[0].content.decode("utf-8").strip(), "value = 2")
                self.assertEqual(source.read_text(encoding="utf-8").strip(), "value = 1")
                denied = session.execute("planner", "wrong-node", "stage_file_state", {"path": "module.py"})
                self.assertEqual(denied["status"], "error")
                session.detach_staged_workspace()
                self.assertFalse(stage_root.exists())
                self.assertNotIn("stage_replace_file", {item["name"] for item in session.definitions("coder")})

    def test_native_tool_fragment_executes_typed_staged_replace(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            config = config_for(root)
            with MemoryStore(config) as memory:
                session = AgentToolSession(config, memory, FixtureDeadline(), lambda *_: None)
                workspace = StagedCodingWorkspace(config, ["module.py"], [check_action()])
                session.attach_staged_workspace(workspace)
                digest = workspace.file_state("module.py")["sha256"]
                calls = parse_native_tool_calls(
                    [
                        {
                            "index": 0,
                            "id": "native-replace",
                            "function": {
                                "name": "stage_replace_file",
                                "arguments": {
                                    "path": "module.py",
                                    "expected_sha256": digest,
                                    "content": "value = 2\n",
                                },
                            },
                        }
                    ]
                )
                result = session.execute("coder", calls[0]["call_id"], calls[0]["name"], calls[0]["arguments"])
                self.assertEqual(result["status"], "ok")
                invalid = session.execute(
                    "coder",
                    "bad-digest",
                    "stage_delete_file",
                    {"path": "module.py", "expected_sha256": "BAD"},
                )
                self.assertEqual(invalid["status"], "error")
                self.assertIn("lower-case SHA-256", str(invalid["content"]))
                session.detach_staged_workspace()

    def test_live_duplicate_is_cached_but_restart_never_replays_staged_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "module.py").write_text("value = 1\n", encoding="utf-8")
            config = config_for(root)
            arguments: dict[str, object]
            with MemoryStore(config) as memory:
                run_id = memory.start_run("staged replay")
                session = AgentToolSession(config, memory, FixtureDeadline(), lambda *_: None, run_id=run_id)
                first_workspace = StagedCodingWorkspace(config, ["module.py"], [check_action()])
                session.attach_staged_workspace(first_workspace)
                arguments = {
                    "path": "module.py",
                    "expected_sha256": first_workspace.file_state("module.py")["sha256"],
                    "content": "value = 2\n",
                }
                first = session.execute("coder", "replace-live", "stage_replace_file", arguments)
                duplicate = session.execute("coder", "replace-live", "stage_replace_file", arguments)
                self.assertEqual(first["status"], "ok")
                self.assertTrue(duplicate["duplicate"])
                state = session.budget_state()
                session.detach_staged_workspace()
                retained = memory.connection.execute(
                    "SELECT COUNT(*) FROM agent_tool_journal WHERE tool_name LIKE 'stage_%'"
                ).fetchone()[0]
                self.assertEqual(retained, 0)

                restarted = AgentToolSession(config, memory, FixtureDeadline(), lambda *_: None, run_id=run_id)
                restarted.restore_budget_state(state)
                second_workspace = StagedCodingWorkspace(config, ["module.py"], [check_action()])
                restarted.attach_staged_workspace(second_workspace)
                refused_old_id = restarted.execute("coder", "replace-live", "stage_replace_file", arguments)
                self.assertEqual(refused_old_id["status"], "error")
                self.assertIn("reused", str(refused_old_id["content"]))
                applied = restarted.execute("coder", "replace-after-restart", "stage_replace_file", arguments)
                self.assertEqual(applied["status"], "ok")
                self.assertEqual(second_workspace.file_state("module.py")["sha256"], content(applied)["sha256"])
                restarted.detach_staged_workspace()


if __name__ == "__main__":
    unittest.main()
