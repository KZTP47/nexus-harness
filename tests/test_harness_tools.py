from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from our_harness.agent_tools import AgentToolSession
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.harness_tools import TOOL_DEFINITIONS, HarnessTools
from our_harness import goal_tools, long_horizon, swarm_work
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.workflow import WorkflowDeadline


class HarnessToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="toolbox arbitrary project ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["workflow"]["max_tool_calls"] = 200
        self.config = LoadedConfig(data, self.root, [], {})
        self.memory = MemoryStore(self.config)
        self.addCleanup(self.memory.close)
        self.session = AgentToolSession(self.config, self.memory, WorkflowDeadline.start(120), lambda *args: None)
        self.tools = HarnessTools(self.session)
        (self.root / "src").mkdir()
        (self.root / "src" / "sample.py").write_text("def hello():\n    return 'Hello tools'\n", encoding="utf-8")
        (self.root / ".env").write_text("PRIVATE=value", encoding="utf-8")

    def test_glob_live_regex_literal_case_and_secret_exclusion(self):
        self.assertEqual(self.tools.execute("glob_search", {"pattern": "*.py"})["files"], ["src/sample.py"])
        result = self.tools.execute("grep_search", {"pattern": "HELLO.*tools", "regex": True, "ignore_case": True})
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertFalse(self.tools.execute("grep_search", {"pattern": "HELLO.*tools"})["matches"])
        self.assertFalse(self.tools.execute("grep_search", {"pattern": "PRIVATE"})["matches"])
        self.assertFalse(self.tools.execute("glob_search", {"pattern": ".env"})["files"])

    def test_search_limits_and_invalid_regex(self):
        for index in range(5):
            (self.root / f"{index}.txt").write_text("match\n", encoding="utf-8")
        result = self.tools.execute("glob_search", {"pattern": "*.txt", "max_results": 2})
        self.assertEqual(len(result["files"]), 2)
        self.assertTrue(result["truncated"])
        with self.assertRaises(HarnessError):
            self.tools.execute("grep_search", {"pattern": "[", "regex": True})

    def test_unknown_arguments_types_and_path_escape_fail(self):
        for name, args in [("glob_search", {"pattern": "*", "max_results": True}),
                           ("grep_search", {"pattern": "x", "path": "../"}),
                           ("git_show", {"commit": "--output=evil"}),
                           ("read_local_skill", {"path": ".env"}),
                           ("edit_file", {"path": "src/sample.py", "old_string": "", "new_string": "oops"})]:
            with self.subTest(name=name), self.assertRaises(HarnessError):
                self.tools.execute(name, args)

    def test_new_calls_observe_changes_and_exact_call_replays(self):
        first = self.session.execute("owner", "one", "glob_search", {"pattern": "*.txt"})
        (self.root / "new.txt").write_text("created", encoding="utf-8")
        second = self.session.execute("owner", "two", "glob_search", {"pattern": "*.txt"})
        self.assertFalse(json.loads(first["content"])["files"])
        self.assertEqual(json.loads(second["content"])["files"], ["new.txt"])
        self.assertTrue(self.session.execute("owner", "one", "glob_search", {"pattern": "*.txt"})["duplicate"])

    def test_read_skill_and_code_navigation_fallback(self):
        folder = self.root / "skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text("# A local skill\nInstructions are reference data.\n", encoding="utf-8")
        self.assertIn("A local skill", str(self.tools.execute("read_local_skill", {"path": "skill/SKILL.md"})))
        result = self.tools.execute("code_navigation", {"asking": "where-is-it", "name": "hello"})
        self.assertFalse(result["exact"])
        self.assertEqual(result["places"][0]["path"], "src/sample.py")

    def test_edit_proposal_can_apply_through_nexus_transaction(self):
        from our_harness.changes import FileTransaction
        original = (self.root / "src/sample.py").read_bytes()
        result = self.tools.execute("edit_file", {"path": "src/sample.py", "old_string": "Hello tools", "new_string": "Improved tools"})
        self.assertFalse(result["applied"])
        self.assertEqual((self.root / "src/sample.py").read_bytes(), original)
        self.assertEqual(result["baseline_sha256"], hashlib.sha256(original).hexdigest())
        plans = swarm_work._validated_changes(self.root, result["changes"])
        FileTransaction(self.root, max_files=2, max_bytes=10000).apply(plans)
        self.assertIn("Improved tools", (self.root / "src/sample.py").read_text())

    def test_notebook_read_insert_replace_delete_preserves_metadata_clears_outputs(self):
        notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"custom": "keep"}, "cells": [
            {"id": "one", "cell_type": "code", "source": ["print(1)\n"], "metadata": {"tag": "keep"}, "outputs": [{"text": "stale"}], "execution_count": 3}]}
        path = self.root / "demo.ipynb"
        path.write_text(json.dumps(notebook), encoding="utf-8")
        read = self.tools.execute("read_notebook", {"path": path.name})
        self.assertNotIn("outputs", read["cells"][0])
        result = self.tools.execute("edit_notebook", {"path": path.name, "cell": 0, "operation": "replace", "source": "print(2)\n"})
        changed = json.loads(result["changes"][0]["content"])
        self.assertEqual(changed["metadata"], notebook["metadata"])
        self.assertEqual(changed["cells"][0]["metadata"], {"tag": "keep"})
        self.assertEqual(changed["cells"][0]["outputs"], [])
        self.assertIsNone(changed["cells"][0]["execution_count"])
        inserted = self.tools.execute("edit_notebook", {"path": path.name, "cell": 1, "operation": "insert", "source": "Hi", "cell_type": "markdown"})
        self.assertEqual(len(json.loads(inserted["changes"][0]["content"])["cells"]), 2)
        deleted = self.tools.execute("edit_notebook", {"path": path.name, "cell": 0, "operation": "delete"})
        self.assertEqual(json.loads(deleted["changes"][0]["content"])["cells"], [])
        with self.assertRaises(HarnessError):
            self.tools.execute("edit_notebook", {"path": path.name, "cell": 1, "operation": "delete"})

    def test_git_tools_execute_on_real_arbitrary_repository(self):
        def git(*argv):
            subprocess.run(["git", *argv], cwd=self.root, check=True, capture_output=True)
        git("init")
        git("add", "src/sample.py")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Initial fixture")
        self.assertIn("Initial fixture", self.tools.execute("git_log", {})["stdout"])
        self.assertIn("Initial fixture", self.tools.execute("git_show", {"commit": "HEAD"})["stdout"])
        self.assertIn("def hello", self.tools.execute("git_show", {"commit": "HEAD", "path": "src/sample.py"})["stdout"])
        self.assertIn("Fixture", self.tools.execute("git_blame", {"path": "src/sample.py", "start_line": 1, "end_line": 2})["stdout"])
        (self.root / "src/sample.py").write_text("new contents\n", encoding="utf-8")
        self.assertIn("src/sample.py", self.tools.execute("git_status", {})["stdout"])
        self.assertIn("+new contents", self.tools.execute("git_diff", {"path": "src/sample.py"})["stdout"])

    def test_resource_and_search_transports_are_bounded_and_report_sources(self):
        self.config.data["mcp"]["servers"] = [{"name": "fixture", "transport": "stdio", "command": "fixture", "args": []}]
        with patch("our_harness.harness_tools.MCPClient") as client:
            peer = client.return_value.__enter__.return_value
            peer.request.return_value = {"resources": [{"uri": "fixture://one"}], "nextCursor": "next"}
            result = self.tools.execute("list_mcp_resources", {"server": "fixture"})
            self.assertEqual(result["result"]["nextCursor"], "next")
            self.tools.execute("list_mcp_resource_templates", {"server": "fixture", "cursor": "next"})
            peer.request.assert_called_with("resources/templates/list", {"cursor": "next"})
            self.tools.execute("read_mcp_resource", {"server": "fixture", "uri": "fixture://one"})
            peer.request.assert_called_with("resources/read", {"uri": "fixture://one"})
        with patch("our_harness.harness_tools.fetch_public", return_value={"url": "https://html.duckduckgo.com/html/", "data": b'<a class="result__a" href="https://example.org/guide">A guide</a>'}):
            self.assertEqual(self.tools.execute("web_search", {"query": "guide"})["results"][0]["url"], "https://example.org/guide")
        with patch("our_harness.harness_tools.fetch_public", return_value={"url": "https://example.org/", "data": b'challenge'}):
            with self.assertRaisesRegex(HarnessError, "no usable results"):
                self.tools.execute("web_search", {"query": "guide"})
        with patch("our_harness.harness_tools.fetch_public", side_effect=[
                {"url": "https://html.duckduckgo.com/", "data": b"challenge"},
                {"url": "https://www.bing.com/search?q=guide&format=rss", "data": b"<rss><channel><item><title>Guide</title><link>https://example.org/real-guide</link></item></channel></rss>"}]):
            result = self.tools.execute("web_search", {"query": "guide"})
            self.assertEqual(result["results"][0]["url"], "https://example.org/real-guide")
            self.assertIn("bing.com", result["source_url"])

    def test_schema_tool_discovery_workflow_and_goal_integration(self):
        names = {one["name"] for one in TOOL_DEFINITIONS}
        self.assertTrue(names <= {one["name"] for one in self.session.definitions()})
        self.assertEqual(self.session.definitions(capabilities=set()), [])
        for schema in (swarm_work.WORK_FORMAT.schema, long_horizon.AGENT_ACTION_FORMAT.schema):
            offered = {one["properties"]["name"]["enum"][0] for one in schema["properties"]["tool_calls"]["items"]["anyOf"]}
            self.assertTrue(names <= offered)
        self.assertTrue(self.tools.execute("tool_search", {"query": "git"})["tools"])

    def test_tool_configuration_and_cancellable_wait(self):
        self.config.data["mcp"]["servers"] = [{"name": "test-peer", "token": "DO_NOT_EXPOSE", "allowed_tools": ["inspect"]}]
        result = self.tools.execute("tool_config", {})
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(result))
        self.assertEqual(result["mcp_servers"], ["test-peer"])
        self.assertLess(self.tools.execute("sleep", {"seconds": 0})["elapsed_seconds"], 1)
        with patch("our_harness.harness_tools.MCPClient") as client:
            peer = client.return_value.__enter__.return_value
            peer.list_tools.return_value = [{"name": "inspect"}, {"name": "unapproved"}]
            peer.protocol_version = "fixture-version"
            self.assertEqual(self.tools.execute("mcp_status", {"server": "test-peer"})["allowed_tools_available"], ["inspect"])

    def test_effect_tools_run_commands_write_and_enforce_cwd(self):
        result = goal_tools.execute(self.config, self.root, "run_command", {"argv": [sys.executable, "-c", "print('NEXUS_OWN_TOOL_OK')"]})
        self.assertEqual(result["result"]["stdout"].strip(), "NEXUS_OWN_TOOL_OK")
        self.assertFalse(result["published"])
        goal_tools.execute(self.config, self.root, "write_file", {"path": "created/output.txt", "content": "written"})
        self.assertEqual((self.root / "created/output.txt").read_text(), "written")
        with self.assertRaises(HarnessError):
            goal_tools.execute(self.config, self.root, "run_command", {"argv": [sys.executable, "-c", "print('oops')"], "cwd": ".."})
        with self.assertRaises(HarnessError):
            goal_tools.execute(self.config, self.root, "write_file", {"path": "../outside.txt", "content": "oops"})

    def test_real_mcp_resource_protocol_and_language_server_queries(self):
        mcp_script = self.root / "mcp_fixture.py"
        mcp_script.write_text('''import json,sys
for line in sys.stdin:
 request=json.loads(line)
 if "id" not in request: continue
 method=request["method"]
 if method=="initialize": result={"protocolVersion":"2025-11-25","capabilities":{"resources":{},"tools":{}},"serverInfo":{"name":"fixture","version":"1"}}
 elif method=="tools/list": result={"tools":[{"name":"inspect","annotations":{"readOnlyHint":True,"destructiveHint":False}}]}
 elif method=="tools/call": result={"content":[{"type":"text","text":"REAL_MCP_CALL "+str(request["params"]["arguments"].get("nested",{}))}]}
 elif method=="resources/list": result={"resources":[{"uri":"fixture://one","name":"One"}],"nextCursor":"second"}
 elif method=="resources/templates/list": result={"resourceTemplates":[{"uriTemplate":"fixture://{name}","name":"Template"}]}
 else: result={"contents":[{"uri":"fixture://one","text":"REAL_RESOURCE"}]}
 print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
''', encoding="utf-8")
        self.config.data["mcp"]["servers"] = [{"name": "fixture", "command": sys.executable, "args": [str(mcp_script)], "transport": "stdio", "allowed_tools": ["inspect"]}]
        self.assertEqual(self.tools.execute("list_mcp_resources", {"server": "fixture"})["result"]["nextCursor"], "second")
        self.assertIn("REAL_RESOURCE", json.dumps(self.tools.execute("read_mcp_resource", {"server": "fixture", "uri": "fixture://one"})))
        self.assertTrue(self.tools.execute("mcp_status", {"server": "fixture"})["connected"])
        called = self.tools.execute("call_mcp_tool", {"server": "fixture", "tool": "inspect", "arguments_json": '{"nested":{"arbitrary_key":"preserved"}}'})
        self.assertIn("preserved", json.dumps(called))
        with self.assertRaises(HarnessError):
            self.tools.execute("call_mcp_tool", {"server": "fixture", "tool": "unapproved", "arguments_json": "{}"})
        lsp_script = self.root / "lsp_fixture.py"
        lsp_script.write_text('''import json,sys
stream=sys.stdin.buffer
while True:
 length=0
 while True:
  line=stream.readline()
  if not line: sys.exit()
  if line in (b"\\r\\n",b"\\n"): break
  if line.lower().startswith(b"content-length:"): length=int(line.split(b":")[1])
 request=json.loads(stream.read(length))
 if "id" not in request: continue
 method=request["method"]
 result={"capabilities":{}} if method=="initialize" else {"method":method,"fixture":"REAL_LSP"}
 body=json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}).encode()
 sys.stdout.buffer.write(b"Content-Length: "+str(len(body)).encode()+b"\\r\\n\\r\\n"+body)
 sys.stdout.buffer.flush()
''', encoding="utf-8")
        with patch("our_harness.navigate._server_for", return_value=("fixture", [sys.executable, str(lsp_script)])):
            for action in ("symbols", "diagnostics", "definition", "references", "hover"):
                result = self.tools.execute("language_server", {"action": action, "path": "src/sample.py"})
                self.assertTrue(result["available"])
                self.assertEqual(result["result"]["fixture"], "REAL_LSP")


if __name__ == "__main__":
    unittest.main()
