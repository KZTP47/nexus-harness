from __future__ import annotations

import http.server
import json
import io
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from our_harness.cli import parser
from our_harness.config import load_config as _load_config
from our_harness.mcp import MCPClient, configured_server
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError, ProviderRequest
from our_harness.providers.base import LocalProcessProvider, OllamaProvider, create_embedding_provider, create_provider
from our_harness.refinement import RefinementManager


def load_config(root: Path, **kwargs):
    local = root / ".harness" / "config.local.json"
    return _load_config(root, explicit=local if local.is_file() else None, **kwargs)


class _EmptyResponse:
    headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return "http://127.0.0.1:8999/mcp"


class _ChunkResponse:
    def __init__(self, chunks, *, headers=None, url="https://example.test/mcp", repeat=False, delay=0.0):
        self.chunks = list(chunks)
        self.headers = dict(headers or {})
        self.url = url
        self.repeat = repeat
        self.delay = delay
        self.closed = False

    def read(self, _limit: int) -> bytes:
        if self.delay:
            time.sleep(self.delay)
        if self.closed:
            return b""
        if self.chunks:
            value = self.chunks.pop(0)
            if self.repeat:
                self.chunks.append(value)
            return value
        return b""

    def close(self) -> None:
        self.closed = True

    def geturl(self) -> str:
        return self.url


class _FixtureOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


class ProviderRoutingTests(unittest.TestCase):
    def test_distinct_embedding_provider_uses_its_local_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.local.json").write_text(
                json.dumps(
                    {
                        "provider": {"name": "openai", "endpoint": "https://api.openai.com/v1"},
                        "memory": {"embedding_provider": "ollama", "embedding_model": "embed-fixture"},
                    }
                ),
                encoding="utf-8",
            )
            provider = create_embedding_provider(load_config(root))
            self.assertIsInstance(provider, OllamaProvider)
            self.assertEqual(provider.settings["endpoint"], "http://127.0.0.1:11434")
            self.assertEqual(provider.settings["model"], "embed-fixture")

    def _local_provider(self, root: Path, program: str, *, timeout: int = 3, output_limit: int = 4096) -> LocalProcessProvider:
        (root / ".harness").mkdir(exist_ok=True)
        (root / ".harness" / "config.json").write_text(
            json.dumps(
                {
                    "provider": {"name": "local", "model": "fixture", "timeout_seconds": timeout},
                    "execution": {"max_output_bytes": output_limit},
                }
            ),
            encoding="utf-8",
        )
        (root / ".harness" / "config.local.json").write_text(
            json.dumps({"provider": {"command": [sys.executable, "-c", program]}}), encoding="utf-8"
        )
        provider = create_provider(load_config(root))
        self.assertIsInstance(provider, LocalProcessProvider)
        return provider

    @staticmethod
    def _request() -> ProviderRequest:
        return ProviderRequest("prefix", "dynamic", [{"role": "user", "content": "hello"}], "fixture")

    def test_local_provider_uses_bounded_stdin_stdout_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = self._local_provider(
                root,
                "import json,sys; request=json.load(sys.stdin); print(json.dumps({'text': request['messages'][0]['content']}))",
            )
            self.assertEqual(provider.complete(self._request()).text, "hello")

    def test_local_provider_rejects_oversize_timeout_and_non_string_text(self) -> None:
        cases = [
            ("import json; print(json.dumps({'text':'x'*10000}))", 3, "exceeded"),
            ("import time; time.sleep(10)", 1, "timed out"),
            ("import json; print(json.dumps({'text':7}))", 3, "string text field"),
        ]
        for program, timeout, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                provider = self._local_provider(Path(temporary), program, timeout=timeout, output_limit=2048)
                with self.assertRaisesRegex(HarnessError, message):
                    provider.complete(self._request())


class MCPTests(unittest.TestCase):
    def test_modern_http_is_stateless_and_self_describing(self) -> None:
        discover = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                "resultType": "complete", "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}}, "ttlMs": 9000, "cacheScope": "public",
            }}).encode()],
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "must-be-ignored"},
        )
        tools = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
                "resultType": "complete", "tools": [{"name": "search"}],
                "ttlMs": 5000, "cacheScope": "private",
            }}).encode()],
            headers={"Content-Type": "application/json"},
        )
        called = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 3, "result": {
                "resultType": "complete", "content": [{"type": "text", "text": "ok"}],
            }}).encode()],
            headers={"Content-Type": "application/json"},
        )
        opener = _FixtureOpener([discover, tools, called])
        client = MCPClient({
            "name": "fixture", "transport": "http", "url": "https://example.test/mcp",
            "protocol_mode": "modern", "allowed_tools": ["search"],
        }, timeout=1)
        client._http_opener = opener

        result = client.connect()
        self.assertEqual(result["supportedVersions"], ["2026-07-28"])
        self.assertEqual(client.protocol_era, "modern")
        self.assertIsNone(client.session_id)
        self.assertEqual(client.list_tools(), [{"name": "search"}])
        self.assertEqual(client.call_tool("search", {"q": "term"})["resultType"], "complete")
        self.assertEqual(client.cache_hints["server/discover"], {"ttlMs": 9000, "cacheScope": "public"})
        self.assertEqual(client.cache_hints["tools/list"], {"ttlMs": 5000, "cacheScope": "private"})

        for request, _timeout in opener.requests:
            body = json.loads(request.data)
            meta = body["params"]["_meta"]
            self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], "2026-07-28")
            self.assertEqual(meta["io.modelcontextprotocol/clientCapabilities"], {})
            self.assertEqual(meta["io.modelcontextprotocol/clientInfo"]["name"], "our-harness")
            self.assertEqual(request.get_header("Mcp-protocol-version"), "2026-07-28")
            self.assertEqual(request.get_header("Mcp-method"), body["method"])
            self.assertIsNotNone(request.get_header("Mcp-name"))
            self.assertIsNone(request.get_header("Mcp-session-id"))
        self.assertEqual(opener.requests[2][0].get_header("Mcp-name"), "search")

    def test_http_auto_falls_back_only_on_method_not_found(self) -> None:
        not_found = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}).encode()],
            headers={"Content-Type": "application/json"},
        )
        initialize = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"protocolVersion": "2024-10-07", "capabilities": {}}}).encode()],
            headers={"Content-Type": "application/json"},
        )
        initialized = _ChunkResponse([], headers={"Content-Type": "application/json"})
        opener = _FixtureOpener([not_found, initialize, initialized])
        client = MCPClient({
            "transport": "http", "url": "https://example.test/mcp", "protocol_mode": "auto",
        }, timeout=1)
        client._http_opener = opener
        client.connect()
        self.assertEqual(client.protocol_era, "legacy")
        self.assertEqual(client.protocol_version, "2024-10-07")
        self.assertEqual(json.loads(opener.requests[0][0].data)["method"], "server/discover")
        self.assertEqual(json.loads(opener.requests[1][0].data)["method"], "initialize")

    def test_http_auto_does_not_treat_server_failure_as_legacy(self) -> None:
        class FailingOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout=None):
                self.calls += 1
                raise urllib.error.HTTPError(request.full_url, 503, "down", {}, None)

        opener = FailingOpener()
        client = MCPClient({
            "transport": "http", "url": "https://example.test/mcp", "protocol_mode": "auto",
        }, timeout=0.5)
        client._http_opener = opener
        with self.assertRaisesRegex(HarnessError, "status 503"):
            client.connect()
        self.assertEqual(opener.calls, 1)

    def test_modern_stdio_uses_disposable_probe_sibling(self) -> None:
        server_code = (
            "import json,sys\n"
            "first=json.loads(sys.stdin.readline())\n"
            "if first['method']=='server/discover':\n"
            " result={'resultType':'complete','supportedVersions':['2026-07-28'],'capabilities':{'tools':{}},'ttlMs':1000,'cacheScope':'private'}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':first['id'],'result':result}),flush=True)\n"
            "else:\n"
            " meta=first.get('params',{}).get('_meta',{})\n"
            " ok=(first['method']=='tools/list' and meta.get('io.modelcontextprotocol/protocolVersion')=='2026-07-28')\n"
            " result={'resultType':'complete','tools':[{'name':'fresh-child'}] if ok else [],'ttlMs':10,'cacheScope':'private'}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':first['id'],'result':result}),flush=True)\n"
        )
        client = MCPClient({
            "transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code],
            "protocol_mode": "modern",
        }, timeout=1)
        try:
            client.connect()
            self.assertEqual(client.protocol_era, "modern")
            self.assertEqual(client.list_tools(), [{"name": "fresh-child"}])
        finally:
            client.close()

    def test_stdio_auto_falls_back_on_fresh_legacy_child(self) -> None:
        server_code = (
            "import json,sys\n"
            "first=json.loads(sys.stdin.readline())\n"
            "if first['method']=='server/discover':\n"
            " print(json.dumps({'jsonrpc':'2.0','id':first['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True)\n"
            "else:\n"
            " result={'protocolVersion':'2025-11-25','capabilities':{}}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':first['id'],'result':result}),flush=True)\n"
            " json.loads(sys.stdin.readline())\n"
            " tools=json.loads(sys.stdin.readline())\n"
            " print(json.dumps({'jsonrpc':'2.0','id':tools['id'],'result':{'tools':[{'name':'legacy'}]}}),flush=True)\n"
        )
        client = MCPClient({
            "transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code],
            "protocol_mode": "auto",
        }, timeout=1)
        try:
            client.connect()
            self.assertEqual(client.protocol_era, "legacy")
            self.assertEqual(client.list_tools(), [{"name": "legacy"}])
        finally:
            client.close()

    def test_http_redirect_cannot_retarget_mcp_request(self) -> None:
        target_hits: list[str] = []

        class TargetHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                target_hits.append("GET")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                target_hits.append("POST")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target.daemon_threads = True
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/admin")
                self.send_header("Content-Length", "0")
                self.end_headers()

        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        source.daemon_threads = True
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            client = MCPClient(
                {"transport": "http", "url": f"http://127.0.0.1:{source.server_port}/mcp"},
                timeout=1,
            )
            with self.assertRaisesRegex(HarnessError, "redirects are not accepted"):
                client.request("tools/list", {})
            self.assertEqual(target_hits, [])
        finally:
            source.shutdown()
            source.server_close()
            source_thread.join(timeout=1)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=1)

    def test_http_notification_accepts_empty_success_body(self) -> None:
        client = MCPClient({"name": "fixture", "transport": "http", "url": "http://127.0.0.1:8999/mcp"})
        client._http_opener = _FixtureOpener([_EmptyResponse()])
        client.notify("notifications/initialized", {})

    def test_http_rejects_prefix_spoof_and_mismatched_jsonrpc(self) -> None:
        for url in (
            "http://localhost.attacker.example/mcp",
            "http://127.0.0.1.attacker.example/mcp",
            "http://user:pass@localhost/mcp",
            "http://localhost/mcp#fragment",
        ):
            client = MCPClient({"transport": "http", "url": url})
            with self.assertRaises(HarnessError):
                client.request("tools/list", {})
        client = MCPClient({"transport": "http", "url": "https://example.invalid/mcp"})
        client._post_http = lambda _message, allow_empty=False, **_kwargs: {"jsonrpc": "2.0", "id": 99, "result": {}}  # type: ignore[method-assign]
        with self.assertRaisesRegex(HarnessError, "mismatched JSON-RPC"):
            client.request("tools/list", {})
        client._post_http = lambda _message, allow_empty=False, **_kwargs: {"jsonrpc": "2.0", "id": float(client.sequence), "result": {}}  # type: ignore[method-assign]
        with self.assertRaisesRegex(HarnessError, "mismatched JSON-RPC"):
            client.request("tools/list", {})

    def test_http_negotiates_protocol_and_replays_session_headers(self) -> None:
        initialize = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25", "capabilities": {}}}).encode()],
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "session-123"},
        )
        initialized = _ChunkResponse([], headers={"Content-Type": "application/json"})
        tools = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}).encode()],
            headers={"Content-Type": "application/json"},
        )
        opener = _FixtureOpener([initialize, initialized, tools])
        client = MCPClient({"name": "fixture", "transport": "http", "url": "https://example.test/mcp"}, timeout=1)
        client._http_opener = opener
        client.connect()
        self.assertEqual(client.protocol_version, "2025-11-25")
        self.assertEqual(client.session_id, "session-123")
        self.assertEqual(client.list_tools(), [])
        initialized_request = opener.requests[1][0]
        tools_request = opener.requests[2][0]
        self.assertEqual(initialized_request.get_header("Mcp-protocol-version"), "2025-11-25")
        self.assertEqual(initialized_request.get_header("Mcp-session-id"), "session-123")
        self.assertEqual(tools_request.get_header("Mcp-session-id"), "session-123")

    def test_http_rejects_protocol_downgrade_and_session_switch(self) -> None:
        unsupported = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "1900-01-01"}}).encode()],
            headers={"Content-Type": "application/json"},
        )
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"}, timeout=0.5)
        client._http_opener = _FixtureOpener([unsupported])
        with self.assertRaisesRegex(HarnessError, "unsupported protocol version"):
            client.connect()

        changed = _ChunkResponse(
            [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}).encode()],
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "session-2"},
        )
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"}, timeout=0.5)
        client.protocol_version = "2025-11-25"
        client.session_id = "session-1"
        client._http_opener = _FixtureOpener([changed])
        with self.assertRaisesRegex(HarnessError, "changed its session ID"):
            client.list_tools()

    def test_http_sse_returns_matching_event_without_waiting_for_eof(self) -> None:
        event = b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'
        response = _ChunkResponse([event], headers={"Content-Type": "text/event-stream"}, repeat=True, delay=0.01)
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"}, timeout=0.5)
        client._http_opener = _FixtureOpener([response])
        started = time.monotonic()
        self.assertEqual(client.list_tools(), [])
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertTrue(response.closed)

    def test_http_dispatches_ping_and_notification_before_response(self) -> None:
        stream = _ChunkResponse(
            [
                b'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed","params":{}}\n\n',
                b'data: {"jsonrpc":"2.0","id":"server-ping","method":"ping","params":{}}\n\n',
                b'data: {"jsonrpc":"2.0","id":"server-unknown","method":"unknown/server/request","params":{}}\n\n',
                b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n',
            ],
            headers={"Content-Type": "text/event-stream"},
        )
        reply = _ChunkResponse([], headers={"Content-Type": "application/json"})
        unsupported_reply = _ChunkResponse([], headers={"Content-Type": "application/json"})
        opener = _FixtureOpener([stream, reply, unsupported_reply])
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"}, timeout=0.5)
        client._http_opener = opener
        self.assertEqual(client.list_tools(), [])
        sent_reply = json.loads(opener.requests[1][0].data)
        self.assertEqual(sent_reply, {"jsonrpc": "2.0", "id": "server-ping", "result": {}})
        sent_unsupported = json.loads(opener.requests[2][0].data)
        self.assertEqual(sent_unsupported["id"], "server-unknown")
        self.assertEqual(sent_unsupported["error"]["code"], -32601)
        self.assertEqual(client.drain_notifications()[0]["method"], "notifications/tools/list_changed")

    def test_stdio_dispatches_ping_and_method_not_found_before_response(self) -> None:
        server_code = (
            "import json,sys\n"
            "initialize=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'jsonrpc':'2.0','id':'ping-1','method':'ping','params':{}}),flush=True)\n"
            "ping_reply=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'jsonrpc':'2.0','id':initialize['id'],'result':{'protocolVersion':'2025-11-25','capabilities':{}}}),flush=True)\n"
            "json.loads(sys.stdin.readline())\n"
            "tools=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'jsonrpc':'2.0','id':'request-2','method':'unknown/server/request','params':{}}),flush=True)\n"
            "unsupported=json.loads(sys.stdin.readline())\n"
            "ok=(ping_reply.get('result')=={} and unsupported.get('error',{}).get('code')==-32601)\n"
            "print(json.dumps({'jsonrpc':'2.0','id':tools['id'],'result':{'tools':[{'name':'ok'}] if ok else []}}),flush=True)\n"
        )
        client = MCPClient(
            {"transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code]},
            timeout=1,
        )
        try:
            client.connect()
            self.assertEqual(client.list_tools(), [{"name": "ok"}])
        finally:
            client.close()

    def test_tools_list_pagination_reuses_one_deadline(self) -> None:
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"}, timeout=1)
        calls = []
        pages = [
            {"tools": [{"name": "a"}], "nextCursor": "cursor-2"},
            {"tools": [{"name": "b"}], "nextCursor": "cursor-3"},
            {"tools": [{"name": "c"}]},
        ]

        def request(method, params, deadline_at):
            calls.append((method, params, deadline_at))
            return pages.pop(0)

        client._request = request
        self.assertEqual([tool["name"] for tool in client.list_tools()], ["a", "b", "c"])
        self.assertEqual([call[1] for call in calls], [{}, {"cursor": "cursor-2"}, {"cursor": "cursor-3"}])
        self.assertEqual(len({call[2] for call in calls}), 1)

    def test_modern_tools_list_cache_hint_is_per_catalog_fetch(self) -> None:
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"})
        client.protocol_era = "modern"
        client.protocol_version = "2026-07-28"
        results = [
            {"resultType": "complete", "tools": [], "ttlMs": 1, "cacheScope": "private"},
            {"resultType": "complete", "tools": [], "ttlMs": 500, "cacheScope": "public"},
        ]
        client._request = lambda *_args: results.pop(0)  # type: ignore[method-assign]
        client.list_tools()
        self.assertEqual(client.cache_hints["tools/list"], {"ttlMs": 1, "cacheScope": "private"})
        client.list_tools()
        self.assertEqual(client.cache_hints["tools/list"], {"ttlMs": 500, "cacheScope": "public"})

    def test_stdio_descendant_retaining_pipes_does_not_extend_deadline(self) -> None:
        server_code = (
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)'],stdout=sys.stdout,stderr=sys.stderr)\n"
            "sys.stdin.readline()\n"
        )
        client = MCPClient(
            {"transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code]},
            timeout=0.2,
        )
        started = time.monotonic()
        with self.assertRaisesRegex(HarnessError, "timed out"):
            client.connect()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 0.8)
        self.assertFalse(any(thread.name == "harness-mcp-stdio-reader" and thread.is_alive() for thread in threading.enumerate()))

    def test_stdio_large_write_obeys_deadline_and_stops_child_and_writer(self) -> None:
        server_code = (
            "import json,sys,time\n"
            "initialize=json.loads(sys.stdin.readline())\n"
            "result={'protocolVersion':'2025-11-25','capabilities':{}}\n"
            "print(json.dumps({'jsonrpc':'2.0','id':initialize['id'],'result':result}),flush=True)\n"
            "json.loads(sys.stdin.readline())\n"
            "time.sleep(5)\n"
        )
        client = MCPClient(
            {"transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code]},
            timeout=0.2,
            max_response_bytes=1_000_000,
        )
        client.connect()
        process = client.process
        self.assertIsNotNone(process)
        started = time.monotonic()
        with self.assertRaisesRegex(HarnessError, "write timed out"):
            client.call_tool("large", {"payload": "x" * 900_000})
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 1.0)
        self.assertIsNotNone(process.poll())
        self.assertFalse(any(thread.name == "harness-mcp-stdio-writer" and thread.is_alive() for thread in threading.enumerate()))
        client.close()

    def test_stdio_oversize_unframed_output_is_bounded_and_stops_process(self) -> None:
        server_code = (
            "import json,os,sys\n"
            "initialize=json.loads(sys.stdin.readline())\n"
            "result={'protocolVersion':'2025-11-25','capabilities':{}}\n"
            "print(json.dumps({'jsonrpc':'2.0','id':initialize['id'],'result':result}),flush=True)\n"
            "json.loads(sys.stdin.readline())\n"
            "json.loads(sys.stdin.readline())\n"
            "payload=b'x'*(5*1024*1024)\n"
            "offset=0\n"
            "while offset < len(payload):\n"
            " offset += os.write(sys.stdout.fileno(),payload[offset:])\n"
        )
        client = MCPClient(
            {"transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code]},
            timeout=0.5,
            max_response_bytes=1024,
        )
        client.connect()
        process = client.process
        self.assertIsNotNone(process)
        started = time.monotonic()
        with self.assertRaisesRegex(HarnessError, "exceeded its limit"):
            client.list_tools()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertIsNotNone(process.poll())
        self.assertFalse(any(thread.name in {"harness-mcp-stdio-reader", "harness-mcp-stdio-writer"} and thread.is_alive() for thread in threading.enumerate()))
        client.close()

    def test_http_trickle_obeys_total_deadline_and_stops_reader(self) -> None:
        response = _ChunkResponse([b": keep-alive\n\n"], headers={"Content-Type": "text/event-stream"}, repeat=True, delay=0.02)
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"}, timeout=0.15, max_response_bytes=1_000_000)
        client._http_opener = _FixtureOpener([response])
        started = time.monotonic()
        with self.assertRaisesRegex(HarnessError, "wall-clock deadline"):
            client.list_tools()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertLess(elapsed, 0.5)
        self.assertTrue(response.closed)
        self.assertFalse(any(thread.name == "harness-mcp-http-reader" and thread.is_alive() for thread in threading.enumerate()))

    def test_http_rejects_unsafe_final_redirect_url_before_parsing(self) -> None:
        response = _ChunkResponse(
            [b'{"jsonrpc":"2.0","id":1,"result":{}}'],
            url="http://attacker.example/mcp",
        )
        client = MCPClient({"transport": "http", "url": "https://example.test/mcp"}, timeout=0.5)
        client._http_opener = _FixtureOpener([response])
        with self.assertRaisesRegex(HarnessError, "HTTPS or loopback"):
            client.request("tools/list", {})

    def test_configured_server_and_cli_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({"mcp": {"servers": [{"name": "fixture", "transport": "http", "url": "https://example.test/mcp", "protocol_mode": "auto"}]}}),
                encoding="utf-8",
            )
            server = configured_server(load_config(root), "fixture")
            self.assertEqual(server["url"], "https://example.test/mcp")
            self.assertEqual(server["protocol_mode"], "auto")
            with self.assertRaisesRegex(HarnessError, "not found"):
                configured_server(load_config(root), "missing")
        parsed = parser().parse_args(["mcp", "call", "fixture", "search", "--arguments", '{"q":"term"}'])
        self.assertEqual(parsed.mcp_command, "call")
        self.assertEqual(parsed.tool, "search")

    def test_stdio_stderr_is_drained_bounded_and_reported(self) -> None:
        server_code = (
            "import json,sys\n"
            "for line in sys.stdin:\n"
            " message=json.loads(line)\n"
            " if 'id' not in message: continue\n"
            " if message['method']=='initialize':\n"
            "  result={'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fixture','version':'1'}}\n"
            "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)\n"
            " else:\n"
            "  sys.stderr.write('fixture diagnostic '+('x'*200000)); sys.stderr.flush()\n"
            "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],'error':{'code':-1,'message':'failed'}}),flush=True)\n"
        )
        client = MCPClient(
            {"name": "fixture", "transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code]},
            timeout=5,
            max_response_bytes=2048,
        )
        try:
            client.connect()
            with self.assertRaisesRegex(HarnessError, "server stderr.*fixture diagnostic"):
                client.list_tools()
            self.assertTrue(client.stderr_truncated)
            self.assertLessEqual(len(client.stderr_buffer), 2048)
        finally:
            client.close()

    def test_stdio_short_stderr_is_reported(self) -> None:
        server_code = (
            "import json,sys,time\n"
            "for line in sys.stdin:\n"
            " message=json.loads(line)\n"
            " if 'id' not in message: continue\n"
            " if message['method']=='initialize':\n"
            "  result={'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fixture','version':'1'}}\n"
            "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)\n"
            " else:\n"
            "  sys.stderr.write('short diagnostic\\n'); sys.stderr.flush(); time.sleep(0.05)\n"
            "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],'error':{'code':-1,'message':'failed'}}),flush=True)\n"
        )
        client = MCPClient(
            {"name": "fixture", "transport": "stdio", "command": sys.executable, "args": ["-u", "-c", server_code]},
            timeout=5,
            max_response_bytes=2048,
        )
        try:
            client.connect()
            with self.assertRaisesRegex(HarnessError, "server stderr.*short diagnostic"):
                client.list_tools()
        finally:
            client.close()


class RefinementCLITests(unittest.TestCase):
    def test_review_and_promote_candidate_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(root)
            with MemoryStore(config) as memory:
                manager = RefinementManager(memory)
                candidate_id = manager.stage_candidate(
                    manager.plan("prompt", "fixture", "Use the checked rule.", ["run-1"], "Keep behavior stable")
                )
            review = parser().parse_args(
                [
                    "--project",
                    str(root),
                    "refine",
                    "review",
                    candidate_id,
                    "--verdict",
                    "PASS",
                    "--reason",
                    "fixture passed",
                    "--verification-json",
                    '[{"name":"fixture","passed":true,"evidence":"ok"}]',
                ]
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(review.handler(review), 0)
            promote = parser().parse_args(["--project", str(root), "refine", "promote", candidate_id])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(promote.handler(promote), 0)
            with MemoryStore(config) as memory:
                self.assertEqual(RefinementManager(memory).candidate(candidate_id)["status"], "promoted")


if __name__ == "__main__":
    unittest.main()
