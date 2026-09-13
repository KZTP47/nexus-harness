from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from our_harness.email_kestra import FLOW, KestraRuntime, _multipart


class KestraContractTests(unittest.TestCase):
    def test_remote_callback_rejected(self):
        with self.assertRaises(ValueError):
            KestraRuntime("runtime", "state", "https://example.com", "token")

    def test_missing_runtime_actionable(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = KestraRuntime(root, root, "http://127.0.0.1:9229", "token")
            with self.assertRaisesRegex(RuntimeError, "missing"):
                runtime.ensure_started()

    def test_flow_contains_no_automatic_approval_or_private_content(self):
        self.assertNotIn("pauseDuration", FLOW)
        self.assertNotIn("scripts.", FLOW)
        self.assertIn("outputs.review.onResume.callback_token", FLOW)
        self.assertIn("/api/email-worker/finalize", FLOW)

    def test_resume_uses_current_callback_after_restart(self):
        runtime = KestraRuntime("runtime", "state", "http://127.0.0.1:19333", "changed-token")
        with patch.object(runtime, "ensure_started"), patch.object(runtime, "execution_status", return_value={"state": {"current": "PAUSED"}}), patch.object(runtime, "_request", return_value={}) as request:
            runtime.resume("execution/with spaces")
        args = request.call_args.args
        self.assertEqual(args[0], "/api/v1/main/executions/execution%2Fwith%20spaces/resume")
        self.assertIn(b"changed-token", args[2])
        self.assertIn(b"19333", args[2])

    def test_resume_refuses_failed_execution(self):
        runtime = KestraRuntime("runtime", "state", "http://127.0.0.1:19333", "token")
        with patch.object(runtime, "ensure_started"), patch.object(runtime, "execution_status", return_value={"state": {"current": "FAILED"}}), patch.object(runtime, "_request") as request:
            with self.assertRaisesRegex(RuntimeError, "FAILED"):
                runtime.resume("failed-id")
        request.assert_not_called()

    def test_data_contract_is_versioned_and_stable_across_callback_change(self):
        first = KestraRuntime("runtime", "arbitrary-state-root", "http://127.0.0.1:19333", "one")
        second = KestraRuntime("runtime", "arbitrary-state-root", "http://127.0.0.1:19444", "two")
        self.assertEqual(first.data_dir, second.data_dir)
        self.assertEqual(first.data_dir.name, "kestra-1.3.38-contract-1")


@unittest.skipUnless(os.environ.get("NEXUS_TEST_REAL_KESTRA") == "1", "opt-in bundled Java engine integration")
class RealKestraTests(unittest.TestCase):
    def test_real_pause_restart_resume_callbacks(self):
        events = []
        token = "test-loopback-capability"

        class Worker(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("X-Nexus-Email-Token") != token:
                    self.send_error(403)
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                events.append((self.path, payload))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        distribution = Path(__file__).resolve().parents[1] / "desktop" / "kestra-runtime"

        def wait_for(runtime, execution, expected):
            deadline = time.monotonic() + 60
            last = {}
            while time.monotonic() < deadline:
                last = runtime.execution_status(execution)
                state = last.get("state", {}).get("current")
                if state == expected:
                    return last
                if state == "FAILED":
                    self.fail("Kestra execution failed: " + json.dumps(last))
                time.sleep(.25)
            self.fail(f"Did not reach {expected}: {last}")

        try:
            with tempfile.TemporaryDirectory(prefix="nexus mail arbitrary root ") as root:
                base = f"http://127.0.0.1:{server.server_port}"
                runtime = KestraRuntime(distribution, root, base, token)
                peer = KestraRuntime(distribution, Path(root) / "parallel-project", base, token)
                try:
                    runtime.ensure_started()
                    peer.ensure_started()
                    self.assertNotEqual(runtime.base_url, peer.base_url)
                    self.assertTrue(peer.status()["running"])
                    if os.name == "nt":
                        sockets = subprocess.run([
                            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            f"Get-NetTCPConnection -State Listen -OwningProcess {runtime.process.pid} | Select-Object -ExpandProperty LocalAddress | ConvertTo-Json",
                        ], capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                        self.assertEqual(sockets.returncode, 0, sockets.stderr)
                        addresses = json.loads(sockets.stdout)
                        if isinstance(addresses, str):
                            addresses = [addresses]
                        self.assertTrue(addresses)
                        self.assertTrue(set(addresses) <= {"127.0.0.1", "::1"}, addresses)
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(runtime.base_url + "/api/v1/main/flows/search", timeout=5)
                    self.assertIn(denied.exception.code, {401, 403})
                    execution = runtime.start_draft("draft-arbitrary-42")
                    wait_for(runtime, execution, "PAUSED")
                    self.assertEqual(events, [("/api/email-worker/generate", {"draft_id": "draft-arbitrary-42"})])
                    runtime.close()
                    # A new loopback capability tests durable Pause and refreshed resume inputs.
                    token = "different-after-restart"
                    runtime = KestraRuntime(distribution, root, base, token)
                    runtime.ensure_started()
                    self.assertTrue(peer.status()["running"])
                    self.assertIsInstance(peer._request("/api/v1/main/flows/search?size=1"), dict)
                    wait_for(runtime, execution, "PAUSED")
                    runtime.resume(execution)
                    wait_for(runtime, execution, "SUCCESS")
                    self.assertEqual(events[-1], ("/api/email-worker/finalize", {"draft_id": "draft-arbitrary-42"}))
                    self.assertEqual(len(events), 2)
                    self.assertEqual(runtime.resume(execution)["state"]["current"], "SUCCESS")
                finally:
                    runtime.close()
                    peer.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
