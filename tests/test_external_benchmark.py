from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from our_harness.external_benchmark import (
    MINI_SELECTION_SALT,
    ExternalBenchmarkError,
    build_evaluation_plan,
    external_benchmark_manifest,
    select_verified_mini,
    validate_predictions,
)
from our_harness.audit import audit_distribution


ROOT = Path(__file__).resolve().parents[1]


def write_lock(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"provider": "test", "name": "same-model", "temperature": 0},
                "dataset": {"revision": "git:dataset123"},
                "evaluator": {"revision": "git:evaluator123"},
                "budgets": {"max_turns": 40, "wall_time_seconds": 1800, "max_cost_usd": 3},
                "tool_policy": ["shell", "filesystem"],
                "harnesses": [
                    {"id": "our-harness", "revision": "source-sha256:abc"},
                    {"id": "mini-swe-agent", "revision": "git:1234567"},
                    {"id": "simple-strands-agent", "revision": "git:7654321"},
                ],
            }
        ),
        encoding="utf-8",
    )


class ExternalBenchmarkTests(unittest.TestCase):
    def test_manifest_distinguishes_verified_mini_dev_and_full(self) -> None:
        manifest = external_benchmark_manifest()
        suites = {suite["id"]: suite for suite in manifest["suites"]}
        self.assertEqual(suites["swebench-verified-mini-v1"]["task_count"], 50)
        self.assertFalse(suites["swebench-verified-mini-v1"]["official_suite"])
        self.assertEqual(suites["swebench-dev"]["dataset_name"], "princeton-nlp/SWE-bench")
        self.assertEqual(suites["swebench-dev"]["split"], "dev")
        self.assertEqual(suites["swebench-verified-full"]["task_count"], 500)

    def test_mini_selection_is_order_independent_and_pinned(self) -> None:
        values = [f"repo__repo-{index}" for index in range(100)]
        forward = select_verified_mini(values)
        backward = select_verified_mini(reversed(values))
        self.assertEqual(forward, backward)
        self.assertEqual(len(forward), 50)
        self.assertEqual(MINI_SELECTION_SALT, "our-harness-swebench-verified-mini-v1")

    def test_plan_uses_argv_and_requires_exact_frozen_mini_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "fairness.json"
            ids = root / "ids.txt"
            all_ids = root / "all-ids.txt"
            predictions = root / "predictions.jsonl"
            write_lock(lock)
            all_values = [f"repo__repo-{index}" for index in range(500)]
            selected = select_verified_mini(all_values)
            all_ids.write_text("".join(f"{item}\n" for item in all_values), encoding="utf-8")
            ids.write_text("".join(f"{item}\n" for item in selected), encoding="utf-8")
            predictions.write_text(
                "".join(
                    json.dumps(
                        {"instance_id": item, "model_name_or_path": "same-model", "model_patch": "diff --git a/a b/a\n"}
                    )
                    + "\n"
                    for item in selected
                ),
                encoding="utf-8",
            )
            plan = build_evaluation_plan(
                suite_id="swebench-verified-mini-v1",
                predictions_path=predictions,
                fairness_lock_path=lock,
                run_id="verified-mini-test",
                instance_ids_path=ids,
                all_instance_ids_path=all_ids,
            )
            argv = plan["official_evaluator_argv"]
            self.assertIsInstance(argv, list)
            self.assertIn("swebench.harness.run_evaluation", argv)
            self.assertEqual(plan["predictions"]["status"], "validated")
            self.assertEqual(plan["predictions"]["records"], 50)
            self.assertEqual(argv[argv.index("--instance_ids") + 1 :], selected)

            tampered = selected.copy()
            tampered[-1] = next(item for item in all_values if item not in selected)
            ids.write_text("".join(f"{item}\n" for item in tampered), encoding="utf-8")
            with self.assertRaisesRegex(ExternalBenchmarkError, "selection rule"):
                build_evaluation_plan(
                    suite_id="swebench-verified-mini-v1",
                    predictions_path=root / "future.jsonl",
                    fairness_lock_path=lock,
                    run_id="verified-mini-tampered",
                    instance_ids_path=ids,
                    all_instance_ids_path=all_ids,
                )
            ids.write_text("".join(f"{item}\n" for item in selected), encoding="utf-8")

            predictions.write_text(predictions.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ExternalBenchmarkError, "frozen instance set"):
                build_evaluation_plan(
                    suite_id="swebench-verified-mini-v1",
                    predictions_path=predictions,
                    fairness_lock_path=lock,
                    run_id="verified-mini-test",
                    instance_ids_path=ids,
                    all_instance_ids_path=all_ids,
                )

    def test_plan_can_be_frozen_before_predictions_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "fairness.json"
            write_lock(lock)
            plan = build_evaluation_plan(
                suite_id="swebench-dev",
                predictions_path=root / "future.jsonl",
                fairness_lock_path=lock,
                run_id="dev-locked",
                max_workers=8,
            )
            self.assertEqual(plan["predictions"]["status"], "pending")
            self.assertNotIn("--instance_ids", plan["official_evaluator_argv"])
            self.assertEqual(plan["suite"]["task_count"], 225)

    def test_prediction_validation_rejects_duplicates_and_mixed_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "predictions.jsonl"
            record = {"instance_id": "a__a-1", "model_name_or_path": "one", "model_patch": "patch"}
            path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ExternalBenchmarkError, "duplicate"):
                validate_predictions(path)
            second = {"instance_id": "b__b-2", "model_name_or_path": "two", "model_patch": "patch"}
            path.write_text(json.dumps(record) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ExternalBenchmarkError, "one model_name_or_path"):
                validate_predictions(path)

    def test_script_refuses_to_overwrite_frozen_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            all_ids = root / "all.txt"
            output = root / "selected.txt"
            all_ids.write_text("".join(f"repo__repo-{index}\n" for index in range(100)), encoding="utf-8")
            output.write_text("do not replace\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "prepare_external_benchmark.py"),
                    "select-mini",
                    "--all-instance-ids",
                    str(all_ids),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "do not replace\n")

    def test_benchmark_runbooks_pass_distribution_portability_audit(self) -> None:
        result = audit_distribution(ROOT)
        benchmark_findings = [
            finding
            for finding in result["findings"]
            if finding["path"].startswith("docs/BENCHMARK") or finding["path"].startswith("docs/EXTERNAL_BENCHMARKS")
        ]
        self.assertEqual(benchmark_findings, [])


if __name__ == "__main__":
    unittest.main()
