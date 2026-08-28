from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from our_harness.cli import main
from our_harness.config import load_config as _load_config
from our_harness.models import HarnessError
from our_harness.workflow import HarnessApplication


def load_config(root: Path, **kwargs):
    local = root / ".harness" / "config.local.json"
    return _load_config(root, explicit=local if local.is_file() else None, **kwargs)


class QueueProvider:
    def __init__(self, responses: list[dict[str, object]]):
        self.responses = list(responses)
        self.requests = []

    def complete(self, _request):
        raise AssertionError("Workflow must use the streaming provider boundary")

    def stream(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected provider call")
        text = json.dumps(self.responses.pop(0))
        yield {"type": "text_delta", "text": text}
        yield {"type": "done", "finish_reason": "stop"}


def provider_responses() -> list[dict[str, object]]:
    content = "VALUE = 2\n"
    return [
        {
            "summary": "Set value",
            "requirement_ledger": [{
                "id": "R1", "requirement": "value is 2",
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
                    "content": content,
                    "delete": False,
                    "reason": "set tested value",
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


def configure(root: Path) -> None:
    (root / ".harness").mkdir()
    check = [
        sys.executable,
        "-c",
        (
            "import json; from pathlib import Path; "
            "assert Path('value.py').read_text() == 'VALUE = 2\\n'; "
            "print(json.dumps({'tests': {'total': 1, 'failed': 0}}))"
        ),
    ]
    (root / ".harness" / "config.local.json").write_text(
        json.dumps({"project": {
            "test_commands": [check],
            "test_evidence_contracts": [{
                "command": check,
                "format": "json-stdout",
                "total_field": "tests.total",
                "failed_field": "tests.failed",
            }],
            "lint_commands": [check],
        }}),
        encoding="utf-8",
    )


def approval_graph(app: HarnessApplication) -> dict[str, object]:
    graph = copy.deepcopy(app.workflow_graph)
    graph["nodes"].append(
        {
            "id": "approval",
            "type": "approval_required",
            "label": "Approval",
            "config": {"message": "Apply the planned file changes?"},
        }
    )
    edge = next(item for item in graph["edges"] if item["id"] == "plan-code")
    edge["target"] = "approval"
    graph["edges"].append(
        {"id": "approval-code", "source": "approval", "target": "coder", "variables": ["plan"]}
    )
    return graph


class WorkflowResumeTests(unittest.TestCase):
    def test_checkpoint_redacts_opaque_environment_secret_and_remains_resumable(self) -> None:
        secret = "opaque-checkpoint-value-7391"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"HARNESS_API_KEY": secret}, clear=False
        ):
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                graph = approval_graph(app)
                app.provider = QueueProvider(provider_responses())
                paused = app.run_task(f"Set the tested value using {secret}", graph=graph)
                run_id = paused["run_id"]
                checkpoint = app.memory.load_run_checkpoint(run_id)
                self.assertNotIn(secret, checkpoint.task)
                self.assertNotIn(secret, json.dumps(checkpoint.payload()))
            database_files = list((root / ".harness" / "memory").glob("harness.db*"))
            self.assertTrue(database_files)
            self.assertTrue(all(secret.encode() not in path.read_bytes() for path in database_files))
            with HarnessApplication(load_config(root)) as app:
                app.decide_run_approval(run_id, True, {"reason": f"approved without retaining {secret}"})
                app.provider = QueueProvider(provider_responses()[1:])
                result = app.resume_task(run_id)
                self.assertEqual(result["state"], "complete")

    def test_approval_pauses_before_mutation_and_decision_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                graph = approval_graph(app)
                app.provider = QueueProvider(provider_responses())
                paused = app.run_task("Set the tested value", graph=graph)
                run_id = paused["run_id"]
                self.assertEqual(paused["state"], "paused")
                self.assertFalse((root / "value.py").exists())
                run_state = app.memory.connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]
                self.assertEqual(run_state, "paused")

            decision = {"reason": "operator reviewed the plan", "ticket": 17}
            with HarnessApplication(load_config(root)) as app:
                first = app.decide_run_approval(run_id, True, decision)
                second = app.decide_run_approval(run_id, True, decision)
                self.assertEqual(first.version, second.version)

            resumed_provider = QueueProvider(provider_responses()[1:])
            with HarnessApplication(load_config(root)) as app:
                app.provider = resumed_provider
                result = app.resume_task(run_id)
                self.assertEqual(result["state"], "complete")
                self.assertIsNone(app.memory.load_run_checkpoint(run_id))
                self.assertEqual(app.resume_task(run_id), result)
            self.assertEqual((root / "value.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertEqual(len(resumed_provider.requests), 2)

    def test_rejected_approval_is_terminal_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                graph = approval_graph(app)
                app.provider = QueueProvider(provider_responses())
                run_id = app.run_task("Set the tested value", graph=graph)["run_id"]
                app.decide_run_approval(run_id, False, {"reason": "scope declined"})
                result = app.resume_task(run_id)
                self.assertEqual(result["state"], "rejected")
                self.assertIsNone(app.memory.load_run_checkpoint(run_id))
            self.assertFalse((root / "value.py").exists())

    def test_crash_after_each_node_resumes_without_repeating_provider_or_file_effects(self) -> None:
        for crash_node in ("start", "planner", "coder", "check-test", "check-lint", "reviewer", "end"):
            with self.subTest(node=crash_node), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                configure(root)
                provider = QueueProvider(provider_responses())
                captured_run_id: list[str] = []

                def crash_at_boundary(event):
                    if event["node"] != crash_node:
                        return
                    if event["kind"] == "checkpoint" or (crash_node == "end" and event["kind"] == "state"):
                        captured_run_id.append(event["run_id"])
                        raise SystemExit("simulated crash")

                with self.assertRaisesRegex(SystemExit, "simulated crash"):
                    with HarnessApplication(load_config(root), crash_at_boundary) as app:
                        app.provider = provider
                        app.run_task("Set the tested value")
                self.assertEqual(len(captured_run_id), 1)

                with HarnessApplication(load_config(root)) as app:
                    app.provider = provider
                    result = app.resume_task(captured_run_id[0])
                    self.assertEqual(result["state"], "complete")
                self.assertEqual(provider.responses, [])
                self.assertEqual(len(provider.requests), 3)
                self.assertEqual((root / "value.py").read_text(encoding="utf-8"), "VALUE = 2\n")
                backups = [item for item in (root / ".harness" / "backups").iterdir() if item.is_dir()]
                self.assertEqual(len(backups), 1)

    def test_resume_rejects_changed_applied_file_and_retains_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            provider = QueueProvider(provider_responses())
            run_id: list[str] = []

            def crash_after_coder(event):
                if event["kind"] == "checkpoint" and event["node"] == "coder":
                    run_id.append(event["run_id"])
                    raise SystemExit("simulated crash")

            with self.assertRaises(SystemExit):
                with HarnessApplication(load_config(root), crash_after_coder) as app:
                    app.provider = provider
                    app.run_task("Set the tested value")
            (root / "value.py").write_text("USER = 9\n", encoding="utf-8")
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                with self.assertRaisesRegex(HarnessError, "changed before verification packet"):
                    app.resume_task(run_id[0])
                self.assertIsNotNone(app.memory.load_run_checkpoint(run_id[0]))
            self.assertEqual((root / "value.py").read_text(encoding="utf-8"), "USER = 9\n")

    def test_crash_from_mutation_event_does_not_repeat_the_coder_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            provider = QueueProvider(provider_responses())
            run_id: list[str] = []

            def crash_from_mutation(event):
                if event["kind"] == "mutation":
                    run_id.append(event["run_id"])
                    raise SystemExit("simulated mutation-event crash")

            with self.assertRaisesRegex(SystemExit, "mutation-event crash"):
                with HarnessApplication(load_config(root), crash_from_mutation) as app:
                    app.provider = provider
                    app.run_task("Set the tested value")
            self.assertEqual((root / "value.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                result = app.resume_task(run_id[0])
                self.assertEqual(result["state"], "complete")
            self.assertEqual(len(provider.requests), 3)
            backups = [item for item in (root / ".harness" / "backups").iterdir() if item.is_dir()]
            self.assertEqual(len(backups), 1)

    def test_crash_before_prepare_restores_path_bearing_source_exactly_without_repeating_coder(self) -> None:
        content = (
            'API_ROUTE = "/api/v1"\n'
            'REMOTE_URL = "https://example.com/v1"\n'
            'CACHE_DIR = "/tmp/cache"\n'
        )
        responses = [
            {
                "summary": "Create path-bearing source",
                "requirement_ledger": [{
                    "id": "R1", "requirement": "source bytes match exactly",
                    "category": "behavior", "counterexample": "R1: routes.py bytes differ from the requested source",
                }],
                "non_goals": [],
                "files": ["routes.py"],
                "verification_commands": [],
                "risks": [],
            },
            {
                "summary": "Create routes",
                "changes": [
                    {
                        "path": "routes.py",
                        "baseline_sha256": None,
                        "content": content,
                        "delete": False,
                        "reason": "retain ordinary paths and URLs",
                    }
                ],
                "commands": [],
                "review": {"verdict": "SKIP", "findings": [{
                    "requirement_id": "R1", "file": "routes.py", "code_path": "module contents",
                    "counterexample_result": "routes.py byte comparison matches",
                }]},
                "memory": [],
            },
            {"verdict": "PASS", "findings": [], "residual_risks": []},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            check = [
                sys.executable,
                "-c",
                (
                    "import json; from pathlib import Path; "
                    f"assert Path('routes.py').read_bytes() == {content.encode('utf-8')!r}; "
                    "print(json.dumps({'tests': {'total': 1, 'failed': 0}}))"
                ),
            ]
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({"project": {
                    "test_commands": [check],
                    "test_evidence_contracts": [{
                        "command": check,
                        "format": "json-stdout",
                        "total_field": "tests.total",
                        "failed_field": "tests.failed",
                    }],
                    "lint_commands": [check],
                }}),
                encoding="utf-8",
            )
            provider = QueueProvider(responses)
            with self.assertRaisesRegex(SystemExit, "crash before prepare"):
                with HarnessApplication(load_config(root)) as app:
                    app.provider = provider
                    with patch.object(
                        app.transactions,
                        "prepare",
                        side_effect=SystemExit("simulated crash before prepare"),
                    ):
                        app.run_task("Create the path-bearing source")

            self.assertEqual(len(provider.requests), 2)
            self.assertFalse((root / "routes.py").exists())
            with HarnessApplication(load_config(root)) as app:
                row = app.memory.connection.execute(
                    "SELECT id FROM runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                run_id = row[0]
                checkpoint = app.memory.load_run_checkpoint(run_id)
                retained_content = checkpoint.state["workflow"]["candidate"]["changes"][0]["content"]
                self.assertEqual(retained_content["schema"], "harness-candidate-content-v1")
                retained_json = json.dumps(checkpoint.payload())
                for exposed in ("/api/v1", "https://example.com/v1", "/tmp/cache"):
                    self.assertNotIn(exposed, retained_json)
                app.provider = provider
                result = app.resume_task(run_id)

            self.assertEqual(result["state"], "complete")
            self.assertEqual(len(provider.requests), 3)
            self.assertEqual((root / "routes.py").read_bytes(), content.encode("utf-8"))
            backups = [item for item in (root / ".harness" / "backups").iterdir() if item.is_dir()]
            self.assertEqual(len(backups), 1)

    def test_candidate_credential_is_rejected_before_mutation_or_checkpoint_persistence(self) -> None:
        secret = "opaque-candidate-secret-8426"
        responses = provider_responses()
        responses[1]["changes"][0]["content"] = f'VALUE = "{secret}"\n'
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"HARNESS_API_KEY": secret}, clear=False
        ):
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                app.provider = QueueProvider(responses)
                with self.assertRaisesRegex(HarnessError, "credential-like material"):
                    app.run_task("Create a value without retaining credentials")
            self.assertFalse((root / "value.py").exists())
            database_files = list((root / ".harness" / "memory").glob("harness.db*"))
            self.assertTrue(database_files)
            self.assertTrue(all(secret.encode() not in path.read_bytes() for path in database_files))

    def test_expired_deadline_and_concurrent_resume_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                graph = approval_graph(app)
                app.provider = QueueProvider(provider_responses())
                run_id = app.run_task("Set the tested value", graph=graph)["run_id"]
                checkpoint = app.memory.load_run_checkpoint(run_id)
                app.memory.compare_and_swap_run_checkpoint(
                    replace(checkpoint, remaining_deadline_seconds=0), checkpoint.version
                )
                with self.assertRaisesRegex(HarnessError, "deadline has expired"):
                    app.resume_task(run_id)

            with HarnessApplication(load_config(root)) as first, HarnessApplication(load_config(root)) as second:
                with first.transactions.locked():
                    with self.assertRaisesRegex(HarnessError, "project transaction lock"):
                        second.resume_task(run_id)

    def test_resume_rejects_config_graph_and_interrupted_transaction_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                graph = approval_graph(app)
                app.provider = QueueProvider(provider_responses())
                config_run = app.run_task("Set the tested value", graph=graph)["run_id"]

            config_path = root / ".harness" / "config.local.json"
            changed_config = json.loads(config_path.read_text(encoding="utf-8"))
            changed_config["workflow"] = {"max_elapsed_seconds": 1799}
            config_path.write_text(json.dumps(changed_config), encoding="utf-8")
            with HarnessApplication(load_config(root)) as app:
                with self.assertRaisesRegex(HarnessError, "current configuration"):
                    app.resume_task(config_run)
                self.assertIsNotNone(app.memory.load_run_checkpoint(config_run))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            provider = QueueProvider(provider_responses())
            run_id: list[str] = []

            def crash_after_start(event):
                if event["kind"] == "checkpoint" and event["node"] == "start":
                    run_id.append(event["run_id"])
                    raise SystemExit("simulated crash")

            with self.assertRaises(SystemExit):
                with HarnessApplication(load_config(root), crash_after_start) as app:
                    app.provider = provider
                    app.run_task("Set the tested value")
            with HarnessApplication(load_config(root)) as app:
                app.workflow_graph["name"] = "changed-default-graph"
                with self.assertRaisesRegex(HarnessError, "current default graph"):
                    app.resume_task(run_id[0])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                graph = approval_graph(app)
                app.provider = QueueProvider(provider_responses())
                run_id = app.run_task("Set the tested value", graph=graph)["run_id"]
            interrupted = root / ".harness" / "backups" / "interrupted"
            interrupted.mkdir(parents=True)
            (interrupted / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "transaction_id": "interrupted",
                        "state": "prepared",
                        "changes": [],
                    }
                ),
                encoding="utf-8",
            )
            with HarnessApplication(load_config(root)) as app:
                with self.assertRaisesRegex(HarnessError, "Unreconciled file transaction"):
                    app.resume_task(run_id)
                self.assertIsNotNone(app.memory.load_run_checkpoint(run_id))

    def test_cancel_rolls_back_once_and_returns_recorded_result_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            provider = QueueProvider(provider_responses())
            run_id: list[str] = []

            def crash_after_coder(event):
                if event["kind"] == "checkpoint" and event["node"] == "coder":
                    run_id.append(event["run_id"])
                    raise SystemExit("simulated crash")

            with self.assertRaises(SystemExit):
                with HarnessApplication(load_config(root), crash_after_coder) as app:
                    app.provider = provider
                    app.run_task("Set the tested value")
            self.assertTrue((root / "value.py").exists())
            decision = {"reason": "operator cancelled continuation"}
            with HarnessApplication(load_config(root)) as app:
                result = app.cancel_run(run_id[0], decision)
                self.assertEqual(result["state"], "cancelled")
                self.assertFalse((root / "value.py").exists())
                self.assertIsNone(app.memory.load_run_checkpoint(run_id[0]))
                self.assertEqual(app.cancel_run(run_id[0], decision), result)

    def test_runs_cli_lists_shows_and_records_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure(root)
            with HarnessApplication(load_config(root)) as app:
                graph = approval_graph(app)
                app.provider = QueueProvider(provider_responses())
                run_id = app.run_task("Set the tested value", graph=graph)["run_id"]

            stdout = StringIO()
            trusted_config = str(root / ".harness" / "config.local.json")
            with redirect_stdout(stdout):
                self.assertEqual(main(["--project", str(root), "--config", trusted_config, "runs", "list"]), 0)
            listed = json.loads(stdout.getvalue())
            self.assertEqual([item["run_id"] for item in listed], [run_id])

            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--project", str(root), "--config", trusted_config, "runs", "show", run_id]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["current_node"], "approval")

            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(
                        [
                            "--project",
                            str(root),
                            "--config",
                            trusted_config,
                            "runs",
                            "approve",
                            run_id,
                            "--decision-json",
                            '{"reason":"reviewed"}',
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(stdout.getvalue())["pending_approval"]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
