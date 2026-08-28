from __future__ import annotations

import copy
from contextlib import closing
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import our_harness.persistent_memory_index as memory_index_module
import our_harness.persistent_memory_structure as memory_structure
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.context import ContextCompiler
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.persistent_memory import (
    DESKTOP_CLOSEOUT_MIN_TIMEOUT_SECONDS,
    DEPLOYMENT_LOCK_OWNER,
    PersistentMemoryHooks,
    _checkout_deployment_lock,
    _is_owned_build_lock_failure,
    initialize_vault,
)
from our_harness.persistent_memory_index import INDEX_DATABASE, INDEX_FOLDER
from our_harness.persistent_memory_structure import (
    NAVIGATION_START,
    SESSION_METADATA_END,
    SESSION_METADATA_START,
    SESSION_TOPICS_END,
    SESSION_TOPICS_START,
    START_HERE_NOTE,
    TOPICS,
    TOPIC_INDEX_NOTE,
    enrich_session_note,
    infer_session_metadata,
)
from our_harness.workflow import HarnessApplication


class PersistentMemoryHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        parent = Path(self.temporary.name)
        self.project = parent / "project"
        self.vault = parent / "vault"
        self.project.mkdir()
        self.vault.mkdir()
        (self.project / ".harness").mkdir()
        initialize_vault(self.project, self.vault)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["persistent_memory"].update(
            {"enabled": True, "vault_path": str(self.vault), "max_context_chars": 20_000}
        )
        self.config = LoadedConfig(data, self.project.resolve(), [], {})

    def test_langgraph_consults_before_and_writes_after(self) -> None:
        (self.vault / "Architecture.md").write_text(
            "The scheduler must keep mutation serialized.", encoding="utf-8"
        )
        hooks = PersistentMemoryHooks(self.config)
        nodes = set(hooks.graph.get_graph().nodes)
        self.assertIn("consult_vault_before_work", nodes)
        self.assertIn("refresh_private_memory_index_before_work", nodes)
        self.assertIn("rebuild_app_and_installer", nodes)
        self.assertIn("write_vault_after_work", nodes)
        self.assertIn("refresh_private_memory_index_after_work", nodes)

        context, consulted = hooks.before_session("change scheduler mutation")
        self.assertIn("mutation serialized", context)
        self.assertIn("Architecture.md", consulted)

        written = hooks.after_session(
            "change scheduler mutation",
            {
                "run_id": "run-1",
                "state": "complete",
                "changes": [{"path": "secret.py", "content": "SOURCE MUST NOT BE STORED"}],
                "summary": "kept bounded",
            },
        )
        note = (self.vault / written).read_text(encoding="utf-8")
        self.assertIn("kept bounded", note)
        self.assertNotIn("SOURCE MUST NOT BE STORED", note)

    def test_initialization_adds_agent_guidance_without_overwriting_it(self) -> None:
        expected = {
            "01 Project Memory/Start Here.md",
            "01 Project Memory/How To Use This Vault.md",
            "01 Project Memory/Codex Working Memory.md",
            "01 Project Memory/Current State.md",
            "01 Project Memory/AI Engineering Guide.md",
        }
        actual = {
            path.relative_to(self.vault).as_posix()
            for path in (self.vault / "01 Project Memory").glob("*.md")
        }
        self.assertEqual(actual, expected)
        working = self.vault / "01 Project Memory" / "Codex Working Memory.md"
        working.write_text("# User-maintained durable truth\n", encoding="utf-8")
        initialize_vault(self.project, self.vault)
        preserved = working.read_text(encoding="utf-8")
        self.assertTrue(preserved.startswith("# User-maintained durable truth\n"))
        self.assertIn("<!-- nexus-managed-navigation:start -->", preserved)

    def test_initialization_creates_navigation_topics_graph_and_workspace_defaults(self) -> None:
        start_here = (self.vault / START_HERE_NOTE).read_text(encoding="utf-8")
        self.assertIn("# Start Here", start_here)
        self.assertIn("[[02 Topics/Topic Index|Open the complete Topic Index]]", start_here)
        self.assertTrue((self.vault / TOPIC_INDEX_NOTE).is_file())
        for topic in TOPICS:
            note = (self.vault / topic.path).read_text(encoding="utf-8")
            self.assertIn("kind: topic-map", note)
            self.assertIn("## Session evidence", note)
            self.assertIn('path:"Sessions"', note)

        graph = json.loads((self.vault / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
        self.assertIn('-path:"Sessions"', graph["search"])
        self.assertFalse(graph["showOrphans"])
        self.assertEqual(
            {group["query"] for group in graph["colorGroups"]},
            {
                'path:"01 Project Memory" OR file:"Project Memory"',
                'path:"02 Topics"',
                'path:"Sessions"',
            },
        )

        workspace = json.loads(
            (self.vault / ".obsidian" / "workspace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(workspace["active"], "nexus-start-here")
        self.assertEqual(
            workspace["main"]["children"][0]["children"][0]["state"]["state"]["file"],
            START_HERE_NOTE.as_posix(),
        )
        file_explorer = workspace["left"]["children"][0]["children"][0]["state"]
        self.assertTrue(file_explorer["state"]["autoReveal"])

    def test_existing_obsidian_graph_and_workspace_bytes_are_never_overwritten(self) -> None:
        graph_path = self.vault / ".obsidian" / "graph.json"
        workspace_path = self.vault / ".obsidian" / "workspace.json"
        graph_bytes = (
            b'{"search":"user query","showTags":true,"colorGroups":[{"user":1}],'
            b'"custom":{"nested":true}}\n'
        )
        workspace_bytes = b'{ "active": "user-tab", "customLayout": [3, 2, 1] }\n'
        graph_path.write_bytes(graph_bytes)
        workspace_path.write_bytes(workspace_bytes)
        initialize_vault(self.project, self.vault)
        self.assertEqual(graph_path.read_bytes(), graph_bytes)
        self.assertEqual(workspace_path.read_bytes(), workspace_bytes)

    def test_structure_markers_are_scoped_and_new_untrusted_markers_are_encoded(self) -> None:
        fake_metadata = (
            f"{SESSION_METADATA_START}\narea: fake\n{SESSION_METADATA_END}"
        )
        fake_topics = (
            f"{SESSION_TOPICS_START}\nnot a managed heading\n{SESSION_TOPICS_END}"
        )
        historical_body = (
            "---\nkind: harness-session\nstate: \"complete\"\n---\n\n"
            "# Session\n\n## Task\n\n"
            + fake_metadata
            + "\n\n"
            + fake_topics
            + "\n\nHistorical tail bytes.\n"
        )
        enriched = enrich_session_note(
            historical_body,
            "desktop packaging",
            {"state": "complete"},
        )
        historical_frontmatter_end = historical_body.find("\n---\n", 4) + len("\n---\n")
        enriched_frontmatter_end = enriched.find("\n---\n", 4) + len("\n---\n")
        self.assertTrue(
            enriched[enriched_frontmatter_end:].startswith(
                historical_body[historical_frontmatter_end:]
            )
        )
        frontmatter = enriched[: enriched.find("\n---\n", 4)]
        self.assertEqual(frontmatter.count(SESSION_METADATA_START), 1)
        self.assertEqual(frontmatter.count(SESSION_METADATA_END), 1)
        self.assertTrue(enriched.rstrip().endswith(SESSION_TOPICS_END))
        self.assertEqual(enriched.count(SESSION_TOPICS_START), 2)

        all_markers = " ".join(
            (
                SESSION_METADATA_START,
                SESSION_METADATA_END,
                SESSION_TOPICS_START,
                SESSION_TOPICS_END,
                memory_structure.NAVIGATION_START,
                memory_structure.NAVIGATION_END,
            )
        )
        hooks = PersistentMemoryHooks(self.config)
        written = hooks.after_session(
            f"new task {all_markers}",
            {
                "run_id": "structure-marker-injection",
                "state": "complete",
                "summary": all_markers,
            },
        )
        note = (self.vault / written).read_text(encoding="utf-8")
        self.assertEqual(note.count(SESSION_METADATA_START), 1)
        self.assertEqual(note.count(SESSION_METADATA_END), 1)
        self.assertEqual(note.count(SESSION_TOPICS_START), 1)
        self.assertEqual(note.count(SESSION_TOPICS_END), 1)
        self.assertNotIn(memory_structure.NAVIGATION_START, note)
        self.assertNotIn(memory_structure.NAVIGATION_END, note)
        self.assertIn("nexus managed marker removed", note)

    def test_structure_transaction_rolls_back_all_bytes_and_listing_on_second_write_failure(self) -> None:
        navigation = (
            self.vault / "Project Memory.md",
            self.vault / "01 Project Memory" / "How To Use This Vault.md",
        )
        for path in navigation:
            content = path.read_text(encoding="utf-8")
            path.write_text(content.split(NAVIGATION_START, 1)[0].rstrip() + "\n", encoding="utf-8")

        def snapshot() -> dict[str, bytes | None]:
            return {
                path.relative_to(self.vault).as_posix(): (
                    path.read_bytes() if path.is_file() else None
                )
                for path in self.vault.rglob("*")
            }

        before = snapshot()
        real_put = memory_structure.put_this_file_in_place
        calls = 0

        def fail_second(path: Path, content: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second structure write failure")
            real_put(path, content)

        with mock.patch.object(
            memory_structure, "put_this_file_in_place", side_effect=fail_second
        ):
            with self.assertRaisesRegex(OSError, "injected second structure write failure"):
                initialize_vault(self.project, self.vault)
        self.assertEqual(snapshot(), before)

    def test_structure_preflight_rejects_link_like_destination_before_any_canonical_write(self) -> None:
        pinned = self.vault / "Project Memory.md"
        pinned.write_text(
            pinned.read_text(encoding="utf-8").split(NAVIGATION_START, 1)[0].rstrip() + "\n",
            encoding="utf-8",
        )
        before = {path: path.read_bytes() for path in self.vault.rglob("*") if path.is_file()}
        real_is_link = memory_structure._is_link_or_junction

        def injected_link(path: Path) -> bool:
            if path.name == "How To Use This Vault.md":
                return True
            return real_is_link(path)

        with mock.patch.object(
            memory_structure, "_is_link_or_junction", side_effect=injected_link
        ):
            with self.assertRaisesRegex(HarnessError, "link/reparse point"):
                initialize_vault(self.project, self.vault)
        after = {path: path.read_bytes() for path in self.vault.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_python311_windows_reparse_attribute_is_treated_as_a_junction(self) -> None:
        class Python311Path:
            def is_symlink(self) -> bool:
                return False

        with mock.patch.object(memory_index_module.os, "name", "nt"), mock.patch.object(
            memory_index_module.os,
            "lstat",
            return_value=SimpleNamespace(st_file_attributes=0x400),
        ):
            self.assertTrue(memory_index_module._is_link_or_junction(Python311Path()))

    def test_historical_session_bytes_stay_unchanged_and_new_sessions_are_structured(self) -> None:
        legacy = self.vault / "Sessions" / "2026-01-01T00-00-00Z-legacy.md"
        legacy.parent.mkdir()
        legacy_content = (
            "---\nkind: harness-session\nstate: \"complete\"\n---\n\n"
            "# Session\n\n## Task\n\nRepair the Electron installer shortcut.\n\n"
            "## Bounded outcome\n\n```json\n{}\n```\n"
        )
        legacy.write_text(legacy_content, encoding="utf-8")
        legacy_before = legacy.read_bytes()
        initialize_vault(self.project, self.vault)
        self.assertEqual(legacy.read_bytes(), legacy_before)

        hooks = PersistentMemoryHooks(self.config)
        written = hooks.after_session(
            "Improve Obsidian vault memory safety and topic links",
            {"run_id": "structured", "state": "complete", "summary": "Linked the vault."},
        )
        note = (self.vault / written).read_text(encoding="utf-8")
        self.assertRegex(note, r'(?m)^area: "Persistent Memory and Safety"$')
        self.assertRegex(note, r'(?m)^status: "complete"$')
        self.assertIn('components: ["persistent-memory"]', note)
        self.assertIn('related_topics: ["[[02 Topics/Persistent Memory and Safety]]"]', note)
        self.assertIn('tags: ["nexus/session", "nexus/area/persistent-memory-and-safety"]', note)
        self.assertIn("[[02 Topics/Persistent Memory and Safety|Persistent Memory and Safety]]", note)

        status = hooks.status()["organization"]
        self.assertTrue(status["start_here"])
        self.assertEqual(status["topic_notes"], len(TOPICS))
        self.assertEqual(status["structured_sessions"], 1)
        self.assertEqual(status["topic_linked_sessions"], 1)
        self.assertTrue(status["workspace_auto_reveal"])

    def test_session_classifier_ignores_mandatory_desktop_closeout_bookkeeping(self) -> None:
        metadata = infer_session_metadata(
            "Repair Obsidian vault retrieval and memory safety",
            {
                "state": "complete",
                "summary": "The persistent memory index is healthy.",
                "closeout_deployment": {
                    "application": "desktop/build-output/win-unpacked/Nexus Harness.exe",
                    "installer": "desktop/build-output/Nexus-Harness-Setup.exe",
                    "desktop_shortcut": "Desktop/Nexus Harness.lnk",
                },
            },
        )
        self.assertEqual(metadata["area"], "Persistent Memory and Safety")
        self.assertNotIn("desktop", metadata["components"])

    def test_private_fts_and_kv_index_drive_task_retrieval(self) -> None:
        (self.vault / "Decisions.md").write_text(
            "# Quartz scheduler\n\nThe quartz mutation queue must stay serialized.",
            encoding="utf-8",
        )
        hooks = PersistentMemoryHooks(self.config)
        context, consulted = hooks.before_session("repair quartz scheduler mutation queue")
        self.assertLessEqual(len(context), self.config.data["persistent_memory"]["max_context_chars"])
        self.assertIn("quartz mutation queue", context.casefold())
        self.assertIn("Decisions.md", consulted)
        database_path = self.vault / INDEX_FOLDER / INDEX_DATABASE
        self.assertTrue(database_path.is_file())
        with closing(sqlite3.connect(database_path)) as database:
            keys = {row[0] for row in database.execute("SELECT key FROM kv")}
            indexed = database.execute(
                "SELECT COUNT(*) FROM files WHERE path = 'Decisions.md'"
            ).fetchone()[0]
        self.assertIn("hook.last_pre", keys)
        self.assertIn("index.status", keys)
        self.assertEqual(indexed, 1)
        hits = hooks.search("quartz serialized")
        self.assertEqual(hits[0]["path"], "Decisions.md")

    def test_pre_hook_fails_when_a_required_note_cannot_enter_the_index(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        (self.vault / "01 Project Memory" / "Current State.md").unlink()
        with self.assertRaisesRegex(HarnessError, "required notes missing from the index"):
            hooks.before_session("verify memory integrity")

    def test_post_hook_refreshes_managed_current_state_and_index_health(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        written = hooks.after_session(
            "repair durable queue",
            {
                "run_id": "current-state",
                "state": "complete",
                "summary": "Queue durability was verified.",
                "verification": {"focused": "passed"},
                "blockers": [],
                "next_step": "Run the broader persistence suite.",
            },
        )
        current = (self.vault / "01 Project Memory" / "Current State.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Queue durability was verified.", current)
        self.assertIn("Run the broader persistence suite.", current)
        self.assertIn(f"[[{written}]]", current)
        status = hooks.status()
        self.assertTrue(status["binding_valid"])
        self.assertEqual(status["index"]["kv"]["hook.last_post"]["written"], written)

    def test_repeated_posts_cannot_inject_or_duplicate_managed_state_markers(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        marker_payload = (
            "untrusted <!-- nexus-managed-current:end --> payload "
            "<!-- nexus-managed-current:start -->"
        )
        for number in (1, 2):
            hooks.after_session(
                f"post {number}: {marker_payload}",
                {
                    "run_id": f"sentinel-{number}-{marker_payload}",
                    "state": marker_payload,
                    "summary": marker_payload,
                    "verification": {"detail": marker_payload},
                    "blockers": [marker_payload],
                    "next_step": marker_payload,
                },
            )
            current = (self.vault / "01 Project Memory" / "Current State.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(current.count("<!-- nexus-managed-current:start -->"), 1)
            self.assertEqual(current.count("<!-- nexus-managed-current:end -->"), 1)
            self.assertIn("[nexus managed marker removed", current)
        notes = list((self.vault / "Sessions").glob("*.md"))
        self.assertEqual(len(notes), 2)
        for note in notes:
            content = note.read_text(encoding="utf-8")
            self.assertNotIn("<!-- nexus-managed-current:start -->", content)
            self.assertNotIn("<!-- nexus-managed-current:end -->", content)

    def test_bounded_context_reserves_all_mandatory_notes_and_deep_unicode_fts_evidence(self) -> None:
        deep = self.vault / "Deep decision.md"
        deep.write_text(
            "# Historic material\n\n" + ("ordinary history\n" * 1_600)
            + "Åtgärd för kö måste förbli serialiserad.\n",
            encoding="utf-8",
        )
        hooks = PersistentMemoryHooks(self.config)
        context, consulted = hooks.before_session("Åtgärd serialiserad")
        self.assertLessEqual(len(context), self.config.data["persistent_memory"]["max_context_chars"])
        self.assertIn("Åtgärd för kö måste förbli serialiserad", context)
        for relative in (
            "Project Memory.md",
            "01 Project Memory/How To Use This Vault.md",
            "01 Project Memory/Codex Working Memory.md",
            "01 Project Memory/Current State.md",
            "01 Project Memory/AI Engineering Guide.md",
        ):
            self.assertIn(f"[mandatory-obsidian:{relative}]", context)
            self.assertIn(relative, consulted)
        self.assertIn("[retrieval-metadata]", context)
        retrieval = hooks.status()["index"]["kv"]["hook.last_pre"]["retrieval"]
        self.assertTrue(retrieval["mandatory"]["all_represented"])
        self.assertGreaterEqual(retrieval["fts"]["selected_evidence"], 1)
        self.assertIn("Deep decision.md", consulted)
        self.assertEqual(hooks.search("ÅTGÄRD")[0]["path"], "Deep decision.md")

        self.config.data["persistent_memory"]["max_context_chars"] = 1_000
        tiny_hooks = PersistentMemoryHooks(self.config)
        tiny_context, tiny_consulted = tiny_hooks.before_session("Åtgärd serialiserad")
        self.assertLessEqual(len(tiny_context), 1_000)
        self.assertIn("Åtgärd", tiny_context)
        self.assertIn('"paths_available_in_hook_metadata": true', tiny_context)
        for relative in (
            "Project Memory.md",
            "01 Project Memory/How To Use This Vault.md",
            "01 Project Memory/Codex Working Memory.md",
            "01 Project Memory/Current State.md",
            "01 Project Memory/AI Engineering Guide.md",
        ):
            self.assertIn(f"[mandatory-obsidian:{relative}]", tiny_context)
            self.assertIn(relative, tiny_consulted)
        tiny_retrieval = tiny_hooks.status()["index"]["kv"]["hook.last_pre"]["retrieval"]
        self.assertTrue(tiny_retrieval["mandatory"]["all_represented"])
        self.assertTrue(
            all(
                chars > 0
                for chars in tiny_retrieval["mandatory"]["content_chars"].values()
            )
        )
        self.assertIn('"mandatory_content_min_chars": 8', tiny_context)

    def test_failed_post_index_refresh_rolls_back(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        assert hooks.memory_index is not None
        healthy = hooks.memory_index.refresh()
        current_path = self.vault / "01 Project Memory" / "Current State.md"
        current_before = current_path.read_text(encoding="utf-8")
        with mock.patch.object(
            hooks.memory_index,
            "refresh",
            side_effect=[HarnessError("injected refresh failure"), healthy],
        ):
            with self.assertRaisesRegex(HarnessError, "injected refresh failure"):
                hooks.after_session(
                    "atomic post", {"run_id": "atomic-run", "state": "complete"}
                )
        self.assertEqual(current_path.read_text(encoding="utf-8"), current_before)
        self.assertEqual(list((self.vault / "Sessions").glob("*-atomic-run*.md")), [])

        hooks.after_session(
            "atomic post", {"run_id": "atomic-run", "state": "complete"}
        )
        self.assertEqual(len(list((self.vault / "Sessions").glob("*-atomic-run*.md"))), 1)

    def test_exact_duplicate_payload_is_idempotent(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        result = {
            "run_id": "exact-retry",
            "state": "complete",
            "summary": "The exact same bounded outcome.",
        }
        first = hooks.after_session("same task", result)
        current_after_first = (
            self.vault / "01 Project Memory" / "Current State.md"
        ).read_text(encoding="utf-8")
        second = hooks.after_session("same task", copy.deepcopy(result))
        current_after_second = (
            self.vault / "01 Project Memory" / "Current State.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(second, first)
        self.assertEqual(current_after_second, current_after_first)
        self.assertEqual(len(list((self.vault / "Sessions").glob("*-exact-retry*.md"))), 1)
        note = (self.vault / first).read_text(encoding="utf-8")
        self.assertRegex(note, r"(?m)^payload_sha256: \"[0-9a-f]{64}\"$")
        self.assertIn("revision: 1", note)
        self.assertIn("supersedes: null", note)
        self.assertIn("The exact same bounded outcome.", current_after_first)

    def test_paused_to_complete_run_appends_a_linked_revision_and_dedupes_retry(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        paused = {
            "run_id": "resumed-run",
            "state": "paused",
            "summary": "Waiting for approval.",
        }
        complete = {
            "run_id": "resumed-run",
            "state": "complete",
            "summary": "Approval received and work verified.",
        }
        first = hooks.after_session("resumable task", paused)
        second = hooks.after_session("resumable task", complete)
        third = hooks.after_session("resumable task", copy.deepcopy(complete))
        fourth = hooks.after_session("resumable task", copy.deepcopy(paused))
        self.assertNotEqual(first, second)
        self.assertEqual(third, second)
        self.assertEqual(fourth, first)
        notes = list((self.vault / "Sessions").glob("*-resumed-run*.md"))
        self.assertEqual(len(notes), 2)

        first_note = (self.vault / first).read_text(encoding="utf-8")
        second_note = (self.vault / second).read_text(encoding="utf-8")
        first_hash = re.search(
            r'(?m)^payload_sha256: "([0-9a-f]{64})"$', first_note
        )
        self.assertIsNotNone(first_hash)
        self.assertIn("revision: 1", first_note)
        self.assertIn("supersedes: null", first_note)
        self.assertIn('state: "paused"', first_note)
        self.assertIn("revision: 2", second_note)
        self.assertIn(f"supersedes: {json.dumps(first)}", second_note)
        self.assertIn(
            f'prior_payload_sha256: "{first_hash.group(1)}"', second_note
        )
        self.assertIn('state: "complete"', second_note)

        current_path = self.vault / "01 Project Memory" / "Current State.md"
        current = current_path.read_text(encoding="utf-8")
        self.assertIn(f"[[{second}]]", current)
        self.assertNotIn(f"[[{first}]]", current)
        self.assertIn("Approval received and work verified.", current)
        self.assertNotIn("Waiting for approval.", current)

    def test_ambiguous_managed_state_fails_closed_without_a_session(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        current_path = self.vault / "01 Project Memory" / "Current State.md"
        current_path.write_text(
            current_path.read_text(encoding="utf-8")
            + "\n<!-- nexus-managed-current:start -->duplicate"
            + "<!-- nexus-managed-current:end -->\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(HarnessError, "exactly one well-ordered managed block"):
            hooks.after_session("reject ambiguity", {"run_id": "ambiguous", "state": "failed"})
        self.assertEqual(list((self.vault / "Sessions").glob("*-ambiguous*.md")), [])

    def test_blank_lifecycle_tasks_fail_closed(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        with self.assertRaisesRegex(HarnessError, "pre-work task must not be empty"):
            hooks.before_session("  ")
        with self.assertRaisesRegex(HarnessError, "post-work task must not be empty"):
            hooks.after_session("", {"state": "failed"})

    def test_langgraph_refuses_closeout_until_desktop_deployment_succeeds(self) -> None:
        self.config.data["persistent_memory"]["enforce_desktop_deployment"] = True
        deployed: list[Path] = []

        def deploy(project: Path) -> dict[str, str]:
            deployed.append(project)
            return {
                "state": "deployed",
                "application": "desktop/build-output/win-unpacked/Nexus Harness.exe",
                "installer": "desktop/build-output/Nexus Harness Setup 0.1.0.exe",
                "desktop_shortcut": "Desktop/Nexus Harness.lnk",
                "icon_source": "Nexus Harness.exe",
            }

        hooks = PersistentMemoryHooks(self.config, deploy_desktop=deploy)
        written = hooks.after_session("ship it", {"run_id": "deploy", "state": "complete"})
        self.assertEqual(deployed, [self.project.resolve()])
        note = (self.vault / written).read_text(encoding="utf-8")
        self.assertIn('"closeout_deployment"', note)
        self.assertIn('"desktop_shortcut": "Desktop/Nexus Harness.lnk"', note)

        def fail(_project: Path) -> dict[str, str]:
            raise HarnessError("build failed")

        failing = PersistentMemoryHooks(self.config, deploy_desktop=fail)
        with self.assertRaisesRegex(HarnessError, "build failed"):
            failing.after_session("do not close", {"run_id": "blocked", "state": "complete"})
        self.assertEqual(list((self.vault / "Sessions").glob("*-blocked.md")), [])

    def test_desktop_closeout_has_budget_for_consumer_drain_and_packaging(self) -> None:
        hooks = PersistentMemoryHooks(self.config)
        observed: list[float] = []
        hooks._deploy_desktop_while_locked = lambda _project, timeout: observed.append(timeout) or {}
        with mock.patch("our_harness.persistent_memory._checkout_deployment_lock"):
            hooks._deploy_desktop(self.project)
        self.assertEqual(observed, [DESKTOP_CLOSEOUT_MIN_TIMEOUT_SECONDS])

    def test_closeout_retries_all_windows_owned_artifact_lock_wordings(self) -> None:
        owned = r"C:\project\desktop\build-output\win-unpacked\resources\runtime\locked.pyc"
        self.assertTrue(
            _is_owned_build_lock_failure(
                f"remove {owned}: The process cannot access the file because it is being used by another process."
            )
        )
        self.assertTrue(_is_owned_build_lock_failure(f"remove {owned}: Access is denied."))
        self.assertFalse(
            _is_owned_build_lock_failure(
                r"remove C:\some-other-app\locked.pyc: being used by another process"
            )
        )

    def test_checkout_deployment_lock_serializes_processes_and_recovers_dead_owner(self) -> None:
        marker = self.project / "deployment-order.txt"
        source = Path(__file__).resolve().parents[1] / "src"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source) + os.pathsep + environment.get("PYTHONPATH", "")
        worker = (
            "import os,sys,time; from pathlib import Path; "
            "from our_harness.persistent_memory import _checkout_deployment_lock; "
            "root=Path(sys.argv[1]); marker=Path(sys.argv[2]); label=sys.argv[3]; delay=float(sys.argv[4]); "
            "lock=_checkout_deployment_lock(root,5,purpose=label); lock.__enter__(); "
            "marker.open('a',encoding='utf-8').write(label+':start\\n'); "
            "time.sleep(delay); marker.open('a',encoding='utf-8').write(label+':end\\n'); lock.__exit__(None,None,None)"
        )
        first = subprocess.Popen(
            [sys.executable, "-c", worker, str(self.project), str(marker), "one", "0.45"],
            env=environment,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.is_file() and "one:start" in marker.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            first.kill()
            self.fail("first deployment-lock process did not acquire the lock")
        owner = json.loads((self.project / DEPLOYMENT_LOCK_OWNER).read_text(encoding="utf-8"))
        self.assertEqual(owner["state"], "owning")
        self.assertEqual(owner["pid"], first.pid)
        self.assertEqual(owner["project_root"], str(self.project.resolve()))

        second = subprocess.Popen(
            [sys.executable, "-c", worker, str(self.project), str(marker), "two", "0.02"],
            env=environment,
        )
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertEqual(
            marker.read_text(encoding="utf-8").splitlines(),
            ["one:start", "one:end", "two:start", "two:end"],
        )

        crashed = (
            "import os,sys; from pathlib import Path; "
            "from our_harness.persistent_memory import _checkout_deployment_lock; "
            "lock=_checkout_deployment_lock(Path(sys.argv[1]),5,purpose='crashed'); lock.__enter__(); "
            "Path(sys.argv[2]).write_text('owned',encoding='utf-8'); os._exit(0)"
        )
        crash_marker = self.project / "crashed-owner.txt"
        dead = subprocess.Popen(
            [sys.executable, "-c", crashed, str(self.project), str(crash_marker)],
            env=environment,
        )
        self.assertEqual(dead.wait(timeout=10), 0)
        self.assertTrue(crash_marker.is_file())
        started = time.monotonic()
        with _checkout_deployment_lock(self.project, 2, purpose="recovered") as recovered:
            self.assertEqual(recovered["pid"], os.getpid())
        self.assertLess(time.monotonic() - started, 1)

    def test_binding_rejects_every_other_project(self) -> None:
        other = self.project.parent / "other"
        other.mkdir()
        data = copy.deepcopy(self.config.data)
        config = LoadedConfig(data, other.resolve(), [], {})
        with self.assertRaisesRegex(HarnessError, "does not match this project"):
            PersistentMemoryHooks(config)

    def test_vault_inside_git_tree_is_rejected(self) -> None:
        (self.vault / ".git").mkdir()
        with self.assertRaisesRegex(HarnessError, "Git worktree"):
            PersistentMemoryHooks(self.config)

    def test_consulted_vault_is_in_compiled_agent_context(self) -> None:
        (self.vault / "Decision.md").write_text("Use a bounded queue.", encoding="utf-8")
        hooks = PersistentMemoryHooks(self.config)
        context, consulted = hooks.before_session("queue")
        with MemoryStore(self.config) as memory:
            compiled = ContextCompiler(
                self.config,
                memory,
                persistent_memory_context=context,
                persistent_memory_consulted=consulted,
            ).compile("queue", [])
        self.assertIn("Use a bounded queue", compiled.dynamic)
        self.assertIn("Decision.md", compiled.manifest["persistent_memory"]["consulted"])

    def test_harness_application_enforces_hooks_around_a_run(self) -> None:
        with HarnessApplication(self.config) as app:
            app._run_task_locked = lambda *_args: {"run_id": "integration", "state": "complete"}
            result = app.run_task("integration boundary")
        self.assertEqual(result["state"], "complete")
        notes = list((self.vault / "Sessions").glob("*-integration.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("integration boundary", notes[0].read_text(encoding="utf-8"))

    def test_workflow_run_then_resume_records_linked_revisions_without_duplicate_retry(self) -> None:
        task = "workflow resume memory boundary"
        with HarnessApplication(self.config) as app:
            def pause_run(*_args):
                run_id = app.memory.start_run(task)
                app._active_run_id = run_id
                return {
                    "run_id": run_id,
                    "state": "paused",
                    "summary": "Workflow is waiting for approval.",
                }

            app._run_task_locked = pause_run
            paused = app.run_task(task)
            run_id = paused["run_id"]
            complete = {
                "run_id": run_id,
                "state": "complete",
                "summary": "Workflow resumed and completed.",
            }
            app._terminal_run_result = lambda resumed_id: (
                copy.deepcopy(complete) if resumed_id == run_id else None
            )
            self.assertEqual(app.resume_task(run_id), complete)
            self.assertEqual(app.resume_task(run_id), complete)

        notes = list((self.vault / "Sessions").glob(f"*-{run_id}*.md"))
        self.assertEqual(len(notes), 2)
        revisions = "\n".join(note.read_text(encoding="utf-8") for note in notes)
        self.assertIn("revision: 1", revisions)
        self.assertIn("revision: 2", revisions)
        current = (self.vault / "01 Project Memory" / "Current State.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Workflow resumed and completed.", current)
        self.assertNotIn("Workflow is waiting for approval.", current)


if __name__ == "__main__":
    unittest.main()
