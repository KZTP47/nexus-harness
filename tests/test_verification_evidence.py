from __future__ import annotations

import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.execution import CommandRunner
from our_harness.models import CommandResult
from our_harness.verification import analyze_verification
from our_harness.workflow import HarnessApplication


def result(
    stdout: str, *, exit_code: int = 0, stderr: str = "", output_truncated: bool = False,
) -> dict:
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": False,
        "output_truncated": output_truncated,
    }


def analyze(command: list[str], output: str, **kwargs) -> dict:
    return analyze_verification([command], [result(output)], test_indexes={0}, **kwargs)


class VerificationEvidenceTests(unittest.TestCase):
    def test_legacy_application_and_quick_start_path_use_the_same_positive_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = copy.deepcopy(DEFAULT_CONFIG)
            command = ["realmat-check"]
            data["project"]["test_commands"] = [command]
            app = HarnessApplication.__new__(HarnessApplication)
            app.config = LoadedConfig(data, Path(temporary), [], {})
            app._detections = lambda: []

            class Runner:
                def run(self, _command, timeout=None):
                    return CommandResult(command, temporary, 0, "No unit tests to run", "", 1)

            app.runner = Runner()
            found = HarnessApplication.test(app)
        self.assertFalse(found["passed"])
        self.assertTrue(found["no_test_evidence"])

    def test_realmat_no_test_phrase_arbitrary_ok_and_logs_never_pass(self) -> None:
        for command, output in (
            (["company-test"], "No unit tests to run"),
            (["go", "test", "./..."], "ok"),
            (["company-test"], "ok"),
            (["company-test"], "connected\nbuild complete\neverything looks good"),
            (["python", "-m", "unittest", "discover"], "Ran 0 tests in 0.000s\n\nOK"),
            (["python", "-m", "pytest"], "no tests ran in 0.01s"),
        ):
            with self.subTest(command=command, output=output):
                found = analyze(command, output)
                self.assertFalse(found["passed"])
                self.assertTrue(found["no_test_evidence"])
                self.assertEqual(found["verification_evidence"], [])

    def test_common_framework_summaries_prove_positive_execution(self) -> None:
        cases = (
            (["python", "-m", "unittest", "discover"], "Ran 12 tests in 0.040s\n\nOK", "unittest", 12),
            (["python", "-m", "pytest", "-q"], "........  [100%]\n8 passed in 0.4s", "pytest", 8),
            (["npm", "run", "test"], "> vitest\n RUN v3.0\n Test Files 1 passed (1)\n Tests  6 passed (6)", "vitest", 6),
            (["npx", "jest"], "Test Suites: 1 passed, 1 total\nTests:       5 passed, 5 total", "jest", 5),
            (["npx", "playwright", "test"], "Running 3 tests using 1 worker\n  3 passed (1.2s)", "playwright", 3),
            (["cargo", "test"], "running 2 tests\ntest a ... ok\ntest b ... ok\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured", "cargo", 2),
            (["dotnet", "test"], "Passed! - Failed: 0, Passed: 4, Skipped: 0, Total: 4, Duration: 1 s", "dotnet", 4),
            (["dotnet", "test"], "Total tests: 3\n     Passed: 3", "dotnet", 3),
            (["mvn", "test"], "[INFO] Tests run: 7, Failures: 0, Errors: 0, Skipped: 0", "maven", 7),
            (["./gradlew", "test"], "9 tests completed, 0 failed\nBUILD SUCCESSFUL", "gradle", 9),
        )
        for command, output, framework, executed in cases:
            with self.subTest(framework=framework):
                found = analyze(command, output)
                self.assertTrue(found["passed"], found)
                self.assertEqual(found["verification_evidence"][0]["framework"], framework)
                self.assertEqual(found["verification_evidence"][0]["executed"], executed)

    def test_go_requires_machine_events_naming_actual_tests(self) -> None:
        events = "\n".join((
            json.dumps({"Action": "run", "Package": "example", "Test": "TestOne"}),
            json.dumps({"Action": "pass", "Package": "example", "Test": "TestOne"}),
            json.dumps({"Action": "pass", "Package": "example"}),
        ))
        found = analyze(["go", "test", "-json", "./..."], events)
        self.assertTrue(found["passed"])
        self.assertEqual(found["verification_evidence"][0]["executed"], 1)
        package_only = analyze(
            ["go", "test", "-json", "./..."],
            json.dumps({"Action": "pass", "Package": "example"}),
        )
        self.assertFalse(package_only["passed"])

    def test_unknown_custom_runner_needs_exact_trusted_json_contract(self) -> None:
        command = ["company-test", "--json"]
        output = json.dumps({"summary": {"executed": 14, "failed": 0}})
        self.assertFalse(analyze(command, output)["passed"])
        contract = [{
            "command": command,
            "format": "json-stdout",
            "total_field": "summary.executed",
            "failed_field": "summary.failed",
        }]
        found = analyze(command, output, evidence_contracts=contract)
        self.assertTrue(found["passed"])
        self.assertEqual(found["verification_evidence"][0]["executed"], 14)
        self.assertFalse(analyze(
            command,
            json.dumps({"summary": {"executed": 14, "failed": 1}}),
            evidence_contracts=contract,
        )["passed"])
        self.assertFalse(analyze(["company-test"], output, evidence_contracts=contract)["passed"])

    def test_json_stdout_contract_ignores_benign_stderr_but_never_uses_it_as_evidence(self) -> None:
        command = ["company-test", "--json"]
        contract = [{
            "command": command,
            "format": "json-stdout",
            "total_field": "summary.executed",
            "failed_field": "summary.failed",
        }]
        report = json.dumps({"summary": {"executed": 3, "failed": 0}})
        with_diagnostic = analyze_verification(
            [command], [result(report, stderr="optional diagnostic warning")],
            test_indexes={0}, evidence_contracts=contract,
        )
        self.assertTrue(with_diagnostic["passed"], with_diagnostic)
        stderr_only = analyze_verification(
            [command], [result("", stderr=report)],
            test_indexes={0}, evidence_contracts=contract,
        )
        self.assertFalse(stderr_only["passed"])
        nonzero = analyze_verification(
            [command], [result(report, exit_code=2, stderr="diagnostic")],
            test_indexes={0}, evidence_contracts=contract,
        )
        self.assertFalse(nonzero["passed"])

    def test_real_command_runner_capped_output_can_never_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["execution"]["max_output_bytes"] = 1024
            runner = CommandRunner(LoadedConfig(data, root, [], {}))
            command = [
                sys.executable,
                "-c",
                (
                    "import json,sys,time; "
                    "print(json.dumps({'summary': {'executed': 1, 'failed': 0}}), flush=True); "
                    "time.sleep(0.1); sys.stderr.write('diagnostic-' * 10000)"
                ),
            ]
            command_result = runner.run(command)
            contract = [{
                "command": command,
                "format": "json-stdout",
                "total_field": "summary.executed",
                "failed_field": "summary.failed",
            }]
            found = analyze_verification(
                [command], [command_result.to_dict()], test_indexes={0}, evidence_contracts=contract,
            )
        self.assertEqual(command_result.exit_code, 0)
        self.assertTrue(command_result.output_truncated)
        self.assertEqual(json.loads(command_result.stdout)["summary"]["executed"], 1)
        self.assertFalse(found["passed"])
        self.assertIn("truncated", found["verification_problems"][0]["reason"])

    def test_truncated_framework_summary_can_never_verify(self) -> None:
        found = analyze_verification(
            [["python", "-m", "pytest", "-q"]],
            [result("4 passed in 0.2s", output_truncated=True)],
            test_indexes={0},
        )
        self.assertFalse(found["passed"])
        self.assertEqual(found["verification_evidence"], [])
        self.assertIn("truncated", found["verification_problems"][0]["reason"])

    def test_silent_lint_can_pass_but_silent_test_cannot(self) -> None:
        no_op = analyze(["python", "-c", "pass"], "")
        self.assertFalse(no_op["passed"])
        lint = analyze_verification(
            [["ruff", "check", "."]], [result("")], test_indexes=set()
        )
        self.assertTrue(lint["passed"])


if __name__ == "__main__":
    unittest.main()
