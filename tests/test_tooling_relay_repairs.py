from __future__ import annotations

import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness import cancellation, public_web
from our_harness.agent_tools import AgentToolSession
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.harness_tools import HarnessTools
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.mcp import MCPClient
from our_harness.navigate import _Talking
from our_harness.workflow import WorkflowDeadline


class ToolingRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portable tools ")
        self.root = Path(self.temp.name)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.memory = MemoryStore(self.config)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.memory.close)
        self.session = self.new_session()
        self.tools = HarnessTools(self.session)

    def new_session(self, seconds=30):
        return AgentToolSession(self.config, self.memory, WorkflowDeadline.start(seconds), lambda *args: None)

    def test_local_skill_complete_paging_restart_and_stale_cursor(self):
        content = "café rocket 🚀 instructions\n" * 900
        (self.root / "SKILL.md").write_bytes(content.encode("utf-8"))
        args = {"path": "SKILL.md", "max_bytes": 1200}
        page = self.tools.execute("read_local_skill", args)
        first_cursor = page["next_cursor"]
        pieces = [page["content"]]
        while page["next_cursor"]:
            page = HarnessTools(self.new_session()).execute("read_local_skill", {**args, "cursor": page["next_cursor"]})
            pieces.append(page["content"])
        self.assertEqual("".join(pieces).encode("utf-8"), content.encode("utf-8"))
        (self.root / "SKILL.md").write_text(content + "changed", encoding="utf-8")
        for cursor in (first_cursor, "bad-cursor"):
            with self.assertRaises(HarnessError):
                self.tools.execute("read_local_skill", {**args, "cursor": cursor})
        (self.root / "ordinary.md").write_text("hello")
        with self.assertRaisesRegex(HarnessError, "wrong-skill-reader"):
            self.tools.execute("read_local_skill", {"path": "ordinary.md"})

    def test_deleted_git_file_and_directory_remain_inspectable(self):
        def git(*args):
            subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)
        git("init")
        (self.root / "nested").mkdir()
        (self.root / "nested/old.txt").write_text("original\n")
        git("add", "nested/old.txt")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "initial")
        self.assertIn("original", self.tools.execute("git_show", {"commit": "HEAD", "path": "nested/old.txt"})["stdout"])
        (self.root / "nested/old.txt").unlink()
        (self.root / "nested").rmdir()
        self.assertIn("original", self.tools.execute("git_show", {"commit": "HEAD", "path": "nested/old.txt"})["stdout"])
        self.assertIn("-original", self.tools.execute("git_diff", {"path": "nested/old.txt"})["stdout"])
        self.assertIn("initial", self.tools.execute("git_log", {"path": "nested/old.txt"})["stdout"])
        for path in ("../escape", ".env"):
            with self.assertRaises(HarnessError):
                self.tools.execute("git_show", {"commit": "HEAD", "path": path})

    def test_mcp_application_failure_both_aliases_and_success(self):
        self.config.data["mcp"]["servers"] = [{"name": "fixture", "allowed_tools": ["query"]}]
        with patch("our_harness.agent_tools.MCPClient") as client:
            peer = client.return_value
            peer.list_tools.return_value = [{"name": "query", "annotations": {"readOnlyHint": True}}]
            for failed in (True, False):
                peer.call_tool.return_value = {"isError": failed, "content": [{"type": "text", "text": "details"}]}
                for name, args in (("mcp_call", {"server": "fixture", "tool": "query", "arguments": {}}),
                                   ("call_mcp_tool", {"server": "fixture", "tool": "query", "arguments_json": "{}"})):
                    result = self.new_session().execute("reader", "call", name, args)
                    self.assertEqual(result["status"], "error" if failed else "ok")
                    self.assertIn("details", result["content"])
            self.assertIsNotNone(client.call_args.kwargs["deadline"])

    def test_mcp_changed_configuration_refreshes_new_call_preserves_old_id(self):
        args = {"server": "fixture", "tool": "query", "arguments": {}}
        self.config.data["mcp"]["servers"] = [{"name": "fixture", "command": "first"}]
        with patch.object(self.session, "_dispatch", return_value={"classification": "read_only", "result": {"route": "first"}}):
            first = self.session.execute("reader", "one", "mcp_call", args)
        self.config.data["mcp"]["servers"] = [{"name": "fixture", "command": "second"}]
        with patch.object(self.session, "_dispatch", return_value={"classification": "read_only", "result": {"route": "second"}}) as dispatch:
            current = self.session.execute("reader", "two", "mcp_call", args)
            old_id = self.session.execute("reader", "one", "mcp_call", args)
            dispatch.assert_called_once()
        self.assertFalse(current["duplicate"])
        self.assertIn("second", current["content"])
        self.assertEqual(old_id["status"], "error")
        self.assertIn("first", first["content"])

    def test_mcp_old_result_contract_normalized_without_resending_after_restart(self):
        run_id = self.memory.start_run("MCP result migration")
        original_record = self.memory.record_agent_tool_result
        def legacy_record(**kwargs):
            kwargs["result"]["status"] = "ok"
            kwargs["result"]["provenance"].pop("result_contract", None)
            kwargs["result"]["provenance"].pop("mcp_config_sha256", None)
            return original_record(**kwargs)
        for scope in ("", "response-1"):
            for name in ("mcp_call", "call_mcp_tool"):
                call_id = name + ("scoped" if scope else "legacy")
                args = {"server": "fixture", "tool": "query", "arguments": {}} if name == "mcp_call" else {"server": "fixture", "tool": "query", "arguments_json": "{}"}
                old = self.new_session()
                old.run_id = run_id
                with patch.object(self.memory, "record_agent_tool_result", side_effect=legacy_record), patch.object(old, "_dispatch", return_value={"classification": "read_only", "result": {"isError": True, "content": ["original failure details"]}}):
                    result = old.execute("reader", call_id, name, args, execution_scope=scope)
                    self.assertEqual(result["status"], "ok")
                    budget = old.budget_state()
                for restore_budget in (False, True):
                    resumed = self.new_session()
                    resumed.run_id = run_id
                    if restore_budget:
                        resumed.restore_budget_state(budget)
                    with patch.object(resumed, "_dispatch") as dispatch:
                        result = resumed.execute("reader", call_id, name, args, execution_scope=scope)
                        dispatch.assert_not_called()
                    self.assertEqual(result["status"], "error")
                    self.assertIn("original failure details", result["content"])
                    self.assertTrue(result["replayed"])
                    self.assertEqual(result["result_contract"], "nexus-mcp-result:v2")
                    self.assertEqual(result["route_binding"], "legacy_receipt_unknown_configuration")

    def test_mcp_late_dns_never_sends_after_deadline_or_stop(self):
        posts = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                posts.append(1)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02))
        serving.start()
        original_dns = socket.getaddrinfo
        try:
            for cancel in (False, True):
                entered, released = threading.Event(), threading.Event()
                def dns(*args, **kwargs):
                    entered.set()
                    released.wait(2)
                    return original_dns(*args, **kwargs)
                token = cancellation.Cancellation()
                errors = []
                slots = threading.BoundedSemaphore(1)
                def run():
                    with cancellation.use(token):
                        try:
                            client = MCPClient({"transport": "http", "url": f"http://127.0.0.1:{server.server_port}/"}, timeout=.08)
                            client._post_http({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
                        except Exception as exc:
                            errors.append(exc)
                with patch.object(socket, "getaddrinfo", dns), patch("our_harness.mcp._HTTP_WORKERS", slots):
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        if cancel:
                            token.cancel()
                        thread.join(.6)
                        self.assertFalse(thread.is_alive())
                        self.assertIsInstance(errors[0], cancellation.ChatCancelled if cancel else HarnessError)
                    finally:
                        released.set()
                        thread.join(1)
                    self.assertTrue(slots.acquire(timeout=1))
                    slots.release()
                self.assertEqual(posts, [])
        finally:
            server.shutdown()
            server.server_close()
            serving.join(1)

    def test_research_inherits_remaining_budget_and_new_session_refreshes(self):
        observed = []
        def fetch(url, **kwargs):
            observed.append(kwargs["timeout"])
            return {"url": url, "data": b"hello", "content_type": "text/plain"}
        with patch("our_harness.research_tools.fetch_public", side_effect=fetch):
            self.new_session(.2).execute("reader", "one", "fetch_url", {"url": "https://example.invalid/a"})
            self.new_session(5).execute("reader", "two", "fetch_url", {"url": "https://example.invalid/a"})
        self.assertLessEqual(observed[0], .2)
        self.assertGreater(observed[1], 1)

    def test_mcp_stages_share_engine_deadline(self):
        client = MCPClient({"transport": "http", "url": "http://localhost/"}, timeout=10, deadline=WorkflowDeadline.start(.2))
        first = client._deadline_at()
        time.sleep(.02)
        second = client._deadline_at()
        self.assertAlmostEqual(first, second, delta=.01)
        with cancellation.use(cancellation.Cancellation()) as token:
            token.cancel()
            with self.assertRaises(cancellation.ChatCancelled):
                client._deadline_at()

    def test_public_dns_deadline_bounded_admission_and_no_late_connect(self):
        released = threading.Event()
        entered = []
        def dns(*args, **kwargs):
            entered.append(1)
            released.wait(1)
            return [(2, 1, 6, "", ("93.184.216.34", 80))]
        try:
            with patch.object(public_web.socket, "getaddrinfo", side_effect=dns), patch.object(public_web.socket, "create_connection") as connect:
                started = time.monotonic()
                for _ in range(4):
                    with self.assertRaisesRegex(HarnessError, "timed out"):
                        public_web.fetch_public("http://example.invalid/", timeout=.02)
                with self.assertRaisesRegex(HarnessError, "still shutting down"):
                    public_web.fetch_public("http://example.invalid/", timeout=.02)
                self.assertLess(time.monotonic()-started, .5)
                self.assertEqual(len(entered), 4)
                released.set()
                time.sleep(.05)
                connect.assert_not_called()
        finally:
            released.set()

    def test_public_private_addresses_still_rejected(self):
        with patch.object(public_web.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]), patch.object(public_web.socket, "create_connection") as connect:
            with self.assertRaisesRegex(HarnessError, "private"):
                public_web.fetch_public("http://example.invalid/", timeout=1)
            connect.assert_not_called()

    def test_public_cancelled_dns_returns_before_resolution(self):
        entered, released = threading.Event(), threading.Event()
        def dns(*args, **kwargs):
            entered.set()
            released.wait(1)
            return [(2, 1, 6, "", ("93.184.216.34", 80))]
        token = cancellation.Cancellation()
        timer = threading.Timer(.05, token.cancel)
        try:
            with patch.object(public_web.socket, "getaddrinfo", side_effect=dns), cancellation.use(token):
                timer.start()
                started = time.monotonic()
                with self.assertRaises(cancellation.ChatCancelled):
                    public_web.fetch_public("http://example.invalid/", timeout=1)
                self.assertLess(time.monotonic()-started, .4)
        finally:
            released.set()
            timer.join()
            time.sleep(.03)

    def test_lsp_nonreading_peer_has_bounded_write_and_cleanup(self):
        talk = _Talking((sys.executable, "-c", "import time;time.sleep(10)"), self.root)
        process = talk.process
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(HarnessError, "timed out"):
                talk.ask("fixture", {"text": "x"*200000}, .04)
        finally:
            talk.stop()
        self.assertLess(time.monotonic()-started, 1.5)
        self.assertIsNotNone(process.poll())
        self.assertFalse(talk._reading.is_alive())

    def test_lsp_cancellation_interrupts_wait_and_cleanup(self):
        talk = _Talking((sys.executable, "-c", "import time;time.sleep(10)"), self.root)
        token = cancellation.Cancellation()
        timer = threading.Timer(.05, token.cancel)
        try:
            with cancellation.use(token):
                timer.start()
                started = time.monotonic()
                with self.assertRaises(cancellation.ChatCancelled):
                    talk.ask("fixture", {}, 5)
                self.assertLess(time.monotonic()-started, 1.5)
        finally:
            talk.stop()
            timer.join()
        self.assertIsNotNone(talk.process.poll())


if __name__ == "__main__":
    unittest.main()
