from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from our_harness import playwright_runtime as runtime
from our_harness import swarm_work


@unittest.skipUnless(os.name == "nt", "Windows AppContainer boundary")
class BrokerFailureTests(unittest.TestCase):
    def broker(self, root: Path, stage: str = "", *, remote: bool = False, failure=None):
        snapshot = root / "arbitrary project"
        snapshot.mkdir(parents=True, exist_ok=True)
        runner = snapshot / "engine-runner.cjs"
        runner.write_text("// engine owned", encoding="utf-8")
        immutable = root / "independent runtime"
        immutable.mkdir(exist_ok=True)
        selected = runtime.BundledPlaywrightRuntime(
            root=immutable, node=immutable / "node.exe", cli=immutable / "cli.js",
            playwright_module=immutable / "playwright", test_module=immutable / "test",
            browsers=immutable / "browsers", chromium=immutable / "chrome.exe",
            node_version="22.18.0", playwright_version="1.62.1", chromium_revision="1234",
        )
        closed = threading.Event()
        proxy_closed = threading.Event()
        calls = []

        def contained(where, argv, environment, timeout, **kwargs):
            component = ("proxy" if where.name == "playwright-origin-proxy" else
                         "proxy_closer" if where.name == "playwright-origin-proxy-control" else
                         "browser" if where.name == "playwright-browser" else
                         "closer" if str(argv[-1]).endswith("playwright-close.cjs") else "runner")
            calls.append(component)
            if component == stage:
                if component == "closer": closed.set()
                if component == "proxy_closer": proxy_closed.set()
                raise failure if failure is not None else OSError(13, f"{component} native launch denied")
            if component in {"browser", "proxy"}:
                evidence = where / ".nexus-verification"
                evidence.mkdir(exist_ok=True)
                if component == "browser":
                    (evidence / "contained-stderr.txt").write_text("DevTools listening on ws://127.0.0.1:1234")
                    closed.wait(2)
                else:
                    (evidence / "contained-stdout.txt").write_text("NEXUS_EXACT_ORIGIN_PROXY_READY")
                    proxy_closed.wait(2)
            if component == "closer": closed.set()
            if component == "proxy_closer": proxy_closed.set()
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False,
                    "containment_profile": "windows-appcontainer-job-v1", "containment_sid": "S-1-portable"}

        with mock.patch("our_harness.windows_containment.appcontainer_available", return_value=True), \
             mock.patch("our_harness.windows_containment.verification_runtime_profile", return_value="Nexus.Portable"), \
             mock.patch("our_harness.windows_containment.run_appcontainer", side_effect=contained):
            result = runtime.run_brokered_playwright_appcontainer(
                snapshot, runner, runtime=selected, timeout=0.2,
                approved_base_url="https://example.com/" if remote else None,
            )
        return result, calls

    def test_process_failures_are_json_safe_and_preserve_the_original_cause(self):
        for stage in ("browser", "proxy", "runner", "closer", "proxy_closer"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                result, calls = self.broker(Path(folder), stage, remote=stage.startswith("proxy"))
                restored = json.loads(json.dumps(result))
                self.assertFalse(restored["passed"])
                self.assertTrue(restored["containment_unavailable"])
                self.assertIn(f"{stage} native launch denied", restored["error"])
                self.assertEqual(restored[stage]["exit_code"], -2)
                self.assertEqual(restored[stage]["error_type"], "PermissionError")
                self.assertEqual(restored[stage]["errno"], 13)
                if stage in {"browser", "proxy"}:
                    self.assertNotIn("runner", calls, "project code cannot launch without readiness")

    def test_failure_receipt_survives_reload_and_does_not_poison_a_new_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            failed, _ = self.broker(root / "first machine", "browser")
            receipt = root / "result.json"
            receipt.write_text(json.dumps(failed))
            self.assertFalse(json.loads(receipt.read_text())["passed"])
            recovered, calls = self.broker(root / "changed installation")
            self.assertTrue(recovered["passed"], recovered)
            self.assertFalse(recovered["containment_unavailable"])
            self.assertEqual(calls, ["browser", "runner", "closer"])

    def test_original_oserror_and_native_windows_code_survive_serialization(self):
        failure = OSError("CreateAppContainerProfile failed: portable diagnostic")
        failure.winerror = 5
        with tempfile.TemporaryDirectory() as folder:
            result, _ = self.broker(Path(folder), "browser", failure=failure)
            restored = json.loads(json.dumps(result))
            self.assertEqual(restored["browser"]["error_type"], "OSError")
            self.assertEqual(restored["browser"]["winerror"], 5)
            self.assertIn(str(failure), restored["error"])


class VerificationFailureProjectionTests(unittest.TestCase):
    def test_browser_infrastructure_failure_is_not_reported_as_a_failed_project_assertion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "choice.spec.cjs").write_text(
                "test('choice',async({page})=>{await page.goto('/choice.html');"
                "await expect(page.locator('#answer')).toHaveText('Ready');});"
            )
            broker = {"passed": False, "containment_unavailable": True,
                      "error": "Contained Chromium: native launch denied"}
            with mock.patch.object(swarm_work, "_run_brokered_playwright_scenario", return_value={
                "passed": False, "broker": broker, "receipt": {},
            }):
                result = swarm_work._run_brokered_playwright_specs(
                    root, ["node", "playwright/cli.js", "test", "choice.spec.cjs"], timeout=1,
                )
            self.assertTrue(result["containment_unavailable"])
            self.assertEqual(result["exit_code"], -2)
            self.assertIn("native launch denied", result["stderr"])
            json.dumps(result)

    def test_project_assertion_failure_remains_a_failed_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "choice.spec.cjs").write_text(
                "test('choice',async({page})=>{await page.goto('/choice.html');"
                "await expect(page.locator('#answer')).toHaveText('Ready');});"
            )
            with mock.patch.object(swarm_work, "_run_brokered_playwright_scenario", return_value={
                "passed": False, "broker": {"passed": True, "containment_unavailable": False},
                "receipt": {"passed": False, "error": "Expected Ready, received Broken"},
            }):
                result = swarm_work._run_brokered_playwright_specs(
                    root, ["node", "playwright/cli.js", "test", "choice.spec.cjs"], timeout=1,
                )
            self.assertFalse(result["containment_unavailable"])
            self.assertEqual(result["exit_code"], 1)
            self.assertIn("Expected Ready", json.dumps(result))

    def test_unmodified_remote_suite_does_not_claim_execution_after_launch_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "remote.spec.cjs").write_text("// selected unmodified suite")
            with mock.patch.object(swarm_work, "_approved_playwright_base_url", return_value="https://example.com/"), \
                 mock.patch.object(swarm_work, "run_brokered_playwright_suite", return_value={
                     "passed": False, "broker": {"containment_unavailable": True,
                         "error": "Exact-origin proxy: native launch denied", "runner": None},
                 }):
                result = swarm_work._run_brokered_playwright_specs(
                    root, ["node", "playwright/cli.js", "test", "remote.spec.cjs"], timeout=1,
                )
            self.assertTrue(result["containment_unavailable"])
            self.assertEqual(result["exit_code"], -2)
            self.assertFalse(result["ordinary_suite_executed"])
            self.assertIn("native launch denied", result["stderr"])
            json.dumps(result)
