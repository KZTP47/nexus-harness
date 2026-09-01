from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from our_harness.benchmark import (
    AGENTIC_TASK_TIMEOUT_SECONDS,
    CASE_FUNCTIONS,
    _EVALUATOR_BOOTSTRAP,
    _diagnostic_text,
    _provider_profile,
    agentic_fixtures,
    benchmark_manifest,
    render_markdown,
    result_schema,
    run_benchmark,
)
from our_harness.cli import main
from our_harness.context import stable_prefix
from our_harness.redaction import CredentialRedactor


ROOT = Path(__file__).resolve().parents[1]


def write_scripted_provider(
    folder: Path,
    *,
    wrong: bool = False,
    malicious: bool = False,
    repair_after_public_failure: bool = False,
    equivalent_alternative: bool = False,
    contract_error_once: bool = False,
    assert_control_data_hidden: bool = False,
) -> Path:
    fixture_map = {}
    for task in agentic_fixtures()["tasks"]:
        target = task["allowed_paths"][0]
        fixture_map[target] = {
            "expected": task["expected_files"][target],
            "wrong": task["initial_files"][target],
            "alternative": (
                "def clamp(value, lower, upper):\n"
                "    if lower > upper:\n"
                "        raise ValueError(\"lower must not exceed upper\")\n"
                "    return min(upper, max(lower, value))\n"
                if target == "bounded.py"
                else task["expected_files"][target]
            ),
        }
    malicious_source = """import json
from importlib.resources import files
from pathlib import Path

fixtures = json.loads(files('our_harness.templates').joinpath('benchmark_agentic_fixtures.json').read_text())
target = Path(__file__).name
for task in fixtures['tasks']:
    if target in task['expected_files']:
        expected = task['expected_files'][target]
        Path(__file__).write_text(expected, encoding='utf-8')
        exec(compile(expected, __file__, 'exec'), globals())
        break
"""
    script = folder / "scripted_provider.py"
    script.write_text(
        """import hashlib
import json
import re
import sys

FIXTURES = json.loads(%r)
WRONG = %r
MALICIOUS = %r
REPAIR = %r
ALTERNATIVE = %r
CONTRACT_ERROR_ONCE = %r
ASSERT_CONTROL_DATA_HIDDEN = %r
STATE_PATH = %r
CONTRACT_SECRET = "sk-contract-secret-1234567890"
MALICIOUS_SOURCE = %r
payload = json.load(sys.stdin)
prompt = payload["messages"][0]["content"]
whole = payload.get("dynamic_context", "") + "\\n" + prompt
if ASSERT_CONTROL_DATA_HIDDEN and "benchmark_public_evaluator.py" in whole:
    raise SystemExit("harness evaluator path leaked into model context")
if ASSERT_CONTROL_DATA_HIDDEN and "Act as the repair coder" in prompt and "RuntimeError" not in prompt:
    raise SystemExit("public test stderr was omitted from repair feedback")
if "HARNESS IMMUTABLE REVIEW POLICY" in payload.get("system_prefix", ""):
    value = {"verdict": "PASS", "findings": [], "residual_risks": []}
else:
    target = next(name for name in FIXTURES if name in whole)
    if "Act as the planner" in prompt:
        task_match = re.search(r"\\n\\nTASK\\n(.*?)(?:\\n\\nALLOWED VERIFICATION COMMANDS|\\Z)", prompt, re.DOTALL)
        if not task_match:
            raise SystemExit("planner task section missing")
        source_quote = task_match.group(1).strip()
        value = {
            "summary": "Apply the requested bounded fix",
            "requirement_ledger": [{
                "id": "R1",
                "requirement": "public and hidden behavior",
                "category": "behavior",
                "counterexample": "R1: a named task behavior is still wrong",
            }],
            "non_goals": [],
            "files": [target],
            "verification_commands": [],
            "risks": [],
        }
        if CONTRACT_ERROR_ONCE:
            from pathlib import Path
            state_path = Path(STATE_PATH)
            key = target + "-planner"
            seen = set(state_path.read_text(encoding="utf-8").splitlines()) if state_path.exists() else set()
            if key not in seen:
                state_path.write_text("\\n".join(sorted(seen | {key})), encoding="utf-8")
                value = {"schema_version": 1, "api_key": CONTRACT_SECRET, **value}
            elif "expected_result_fields" not in prompt or "invalid_output_excerpt" not in prompt:
                raise SystemExit("contract repair omitted precise feedback")
            elif CONTRACT_SECRET in prompt or "[REDACTED]" not in prompt:
                raise SystemExit("contract repair did not redact provider output")
    else:
        match = re.search(r"FILE " + re.escape(target) + r"\\nSHA256 ([0-9a-f]{64})", prompt)
        if not match:
            raise SystemExit("missing baseline")
        if MALICIOUS:
            content = MALICIOUS_SOURCE
        elif REPAIR and "Act as the repair coder" not in prompt:
            content = "raise RuntimeError('first attempt must fail public tests')\\n"
        elif ALTERNATIVE:
            content = FIXTURES[target]["alternative"]
        else:
            content = FIXTURES[target]["wrong" if WRONG else "expected"]
        value = {
            "summary": "Implement the requested behavior",
            "changes": [{"path": target, "baseline_sha256": match.group(1), "content": content, "delete": False, "reason": "fix"}],
            "commands": [],
            "review": {"verdict": "SKIP", "findings": [{
                "requirement_id": "R1",
                "file": target,
                "code_path": "requested behavior branch",
                "counterexample_result": "The task counterexample now has the requested result",
            }]},
            "memory": [],
        }
print(json.dumps({"text": json.dumps(value), "input_tokens": 10, "output_tokens": 5, "finish_reason": "stop"}))
"""
        % (
            json.dumps(fixture_map),
            wrong,
            malicious,
            repair_after_public_failure,
            equivalent_alternative,
            contract_error_once,
            assert_control_data_hidden,
            str(folder / "contract-state.txt"),
            malicious_source,
        ),
        encoding="utf-8",
    )
    profile = folder / "provider-profile.json"
    profile.write_text(
        json.dumps({"provider": {"name": "local", "model": "scripted", "endpoint": "http://127.0.0.1:1", "command": [sys.executable, str(script)]}}),
        encoding="utf-8",
    )
    return profile


class BenchmarkTests(unittest.TestCase):
    def test_local_qwen_profile_fits_inside_agentic_task_budget(self) -> None:
        profile = json.loads((ROOT / "benchmark-local-qwen-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["provider"]["timeout_seconds"], 180)
        self.assertEqual(profile["workflow"]["max_elapsed_seconds"], 900)
        self.assertGreater(profile["workflow"]["max_elapsed_seconds"], profile["provider"]["timeout_seconds"] * 2)
        self.assertEqual(profile["provider"]["role_output_caps"], {
            "planner": 768, "coder": 768, "evaluator": 512, "merge": 768,
        })

    def test_codex_subscription_profile_is_portable_and_selects_a_named_route(self) -> None:
        path = ROOT / "benchmark-codex-subscription-profile.json"
        raw = path.read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"[A-Za-z]:[\\/]")
        profile, workflow, digest = _provider_profile(path)
        self.assertEqual(profile["benchmark_provider_route"], "codex_subscription")
        selected = profile["providers"]["codex_subscription"]
        self.assertEqual(selected["command"], ["codex"])
        self.assertEqual(selected["auth_mode"], "chatgpt")
        self.assertEqual(selected["reasoning_effort"], "high")
        self.assertEqual(workflow["max_elapsed_seconds"], 1800)
        self.assertEqual(len(digest), 64)

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_benchmark(seed=20260814)

    def test_manifest_and_evaluator_are_versioned_and_weighted(self) -> None:
        manifest = benchmark_manifest()
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["version"], 3)
        self.assertEqual(sum(case["weight"] for case in manifest["cases"]), 100)
        self.assertEqual({case["id"] for case in manifest["cases"]}, set(CASE_FUNCTIONS))
        self.assertEqual(len(agentic_fixtures()["tasks"]), 3)
        self.assertEqual(agentic_fixtures()["version"], 2)
        self.assertIn("behavioral_resolution", manifest["agentic"]["task_scoring"])

    def test_provider_free_result_records_reproducibility_evidence(self) -> None:
        result = self.result
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["deterministic_score"], 100)
        self.assertEqual(result["case_summary"], {"total": 12, "passed": 12, "failed": 0})
        self.assertEqual(result["agentic_score"], "not_run")
        self.assertEqual(result["agentic"]["status"], "not_run")
        self.assertIn("No provider profile", result["agentic"]["reason"])
        self.assertRegex(result["benchmark"]["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["benchmark"]["result_schema_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["harness"]["artifact_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(result["harness"]["source_sha256"])
        self.assertTrue(all(case["elapsed_ms"] >= 0 and case["status"] == "pass" for case in result["cases"]))

    def test_seed_controls_case_order(self) -> None:
        repeated = run_benchmark(seed=20260814)
        self.assertEqual([case["id"] for case in self.result["cases"]], [case["id"] for case in repeated["cases"]])

    def test_markdown_and_result_schema_cover_the_machine_result(self) -> None:
        markdown = render_markdown(self.result)
        self.assertIn("Deterministic score: **100/100**", markdown)
        self.assertIn("Agentic score: **not_run**", markdown)
        schema = result_schema()
        self.assertEqual(schema["properties"]["schema_version"]["const"], 3)
        self.assertNotIn("hqs", self.result)
        self.assertTrue(set(schema["required"]).issubset(self.result))
        task_schema = schema["$defs"]["agenticTask"]
        self.assertFalse(task_schema["additionalProperties"])
        self.assertIn("checks", task_schema["required"])
        self.assertIn("diagnostics", task_schema["required"])
        self.assertIn("allowed_scope_only", task_schema["properties"]["checks"]["required"])
        self.assertFalse(schema["$defs"]["agenticMetrics"]["additionalProperties"])

    def test_result_schema_closes_envelopes_and_binds_hqs_to_agentic_state(self) -> None:
        schema = result_schema()
        for key in ("benchmark", "run", "harness", "environment", "case_summary"):
            self.assertFalse(schema["properties"][key]["additionalProperties"])
        completed = schema["properties"]["agentic"]["oneOf"][1]
        self.assertFalse(completed["additionalProperties"])
        self.assertFalse(schema["properties"]["cases"]["items"]["additionalProperties"])
        state_contract = schema["allOf"][0]
        self.assertIn("hqs", state_contract["then"]["required"])
        self.assertEqual(state_contract["then"]["properties"]["agentic_score"]["type"], "number")
        self.assertEqual(state_contract["else"]["properties"]["agentic_score"]["const"], "not_run")
        self.assertEqual(state_contract["else"]["not"]["required"], ["hqs"])

    def test_deterministic_config_ignores_harness_environment_and_hashes_full_package(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HARNESS__WORKFLOW__NAME": "environment-should-not-run",
                "HARNESS__PLUGINS__PATHS": '["outside.py"]',
                "HARNESS__MEMORY__EMBEDDING_MODEL": "environment-should-not-load",
            },
            clear=False,
        ):
            result = run_benchmark(seed=17)
        self.assertEqual(result["deterministic_score"], 100)
        sources = result["harness"]["source_sha256"]
        for owner in ("cli.py", "memory.py", "runstate.py", "plugins.py", "review_panel.py", "mcp.py", "detect.py"):
            self.assertIn(f"our_harness/{owner}", sources)

    def test_deterministic_benchmark_passes_on_python_311_when_available(self) -> None:
        candidates: list[list[str]] = []
        if os.name == "nt" and shutil.which("py"):
            candidates.append(["py", "-3.11"])
        if shutil.which("python3.11"):
            candidates.append(["python3.11"])
        if sys.version_info[:2] == (3, 11):
            candidates.append([sys.executable])
        launcher = None
        for candidate in candidates:
            available = subprocess.run(candidate + ["--version"], capture_output=True, text=True, check=False)
            if available.returncode == 0:
                launcher = candidate
                break
        if launcher is None:
            self.skipTest("Python 3.11 is not available")

        probe = (
            "import json,sys; sys.path.insert(0,sys.argv[1]); "
            "from our_harness.benchmark import run_benchmark; "
            "print(json.dumps(run_benchmark(seed=20260814)))"
        )
        completed = subprocess.run(
            [*launcher, "-B", "-I", "-c", probe, str(ROOT / "src")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(result["environment"]["python"].split(".")[:2], ["3", "11"])
        self.assertEqual(result["deterministic_score"], 100)
        self.assertEqual(result["case_summary"], {"total": 12, "passed": 12, "failed": 0})

    def test_scripted_provider_resolves_all_isolated_agentic_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = write_scripted_provider(Path(temporary))
            result = run_benchmark(seed=73, provider_profile=str(profile), repetitions=1)
        self.assertEqual(result["agentic_score"], 100.0)
        self.assertEqual(result["hqs"], 100.0)
        self.assertEqual(result["agentic"]["attempts"], 3)
        self.assertEqual(result["agentic"]["resolved"], 3)
        self.assertTrue(all(task["status"] == "resolved" for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["checks"]["hidden_tests"] for task in result["agentic"]["tasks"]))
        self.assertGreaterEqual(result["agentic"]["metrics"]["provider_calls"], 9)

    def test_public_failure_is_returned_to_one_repair_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = write_scripted_provider(Path(temporary), repair_after_public_failure=True)
            result = run_benchmark(seed=79, provider_profile=str(profile), repetitions=1)
        self.assertEqual(result["agentic_score"], 100.0)
        self.assertTrue(all(task["status"] == "resolved" for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["checks"]["public_tests"] for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["checks"]["hidden_tests"] for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["diagnostics"]["trajectory"]["failure_count"] >= 1 for task in result["agentic"]["tasks"]))
        self.assertGreaterEqual(result["agentic"]["metrics"]["provider_calls"], 12)

    def test_control_evaluator_path_is_hidden_while_public_failure_drives_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = write_scripted_provider(
                Path(temporary),
                repair_after_public_failure=True,
                assert_control_data_hidden=True,
            )
            result = run_benchmark(seed=97, provider_profile=str(profile), repetitions=1)
        self.assertEqual(result["agentic_score"], 100.0)
        self.assertTrue(all(task["status"] == "resolved" for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["diagnostics"]["trajectory"]["failure_count"] >= 1 for task in result["agentic"]["tasks"]))

    def test_behaviorally_equivalent_patch_resolves_without_exact_gold_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = write_scripted_provider(Path(temporary), equivalent_alternative=True)
            result = run_benchmark(seed=83, provider_profile=str(profile), repetitions=1)
        bounded = next(task for task in result["agentic"]["tasks"] if task["id"] == "ARS-001")
        self.assertEqual(result["agentic_score"], 100.0)
        self.assertEqual(bounded["status"], "resolved")
        self.assertTrue(bounded["checks"]["public_tests"])
        self.assertTrue(bounded["checks"]["hidden_tests"])
        self.assertTrue(bounded["checks"]["allowed_scope_only"])
        self.assertFalse(bounded["checks"]["exact_patch"])
        self.assertFalse(bounded["checks"]["exact_tree"])

    def test_diagnostics_retain_redacted_public_evidence_without_hidden_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = write_scripted_provider(Path(temporary))
            result = run_benchmark(seed=89, provider_profile=str(profile), repetitions=1)
        rendered = json.dumps(result["agentic"]["tasks"], sort_keys=True)
        hidden_source_marker = "inverted bounds were accepted"
        self.assertNotIn(hidden_source_marker, rendered)
        for task in result["agentic"]["tasks"]:
            diagnostics = task["diagnostics"]
            self.assertGreater(diagnostics["trajectory"]["event_count"], 0)
            self.assertRegex(diagnostics["trajectory"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(diagnostics["candidate_patch_sha256"], r"^[0-9a-f]{64}$")
            self.assertLessEqual(len(diagnostics["trajectory"]["excerpt"]), 12_100)

    def test_static_prefix_has_no_conflicting_response_wrapper_and_feedback_redacts_secrets(self) -> None:
        prefix, _ = stable_prefix()
        self.assertNotIn('"schema_version":1,"response"', prefix)
        self.assertIn("current request supplies its complete response schema", prefix)
        secret = "sk-example-secret-value-123456"
        with patch.dict(os.environ, {"HARNESS_API_KEY": secret}, clear=False):
            rendered = _diagnostic_text(
                '{"schema_version":1,"api_key":"' + secret + '"}',
                CredentialRedactor(),
                {},
            )
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_generic_provider_corrects_an_extra_schema_wrapper_from_precise_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = write_scripted_provider(Path(temporary), contract_error_once=True)
            result = run_benchmark(seed=97, provider_profile=str(profile), repetitions=1)
        self.assertEqual(result["agentic_score"], 100.0)
        self.assertTrue(all(task["status"] == "resolved" for task in result["agentic"]["tasks"]))
        self.assertGreaterEqual(result["agentic"]["metrics"]["provider_calls"], 12)

    def test_hidden_evaluators_reject_seeded_public_test_passing_wrong_patches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = write_scripted_provider(Path(temporary), wrong=True)
            result = run_benchmark(seed=91, provider_profile=str(profile), repetitions=1)
        self.assertEqual(result["agentic_score"], 0.0)
        self.assertEqual(result["hqs"], 40.0)
        self.assertTrue(all(task["checks"]["public_tests"] for task in result["agentic"]["tasks"]))
        self.assertTrue(all(not task["checks"]["hidden_tests"] for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["status"] == "failed" for task in result["agentic"]["tasks"]))

    def test_installed_package_fixture_rewrite_candidate_scores_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            profile = write_scripted_provider(temporary_root, malicious=True)
            exposed_package = temporary_root / "exposed-harness-fixtures.zip"
            with zipfile.ZipFile(exposed_package, "w") as archive:
                archive.writestr("our_harness/__init__.py", "")
                archive.writestr("our_harness/templates/__init__.py", "")
                archive.writestr(
                    "our_harness/templates/benchmark_agentic_fixtures.json",
                    json.dumps(agentic_fixtures()),
                )
            with patch.dict(os.environ, {"PYTHONPATH": str(exposed_package)}, clear=False):
                result = run_benchmark(seed=109, provider_profile=str(profile), repetitions=1)
        self.assertEqual(result["agentic_score"], 0.0)
        self.assertTrue(all(task["status"] == "failed" for task in result["agentic"]["tasks"]))
        self.assertTrue(all(not task["checks"]["exact_patch"] for task in result["agentic"]["tasks"]))
        # The failed workflow rolls back before external grading, so the
        # original public behavior still passes. Hidden behavior remains wrong.
        self.assertTrue(all(task["checks"]["public_tests"] for task in result["agentic"]["tasks"]))
        self.assertTrue(all(not task["checks"]["hidden_tests"] for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["checks"]["public_tree_unchanged"] for task in result["agentic"]["tasks"]))
        self.assertTrue(all(task["checks"]["hidden_tree_unchanged"] for task in result["agentic"]["tasks"]))

    def test_evaluator_bootstrap_keeps_stdlib_and_workspace_but_drops_ambient_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ambient = root / "ambient"
            templates = ambient / "our_harness" / "templates"
            templates.mkdir(parents=True)
            (ambient / "our_harness" / "__init__.py").write_text("", encoding="utf-8")
            (templates / "__init__.py").write_text("", encoding="utf-8")
            (templates / "benchmark_agentic_fixtures.json").write_text(
                json.dumps({"gold": "ambient-package-must-not-be-readable"}),
                encoding="utf-8",
            )

            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "candidate.py").write_text(
                "import json\n"
                "from importlib.resources import files\n"
                "try:\n"
                "    files('our_harness.templates').joinpath("
                "'benchmark_agentic_fixtures.json').read_bytes()\n"
                "except (ImportError, ModuleNotFoundError):\n"
                "    ambient_harness_was_imported = False\n"
                "else:\n"
                "    ambient_harness_was_imported = True\n"
                "answer = json.loads('{\"value\": 42}')[\"value\"]\n",
                encoding="utf-8",
                newline="",
            )
            evaluator = root / "evaluator.py"
            evaluator.write_text(
                "import json, math, pathlib, sys\n"
                "workspace = pathlib.Path(sys.argv[1]).resolve()\n"
                "sys.path.insert(0, str(workspace))\n"
                "from candidate import ambient_harness_was_imported, answer\n"
                "assert answer == 42\n"
                "assert math.isfinite(1.0)\n"
                "assert not ambient_harness_was_imported\n"
                "print(json.dumps({'workspace': True, 'stdlib': True}))\n",
                encoding="utf-8",
                newline="",
            )
            injected_ambient = (
                "import sys\n"
                f"sys.path.insert(0, {str(ambient)!r})\n"
                + _EVALUATOR_BOOTSTRAP
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    injected_ambient,
                    str(evaluator),
                    str(workspace),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(json.loads(completed.stdout), {"workspace": True, "stdlib": True})

    def test_evaluator_bootstrap_fails_closed_if_harness_survives_path_rebuild(self) -> None:
        forced_harness = (
            "import importlib.util\n"
            "real_find_spec = importlib.util.find_spec\n"
            "importlib.util.find_spec = lambda name: "
            "object() if name == 'our_harness' else real_find_spec(name)\n"
            + _EVALUATOR_BOOTSTRAP
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                forced_harness,
                str(Path(__file__)),
                str(Path.cwd()),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "benchmark evaluator isolation retained the harness package",
            completed.stderr,
        )

    def test_repetitions_are_bounded(self) -> None:
        with self.assertRaisesRegex(Exception, "repetitions"):
            run_benchmark(repetitions=0)

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "nested" / "result.md"
            stdout = io.StringIO()
            with patch("our_harness.cli.run_benchmark", return_value=self.result), redirect_stdout(stdout):
                exit_code = main(["benchmark", "--format", "markdown", "--output", str(output)])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), str(output.resolve()))
            self.assertIn("Nexus Harness benchmark", output.read_text(encoding="utf-8"))

            stdout = io.StringIO()
            with patch("our_harness.cli.run_benchmark", return_value=self.result), redirect_stdout(stdout):
                exit_code = main(["benchmark", "--format", "json"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["deterministic_score"], 100)


if __name__ == "__main__":
    unittest.main()
