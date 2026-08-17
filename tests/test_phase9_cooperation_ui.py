from __future__ import annotations

import copy
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from importlib.resources import files
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.cooperation import CooperativeScheduler
from our_harness.graphs import migrate_graph, uses_cooperative_execution, validate_graph
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.server import HarnessHTTPServer


def isolated_config(root: Path) -> LoadedConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
    data["providers"] = {
        "reasoning": {
            "kind": "anthropic",
            "model": "fixture-reasoner",
            "endpoint": "https://api.example.invalid",
            "api_key_env": "PHASE9_TEST_KEY",
        },
        "local-code": {
            "kind": "ollama",
            "model": "fixture-coder",
            "endpoint": "http://127.0.0.1:11434",
            "allow_project_graphs": True,
        },
    }
    return LoadedConfig(data, root, [], {})


def cooperative_graph() -> dict:
    agent = {
        "provider_route": "local-code",
        "model": "fixture",
        "role_name": "fixture",
        "system_prompt": "",
        "capabilities": ["workspace.read"],
    }
    return {
        "schema_version": 2,
        "name": "cooperation-fixture",
        "entry": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "planner", "type": "planner", "config": dict(agent)},
            {"id": "coder", "type": "coder", "config": dict(agent)},
            {
                "id": "merge",
                "type": "merge",
                "config": {**agent, "required_slots": ["strategy", "implementation"], "output_field": "merged_output", "output_contract": "implementation_plan"},
            },
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "start-plan", "source": "start", "target": "planner", "mode": "state", "variables": ["task"], "return_fields": []},
            {"id": "start-code", "source": "start", "target": "coder", "mode": "delegate", "variables": ["task"], "return_fields": ["source_code"]},
            {"id": "plan-merge", "source": "planner", "target": "merge", "mode": "merge_input", "target_slot": "strategy", "variables": ["plan"], "return_fields": []},
            {"id": "code-merge", "source": "coder", "target": "merge", "mode": "merge_input", "target_slot": "implementation", "variables": ["source_code"], "return_fields": []},
            {"id": "merge-end", "source": "merge", "target": "end", "mode": "state", "variables": ["merged_output"], "return_fields": []},
        ],
    }


def repair_graph() -> dict:
    agent = {
        "provider_route": "local-code", "model": "fixture", "role_name": "coder",
        "system_prompt": "", "capabilities": ["workspace.read"],
    }
    return {
        "schema_version": 2, "entry": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "coder", "type": "coder", "config": agent},
            {"id": "test", "type": "tool", "config": {"role": "unit_test"}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "start-code", "source": "start", "target": "coder", "mode": "state", "variables": ["task"]},
            {"id": "code-test", "source": "coder", "target": "test", "mode": "state", "variables": ["source_code"]},
            {"id": "pass", "source": "test", "target": "end", "mode": "state", "condition": "stage_passed == true", "variables": []},
            {"id": "repair", "source": "test", "target": "coder", "mode": "delegate", "condition": "stage_passed == false", "variables": ["error_trace"], "return_fields": ["source_code"], "loop": {"max_iterations": 2, "temperature_decay": .5, "timeout_seconds": 60}},
        ],
    }


class GraphV2Tests(unittest.TestCase):
    def test_v1_migration_is_detached_and_typed(self) -> None:
        source = {"schema_version": 1, "entry": "agent", "nodes": [{"id": "agent", "type": "planner", "label": "Plan"}], "edges": []}
        migrated = migrate_graph(source)
        self.assertEqual(source["schema_version"], 1)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["nodes"][0]["config"]["role_name"], "Plan")
        self.assertEqual(validate_graph(migrated), [])

    def test_migrated_gauntlet_stays_on_the_serial_engine(self) -> None:
        source = json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8"))
        migrated = migrate_graph(source)
        self.assertEqual(validate_graph(migrated), [])
        self.assertFalse(uses_cooperative_execution(migrated))

    def test_merge_output_contract_is_typed(self) -> None:
        graph = cooperative_graph()
        graph["nodes"][3]["config"]["output_contract"] = "implementation_plan"
        self.assertEqual(validate_graph(graph), [])
        graph["nodes"][3]["config"]["output_contract"] = "arbitrary_object"
        self.assertTrue(any("output_contract" in issue.path for issue in validate_graph(graph)))

    def test_merge_and_delegation_contracts_are_validated(self) -> None:
        graph = cooperative_graph()
        self.assertEqual(validate_graph(graph), [])
        graph["edges"][3]["target_slot"] = "strategy"
        issues = validate_graph(graph)
        self.assertTrue(any("already connected" in issue.message for issue in issues))
        self.assertTrue(any("implementation" in issue.message for issue in issues))

    def test_scheduler_fans_out_then_waits_for_all_merge_slots(self) -> None:
        scheduler = CooperativeScheduler(cooperative_graph(), max_parallelism=2, max_dispatches=8)
        scheduler.set_entry_state({"task": "fixture"})
        self.assertEqual([item.node_id for item in scheduler.ready()], ["start"])
        scheduler.complete("start", {"task": "fixture"})
        self.assertEqual([item.node_id for item in scheduler.ready()], ["planner", "coder"])
        scheduler.complete("coder", {"source_code": "value"})
        self.assertEqual(scheduler.ready(), ())
        scheduler.complete("planner", {"plan": "steps"})
        merge = scheduler.ready()
        self.assertEqual([item.node_id for item in merge], ["merge"])
        self.assertEqual(set(merge[0].inputs["merge_inputs"]), {"strategy", "implementation"})

    def test_scheduler_reopens_nodes_on_a_bounded_repair_edge(self) -> None:
        scheduler = CooperativeScheduler(repair_graph(), max_parallelism=1, max_dispatches=10)
        scheduler.set_entry_state({"task": "repair", "temperature": .2})
        self.assertEqual(scheduler.ready()[0].node_id, "start")
        scheduler.complete("start", {"task": "repair"})
        self.assertEqual(scheduler.ready()[0].attempt, 1)
        scheduler.complete("coder", {"source_code": "first"})
        self.assertEqual(scheduler.ready()[0].node_id, "test")
        scheduler.complete("test", {"stage_passed": False, "error_trace": "failure"})
        retry = scheduler.ready()[0]
        self.assertEqual((retry.node_id, retry.attempt), ("coder", 2))
        self.assertAlmostEqual(scheduler.snapshot()["state"]["temperature"], .1)

    def test_delegate_must_return_its_declared_fields(self) -> None:
        scheduler = CooperativeScheduler(cooperative_graph(), max_parallelism=2, max_dispatches=8)
        scheduler.set_entry_state({"task": "fixture"})
        scheduler.ready()
        scheduler.complete("start", {"task": "fixture"})
        scheduler.ready()
        with self.assertRaisesRegex(HarnessError, "source_code"):
            scheduler.complete("coder", {"summary": "missing contract value"})
        self.assertIn("coder", scheduler.running)
        scheduler.complete("coder", {"source_code": "value"})

    def test_restore_requeues_two_interrupted_dispatches_with_inputs(self) -> None:
        scheduler = CooperativeScheduler(cooperative_graph(), max_parallelism=2, max_dispatches=3, timeout_seconds=100)
        base = scheduler.started_at
        scheduler.set_entry_state({"task": "fixture"})
        scheduler.ready(now=base + 1)
        scheduler.complete("start", {"task": "fixture"}, now=base + 2)
        first = scheduler.ready(now=base + 3)
        self.assertEqual([item.node_id for item in first], ["planner", "coder"])
        snapshot = scheduler.snapshot(now=base + 5)
        self.assertEqual(snapshot["running"], ["planner", "coder"])
        json.dumps(snapshot, allow_nan=False)

        restored = CooperativeScheduler.restore(cooperative_graph(), snapshot, now=1000)
        self.assertEqual(restored.running, set())
        replay = restored.ready(now=1001)
        self.assertEqual([item.node_id for item in replay], ["planner", "coder"])
        self.assertEqual([item.attempt for item in replay], [1, 1])
        self.assertEqual([item.inputs for item in replay], [item.inputs for item in first])
        self.assertEqual(restored.dispatches, 3)

    def test_restore_survives_a_second_crash_before_redispatch(self) -> None:
        scheduler = CooperativeScheduler(cooperative_graph(), max_parallelism=2, max_dispatches=3, timeout_seconds=100)
        base = scheduler.started_at
        scheduler.set_entry_state({"task": "fixture"})
        scheduler.ready(now=base + 1)
        scheduler.complete("start", {"task": "fixture"}, now=base + 2)
        scheduler.ready(now=base + 3)

        restored_once = CooperativeScheduler.restore(
            cooperative_graph(), scheduler.snapshot(now=base + 4), now=1000,
        )
        pending = restored_once.snapshot(now=1001)
        self.assertEqual(pending["running"], [])
        self.assertEqual(pending["ready"], ["planner", "coder"])
        self.assertEqual(pending["redispatch_attempts"], {"planner": 1, "coder": 1})

        restored_twice = CooperativeScheduler.restore(cooperative_graph(), pending, now=2000)
        replay = restored_twice.ready(now=2001)
        self.assertEqual([(item.node_id, item.attempt) for item in replay], [("planner", 1), ("coder", 1)])
        self.assertEqual(restored_twice.dispatches, 3)

    def test_restore_preserves_partial_merge_and_shared_state(self) -> None:
        scheduler = CooperativeScheduler(cooperative_graph(), max_parallelism=2, max_dispatches=12)
        scheduler.set_entry_state({"task": "fixture", "shared": "retained"})
        scheduler.ready()
        scheduler.complete("start", {"task": "fixture"})
        scheduler.ready()
        scheduler.complete("coder", {"source_code": "value"})
        snapshot = scheduler.snapshot()

        restored = CooperativeScheduler.restore(cooperative_graph(), snapshot)
        planner = restored.ready()
        self.assertEqual([item.node_id for item in planner], ["planner"])
        self.assertEqual(restored.snapshot()["state"]["shared"], "retained")
        restored.complete("planner", {"plan": "steps"})
        merge = restored.ready()
        self.assertEqual([item.node_id for item in merge], ["merge"])
        self.assertEqual(merge[0].inputs["merge_inputs"]["implementation"], {"source_code": "value"})

    def test_restore_retains_global_and_loop_deadlines(self) -> None:
        scheduler = CooperativeScheduler(repair_graph(), max_parallelism=1, max_dispatches=12, timeout_seconds=20)
        base = scheduler.started_at
        scheduler.set_entry_state({"task": "repair", "temperature": .2})
        scheduler.ready(now=base + 1)
        scheduler.complete("start", {"task": "repair"}, now=base + 2)
        scheduler.ready(now=base + 3)
        scheduler.complete("coder", {"source_code": "first"}, now=base + 4)
        scheduler.ready(now=base + 5)
        scheduler.complete("test", {"stage_passed": False, "error_trace": "failure"}, now=base + 6)
        snapshot = scheduler.snapshot(now=base + 15)
        self.assertEqual(snapshot["loop_counts"], {"repair": 1})
        self.assertAlmostEqual(snapshot["loop_elapsed_seconds"]["repair"], 9)

        restored = CooperativeScheduler.restore(repair_graph(), snapshot, now=500)
        self.assertEqual(restored.snapshot(now=500)["loop_counts"], {"repair": 1})
        self.assertAlmostEqual(restored.snapshot(now=500)["loop_elapsed_seconds"]["repair"], 9)
        restored.ready(now=504.9)
        with self.assertRaisesRegex(HarnessError, "timeout"):
            restored.ready(now=505)

    def test_restore_rejects_corrupted_or_mismatched_snapshots(self) -> None:
        scheduler = CooperativeScheduler(cooperative_graph(), max_parallelism=2, max_dispatches=12)
        scheduler.set_entry_state({"task": "fixture"})
        scheduler.ready()
        valid = scheduler.snapshot()
        corruptions = []
        for mutate in (
            lambda item: item.update(schema_version=99),
            lambda item: item.update(graph_sha256="0" * 64),
            lambda item: item.update(dispatches=0),
            lambda item: item["running"].append("unknown"),
            lambda item: item["available"].pop("start"),
            lambda item: item["completed"].update(start={}),
            lambda item: item["loop_counts"].update(unknown=1),
            lambda item: item["limits"].update(remaining_deadline_seconds=float("nan")),
        ):
            candidate = copy.deepcopy(valid)
            mutate(candidate)
            corruptions.append(candidate)
        corruptions.append([])
        for candidate in corruptions:
            with self.subTest(candidate_type=type(candidate).__name__), self.assertRaises(HarnessError):
                CooperativeScheduler.restore(cooperative_graph(), candidate)


class Phase9ReadAPITests(unittest.TestCase):
    def test_memory_usage_and_prompt_reads_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = isolated_config(Path(temporary))
            with MemoryStore(config) as memory:
                episode = memory.add_episode("decision", "Parser rule", "Keep quoted whitespace")
                memory.record_memory_provenance("episode", episode, "discovered_by", "run-1", "planner", "reasoning", "fixture")
                memory.record_provider_usage({
                    "run_id": "run-1", "node_id": "planner", "role": "planner",
                    "provider_profile_id": "reasoning", "provider": "anthropic", "model": "fixture",
                    "input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 10,
                    "cache_write_input_tokens": 0, "latency_ms": 25, "cost_microusd": 7,
                    "reasoning_tokens": 4, "tool_use_tokens": 2, "billed_output_tokens": 18,
                    "price_status": "configured", "price_snapshot_id": "price-1",
                })
                graph = memory.memory_graph(limit=1)
                usage = memory.usage_records(limit=1)
            self.assertEqual(graph["nodes"][0]["label"], "Parser rule")
            self.assertEqual(graph["links"][0]["node_id"], "planner")
            self.assertEqual(usage["records"][0]["provider_route"], "reasoning")
            self.assertEqual(usage["records"][0]["cost_nanos"], "7000")
            self.assertEqual(usage["records"][0]["reasoning_tokens"], 4)
            self.assertEqual(usage["records"][0]["tool_use_tokens"], 2)
            self.assertEqual(usage["records"][0]["billed_output_tokens"], 18)

    def test_old_usage_database_migrates_all_token_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = isolated_config(Path(temporary))
            path = Path(temporary) / ".harness" / "memory" / "harness.db"
            path.parent.mkdir(parents=True)
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE provider_usage(sequence INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,"
                "node_id TEXT NOT NULL,agent_role TEXT NOT NULL,provider_route TEXT NOT NULL,provider TEXT NOT NULL,"
                "model TEXT NOT NULL,input_tokens INTEGER,output_tokens INTEGER,cached_input_tokens INTEGER,"
                "cache_write_input_tokens INTEGER,latency_ms INTEGER,cost_nanos TEXT,cost_basis TEXT NOT NULL,"
                "rate_id TEXT,created_at_ms INTEGER NOT NULL)"
            )
            connection.commit(); connection.close()
            with MemoryStore(config) as memory:
                columns = {row[1] for row in memory.connection.execute("PRAGMA table_info(provider_usage)")}
            self.assertTrue({"reasoning_tokens", "tool_use_tokens", "billed_output_tokens"}.issubset(columns))

    def test_graph_prompt_versions_dedupe_and_link_to_their_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = isolated_config(Path(temporary))
            node = cooperative_graph()["nodes"][1]
            node["config"].update({"role_name": "Planner A", "system_prompt": "Check contracts first."})
            with MemoryStore(config) as memory:
                first = memory.record_graph_prompt_version(node)
                self.assertEqual(memory.record_graph_prompt_version(node), first)
                node["config"]["system_prompt"] = "Check contracts and tests first."
                second = memory.record_graph_prompt_version(node)
                records = memory.prompt_lineage(name="graph-agent:planner")["records"]
            self.assertNotEqual(first, second)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1]["parent_id"], first)
            self.assertEqual(records[1]["metadata"]["provider_route"], "local-code")

    def test_http_catalog_and_read_apis_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = isolated_config(Path(temporary))
            with MemoryStore(config) as memory:
                memory.add_episode("decision", "Visible record", "redacted local summary")
            server = HarnessHTTPServer(("127.0.0.1", 0), config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host = f"127.0.0.1:{server.server_port}"

            def get(path: str, token: str = "", same_site: bool = False) -> tuple[int, dict]:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                headers = {"Host": host}
                if token:
                    headers["X-Harness-Token"] = token
                if same_site:
                    headers["Sec-Fetch-Site"] = "same-origin"
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()
                return response.status, body

            def post(path: str, body: dict, token: str) -> tuple[int, dict]:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request(
                    "POST", path, body=json.dumps(body),
                    headers={"Host": host, "X-Harness-Token": token, "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                value = json.loads(response.read())
                connection.close()
                return response.status, value

            try:
                status, _ = get("/api/catalog")
                self.assertEqual(status, 400)
                # Only the panel's own page may collect the session key, so
                # this test asks the way a browser does.
                status, _ = get("/api/bootstrap")
                self.assertEqual(status, 400)
                status, bootstrap = get("/api/bootstrap", same_site=True)
                self.assertEqual(status, 200)
                status, catalog = get("/api/catalog", bootstrap["token"])
                self.assertEqual(status, 200)
                self.assertEqual({item["route_id"] for item in catalog["providers"]}, {"local-code", "reasoning"})
                routes = {item["route_id"]: item for item in catalog["providers"]}
                self.assertEqual(routes["reasoning"]["kind"], "anthropic")
                self.assertFalse(routes["reasoning"]["graph_routing_allowed"])
                self.assertTrue(routes["local-code"]["graph_routing_allowed"])
                self.assertEqual(routes["local-code"]["max_data_class"], "project_private")
                self.assertIn("native_tools", routes["local-code"]["capabilities"])
                self.assertTrue(routes["local-code"]["model_catalog"])
                self.assertNotIn("api_key", json.dumps(catalog).lower())
                self.assertEqual({item["id"] for item in catalog["capabilities"]}, {"workspace.read", "workspace.write"})
                self.assertEqual(catalog["agents"], [])
                status, memory = get("/api/memory?limit=1", bootstrap["token"])
                self.assertEqual(status, 200)
                self.assertEqual(memory["nodes"][0]["label"], "Visible record")
                status, validation = post("/api/validate", {"graph": server.template}, bootstrap["token"])
                self.assertEqual(status, 200)
                self.assertTrue(validation["valid"])
                status, rejected = post("/api/run", {"task": "fixture", "graph": {"schema_version": 2}}, bootstrap["token"])
                self.assertEqual(status, 400)
                self.assertIn("not executable", rejected["error"])
                self.assertTrue(server.reserve_run())
                server.release_run()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)


class Phase9StaticUITests(unittest.TestCase):
    def test_phase9_controls_and_accessibility_contract_exist(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "our_harness" / "ui"
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        for token in ("agentDialog", "agentRef", "nodeAgentRef", "nodeProvider", "edgeMode", "memoryGraph", "promptLineage", "usageBody", 'role="alert"'):
            self.assertIn(token, html)
        self.assertIn("const candidate = migrateGraph", script)
        self.assertLess(script.index("const candidate = migrateGraph"), script.index("graph = result.graph || candidate"))
        for token in ("restoreFocus", "prefers-reduced-motion", "setTimeout(pollEvents", "Control+Z", "merge_input", 'setAttribute("aria-invalid"', "lastRunAnnouncementAt", "collectRecordPages", "lastLiveDataRefreshAt", "implementation_plan"):
            self.assertIn(token, script)
        for token in ("startEdgeDrag", "updateEdgeDrag", "finishEdgeDrag", "connectionCompatibility", "edge-preview", "data-input-port", 'addEventListener("pointercancel"', 'addEventListener("click"'):
            self.assertIn(token, script)
        self.assertIn('event.key === "Enter" && connectSource', script)
        self.assertIn('event.key === "Escape" && edgeDrag', script)
        self.assertNotIn("shell.execute", script)


if __name__ == "__main__":
    unittest.main()
