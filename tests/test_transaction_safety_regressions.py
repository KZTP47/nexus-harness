from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.changes import FileTransaction, file_sha256
from our_harness.config import load_config as _load_config
from our_harness.models import ChangePlan, HarnessError
from our_harness.workflow import HarnessApplication


def load_config(root: Path, **kwargs):
    local = root / ".harness" / "config.local.json"
    return _load_config(root, explicit=local if local.is_file() else None, **kwargs)


class CapturingProvider:
    def __init__(self, responses: list[dict[str, object]]):
        self.responses = list(responses)
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected provider call")
        yield {"type": "text_delta", "text": json.dumps(self.responses.pop(0))}
        yield {"type": "done", "finish_reason": "stop"}


def workflow_responses() -> list[dict[str, object]]:
    return [
        {
            "summary": "Set value",
            "requirement_ledger": [{
                "id": "R1", "requirement": "value is two",
                "category": "behavior", "counterexample": "R1: value.py does not set VALUE to 2",
            }],
            "non_goals": [],
            "files": ["value.py"],
            "verification_commands": [],
            "risks": [],
        },
        {
            "summary": "Apply value",
            "changes": [
                {
                    "path": "value.py",
                    "baseline_sha256": None,
                    "content": "VALUE = 2\n",
                    "delete": False,
                    "reason": "requested value",
                }
            ],
            "commands": [],
            "review": {"verdict": "SKIP", "findings": [{
                "requirement_id": "R1", "file": "value.py", "code_path": "VALUE assignment",
                "counterexample_result": "value.py now contains VALUE = 2",
            }]},
            "memory": [],
        },
        {"verdict": "PASS", "findings": [], "residual_risks": []},
    ]


def configure_workflow(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
    (root / ".harness").mkdir()
    check = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('value.py').read_text() == 'VALUE = 2\\n'",
    ]
    (root / ".harness" / "config.local.json").write_text(
        json.dumps({"project": {"test_commands": [check]}}),
        encoding="utf-8",
    )


class TransactionSafetyRegressionTests(unittest.TestCase):
    def test_prepare_writes_intent_and_backups_before_project_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "value.txt"
            target.write_text("before", encoding="utf-8")
            transaction = FileTransaction(root)
            transaction_id = transaction.new_transaction_id()
            plan = ChangePlan("value.txt", file_sha256(target), "after")
            prepared = transaction.prepare([plan], transaction_id)
            self.assertEqual(prepared["state"], "prepared")
            self.assertEqual(prepared["transaction_id"], transaction_id)
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertEqual(transaction.reconcile()[0]["status"], "not_applied")
            applied = transaction.apply([plan], transaction_id)
            self.assertEqual(applied["state"], "applied")
            self.assertEqual(target.read_text(encoding="utf-8"), "after")

    def test_project_lock_excludes_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transaction = FileTransaction(root)
            code = (
                "import sys\n"
                "from pathlib import Path\n"
                "from our_harness.changes import FileTransaction\n"
                "from our_harness.models import HarnessError\n"
                "try:\n"
                "    with FileTransaction(Path(sys.argv[1])).locked(timeout_seconds=0.2):\n"
                "        raise SystemExit(3)\n"
                "except HarnessError:\n"
                "    raise SystemExit(0)\n"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            with transaction.locked():
                result = subprocess.run(
                    [sys.executable, "-B", "-c", code, str(root)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_apply_revalidates_identity_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "value.txt"
            target.write_text("baseline", encoding="utf-8")
            transaction = FileTransaction(root)
            original = transaction._assert_unchanged
            changed = False

            def race(relative, expected, operation):
                nonlocal changed
                if operation == "backup" and not changed:
                    changed = True
                    target.write_text("concurrent user content", encoding="utf-8")
                return original(relative, expected, operation)

            with patch.object(transaction, "_assert_unchanged", side_effect=race):
                with self.assertRaisesRegex(HarnessError, "Baseline conflict before backup"):
                    transaction.apply([ChangePlan("value.txt", file_sha256(target), "replacement")])
            self.assertEqual(target.read_text(encoding="utf-8"), "concurrent user content")

    def test_apply_revalidates_identity_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "value.txt"
            target.write_text("baseline", encoding="utf-8")
            transaction = FileTransaction(root)
            original = transaction._assert_unchanged
            changed = False

            def race(relative, expected, operation):
                nonlocal changed
                if operation == "replacement" and not changed:
                    changed = True
                    target.write_text("late user content", encoding="utf-8")
                return original(relative, expected, operation)

            with patch.object(transaction, "_assert_unchanged", side_effect=race):
                with self.assertRaisesRegex(HarnessError, "Baseline conflict before replacement"):
                    transaction.apply([ChangePlan("value.txt", file_sha256(target), "replacement")])
            self.assertEqual(target.read_text(encoding="utf-8"), "late user content")

    def test_rollback_validates_every_backup_before_any_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first before", encoding="utf-8")
            second.write_text("second before", encoding="utf-8")
            transaction = FileTransaction(root)
            manifest = transaction.apply(
                [
                    ChangePlan("first.txt", file_sha256(first), "first after"),
                    ChangePlan("second.txt", file_sha256(second), "second after"),
                ]
            )
            backup = root / ".harness" / "backups" / str(manifest["transaction_id"]) / "files" / "second.txt"
            backup.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "backup failed integrity verification"):
                transaction.rollback(str(manifest["transaction_id"]))
            self.assertEqual(first.read_text(encoding="utf-8"), "first after")
            self.assertEqual(second.read_text(encoding="utf-8"), "second after")

    def test_rollback_retries_after_io_failure_from_mixed_before_after_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first before", encoding="utf-8")
            second.write_text("second before", encoding="utf-8")
            transaction = FileTransaction(root)
            manifest = transaction.apply(
                [
                    ChangePlan("first.txt", file_sha256(first), "first after"),
                    ChangePlan("second.txt", file_sha256(second), "second after"),
                ]
            )
            original_restore = transaction._restore_rollback_record

            def fail_second_boundary(path, before, record):
                if path.name == "first.txt":
                    raise OSError("injected restore failure")
                return original_restore(path, before, record)

            with patch.object(transaction, "_restore_rollback_record", side_effect=fail_second_boundary):
                with self.assertRaisesRegex(OSError, "injected restore failure"):
                    transaction.rollback(str(manifest["transaction_id"]))
            self.assertEqual(first.read_text(encoding="utf-8"), "first after")
            self.assertEqual(second.read_text(encoding="utf-8"), "second before")
            self.assertEqual(transaction.reconcile()[0]["status"], "rollback_in_progress")
            result = transaction.recover(str(manifest["transaction_id"]), "rollback")
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(first.read_text(encoding="utf-8"), "first before")
            self.assertEqual(second.read_text(encoding="utf-8"), "second before")
            self.assertEqual(transaction.reconcile(), [])

    def test_rollback_retries_hard_crash_after_restore_before_progress_write(self) -> None:
        for crash_name in ("second.txt", "first.txt"):
            with self.subTest(restore_boundary=crash_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                first = root / "first.txt"
                second = root / "second.txt"
                first.write_text("first before", encoding="utf-8")
                second.write_text("second before", encoding="utf-8")
                transaction = FileTransaction(root)
                manifest = transaction.apply(
                    [
                        ChangePlan("first.txt", file_sha256(first), "first after"),
                        ChangePlan("second.txt", file_sha256(second), "second after"),
                    ]
                )
                original_restore = transaction._restore_rollback_record

                def crash_after_restore(path, before, record):
                    original_restore(path, before, record)
                    if path.name == crash_name:
                        raise SystemExit("injected hard crash")

                with patch.object(transaction, "_restore_rollback_record", side_effect=crash_after_restore):
                    with self.assertRaisesRegex(SystemExit, "injected hard crash"):
                        transaction.rollback(str(manifest["transaction_id"]))
                retained = transaction.load_manifest(str(manifest["transaction_id"]))
                self.assertEqual(retained["state"], "rolling_back")
                self.assertNotIn(crash_name, retained["rollback_completed"])
                result = transaction.rollback(str(manifest["transaction_id"]))
                self.assertEqual(result["rolled_back"], ["first.txt", "second.txt"])
                self.assertEqual(first.read_text(encoding="utf-8"), "first before")
                self.assertEqual(second.read_text(encoding="utf-8"), "second before")

    def test_late_scope_mutation_cannot_return_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure_workflow(root)
            target = root / "value.py"
            mutated = False

            def sink(event):
                nonlocal mutated
                if event["kind"] == "state" and event["node"] == "end" and not mutated:
                    mutated = True
                    target.write_text("USER = 3\n", encoding="utf-8")

            with HarnessApplication(load_config(root), sink) as application:
                application.provider = CapturingProvider(workflow_responses())
                with self.assertRaisesRegex(HarnessError, "changed after verification packet"):
                    application.run_task("Set the tested value")
                state = application.memory.connection.execute("SELECT state FROM runs").fetchone()[0]
            self.assertEqual(state, "failed")
            self.assertEqual(target.read_text(encoding="utf-8"), "USER = 3\n")

    def test_reviewer_receives_only_policy_and_frozen_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure_workflow(root)
            marker = "AUTHOR_DYNAMIC_MARKER_7f345"
            (root / "notes.txt").write_text(marker, encoding="utf-8")
            provider = CapturingProvider(workflow_responses())
            with HarnessApplication(load_config(root)) as application:
                application.provider = provider
                result = application.run_task("Set the tested β value")
                stored_packet = application.memory.connection.execute(
                    "SELECT packet_json FROM review_packets WHERE run_id=?", (result["run_id"],)
                ).fetchone()[0]
            self.assertEqual(result["state"], "complete")
            review_request = provider.requests[-1]
            self.assertTrue(review_request.system_prefix.startswith("HARNESS IMMUTABLE REVIEW POLICY v2"))
            self.assertEqual(review_request.dynamic_context, "")
            self.assertTrue(review_request.messages[0]["content"].startswith("PACKET\n"))
            self.assertNotIn(marker, review_request.system_prefix)
            self.assertNotIn(marker, review_request.messages[0]["content"])
            self.assertIn("β", review_request.messages[0]["content"])
            self.assertIn("β", stored_packet)
            self.assertNotIn("\\u03b2", stored_packet)

    def test_hard_crash_after_apply_reconciles_without_repeating_coder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure_workflow(root)
            provider = CapturingProvider(workflow_responses())
            run_id = ""
            with self.assertRaisesRegex(SystemExit, "hard crash after apply"):
                with HarnessApplication(load_config(root)) as application:
                    application.provider = provider
                    original_apply = application.transactions.apply

                    def crash_after_apply(plans, transaction_id=None):
                        original_apply(plans, transaction_id)
                        raise SystemExit("hard crash after apply")

                    with patch.object(application.transactions, "apply", side_effect=crash_after_apply):
                        application.run_task("Set the tested value")
            with HarnessApplication(load_config(root)) as application:
                row = application.memory.connection.execute(
                    "SELECT id FROM runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                run_id = row[0]
                application.provider = provider
                result = application.resume_task(run_id)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(len(provider.requests), 3)
            self.assertEqual((root / "value.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            backups = list((root / ".harness" / "backups").iterdir())
            self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
