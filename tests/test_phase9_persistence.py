from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.config import load_isolated_config
from our_harness.context import ContextCompiler
from our_harness.memory import MemoryStore, SCHEMA_VERSION
from our_harness.models import HarnessError


class Phase9PersistenceTests(unittest.TestCase):
    def test_usage_retains_provider_token_and_price_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_isolated_config(Path(temporary))
            with MemoryStore(config) as memory:
                sequence = memory.record_provider_usage(
                    {
                        "run_id": "run-1",
                        "node_id": "reviewer",
                        "agent_role": "reviewer",
                        "provider_route": "reasoning",
                        "provider": "openai",
                        "model": "fixture",
                        "input_tokens": 100,
                        "output_tokens": 30,
                        "cached_input_tokens": 20,
                        "cache_write_input_tokens": 3,
                        "reasoning_tokens": 11,
                        "tool_use_tokens": 7,
                        "billed_output_tokens": 41,
                        "latency_ms": 25,
                        "cost_microusd": 19,
                        "price_status": "configured",
                        "price_snapshot_id": "price-2026-08",
                    }
                )
                record = memory.usage_records()["records"][0]
            self.assertEqual(sequence, record["sequence"])
            self.assertEqual(record["reasoning_tokens"], 11)
            self.assertEqual(record["tool_use_tokens"], 7)
            self.assertEqual(record["billed_output_tokens"], 41)
            self.assertEqual(record["cost_microusd"], 19)
            self.assertEqual(record["price_status"], "configured")
            self.assertEqual(record["price_snapshot_id"], "price-2026-08")
            self.assertEqual(record["cost_nanos"], "19000")

    def test_usage_rejects_invalid_new_token_and_cost_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with MemoryStore(load_isolated_config(Path(temporary))) as memory:
                for field, value in (("reasoning_tokens", -1), ("tool_use_tokens", True), ("billed_output_tokens", 1.5), ("cost_microusd", -2)):
                    with self.subTest(field=field), self.assertRaises(HarnessError):
                        memory.record_provider_usage({field: value})
                with self.assertRaisesRegex(HarnessError, "disagree"):
                    memory.record_provider_usage({"cost_microusd": 2, "cost_nanos": "3000"})

    def test_v6_usage_and_duplicate_prompt_rows_migrate_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / ".harness" / "memory" / "harness.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE provider_usage("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,node_id TEXT NOT NULL,"
                "agent_role TEXT NOT NULL,provider_route TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,"
                "input_tokens INTEGER,output_tokens INTEGER,cached_input_tokens INTEGER,"
                "cache_write_input_tokens INTEGER,latency_ms INTEGER,cost_nanos TEXT,cost_basis TEXT NOT NULL,"
                "rate_id TEXT,created_at_ms INTEGER NOT NULL);"
                "INSERT INTO provider_usage(run_id,node_id,agent_role,provider_route,provider,model,input_tokens,"
                "output_tokens,cached_input_tokens,cache_write_input_tokens,latency_ms,cost_nanos,cost_basis,rate_id,created_at_ms) "
                "VALUES('old-run','planner','planner','reasoning','openai','old-model',10,2,1,0,5,'7000','configured','old-rate',1);"
                "CREATE TABLE prompt_versions(id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,body TEXT NOT NULL,"
                "parent_id TEXT,active INTEGER NOT NULL,created_at INTEGER NOT NULL,metadata_json TEXT NOT NULL);"
                "INSERT INTO prompt_versions VALUES('old-a','prompt','graph-agent:planner','same',NULL,1,1,'{}');"
                "INSERT INTO prompt_versions VALUES('old-b','prompt','graph-agent:planner','same','old-a',1,2,'{}');"
            )
            connection.commit()
            connection.close()

            with MemoryStore(load_isolated_config(root)) as memory:
                usage = memory.usage_records()["records"][0]
                prompt_rows = memory.connection.execute(
                    "SELECT id,active,content_sha256 FROM prompt_versions ORDER BY rowid"
                ).fetchall()
                schema = memory.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(int(schema), SCHEMA_VERSION)
            self.assertEqual(usage["cost_microusd"], 7)
            self.assertEqual(usage["price_status"], "configured")
            self.assertEqual(usage["price_snapshot_id"], "old-rate")
            self.assertIsNone(usage["reasoning_tokens"])
            self.assertEqual([row["active"] for row in prompt_rows], [0, 0])
            self.assertIsNotNone(prompt_rows[0]["content_sha256"])
            self.assertIsNone(prompt_rows[1]["content_sha256"])

    def test_agent_prompt_versions_dedupe_link_redact_and_stay_inactive(self) -> None:
        secret = "sk-phase9-prompt-secret-123456789"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"HARNESS_API_KEY": secret}, clear=False
        ):
            root = Path(temporary)
            config = load_isolated_config(root)
            with MemoryStore(config) as memory:
                first = memory.record_agent_prompt_version(
                    "planner",
                    f"Plan carefully. token={secret}",
                    provider="openai",
                    model="fixture",
                    run_id="run-1",
                    provider_route="reasoning",
                    metadata={"note": secret},
                )
                duplicate = memory.record_agent_prompt_version(
                    "planner",
                    f"Plan carefully. token={secret}",
                    provider="anthropic",
                    model="fixture-2",
                    run_id="run-2",
                    provider_route="review",
                )
                second = memory.record_agent_prompt_version(
                    "planner",
                    "Plan carefully and verify tests.",
                    provider="openai",
                    model="fixture",
                    run_id="run-3",
                    provider_route="reasoning",
                )
                lineage = memory.prompt_lineage(name="planner")["records"]
                compiled = ContextCompiler(config, memory).compile("unrelated task", [])
                persisted = "\n".join(
                    str(row[0])
                    for table, column in (
                        ("prompt_versions", "body"),
                        ("prompt_versions", "metadata_json"),
                        ("prompt_version_observations", "metadata_json"),
                    )
                    for row in memory.connection.execute(f"SELECT {column} FROM {table}")
                )
            self.assertEqual(first, duplicate)
            self.assertNotEqual(first, second)
            self.assertEqual(len(lineage), 2)
            self.assertEqual(lineage[1]["parent_id"], first)
            self.assertTrue(all(record["active"] == 0 for record in lineage))
            self.assertEqual(len(lineage[0]["observations"]), 2)
            self.assertNotIn("Plan carefully", compiled.dynamic)
            self.assertNotIn(secret, persisted)
            self.assertIn("[REDACTED]", persisted)

    def test_graph_prompt_adapter_is_inactive(self) -> None:
        node = {
            "id": "coder",
            "type": "coder",
            "config": {
                "role_name": "Coder",
                "system_prompt": "Write the patch.",
                "provider_route": "local-code",
                "provider": "ollama",
                "model": "fixture",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            with MemoryStore(load_isolated_config(Path(temporary))) as memory:
                prompt_id = memory.record_graph_prompt_version(node, run_id="run-graph")
                record = memory.prompt_lineage(name="graph-agent:coder")["records"][0]
            self.assertEqual(record["id"], prompt_id)
            self.assertEqual(record["active"], 0)
            self.assertEqual(record["observations"][0]["run_id"], "run-graph")


if __name__ == "__main__":
    unittest.main()
