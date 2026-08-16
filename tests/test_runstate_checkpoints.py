from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from our_harness.config import load_config
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.runstate import RunCheckpoint, graph_sha256


def frozen_graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "name": "resume-test",
        "entry": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "approval", "type": "approval"},
        ],
        "edges": [{"source": "start", "target": "approval"}],
    }


def checkpoint(run_id: str, *, sequence: int = 4) -> RunCheckpoint:
    return RunCheckpoint.create(
        run_id=run_id,
        task="Resume a bounded fixture run",
        frozen_graph=frozen_graph(),
        current_node="approval",
        state={"plan_ready": True, "attempt": 2, "edge_inputs": {"approved": False}},
        transaction_ids=["1700000000-abc123"],
        transaction_manifests=[
            {
                "schema_version": 2,
                "transaction_id": "1700000000-abc123",
                "state": "applied",
                "changes": [{"path": "src/value.py", "after_sha256": "a" * 64}],
            }
        ],
        remaining_deadline_seconds=120.0,
        pending_approval={"kind": "command", "summary": "Run fixture checks"},
        sequence=sequence,
    )


class RunCheckpointTests(unittest.TestCase):
    def test_checkpoint_survives_restart_and_supports_list_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            config = load_config(root)
            with MemoryStore(config) as memory:
                run_id = memory.start_run("Resume a bounded fixture run", graph_sha256(frozen_graph()))
                saved = memory.save_run_checkpoint(checkpoint(run_id))
                self.assertEqual(saved.version, 1)
                self.assertGreater(saved.updated_at_ms, 0)
            time.sleep(0.01)
            with MemoryStore(load_config(root)) as reopened:
                loaded = reopened.load_run_checkpoint(run_id, expected_graph_sha256=graph_sha256(frozen_graph()))
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.current_node, "approval")
                self.assertEqual(loaded.state["attempt"], 2)
                self.assertEqual(loaded.transaction_ids, ("1700000000-abc123",))
                self.assertLess(loaded.remaining_deadline_seconds, 120.0)
                self.assertGreater(loaded.remaining_deadline_seconds, 100.0)
                self.assertEqual([item.run_id for item in reopened.list_run_checkpoints()], [run_id])
                self.assertFalse(reopened.delete_run_checkpoint(run_id, expected_version=2))
                self.assertTrue(reopened.delete_run_checkpoint(run_id, expected_version=1))
                self.assertIsNone(reopened.load_run_checkpoint(run_id))

    def test_compare_and_swap_rejects_a_stale_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with MemoryStore(load_config(root)) as memory:
                run_id = memory.start_run("Resume a bounded fixture run", graph_sha256(frozen_graph()))
                first = memory.compare_and_swap_run_checkpoint(checkpoint(run_id), 0)
                second_candidate = replace(first, state={**first.state, "attempt": 3}, sequence=5)
                second = memory.compare_and_swap_run_checkpoint(second_candidate, first.version)
                self.assertEqual(second.version, 2)
                with self.assertRaisesRegex(HarnessError, "changed since version 1"):
                    memory.compare_and_swap_run_checkpoint(replace(first, sequence=6), first.version)
                loaded = memory.load_run_checkpoint(run_id)
                assert loaded is not None
                self.assertEqual(loaded.version, 2)
                self.assertEqual(loaded.state["attempt"], 3)

    def test_graph_mismatch_and_stored_graph_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with MemoryStore(load_config(root)) as memory:
                run_id = memory.start_run("Resume a bounded fixture run", graph_sha256(frozen_graph()))
                memory.save_run_checkpoint(checkpoint(run_id))
                with self.assertRaisesRegex(HarnessError, "does not match the requested frozen graph"):
                    memory.load_run_checkpoint(run_id, expected_graph_sha256="b" * 64)
                altered = {**frozen_graph(), "name": "altered"}
                with memory.connection:
                    memory.connection.execute(
                        "UPDATE run_checkpoints SET graph_json=? WHERE run_id=?",
                        (json.dumps(altered), run_id),
                    )
                with self.assertRaisesRegex(HarnessError, "frozen graph hash"):
                    memory.load_run_checkpoint(run_id)

    def test_corrupted_state_and_schema_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with MemoryStore(load_config(root)) as memory:
                run_id = memory.start_run("Resume a bounded fixture run", graph_sha256(frozen_graph()))
                memory.save_run_checkpoint(checkpoint(run_id))
                with memory.connection:
                    memory.connection.execute(
                        "UPDATE run_checkpoints SET state_json=? WHERE run_id=?", ("{not-json", run_id)
                    )
                with self.assertRaisesRegex(HarnessError, "corrupted JSON"):
                    memory.load_run_checkpoint(run_id)
                memory.save_run_checkpoint(checkpoint(run_id))
                with memory.connection:
                    memory.connection.execute(
                        "UPDATE run_checkpoints SET state_json=? WHERE run_id=?",
                        (json.dumps({"plan_ready": False}), run_id),
                    )
                with self.assertRaisesRegex(HarnessError, "payload hash validation"):
                    memory.load_run_checkpoint(run_id)
                memory.save_run_checkpoint(checkpoint(run_id))
                with memory.connection:
                    memory.connection.execute(
                        "UPDATE run_checkpoints SET updated_at_ms=updated_at_ms+1000 WHERE run_id=?", (run_id,)
                    )
                with self.assertRaisesRegex(HarnessError, "payload hash validation"):
                    memory.load_run_checkpoint(run_id)
                memory.save_run_checkpoint(checkpoint(run_id))
                with memory.connection:
                    memory.connection.execute(
                        "UPDATE run_checkpoints SET version=version+1 WHERE run_id=?", (run_id,)
                    )
                with self.assertRaisesRegex(HarnessError, "payload hash validation"):
                    memory.load_run_checkpoint(run_id)
                memory.save_run_checkpoint(checkpoint(run_id))
                with memory.connection:
                    memory.connection.execute(
                        "UPDATE run_checkpoints SET schema_version=99 WHERE run_id=?", (run_id,)
                    )
                with self.assertRaisesRegex(HarnessError, "unsupported schema version"):
                    memory.load_run_checkpoint(run_id)

    def test_sensitive_values_and_absolute_paths_are_never_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with MemoryStore(load_config(root)) as memory:
                run_id = memory.start_run("Resume a bounded fixture run", graph_sha256(frozen_graph()))
                secret = "sk-proj-1234567890abcdefghijklmnop"
                saved = memory.save_run_checkpoint(replace(checkpoint(run_id), state={"api_key": secret}))
                self.assertNotIn("api_key", saved.state)
                saved = memory.save_run_checkpoint(replace(checkpoint(run_id), state={"cwd": str(root.resolve())}))
                self.assertEqual(saved.state["cwd"], "[omitted from retained checkpoint]")
                saved = memory.save_run_checkpoint(
                    replace(checkpoint(run_id), state={"source": "file:///tmp/private/state.json"})
                )
                self.assertEqual(saved.state["source"], "[omitted from retained checkpoint]")
                with self.assertRaisesRegex(HarnessError, "unsupported value type"):
                    memory.save_run_checkpoint(replace(checkpoint(run_id), state={"unsafe": ("tuple",)}))
                count = memory.connection.execute("SELECT COUNT(*) FROM run_checkpoints").fetchone()[0]
                serialized = "\n".join(
                    str(value)
                    for row in memory.connection.execute("SELECT * FROM run_checkpoints")
                    for value in row
                )
                self.assertEqual(count, 1)
                self.assertNotIn(secret, serialized)
                self.assertNotIn(str(root.resolve()), serialized)


if __name__ == "__main__":
    unittest.main()
