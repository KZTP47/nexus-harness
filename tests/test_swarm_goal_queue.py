from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sqlite3
from contextlib import closing

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.runtime_integrity import mac
from our_harness.swarm_goal_queue import SwarmGoalQueueStore


class DurableBoardGoalQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "project").mkdir()
        patched = mock.patch.dict(os.environ, {
            "OUR_HARNESS_SWARM_RUN_DIR": str(self.root / "runtime"),
        })
        patched.start()
        self.addCleanup(patched.stop)
        self.config = LoadedConfig(
            copy.deepcopy(DEFAULT_CONFIG), self.root / "project", [], {}
        )
        self.store = SwarmGoalQueueStore(self.config)

    def board(self) -> dict:
        return {
            "version": 7,
            "agents": [
                {"id": "agent-1", "name": "Lead", "who": "claude", "ready": True},
                {"id": "agent-2", "name": "Peer", "who": "codex", "ready": True},
            ],
            "projects": [{
                "id": "project-1", "name": "Project", "path": str(self.root / "project"),
                "is_there": True, "tasks": ["First exact goal\nwith formatting", "Second goal"],
            }],
            "works_on": [
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
            "talks_to": [{"one": "agent-1", "other": "agent-2"}],
        }

    def claim(self, queue: dict, request_id: str = "work-1") -> dict:
        current = queue["current"]
        return self.store.claim(
            queue["queue_id"], current["id"], objective=current["objective"],
            agent_id=current["lead_id"], peer_id=current["peer_id"],
            project_id=current["project_id"], conversation_id="pair-chat-1",
            request_id=request_id,
        )

    def install_prior_v2_integrity(self, *, with_public: bool) -> dict:
        """Replace only integrity metadata/anchor with an authentic prior v2."""

        with closing(sqlite3.connect(self.store.database)) as database:
            database.row_factory = sqlite3.Row
            held = database.execute(
                "SELECT * FROM goal_queue_integrity WHERE singleton=1"
            ).fetchone()
            old_state = {
                "revision": int(held["revision"]),
                "set_sha256": str(held["set_sha256"]),
                "queue_count": int(held["queue_count"]),
                "head_queue_id": str(held["head_queue_id"]),
                "active_queue_id": str(held["active_queue_id"]),
            }
            public_raw = str(held["public_json"])
            if with_public:
                old_state["public_sha256"] = str(held["public_sha256"])
            database.execute("DROP TABLE goal_queue_integrity")
            public_columns = (
                ",public_json TEXT NOT NULL,public_sha256 TEXT NOT NULL"
                if with_public else ""
            )
            database.execute(
                "CREATE TABLE goal_queue_integrity("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "revision INTEGER NOT NULL,set_sha256 TEXT NOT NULL,"
                "queue_count INTEGER NOT NULL,head_queue_id TEXT NOT NULL,"
                "active_queue_id TEXT NOT NULL"
                f"{public_columns},integrity_mac TEXT NOT NULL)"
            )
            columns = (
                "singleton,revision,set_sha256,queue_count,head_queue_id,"
                "active_queue_id" + (
                    ",public_json,public_sha256" if with_public else ""
                ) + ",integrity_mac"
            )
            values = [
                1, old_state["revision"], old_state["set_sha256"],
                old_state["queue_count"], old_state["head_queue_id"],
                old_state["active_queue_id"],
            ]
            if with_public:
                values.extend([public_raw, old_state["public_sha256"]])
            values.append(mac("swarm-goal-queue-state-v2", old_state))
            database.execute(
                f"INSERT INTO goal_queue_integrity({columns}) VALUES("
                + ",".join("?" for _one in values) + ")",
                values,
            )
            database.commit()
        old_anchor = {
            "schema_version": 2,
            "queue_schema_version": 1,
            "database": str(self.store.database.resolve()),
            "purpose": "durable-server-owned-board-goal-cursor",
            **old_state,
        }
        self.store._anchor.write_text(json.dumps({
            **old_anchor,
            "integrity_mac": mac("swarm-goal-queue-anchor-v2", old_anchor),
        }), encoding="utf-8")
        return old_state

    def test_verified_cursor_survives_reopen_and_never_replays_completed_item(self) -> None:
        queue = self.store.start(self.board(), "board-click-1")
        first_id = queue["current"]["id"]
        self.claim(queue)
        queue = self.store.record_result(
            queue["queue_id"], first_id,
            {"goal_complete": True, "verified": True, "run_id": "run-1"},
        )
        self.assertEqual(queue["completed"], 1)
        self.assertEqual(queue["cursor"], 1)
        self.assertEqual(queue["current"]["objective"], "Second goal")

        reopened = SwarmGoalQueueStore(self.config).status()
        self.assertEqual(reopened["cursor"], 1)
        self.assertEqual(reopened["current"]["objective"], "Second goal")
        late = self.store.record_result(
            queue["queue_id"], first_id,
            {"goal_complete": True, "verified": True, "run_id": "run-1"},
        )
        self.assertEqual(late["cursor"], 1)
        self.assertTrue(late["reused"])

    def test_paused_item_resumes_exactly_then_queue_continues_and_completes(self) -> None:
        queue = self.store.start(self.board(), "board-click-2")
        first = queue["current"]
        self.claim(queue)
        queue = self.store.record_result(
            queue["queue_id"], first["id"], {
                "goal_complete": False, "verified": False,
                "status": "paused_provider", "resume_token": "resume-one",
            }
        )
        self.assertEqual(queue["status"], "paused")
        self.assertEqual(queue["cursor"], 0)
        self.assertEqual(queue["current"]["resume_token"], "resume-one")
        self.assertEqual(
            SwarmGoalQueueStore(self.config).active_project_paths(),
            [str((self.root / "project").resolve())],
        )

        self.claim(queue, "work-resume-1")
        queue = self.store.record_result(
            queue["queue_id"], first["id"],
            {"goal_complete": True, "verified": True},
        )
        self.assertEqual(queue["status"], "queued")
        self.assertEqual(queue["current"]["objective"], "Second goal")
        second = queue["current"]
        self.claim(queue, "work-2")
        queue = self.store.record_result(
            queue["queue_id"], second["id"],
            {"goal_complete": True, "verified": True},
        )
        self.assertEqual(queue["status"], "complete")
        self.assertEqual(queue["completed"], 2)
        self.assertIsNone(queue["current"])

    def test_reload_recovers_dead_inflight_owner_without_changing_the_goal(self) -> None:
        queue = self.store.start(self.board(), "board-click-3")
        original = queue["current"]["objective"]
        self.claim(queue)
        with mock.patch("our_harness.swarm_goal_queue._owner_is_alive", return_value=False):
            recovered = SwarmGoalQueueStore(self.config).status()
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(recovered["cursor"], 0)
        self.assertEqual(recovered["current"]["objective"], original)
        self.assertIn("closed", recovered["current"]["last_error"])

    def test_ambiguous_response_reconciles_from_durable_work_result(self) -> None:
        queue = self.store.start(self.board(), "board-click-4")
        first = queue["current"]
        self.claim(queue, "durable-work-request")
        reconciled = self.store.reconcile(lambda identity: {
            "run_id": "run-after-reload", "request_id": identity,
            "status": "complete",
            "result": {"goal_complete": True, "verified": True},
        })
        self.assertEqual(reconciled["cursor"], 1)
        self.assertEqual(reconciled["completed"], 1)
        self.assertEqual(reconciled["current"]["objective"], "Second goal")

    def test_active_queue_and_request_retry_are_idempotent(self) -> None:
        first = self.store.start(self.board(), "board-click-5")
        invalid_now = {"agents": [], "projects": [], "works_on": [], "talks_to": []}
        same_request = self.store.start(invalid_now, "board-click-5")
        another_click = self.store.start(invalid_now, "board-click-6")
        self.assertEqual(first["queue_id"], same_request["queue_id"])
        self.assertEqual(first["queue_id"], another_click["queue_id"])
        self.assertEqual(same_request["current"]["objective"], "First exact goal\nwith formatting")
        self.assertTrue(same_request["reused"])
        self.assertTrue(another_click["reused"])

    def test_mismatched_goal_is_rejected_without_advancing_or_substitution(self) -> None:
        queue = self.store.start(self.board(), "board-click-7")
        current = queue["current"]
        with self.assertRaisesRegex(HarnessError, "exact current goal"):
            self.store.claim(
                queue["queue_id"], current["id"], objective="A replacement",
                agent_id="agent-1", peer_id="agent-2", project_id="project-1",
                conversation_id="pair-chat-1", request_id="work-wrong",
            )
        unchanged = self.store.status()
        self.assertEqual(unchanged["cursor"], 0)
        self.assertEqual(unchanged["current"]["objective"], current["objective"])
        self.assertEqual(unchanged["current"]["state"], "queued")

    def test_unready_project_is_explicitly_rejected_before_queue_creation(self) -> None:
        board = self.board()
        board["agents"][1]["ready"] = False
        with self.assertRaisesRegex(HarnessError, "No goal was started"):
            self.store.start(board, "blocked")
        self.assertIsNone(self.store.status())

    def test_keyed_integrity_rejects_rewritten_document_and_plain_digest(self) -> None:
        queue = self.store.start(self.board(), "integrity")
        with closing(sqlite3.connect(self.store.database)) as database:
            row = database.execute(
                "SELECT document_json FROM goal_queues WHERE queue_id=?",
                (queue["queue_id"],),
            ).fetchone()
            rewritten = row[0].replace('"state":"queued"', '"state":"complete"', 1)
            import hashlib
            database.execute(
                "UPDATE goal_queues SET document_json=?,document_sha256=? WHERE queue_id=?",
                (rewritten, hashlib.sha256(rewritten.encode("utf-8")).hexdigest(), queue["queue_id"]),
            )
            database.commit()
        with self.assertRaisesRegex(HarnessError, "keyed integrity"):
            self.store.status()

    def test_external_head_anchor_rejects_deleted_active_queue_row(self) -> None:
        queue = self.store.start(self.board(), "delete-active-row")
        with closing(sqlite3.connect(self.store.database)) as database:
            database.execute(
                "DELETE FROM goal_queues WHERE queue_id=?", (queue["queue_id"],),
            )
            database.commit()

        with self.assertRaisesRegex(HarnessError, "monotonic head"):
            self.store.status()

    def test_external_head_anchor_rejects_old_valid_database_rollback(self) -> None:
        queue = self.store.start(self.board(), "rollback-valid-database")
        old_database = self.root / "old-valid-goal-queue.sqlite3"
        with closing(sqlite3.connect(self.store.database)) as source, \
                closing(sqlite3.connect(old_database)) as destination:
            source.backup(destination)

        first = queue["current"]
        self.claim(queue, "rollback-work")
        advanced = self.store.record_result(
            queue["queue_id"], first["id"],
            {"goal_complete": True, "verified": True},
        )
        self.assertEqual(advanced["cursor"], 1)

        # Restore the complete, internally valid older SQLite image while the
        # separately keyed external anchor remains at the newer revision.
        with closing(sqlite3.connect(old_database)) as source, \
                closing(sqlite3.connect(self.store.database)) as destination:
            source.backup(destination)

        with self.assertRaisesRegex(HarnessError, "monotonic head"):
            self.store.status()

    def test_committed_database_one_step_ahead_repairs_interrupted_anchor_publish(self) -> None:
        queue = self.store.start(self.board(), "anchor-publish-crash")
        with mock.patch(
            "our_harness.swarm_goal_queue.atomic_text",
            side_effect=OSError("process stopped before anchor replace"),
        ), self.assertRaisesRegex(OSError, "before anchor replace"):
            self.claim(queue, "anchor-publish-work")

        recovered = SwarmGoalQueueStore(self.config).status()
        self.assertEqual(recovered["queue_id"], queue["queue_id"])
        self.assertEqual(recovered["current"]["state"], "running")

    def test_integrity_poll_never_scans_goal_document_bodies(self) -> None:
        board = self.board()
        future = "future objective payload " + ("x" * 199_970)
        board["projects"][0]["tasks"] = ["small exact current goal"] + [future] * 39
        queue = self.store.start(board, "metadata-only-integrity-poll")
        self.claim(queue, "metadata-only-running-work")
        statements: list[str] = []
        original_connect = self.store._connect

        def traced_connect():
            database = original_connect()
            database.set_trace_callback(statements.append)
            return database

        with mock.patch.object(self.store, "_connect", side_effect=traced_connect):
            status = self.store.status()

        set_scans = [
            statement for statement in statements
            if "FROM goal_queues ORDER BY queue_id" in statement
        ]
        self.assertTrue(set_scans, statements)
        self.assertTrue(all("document_json" not in statement for statement in set_scans))
        self.assertTrue(all("document_json" not in statement for statement in statements))
        self.assertEqual(status["total"], 40)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["current"]["objective"], "small exact current goal")
        self.assertNotIn(future, json.dumps(status))

    def test_existing_v1_anchor_migrates_without_losing_active_queue(self) -> None:
        queue = self.store.start(self.board(), "legacy-anchor-migration")
        with closing(sqlite3.connect(self.store.database)) as database:
            database.execute("DROP TABLE goal_queue_integrity")
            database.commit()
        legacy = {
            "schema_version": 1,
            "database": str(self.store.database.resolve()),
            "purpose": "durable-server-owned-board-goal-cursor",
        }
        self.store._anchor.write_text(json.dumps({
            **legacy,
            "integrity_mac": mac("swarm-goal-queue-anchor-v1", legacy),
        }), encoding="utf-8")

        migrated = SwarmGoalQueueStore(self.config).status()
        self.assertEqual(migrated["queue_id"], queue["queue_id"])
        anchor = json.loads(self.store._anchor.read_text(encoding="utf-8"))
        self.assertEqual(anchor["schema_version"], 3)
        self.assertEqual(anchor["active_queue_id"], queue["queue_id"])

    def test_existing_v2_metadata_and_anchor_migrate_exact_active_queue(self) -> None:
        queue = self.store.start(self.board(), "v2-active-migration")
        running = self.claim(queue, "v2-running-work")
        with closing(sqlite3.connect(self.store.database)) as database:
            database.row_factory = sqlite3.Row
            held = database.execute(
                "SELECT * FROM goal_queue_integrity WHERE singleton=1"
            ).fetchone()
            old_state = {
                "revision": int(held["revision"]),
                "set_sha256": str(held["set_sha256"]),
                "queue_count": int(held["queue_count"]),
                "head_queue_id": str(held["head_queue_id"]),
                "active_queue_id": str(held["active_queue_id"]),
            }
            database.execute("DROP TABLE goal_queue_integrity")
            database.execute("""
                CREATE TABLE goal_queue_integrity(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  revision INTEGER NOT NULL,
                  set_sha256 TEXT NOT NULL,
                  queue_count INTEGER NOT NULL,
                  head_queue_id TEXT NOT NULL,
                  active_queue_id TEXT NOT NULL,
                  integrity_mac TEXT NOT NULL
                )
            """)
            database.execute(
                "INSERT INTO goal_queue_integrity VALUES(1,?,?,?,?,?,?)",
                (
                    old_state["revision"], old_state["set_sha256"],
                    old_state["queue_count"], old_state["head_queue_id"],
                    old_state["active_queue_id"],
                    mac("swarm-goal-queue-state-v2", old_state),
                ),
            )
            database.commit()
        old_anchor = {
            "schema_version": 2,
            "queue_schema_version": 1,
            "database": str(self.store.database.resolve()),
            "purpose": "durable-server-owned-board-goal-cursor",
            **old_state,
        }
        self.store._anchor.write_text(json.dumps({
            **old_anchor,
            "integrity_mac": mac("swarm-goal-queue-anchor-v2", old_anchor),
        }), encoding="utf-8")

        migrated = SwarmGoalQueueStore(self.config).status()
        self.assertEqual(migrated["queue_id"], running["queue_id"])
        self.assertEqual(migrated["status"], "running")
        self.assertEqual(migrated["cursor"], running["cursor"])
        self.assertEqual(migrated["current"]["id"], running["current"]["id"])
        self.assertEqual(
            migrated["current"]["objective"], running["current"]["objective"],
        )
        anchor = json.loads(self.store._anchor.read_text(encoding="utf-8"))
        self.assertEqual(anchor["schema_version"], 3)
        with closing(sqlite3.connect(self.store.database)) as database:
            columns = {
                row[1] for row in database.execute(
                    "PRAGMA table_info(goal_queue_integrity)"
                ).fetchall()
            }
        self.assertTrue({"public_json", "public_sha256"}.issubset(columns))

    def test_v2_without_projection_recovers_db_ahead_migration_crash(self) -> None:
        queue = self.store.start(self.board(), "v2-no-projection-crash")
        running = self.claim(queue, "v2-no-projection-work")
        self.install_prior_v2_integrity(with_public=False)

        with mock.patch(
            "our_harness.swarm_goal_queue.atomic_text",
            side_effect=OSError("stopped before publishing v3 anchor"),
        ), self.assertRaisesRegex(OSError, "publishing v3 anchor"):
            SwarmGoalQueueStore(self.config)

        recovered = SwarmGoalQueueStore(self.config).status()
        self.assertEqual(recovered["queue_id"], running["queue_id"])
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered["current"]["id"], running["current"]["id"])
        self.assertEqual(recovered["current"]["objective"], running["current"]["objective"])
        self.assertEqual(
            json.loads(self.store._anchor.read_text(encoding="utf-8"))["schema_version"],
            3,
        )

    def test_v2_with_projection_recovers_db_ahead_migration_crash(self) -> None:
        queue = self.store.start(self.board(), "v2-projection-crash")
        running = self.claim(queue, "v2-projection-work")
        self.install_prior_v2_integrity(with_public=True)

        with mock.patch(
            "our_harness.swarm_goal_queue.atomic_text",
            side_effect=OSError("stopped before publishing projected v3 anchor"),
        ), self.assertRaisesRegex(OSError, "projected v3 anchor"):
            SwarmGoalQueueStore(self.config)

        recovered = SwarmGoalQueueStore(self.config).status()
        self.assertEqual(recovered["queue_id"], running["queue_id"])
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered["current"]["objective"], running["current"]["objective"])

    def test_no_anchor_empty_v2_commits_upgrade_before_publication(self) -> None:
        self.install_prior_v2_integrity(with_public=False)
        self.store._anchor.unlink()

        with mock.patch(
            "our_harness.swarm_goal_queue.atomic_text",
            side_effect=OSError("stopped while publishing initial v3 anchor"),
        ), self.assertRaisesRegex(OSError, "initial v3 anchor"):
            SwarmGoalQueueStore(self.config)

        reopened = SwarmGoalQueueStore(self.config)
        self.assertIsNone(reopened.status())
        anchor = json.loads(self.store._anchor.read_text(encoding="utf-8"))
        self.assertEqual(anchor["schema_version"], 3)
        self.assertEqual(anchor["revision"], 0)

    def test_same_request_retry_does_not_replace_the_original_live_owner(self) -> None:
        queue = self.store.start(self.board(), "same-owner")
        running = self.claim(queue, "same-work-request")
        before = running["current"]
        with mock.patch("our_harness.swarm_goal_queue.os.getpid", return_value=999_999), \
                mock.patch("our_harness.swarm_goal_queue._owner_is_alive", return_value=True):
            replay = self.claim(running, "same-work-request")
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["current"]["owner_pid"], before["owner_pid"])
        self.assertEqual(replay["current"]["owner_token"], before["owner_token"])
        self.assertEqual(replay["current"]["attempts"], before["attempts"])


if __name__ == "__main__":
    unittest.main()
