import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from our_harness.email_engine_runtime import EmailEngineRuntime, redis_preflight
from our_harness.models import HarnessError


HTTP_CHILD = '''
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(b'{"success":true}')
 def log_message(self,*args): pass
HTTPServer(('127.0.0.1',int(os.environ['EENGINE_PORT'])),Handler).serve_forever()
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nexus-runtime-portable-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "arbitrary service.py"
        self.script.write_text(HTTP_CHILD, encoding="utf-8")

    def runtime(self, **config):
        runtime = EmailEngineRuntime(self.root / "project", config)
        self.addCleanup(runtime.close)
        return runtime

    def local(self, **overrides):
        return self.runtime(**(dict(mode="local", command=[sys.executable, str(self.script)],
            redis_url="redis://127.0.0.1:6379/0", secret="synthetic-" * 8) | overrides))

    def test_unconfigured_does_not_claim_ready_or_install(self):
        state = self.runtime().status()
        self.assertEqual(state["state"], "not_configured")
        self.assertFalse(state["ready"])
        self.assertFalse((self.root / "project").exists())

    def test_external_rejects_plaintext_remote_credentials_and_bad_schemes(self):
        for url in ["http://example.test", "https://user:secret@example.test", "file:///tmp", "https://example.test?token=secret"]:
            with self.subTest(url=url), self.assertRaises(HarnessError):
                self.runtime(url=url).start()

    def test_root_and_command_changes_invalidate_nonsecret_fingerprint(self):
        config = {"mode":"external", "url":"https://mail.example.test", "token":"synthetic-secret"}
        a = EmailEngineRuntime(self.root / "a", config)
        b = EmailEngineRuntime(self.root / "b", config)
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(a.fingerprint, EmailEngineRuntime(self.root / "a", config).fingerprint)
        self.assertNotEqual(a.fingerprint, EmailEngineRuntime(self.root / "a", config | {"url":"https://other.example.test"}).fingerprint)
        self.assertNotIn("synthetic-secret", json.dumps(a.status()))

    @patch("our_harness.email_engine_runtime.redis_preflight", return_value={"ready":True})
    def test_owned_process_restart_lease_and_external_stop_boundary(self, probe):
        runtime = self.local()
        first = runtime.start(timeout=5)
        self.assertTrue(first["ready"])
        external = self.runtime(mode="external", url=first["url"])
        self.assertTrue(external.start()["ready"])
        external.close()
        self.assertIsNone(runtime._process.poll())
        second = self.local()
        with self.assertRaises(HarnessError):
            second.start()
        old = runtime._process
        old.terminate(); old.wait(timeout=5)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (runtime._process is old or not runtime.status()["ready"]):
            time.sleep(.1)
        self.assertIsNot(runtime._process, old)
        self.assertTrue(runtime.status()["ready"])
        runtime.close()
        self.assertTrue(second.start(timeout=5)["ready"])
        receipt = (second.root / "runtime.json").read_text()
        self.assertNotIn("synthetic-", receipt)
        self.assertEqual(json.loads(receipt)["schema"], 1)

    @patch("our_harness.email_engine_runtime.redis_preflight", return_value={"ready":True})
    def test_missing_and_exiting_executable_release_owner(self, probe):
        with self.assertRaises(HarnessError):
            self.local(command=[str(self.root / "missing.exe")]).start()
        with self.assertRaises(HarnessError):
            self.local(command=[sys.executable, "-c", "raise SystemExit(2)"]).start(timeout=2)
        self.assertTrue(self.local().start(timeout=5)["ready"])

    def test_redis_failure_prevents_process_launch(self):
        with patch("our_harness.email_engine_runtime.redis_preflight", side_effect=HarnessError("unavailable")), \
             patch("our_harness.email_engine_runtime.subprocess.Popen") as popen:
            with self.assertRaises(HarnessError):
                self.local().start()
            popen.assert_not_called()


class RedisProbeTests(unittest.TestCase):
    def server(self, *, version="7.2.0", eviction="noeviction", persistence=True):
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while line := self.rfile.readline():
                    parts = []
                    for _ in range(int(line[1:])):
                        size = int(self.rfile.readline()[1:]); parts.append(self.rfile.read(size).decode()); self.rfile.read(2)
                    if parts[0] == "INFO":
                        content = f"redis_version:{version}\r\ncluster_enabled:0\r\n".encode()
                        self.wfile.write(b"$" + str(len(content)).encode() + b"\r\n" + content + b"\r\n")
                    elif parts[0] == "CONFIG":
                        key = parts[-1]
                        value = {"maxmemory-policy":eviction,"save":"60 1" if persistence else "","appendonly":"no"}[key]
                        self.wfile.write(b"*2\r\n" + b"".join(b"$" + str(len(x)).encode() + b"\r\n" + x.encode() + b"\r\n" for x in [key,value]))
                    else:
                        self.wfile.write(b"+PONG\r\n" if parts == ["PING"] else b"+OK\r\n")
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"redis://127.0.0.1:{server.server_address[1]}/0"

    def test_compatible_protocol_and_incompatible_controls(self):
        self.assertTrue(redis_preflight(self.server())["ready"])
        for config in [{"version":"5.0.0"},{"eviction":"allkeys-lru"},{"persistence":False}]:
            with self.subTest(config=config), self.assertRaises(HarnessError):
                redis_preflight(self.server(**config))


if __name__ == "__main__":
    unittest.main()
