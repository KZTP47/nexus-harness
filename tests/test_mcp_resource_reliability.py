from __future__ import annotations
import copy
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from our_harness import cancellation, harness_tools
from our_harness.agent_tools import AgentToolSession
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.workflow import WorkflowDeadline


class ResourceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="resource project ")
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(self.temp.name), [], {})
        self.config.data["mcp"]["servers"] = [{"name":"fixture", "command":"first", "args":[]}]
        self.memory = MemoryStore(self.config)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.memory.close)
        self.connections = []
        self.rows = [{"uri":f"fixture://{i}","name":"café 🚀"*20} for i in range(16)]
        owner = self
        class Peer:
            def __init__(self, *args, **kwargs):
                self.cursor = "raw-server-" + str(len(owner.connections))
                self.closed = False
                self.calls = []
                owner.connections.append(self)
            def __enter__(self): return self
            def __exit__(self,*args): self.closed = True
            def request(self, method, params):
                self.calls.append((method,params))
                if method == "resources/read":
                    return {"contents":[{"uri":params["uri"],"mimeType":"text/plain","text":"café 🚀"}]}
                field = "resources" if method == "resources/list" else "resourceTemplates"
                if params:
                    if params["cursor"] != self.cursor: raise HarnessError("wrong server session")
                    return {field:owner.rows[8:],"_meta":{"page":"second"}}
                return {field:owner.rows[:8],"nextCursor":self.cursor,"_meta":{"page":"first"}}
        self.peer = Peer
        self.session = self.new_session()
        self.counter = 0

    def new_session(self, run_id=None):
        value = AgentToolSession(self.config,self.memory,WorkflowDeadline.start(30),lambda *a:None,run_id=run_id)
        value.per_call_bytes=1100
        value.max_calls=200
        return value

    def call(self,name,args,session=None,call_id=None):
        self.counter += 1
        result = (session or self.session).execute("reader",call_id or str(self.counter),name,args)
        self.assertLessEqual(len(result["content"].encode()),(session or self.session).per_call_bytes)
        self.assertFalse(result["truncated"])
        return result,json.loads(result["content"]) if result["content"] else {}

    def test_both_lists_use_one_connection_and_preserve_unicode_metadata(self):
        with patch.object(harness_tools,"MCPClient",self.peer):
            for name,field in (("list_mcp_resources","resources"),("list_mcp_resource_templates","resourceTemplates")):
                result,page=self.call(name,{"server":"fixture"})
                self.assertEqual(result["status"],"ok")
                self.assertTrue(page["result"]["nextCursor"].startswith("nexus-resource-v1:"))
                rows=list(page["result"][field])
                self.assertEqual([one["metadata"]["_meta"]["page"] for one in page["source_page_metadata"]],["first","second"])
                count=len(self.connections)
                while page["result"].get("nextCursor"):
                    result,page=self.call(name,{"server":"fixture","cursor":page["result"]["nextCursor"]})
                    self.assertEqual(result["status"],"ok")
                    rows.extend(page["result"][field])
                self.assertEqual(rows,self.rows)
                self.assertEqual(len(self.connections),count)
                self.assertEqual(len(self.connections[-1].calls),2)
                self.assertTrue(self.connections[-1].closed)

    def test_foreign_raw_method_config_restart_cursors_rejected_without_connection(self):
        with patch.object(harness_tools,"MCPClient",self.peer):
            _,page=self.call("list_mcp_resources",{"server":"fixture"})
            cursor=page["result"]["nextCursor"]
            for name,held,token in (("list_mcp_resource_templates",self.session,cursor),("list_mcp_resources",self.new_session(),cursor),("list_mcp_resources",self.session,"raw-server-0")):
                result,_=self.call(name,{"server":"fixture","cursor":token},held)
                self.assertEqual(result["status"],"error")
            self.config.data["mcp"]["servers"][0]["command"]="changed"
            result,_=self.call("list_mcp_resources",{"server":"fixture","cursor":cursor})
            self.assertEqual(result["status"],"error")
            self.assertEqual(len(self.connections),1)

    def test_cursorless_refresh_and_snapshot_count_and_byte_caps(self):
        with patch.object(harness_tools,"MCPClient",self.peer),patch.object(harness_tools,"RESOURCE_SNAPSHOT_LIMIT",1):
            _,old=self.call("list_mcp_resources",{"server":"fixture"})
            result,_=self.call("list_mcp_resources",{"server":"fixture"})
            self.assertEqual(result["status"],"error")
            self.assertEqual(len(self.connections),2)
            result,_=self.call("list_mcp_resources",{"server":"fixture","cursor":old["result"]["nextCursor"]})
            self.assertEqual(result["status"],"ok")
        with patch.object(harness_tools,"MCPClient",self.peer),patch.object(harness_tools,"RESOURCE_TOTAL_BYTES",1):
            result,_=self.call("list_mcp_resources",{"server":"fixture"},self.new_session())
            self.assertEqual(result["status"],"error")

    def test_page_and_aggregate_caps_close_connection_without_partial_snapshot(self):
        for constant,limit in (("RESOURCE_PAGE_LIMIT",1),("RESOURCE_SNAPSHOT_BYTES",50)):
            held=self.new_session()
            with patch.object(harness_tools,"MCPClient",self.peer),patch.object(harness_tools,constant,limit):
                result,_=self.call("list_mcp_resources",{"server":"fixture"},held)
            self.assertEqual(result["status"],"error")
            self.assertFalse(held._mcp_resource_snapshots)
            self.assertTrue(self.connections[-1].closed)

    def test_cycle_and_cancel_close_connection(self):
        original=self.peer.request
        def cycle(peer,method,params):
            return {"resources":[],"nextCursor":"same"}
        with patch.object(harness_tools,"MCPClient",self.peer),patch.object(self.peer,"request",cycle):
            result,_=self.call("list_mcp_resources",{"server":"fixture"})
            self.assertEqual(result["status"],"error")
            self.assertTrue(self.connections[-1].closed)
        token=cancellation.Cancellation()
        def cancel(peer,method,params):
            token.cancel()
            return original(peer,method,params)
        with patch.object(harness_tools,"MCPClient",self.peer),patch.object(self.peer,"request",cancel),cancellation.use(token):
            with self.assertRaises(cancellation.ChatCancelled):
                self.call("list_mcp_resources",{"server":"fixture"})
        self.assertTrue(self.connections[-1].closed)

    def test_collection_deadline_closes_before_another_page(self):
        original=self.peer.request
        def delayed(peer,method,params):
            time.sleep(.04)
            return original(peer,method,params)
        held=self.new_session();held.deadline=WorkflowDeadline.start(.02)
        with patch.object(harness_tools,"MCPClient",self.peer),patch.object(self.peer,"request",delayed):
            with self.assertRaises(HarnessError):
                self.call("list_mcp_resources",{"server":"fixture"},held)
        self.assertTrue(self.connections[-1].closed)
        self.assertEqual(len(self.connections[-1].calls),1)
        self.assertFalse(held._mcp_resource_snapshots)

    def test_read_blob_unicode_oversize_and_tiny_budget_stay_complete(self):
        with patch.object(harness_tools,"MCPClient",self.peer):
            result,page=self.call("read_mcp_resource",{"server":"fixture","uri":"fixture://one"})
            self.assertEqual(result["status"],"ok")
            self.assertEqual(page["result"]["contents"][0]["text"],"café 🚀")
        def small_blob(peer,method,params):return {"contents":[{"uri":"fixture://one","blob":"YQ==","mimeType":"application/octet-stream"}]}
        with patch.object(harness_tools,"MCPClient",self.peer),patch.object(self.peer,"request",small_blob):
            result,page=self.call("read_mcp_resource",{"server":"fixture","uri":"fixture://one"})
            self.assertEqual(result["status"],"ok")
            self.assertEqual(page["result"]["contents"][0]["blob"],"YQ==")
        for remaining in (100,20):
            held=self.new_session();held.total_bytes=held.total_bytes_limit-remaining
            def blob(peer,method,params):return {"contents":[{"uri":"fixture://one","blob":"YQ=="*1000,"mimeType":"application/octet-stream"}]}
            with patch.object(harness_tools,"MCPClient",self.peer),patch.object(self.peer,"request",blob):
                result,_=self.call("read_mcp_resource",{"server":"fixture","uri":"fixture://one"},held)
            self.assertEqual(result["status"],"error")
            self.assertLessEqual(result["content_bytes"],remaining)
        for remaining in (0,1,2):
            held=self.new_session();held.total_bytes=held.total_bytes_limit-remaining
            with patch.object(harness_tools,"MCPClient",self.peer), self.assertRaisesRegex(HarnessError,"output budget exhausted"):
                self.call("read_mcp_resource",{"server":"fixture","uri":"fixture://one"},held)
        self.rows=[{"uri":"fixture://huge","name":"x"*5000}]
        with patch.object(harness_tools,"MCPClient",self.peer):
            result,_=self.call("list_mcp_resources",{"server":"fixture"})
            self.assertEqual(result["status"],"error")

    def test_cached_and_journal_pages_reject_reduced_budget_and_stale_final_cursor(self):
        run_id=self.memory.start_run("resource replay")
        held=self.new_session(run_id)
        with patch.object(harness_tools,"MCPClient",self.peer):
            _,page=self.call("list_mcp_resources",{"server":"fixture"},held,"first")
            first_cursor=page["result"]["nextCursor"]
            args={"server":"fixture","cursor":first_cursor}
            while True:
                result,page=self.call("list_mcp_resources",args,held)
                if not page["result"].get("nextCursor"):break
                args={"server":"fixture","cursor":page["result"]["nextCursor"]}
            final_id=str(self.counter)
            restarted=self.new_session(run_id)
            result,_=self.call("list_mcp_resources",args,restarted,final_id)
            self.assertEqual(result["status"],"error")
            self.assertTrue(result["replayed"])
            held.total_bytes=held.total_bytes_limit-100
            result,_=self.call("list_mcp_resources",{"server":"fixture"},held,"first")
            self.assertEqual(result["status"],"error")
            self.assertTrue(result["replayed"])
            self.assertEqual(len(self.connections),1)
        # Without a journal the exact-call memory cache has the same budget guard.
        held=self.new_session()
        with patch.object(harness_tools,"MCPClient",self.peer):
            self.call("read_mcp_resource",{"server":"fixture","uri":"fixture://one"},held,"cached")
            held.total_bytes=held.total_bytes_limit-20
            result,_=self.call("read_mcp_resource",{"server":"fixture","uri":"fixture://one"},held,"cached")
            self.assertEqual(result["status"],"error")


if __name__=="__main__": unittest.main()
