from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from our_harness import playwright_runtime
from our_harness.playwright_runtime import (
    BundledPlaywrightRuntime,
    compile_safe_playwright_scenario,
    discover_bundled_playwright_runtime,
    extract_safe_playwright_scenario,
    normalize_approved_https_base_url,
    run_brokered_playwright_appcontainer,
    run_brokered_playwright_suite,
    run_safe_playwright_scenario,
    validate_safe_playwright_scenario,
)


class PlaywrightRuntimeDiscoveryTests(unittest.TestCase):
    @staticmethod
    def broad_scenario() -> dict[str, object]:
        return {
            "config": {
                "baseURL": "https://example.com/app/",
                "timeout_ms": 5_000,
                "test_id_attribute": "data-qa",
            },
            "steps": [
                {"op": "goto", "url": "start"},
                {"op": "fill", "target": {"kind": "getByLabel", "value": "Name", "exact": True}, "value": "Ada"},
                {"op": "keyboard", "target": {"kind": "getByPlaceholder", "value": "Search"}, "action": "press", "value": "Enter"},
                {"op": "check", "target": {"kind": "getByText", "value": "Remember", "filter": {"hasText": "Remember"}}},
                {"op": "selectOption", "target": {"kind": "getByTestId", "value": "country"}, "values": ["SE"]},
                {"op": "click", "target": {"kind": "getByRole", "role": "button", "name": "Save", "nth": -1}},
                {"op": "assert", "condition": "visible", "target": {"kind": "locator", "selector": "#done"}},
                {"op": "assert", "condition": "hidden", "target": {"kind": "locator", "selector": ".spinner"}},
                {"op": "assert", "condition": "text", "target": {"kind": "locator", "selector": "#done"}, "expected": "Saved"},
                {"op": "assert", "condition": "value", "target": {"kind": "locator", "selector": "#name"}, "expected": "Ada"},
                {"op": "assert", "condition": "attribute", "target": {"kind": "locator", "selector": "#done"}, "name": "data-state", "expected": "ready"},
                {"op": "assert", "condition": "count", "target": {"kind": "locator", "selector": "li"}, "expected": 2},
                {"op": "assert", "condition": "url", "expected": "done"},
                {"op": "api", "method": "GET", "url": "/health", "expected_status": 200, "expected_text": "ok"},
            ],
        }

    def test_exact_https_origin_rejects_local_decoys_and_cross_origin_steps(self) -> None:
        canonical = normalize_approved_https_base_url("HTTPS://EXAMPLE.COM:443/app/")
        self.assertEqual(canonical, ("https://example.com/app/", "https://example.com", "example.com", 443))
        for decoy in (
            "http://example.com/", "https://localhost/", "https://demo.localhost/",
            "https://127.0.0.1:9443/", "https://[::1]/",
        ):
            with self.subTest(decoy=decoy), self.assertRaises(ValueError):
                normalize_approved_https_base_url(decoy)
        with self.assertRaisesRegex(ValueError, "exact approved origin"):
            validate_safe_playwright_scenario({
                "base_url": "https://example.com/app/",
                "steps": [
                    {"op": "goto", "url": "https://127.0.0.1:8443/decoy"},
                    {"op": "assert", "condition": "url", "expected": "/"},
                ],
            }, "https://example.com/app/")

    def test_common_data_only_scenario_compiles_to_awaited_engine_runner(self) -> None:
        validated, source = compile_safe_playwright_scenario(
            self.broad_scenario(), "https://example.com/app/",
        )
        self.assertEqual(validated["origin"], "https://example.com")
        self.assertEqual(validated["steps"][0]["url"], "https://example.com/app/start")
        self.assertEqual(validated["steps"][-1]["url"], "https://example.com/health")
        self.assertIn("await located.selectOption", source)
        self.assertIn("await pw.request.newContext", source)
        self.assertIn("await response.securityDetails()", source)
        self.assertIn("receipt.assertions.push", source)
        self.assertNotIn("require('@playwright/test')", source)
        unsafe = dict(self.broad_scenario())
        unsafe["steps"] = [
            {"op": "goto", "url": "/"},
            {"op": "evaluate", "source": "require('child_process').spawn('cmd')"},
        ]
        with self.assertRaisesRegex(ValueError, "unsupported operation"):
            validate_safe_playwright_scenario(unsafe, "https://example.com/app/")

    def test_common_async_suite_extraction_supports_aliases_locators_and_multiple_assertions(self) -> None:
        source = """
test('common suite', async ({ page }) => {
  await page.goto('/app');
  const save = page.getByRole('button', { name: 'Save', exact: true });
  await page.getByLabel('Name').fill('Ada');
  await page.getByPlaceholder('Search').press('Enter');
  await page.getByTestId('terms').check();
  await page.locator('select').selectOption('SE');
  await save.click();
  await expect(page.getByText('Saved')).toBeVisible();
  await expect(page.locator('li').filter({ hasText: 'ready' }).nth(0)).toHaveCount(1);
  await expect(page.getByLabel('Name')).toHaveValue('Ada');
  await expect(page).toHaveURL('/done');
});
"""
        extracted = extract_safe_playwright_scenario(source, "https://example.com/")
        self.assertIsNotNone(extracted)
        assert extracted is not None
        self.assertEqual("goto", extracted["steps"][0]["op"])
        self.assertTrue(any(
            one.get("target", {}).get("kind") == "getByRole" and one["op"] == "click"
            for one in extracted["steps"]
        ))
        self.assertEqual(4, sum(one["op"] == "assert" for one in extracted["steps"]))

    def test_safe_scenario_receipt_joins_tls_cdp_proxy_routes_and_dom_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary)

            def broker(where, runner, **kwargs):
                receipt_path = Path(kwargs["environment"]["NEXUS_PLAYWRIGHT_RECEIPT_PATH"])
                receipt_path.write_text(json.dumps({
                    "passed": True,
                    "configured_base_url": "https://example.com/",
                    "configured_origin": "https://example.com",
                    "final_url": "https://example.com/",
                    "final_origin": "https://example.com",
                    "external_write_denied": True,
                    "tls": [{"url": "https://example.com/", "protocol": "TLS 1.3"}],
                    "request_routes": [{
                        "event": "request", "resource_type": "document",
                        "url": "https://example.com/", "exact_origin": True,
                    }],
                    "assertions": [{"condition": "text", "passed": True}],
                    "api": [],
                }), encoding="utf-8")
                return {
                    "passed": True, "exact_origin_route_attested": True,
                    "origin_routes": [{"route": "https-connect", "authority": "example.com:443", "allowed": True}],
                    "endpoint_scope": "same-profile-appcontainer-loopback",
                    "profile": "Nexus.Test.Profile",
                    "boundary_inheritance_attested": True,
                    "process_capabilities": {"origin_proxy": [playwright_runtime.INTERNET_CLIENT]},
                    "external_write_authority": str(where),
                }

            scenario = {
                "base_url": "https://example.com/",
                "steps": [
                    {"op": "goto", "url": "/"},
                    {"op": "assert", "condition": "text", "target": {"kind": "locator", "selector": "h1"}, "expected": "Example Domain"},
                ],
            }
            with mock.patch.object(playwright_runtime, "run_brokered_playwright_appcontainer", side_effect=broker):
                result = run_safe_playwright_scenario(snapshot, scenario, "https://example.com/")
            self.assertTrue(result["passed"], result)
            evidence = result["evidence_receipt"]
            self.assertEqual(evidence["route_mode"], "REMOTE_TLS_TUNNEL")
            self.assertEqual(evidence["configured_origin"], evidence["final_origin"])
            self.assertTrue(evidence["exact_origin_route_attested"])
            self.assertEqual(evidence["tls"][0]["protocol"], "TLS 1.3")
            self.assertTrue(evidence["dom_assertions"][0]["passed"])

    @unittest.skipUnless(os.name == "nt", "Windows unmodified Playwright suite")
    def test_unmodified_suite_runs_inprocess_worker_with_helper_fixture_api_and_tls(self) -> None:
        runtime = discover_bundled_playwright_runtime()
        if runtime is None:
            self.skipTest("bundled Playwright runtime is not installed")
        fixture = ROOT / "tests" / "fixtures" / "playwright_unmodified_exact"
        source = (fixture / "exact.spec.cjs").read_text(encoding="utf-8")
        extracted = extract_safe_playwright_scenario(source, "https://example.com/")
        self.assertIsNotNone(extracted)
        assert extracted is not None
        self.assertFalse(any(
            one.get("op") == "api" for one in extracted["steps"]
        ), "semantic IR intentionally cannot represent this suite's request fixture assertions")
        with tempfile.TemporaryDirectory(prefix="nexus-unmodified-suite-test-") as temporary:
            snapshot = Path(temporary) / "project"
            shutil.copytree(fixture, snapshot)
            before = (snapshot / "exact.spec.cjs").read_bytes()
            result = run_brokered_playwright_suite(
                snapshot, ["test", "exact.spec.cjs"], "https://example.com/",
                environment={"NEXUS_TEST_EXACT_BASE_URL": "https://example.com/"},
                timeout=60,
                runtime=runtime,
            )
            self.assertEqual(before, (snapshot / "exact.spec.cjs").read_bytes())
        self.assertTrue(result["passed"], result)
        receipt = result["receipt"]
        self.assertEqual("IN_PROCESS_WORKER_MAIN", receipt["worker_mode"])
        self.assertTrue(receipt["worker_ready"] and receipt["worker_exited"])
        self.assertTrue(receipt["external_write_denied"])
        self.assertTrue(all(one["status"] == one["expectedStatus"] for one in receipt["tests"]))
        assertions = [one for one in receipt["steps"] if one.get("category") == "expect"]
        self.assertGreaterEqual(len(assertions), 7)
        self.assertTrue(all(one.get("error") is None for one in assertions))
        self.assertTrue(any(
            one.get("category") == "fixture" and one.get("title") == 'Fixture "webServer"'
            for one in receipt["steps"]
        ))
        self.assertGreaterEqual(len(receipt["api"]), 1)
        self.assertTrue(all(one["url"].startswith("https://example.com/") for one in receipt["api"]))
        self.assertTrue(any(str(one.get("protocol", "")).startswith("TLS") for one in receipt["tls"]))
        self.assertTrue(result["broker"]["boundary_inheritance_attested"])
        self.assertTrue(result["broker"]["exact_origin_route_attested"])

    def test_broker_owns_two_appcontainers_readiness_and_deterministic_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "immutable"
            snapshot = root / "snapshot"
            runtime_root.mkdir()
            snapshot.mkdir()
            runner = snapshot / "engine-runner.cjs"
            runner.write_text("// engine owned", encoding="utf-8")
            fake_runtime = BundledPlaywrightRuntime(
                root=runtime_root, node=runtime_root / "node.exe",
                cli=runtime_root / "cli.js", playwright_module=runtime_root / "playwright",
                test_module=runtime_root / "test", browsers=runtime_root / "browsers",
                chromium=runtime_root / "chrome.exe", node_version="22.18.0",
                playwright_version="1.62.1", chromium_revision="1234",
            )
            browser_closed = threading.Event()
            calls: list[dict[str, object]] = []

            def contained(where, argv, environment, timeout, **kwargs):
                calls.append({"where": where, "argv": argv, "environment": environment, **kwargs})
                if where.name == "playwright-browser":
                    stderr = where / ".nexus-verification" / "contained-stderr.txt"
                    stderr.parent.mkdir(parents=True, exist_ok=True)
                    stderr.write_text("DevTools listening on ws://127.0.0.1:1234\n", encoding="utf-8")
                    browser_closed.wait(5)
                    return {"exit_code": 0, "containment_profile": "windows-appcontainer-job-v1", "containment_sid": "S-1-test"}
                if str(argv[-1]).endswith("playwright-close.cjs"):
                    self.assertIn("Browser.close", Path(argv[-1]).read_text(encoding="utf-8"))
                    browser_closed.set()
                    return {"exit_code": 0, "containment_profile": "windows-appcontainer-job-v1", "containment_sid": "S-1-test"}
                return {"exit_code": 0, "containment_profile": "windows-appcontainer-job-v1", "containment_sid": "S-1-test"}

            with mock.patch("our_harness.windows_containment.appcontainer_available", return_value=True), mock.patch(
                "our_harness.windows_containment.verification_runtime_profile", return_value="Nexus.Test.Profile"
            ), mock.patch("our_harness.windows_containment.run_appcontainer", side_effect=contained):
                result = run_brokered_playwright_appcontainer(
                    snapshot, runner, runtime=fake_runtime, environment={"NEXUS_DENIED_WRITE": str(root / "denied")},
                    timeout=5,
                )

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["readiness_attested"])
            self.assertEqual(result["profile"], "Nexus.Test.Profile")
            self.assertEqual(result["external_write_authority"], str(snapshot.resolve()))
            self.assertEqual(len(calls), 3, "browser, runner, and engine closer each get one contained launch")
            runner_call = next(
                call for call in calls
                if any(str(one).endswith(runner.name) for one in call["argv"])
            )
            self.assertTrue(runner_call["map_authorized_roots"])
            self.assertEqual(runner_call["read_execute_roots"], (runtime_root,))
            self.assertEqual(runner_call["capability_sids"], (playwright_runtime.PRIVATE_NETWORK_CLIENT_SERVER,))

    def test_exact_explicit_runtime_is_discovered_with_containment_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime" / "playwright"
            for relative in (
                "node.exe", "node_modules/playwright/cli.js",
                "node_modules/playwright/index.js", "node_modules/@playwright/test/index.js",
                "browsers/chromium_headless_shell-1234/chrome/chrome-headless-shell.exe",
            ):
                target = runtime / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("stand-in", encoding="utf-8")
            manifest = {
                "playwright": {
                    "schema_version": 1,
                    "node_version": "22.18.0", "playwright_version": "1.62.1",
                    "chromium_revision": "1234", "node": "node.exe",
                    "playwright_cli": "node_modules/playwright/cli.js",
                    "playwright_module": "node_modules/playwright",
                    "playwright_test_module": "node_modules/@playwright/test",
                    "browsers_path": "browsers",
                    "chromium_executable": "browsers/chromium_headless_shell-1234/chrome/chrome-headless-shell.exe",
                }
            }
            (runtime.parent / "NEXUS_RUNTIME.json").write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.dict(os.environ, {"NEXUS_PLAYWRIGHT_RUNTIME": str(runtime)}):
                found = discover_bundled_playwright_runtime(required=True)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.root, runtime.resolve())
            self.assertEqual(found.chromium_revision, "1234")
            environment = found.environment({"SYSTEMROOT": "C:/Windows"})
            self.assertEqual(environment["PLAYWRIGHT_BROWSERS_PATH"], str(found.browsers))
            self.assertEqual(environment["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"], "1")

    def test_invalid_explicit_runtime_fails_closed_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"NEXUS_PLAYWRIGHT_RUNTIME": temporary}):
                with self.assertRaisesRegex(RuntimeError, "Bundled Playwright runtime is unavailable"):
                    discover_bundled_playwright_runtime(required=True)
