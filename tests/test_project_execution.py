from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from our_harness.project_execution import (
    CONTRACT_FINGERPRINT,
    DATABASE_NAME,
    INTEGRITY_ANCHOR_NAME,
    INTEGRITY_KEY_NAME,
    ProjectExecutionConflict,
    ProjectExecutionCoordinator,
    ProjectExecutionError,
    ProjectExecutionIntegrityError,
    ProjectExecutionIntentConflict,
    ProjectExecutionStateError,
    ProjectExecutionTokenError,
    SCHEMA_VERSION,
)


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class ProjectExecutionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "arbitrary-user-runtime"
        self.clock = FakeClock()
        self.living: dict[int, bool] = {}
        self.coordinator = ProjectExecutionCoordinator(
            base_dir=self.runtime,
            provisional_ttl_ms=100,
            clock_ms=self.clock,
            owner_alive=lambda pid, _token: self.living.get(pid, True),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def project(self, name: str) -> Path:
        where = self.root / "projects" / name
        where.mkdir(parents=True, exist_ok=True)
        return where

    def reserve(
        self,
        request_id: str,
        roots: list[Path],
        *,
        intent: object | None = None,
        pid: int = 101,
        token: str = "process-a",
    ):
        self.living.setdefault(pid, True)
        return self.coordinator.reserve(
            request_id=request_id,
            intent={"engine": "test", "work": request_id}
            if intent is None
            else intent,
            roots=roots,
            provisional_owner_id=f"provisional:{request_id}",
            owner_pid=pid,
            owner_process_token=token,
        )

    def bind(self, reservation, owner: str = "engine:job"):
        return self.coordinator.bind(
            reservation.reservation_id,
            reservation.lease_token,
            reservation.generation,
            durable_owner_id=owner,
        )

    def test_runtime_is_user_scoped_and_records_schema_contract_anchor(self) -> None:
        project = self.project("one")
        reservation = self.reserve("schema-request", [project])

        canonical_runtime = self.runtime.resolve()
        self.assertEqual(self.coordinator.base_dir, canonical_runtime)
        self.assertEqual(self.coordinator.path, canonical_runtime / DATABASE_NAME)
        self.assertEqual(self.coordinator.key_path, canonical_runtime / INTEGRITY_KEY_NAME)
        self.assertTrue(self.coordinator.path.is_file())
        self.assertEqual(self.coordinator.key_path.stat().st_size, 32)
        self.assertFalse((project / DATABASE_NAME).exists())
        self.assertEqual(reservation.schema_version, SCHEMA_VERSION)
        self.assertEqual(reservation.contract_fingerprint, CONTRACT_FINGERPRINT)

        with closing(sqlite3.connect(self.coordinator.path)) as db:
            metadata = db.execute(
                "SELECT schema_version,contract_fingerprint,user_scope,integrity_mac "
                "FROM project_execution_metadata WHERE singleton=1"
            ).fetchone()
        self.assertEqual(metadata[0], SCHEMA_VERSION)
        self.assertEqual(metadata[1], CONTRACT_FINGERPRINT)
        self.assertRegex(metadata[2], r"^[0-9a-f]{64}$")
        self.assertRegex(metadata[3], r"^[0-9a-f]{64}$")

    def test_runtime_aliases_share_one_store_but_siblings_do_not(self) -> None:
        parent = self.root / "runtime identity"
        detour = parent / "detour"
        detour.mkdir(parents=True)
        canonical_runtime = parent / "shared"
        aliased_runtime = detour / ".." / "shared"
        through_alias = ProjectExecutionCoordinator(base_dir=aliased_runtime)
        through_canonical = ProjectExecutionCoordinator(base_dir=canonical_runtime)
        self.assertEqual(through_alias.base_dir, canonical_runtime.resolve())
        self.assertEqual(through_canonical.base_dir, canonical_runtime.resolve())

        project = self.project("runtime-alias")
        claim = through_alias.reserve(
            request_id="runtime-alias",
            intent={"engine": "test", "work": "alias"},
            roots=[project],
            provisional_owner_id="alias-owner",
        )
        replay = through_canonical.by_request("runtime-alias")
        self.assertIsNotNone(replay)
        assert replay is not None
        self.assertEqual(replay.reservation_id, claim.reservation_id)

        sibling = ProjectExecutionCoordinator(base_dir=parent / "different")
        self.assertIsNone(sibling.by_request("runtime-alias"))

    def test_runtime_base_symlink_is_rejected_without_writing_its_target(self) -> None:
        target = self.root / "runtime-link-target"
        target.mkdir()
        link = self.root / "runtime-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"this host cannot create a directory symlink: {exc}")

        with self.assertRaisesRegex(
            ProjectExecutionIntegrityError, "link or reparse point"
        ):
            ProjectExecutionCoordinator(base_dir=link)
        self.assertFalse((target / DATABASE_NAME).exists())
        self.assertFalse((target / INTEGRITY_KEY_NAME).exists())

    def test_multi_root_reservation_is_atomic_for_exact_and_nested_overlap(self) -> None:
        first = self.project("first")
        first_child = first / "nested"
        first_child.mkdir()
        second = self.project("second")
        free = self.project("free")
        held = self.reserve("held", [first, first_child, second])
        # A nested root is redundant and is intentionally collapsed.
        self.assertEqual(set(held.roots), {str(first.resolve()).lower(), str(second.resolve()).lower()})

        for request_id, roots in (
            ("exact", [first]),
            ("descendant", [first_child]),
            ("ancestor", [first.parent]),
        ):
            with self.subTest(request_id=request_id):
                with self.assertRaises(ProjectExecutionConflict):
                    self.reserve(request_id, roots)

        # If any root conflicts, none of the candidate roots are reserved.
        with self.assertRaises(ProjectExecutionConflict):
            self.reserve("atomic-failure", [free, first_child])
        free_claim = self.reserve("free-after-failure", [free])
        self.assertEqual(free_claim.state, "provisional")

    def test_request_and_canonical_intent_are_stably_idempotent(self) -> None:
        parent = self.project("intent")
        child = parent / "child"
        child.mkdir()
        first = self.reserve(
            "same-request",
            [parent, child],
            intent={"b": [2, 3], "a": 1},
        )
        replay = self.reserve(
            "same-request",
            [child, parent],
            intent={"a": 1, "b": [2, 3]},
            pid=999,
            token="different-process",
        )
        self.assertEqual(replay.reservation_id, first.reservation_id)
        self.assertEqual(replay.lease_token, first.lease_token)
        self.assertEqual(replay.generation, first.generation)

        with self.assertRaises(ProjectExecutionIntentConflict):
            self.reserve(
                "same-request",
                [parent],
                intent={"a": 1, "b": [2, 4]},
            )
        with self.assertRaises(ProjectExecutionIntentConflict):
            self.reserve(
                "same-request",
                [self.project("different-root")],
                intent={"a": 1, "b": [2, 3]},
            )

        with self.assertRaisesRegex(Exception, "may not be truncated"):
            self.coordinator.reserve(
                request_id="r" * 161,
                intent={},
                roots=[parent],
                provisional_owner_id="owner",
            )

    def test_only_dead_or_expired_provisional_claims_are_reclaimed(self) -> None:
        dead_root = self.project("dead")
        dead = self.reserve("dead-owner", [dead_root], pid=201, token="birth-1")
        self.living[201] = False
        successor = self.reserve("dead-successor", [dead_root / "nested"], pid=202)
        self.assertEqual(successor.state, "provisional")
        expired_dead = self.coordinator.by_request("dead-owner")
        self.assertIsNotNone(expired_dead)
        assert expired_dead is not None
        self.assertEqual(expired_dead.state, "released")
        self.assertEqual(expired_dead.release_reason, "provisional_expired")

        timed_root = self.project("timed")
        timed = self.reserve("timed-owner", [timed_root], pid=203)
        self.clock.now_ms = timed.expires_at_ms - 1
        with self.assertRaises(ProjectExecutionConflict):
            self.reserve("too-early", [timed_root], pid=204)
        self.clock.now_ms = timed.expires_at_ms
        after_deadline = self.reserve("after-deadline", [timed_root], pid=204)
        self.assertEqual(after_deadline.state, "provisional")

    def test_same_request_reclaims_stale_provisional_with_new_token_generation(self) -> None:
        root = self.project("reclaim")
        first = self.reserve("replay-after-expiry", [root], intent={"task": "same"})
        self.clock.now_ms = first.expires_at_ms
        successor = self.reserve(
            "replay-after-expiry",
            [root],
            intent={"task": "same"},
            pid=302,
            token="birth-2",
        )
        self.assertEqual(successor.reservation_id, first.reservation_id)
        self.assertEqual(successor.generation, first.generation + 1)
        self.assertNotEqual(successor.lease_token, first.lease_token)

        for operation in (
            lambda: self.coordinator.bind(
                first.reservation_id,
                first.lease_token,
                first.generation,
                durable_owner_id="late-owner",
            ),
            lambda: self.coordinator.release(
                first.reservation_id, first.lease_token, first.generation
            ),
        ):
            with self.assertRaises(ProjectExecutionTokenError):
                operation()
        self.assertEqual(self.coordinator.get(successor.reservation_id).state, "provisional")

    def test_bind_is_compare_and_swap_and_idempotent_for_the_winner(self) -> None:
        root = self.project("bind")
        claim = self.reserve("bind-request", [root])
        with self.assertRaises(ProjectExecutionTokenError):
            self.coordinator.bind(
                claim.reservation_id,
                "0" * 64,
                claim.generation,
                durable_owner_id="engine:a",
            )
        with self.assertRaises(ProjectExecutionTokenError):
            self.coordinator.bind(
                claim.reservation_id,
                claim.lease_token,
                claim.generation + 1,
                durable_owner_id="engine:a",
            )

        other = ProjectExecutionCoordinator(
            base_dir=self.runtime,
            provisional_ttl_ms=100,
            clock_ms=self.clock,
            owner_alive=lambda pid, _token: self.living.get(pid, True),
        )
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []

        def bind_as(coordinator, owner: str) -> None:
            barrier.wait()
            try:
                value = coordinator.bind(
                    claim.reservation_id,
                    claim.lease_token,
                    claim.generation,
                    durable_owner_id=owner,
                )
                outcomes.append(("ok", value.durable_owner_id))
            except (ProjectExecutionStateError, ProjectExecutionTokenError):
                outcomes.append(("state-error", owner))

        one = threading.Thread(target=bind_as, args=(self.coordinator, "engine:a"))
        two = threading.Thread(target=bind_as, args=(other, "engine:b"))
        one.start()
        two.start()
        one.join(10)
        two.join(10)
        self.assertFalse(one.is_alive())
        self.assertFalse(two.is_alive())
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1)
        self.assertEqual([kind for kind, _ in outcomes].count("state-error"), 1)

        winner = self.coordinator.get(claim.reservation_id)
        self.assertEqual(winner.generation, claim.generation + 1)
        self.assertNotEqual(winner.lease_token, claim.lease_token)
        for operation in (
            lambda: self.coordinator.set_state(
                claim.reservation_id,
                claim.lease_token,
                claim.generation,
                "paused",
            ),
            lambda: self.coordinator.release(
                claim.reservation_id, claim.lease_token, claim.generation
            ),
        ):
            with self.assertRaises(ProjectExecutionTokenError):
                operation()
        replay = self.coordinator.bind(
            winner.reservation_id,
            winner.lease_token,
            winner.generation,
            durable_owner_id=winner.durable_owner_id,
        )
        self.assertEqual(replay, winner)
        # If the commit succeeded but the response was lost, the exact old
        # bind request can recover the rotated durable capability after restart.
        restarted = ProjectExecutionCoordinator(base_dir=self.runtime)
        response_loss_replay = restarted.bind(
            claim.reservation_id,
            claim.lease_token,
            claim.generation,
            durable_owner_id=winner.durable_owner_id,
        )
        self.assertEqual(response_loss_replay, winner)

    def test_bind_accepts_unicode_owner_and_replays_with_current_capability(self) -> None:
        claim = self.reserve("unicode-owner", [self.project("unicode-owner")])
        bound = self.bind(claim, "motor:jobb:räv🦊")
        restarted = ProjectExecutionCoordinator(base_dir=self.runtime)
        replay = restarted.bind(
            bound.reservation_id,
            bound.lease_token,
            bound.generation,
            durable_owner_id="motor:jobb:räv🦊",
        )
        self.assertEqual(replay, bound)

    def test_bound_running_paused_and_waiting_claims_survive_death_time_and_restart(self) -> None:
        root = self.project("durable")
        claim = self.reserve("durable-request", [root], pid=401)
        bound = self.bind(claim, "long-goal:one")
        self.living[401] = False
        self.clock.now_ms += 10_000_000
        self.assertEqual(self.coordinator.sweep_provisionals(), 0)
        with self.assertRaises(ProjectExecutionConflict):
            self.reserve("blocked-by-running", [root / "child"], pid=402)

        paused = self.coordinator.set_state(
            bound.reservation_id, bound.lease_token, bound.generation, "paused"
        )
        restarted = ProjectExecutionCoordinator(
            base_dir=self.runtime,
            provisional_ttl_ms=100,
            clock_ms=self.clock,
            owner_alive=lambda _pid, _token: False,
        )
        self.assertEqual(restarted.get(paused.reservation_id).state, "paused")
        self.assertEqual(restarted.sweep_provisionals(), 0)
        with self.assertRaises(ProjectExecutionConflict):
            restarted.reserve(
                request_id="blocked-by-paused",
                intent={"work": "other"},
                roots=[root],
                provisional_owner_id="new-owner",
                owner_pid=999,
                owner_process_token="dead",
            )

        waiting = restarted.set_state(
            paused.reservation_id, paused.lease_token, paused.generation, "waiting"
        )
        restarted_again = ProjectExecutionCoordinator(
            base_dir=self.runtime,
            provisional_ttl_ms=100,
            clock_ms=self.clock,
            owner_alive=lambda _pid, _token: False,
        )
        self.assertEqual(restarted_again.get(waiting.reservation_id).state, "waiting")
        self.assertEqual(restarted_again.sweep_provisionals(), 0)

    def test_release_is_token_safe_idempotent_and_frees_all_roots(self) -> None:
        one = self.project("release-one")
        two = self.project("release-two")
        claim = self.bind(self.reserve("release", [one, two]))
        with self.assertRaises(ProjectExecutionTokenError):
            self.coordinator.release(
                claim.reservation_id, "f" * 64, claim.generation
            )
        released = self.coordinator.release(
            claim.reservation_id,
            claim.lease_token,
            claim.generation,
            reason="finished",
        )
        self.assertEqual(released.state, "released")
        self.assertEqual(released.release_reason, "finished")
        self.assertEqual(
            self.coordinator.release(
                claim.reservation_id, claim.lease_token, claim.generation
            ),
            released,
        )
        self.assertEqual(self.reserve("reuse-one", [one]).state, "provisional")
        self.assertEqual(self.reserve("reuse-two", [two]).state, "provisional")

    def test_two_instances_cannot_win_overlapping_multi_root_reservations(self) -> None:
        parent = self.project("race")
        child = parent / "child"
        child.mkdir()
        second = ProjectExecutionCoordinator(
            base_dir=self.runtime,
            provisional_ttl_ms=100,
            clock_ms=self.clock,
            owner_alive=lambda _pid, _token: True,
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def contend(coordinator, request_id: str, root: Path) -> None:
            barrier.wait()
            try:
                coordinator.reserve(
                    request_id=request_id,
                    intent={"request": request_id},
                    roots=[root, self.project(f"side-{request_id}")],
                    provisional_owner_id=request_id,
                    owner_pid=501,
                    owner_process_token="alive",
                )
                outcomes.append("won")
            except ProjectExecutionConflict:
                outcomes.append("conflict")

        first_thread = threading.Thread(
            target=contend, args=(self.coordinator, "race-parent", parent)
        )
        second_thread = threading.Thread(
            target=contend, args=(second, "race-child", child)
        )
        first_thread.start()
        second_thread.start()
        first_thread.join(10)
        second_thread.join(10)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertCountEqual(outcomes, ["won", "conflict"])
        self.assertEqual(len(self.coordinator.list_active()), 1)

    def test_hmac_rejects_row_metadata_and_user_scope_tampering(self) -> None:
        root = self.project("integrity")
        claim = self.reserve("integrity-request", [root])
        with closing(sqlite3.connect(self.coordinator.path)) as db:
            db.execute(
                "UPDATE project_execution_reservations SET state='paused' "
                "WHERE reservation_id=?",
                (claim.reservation_id,),
            )
            db.commit()
        with self.assertRaises(ProjectExecutionIntegrityError):
            self.coordinator.get(claim.reservation_id)

        # Use an independent runtime so metadata validation is not masked by the
        # deliberately corrupted reservation above.
        other_runtime = self.root / "metadata-runtime"
        other = ProjectExecutionCoordinator(base_dir=other_runtime)
        with closing(sqlite3.connect(other.path)) as db:
            db.execute(
                "UPDATE project_execution_metadata SET contract_fingerprint=? "
                "WHERE singleton=1",
                ("0" * 64,),
            )
            db.commit()
        with self.assertRaises(ProjectExecutionIntegrityError):
            ProjectExecutionCoordinator(base_dir=other_runtime)

        scoped_runtime = self.root / "scope-runtime"
        ProjectExecutionCoordinator(base_dir=scoped_runtime, user_scope="a" * 64)
        with self.assertRaises(ProjectExecutionIntegrityError):
            ProjectExecutionCoordinator(base_dir=scoped_runtime, user_scope="b" * 64)

    def test_hmac_rejects_deleted_rows_non_ascii_macs_and_missing_store(self) -> None:
        deletion_runtime = self.root / "deletion-runtime"
        deletion = ProjectExecutionCoordinator(base_dir=deletion_runtime)
        root = self.project("deleted-row")
        claim = deletion.reserve(
            request_id="deleted-row",
            intent={"work": "delete"},
            roots=[root],
            provisional_owner_id="test",
        )
        with closing(sqlite3.connect(deletion.path)) as db:
            db.execute(
                "DELETE FROM project_execution_reservations WHERE reservation_id=?",
                (claim.reservation_id,),
            )
            db.commit()
        with self.assertRaises(ProjectExecutionIntegrityError):
            deletion.list_active()

        malformed_runtime = self.root / "malformed-runtime"
        malformed = ProjectExecutionCoordinator(base_dir=malformed_runtime)
        with closing(sqlite3.connect(malformed.path)) as db:
            db.execute(
                "UPDATE project_execution_metadata SET integrity_mac='räv' "
                "WHERE singleton=1"
            )
            db.commit()
        with self.assertRaises(ProjectExecutionIntegrityError):
            ProjectExecutionCoordinator(base_dir=malformed_runtime)

        missing_runtime = self.root / "missing-runtime"
        missing = ProjectExecutionCoordinator(base_dir=missing_runtime)
        self.assertTrue((missing_runtime / INTEGRITY_ANCHOR_NAME).is_file())
        for suffix in ("-wal", "-shm", ""):
            Path(f"{missing.path}{suffix}").unlink(missing_ok=True)
        with self.assertRaises(ProjectExecutionIntegrityError):
            ProjectExecutionCoordinator(base_dir=missing_runtime)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows path aliases")
    def test_windows_extended_namespace_alias_cannot_bypass_overlap(self) -> None:
        root = self.project("windows-alias")
        self.reserve("ordinary-alias", [root])
        extended = Path("\\\\?\\" + str(root))
        with self.assertRaises(ProjectExecutionConflict):
            self.reserve("extended-alias", [extended])

    @unittest.skipUnless(__import__("os").name == "nt", "Windows 8.3 path aliases")
    def test_windows_short_and_long_runtime_aliases_share_one_store(self) -> None:
        import ctypes
        import os

        long_parent = self.root.resolve()
        buffer = ctypes.create_unicode_buffer(32_768)
        copied = ctypes.windll.kernel32.GetShortPathNameW(
            str(long_parent), buffer, len(buffer)
        )
        if copied == 0 or copied >= len(buffer):
            self.skipTest("this Windows volume does not expose an 8.3 alias")
        short_parent = Path(buffer.value)
        self.assertTrue(long_parent.samefile(short_parent))
        if os.path.normcase(str(short_parent)) == os.path.normcase(str(long_parent)):
            self.skipTest("this Windows volume does not expose a distinct 8.3 alias")

        short_runtime = short_parent / "short-long-shared-runtime"
        long_runtime = long_parent / "short-long-shared-runtime"
        through_short = ProjectExecutionCoordinator(base_dir=short_runtime)
        through_long = ProjectExecutionCoordinator(base_dir=long_runtime)
        self.assertEqual(through_short.base_dir, through_long.base_dir)
        self.assertEqual(through_short.path, through_long.path)

        project = self.project("windows-runtime-alias")
        claim = through_short.reserve(
            request_id="windows-runtime-alias",
            intent={"engine": "test", "work": "windows-alias"},
            roots=[project],
            provisional_owner_id="windows-alias-owner",
        )
        replay = through_long.by_request("windows-runtime-alias")
        self.assertIsNotNone(replay)
        assert replay is not None
        self.assertEqual(replay.reservation_id, claim.reservation_id)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows junctions")
    def test_windows_runtime_junction_is_rejected_without_writing_its_target(self) -> None:
        import subprocess

        target = self.root / "runtime-junction-target"
        target.mkdir()
        junction = self.root / "runtime-junction"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0 or not junction.is_dir():
            self.skipTest("this Windows host cannot create a directory junction")
        try:
            with mock.patch.dict(
                "os.environ", {"OUR_HARNESS_PROJECT_EXECUTION_DIR": str(junction)}
            ):
                with self.assertRaisesRegex(
                    ProjectExecutionIntegrityError, "link or reparse point"
                ):
                    ProjectExecutionCoordinator()
            self.assertFalse((target / DATABASE_NAME).exists())
            self.assertFalse((target / INTEGRITY_KEY_NAME).exists())
        finally:
            junction.rmdir()

    def test_persisted_claim_remains_releasable_after_root_disappears(self) -> None:
        root = self.project("disappearing")
        bound = self.bind(self.reserve("disappearing", [root]))
        root.rename(root.with_name("moved-away"))
        self.assertEqual(self.coordinator.get(bound.reservation_id).state, "running")
        released = self.coordinator.release(
            bound.reservation_id, bound.lease_token, bound.generation
        )
        self.assertEqual(released.state, "released")

    def test_ttl_and_generation_reject_non_integral_values(self) -> None:
        root = self.project("strict-integers")
        for ttl in (1.9, "100", True):
            with self.subTest(ttl=ttl), self.assertRaises(ProjectExecutionError):
                self.coordinator.reserve(
                    request_id=f"ttl-{type(ttl).__name__}",
                    intent={"ttl": str(ttl)},
                    roots=[root],
                    provisional_owner_id="strict",
                    ttl_ms=ttl,
                )
        claim = self.reserve("strict-generation", [root])
        for generation in (1.9, "1", True):
            with self.subTest(generation=generation), self.assertRaises(
                ProjectExecutionTokenError
            ):
                self.coordinator.release(
                    claim.reservation_id, claim.lease_token, generation
                )

    def test_provisional_renewal_is_capability_guarded(self) -> None:
        root = self.project("renew")
        claim = self.reserve("renew-request", [root])
        self.clock.now_ms += 50
        renewed = self.coordinator.renew_provisional(
            claim.reservation_id, claim.lease_token, claim.generation, ttl_ms=200
        )
        self.assertEqual(renewed.expires_at_ms, self.clock.now_ms + 200)
        with self.assertRaises(ProjectExecutionTokenError):
            self.coordinator.renew_provisional(
                claim.reservation_id, "0" * 64, claim.generation
            )
        bound = self.bind(renewed)
        with self.assertRaises(ProjectExecutionStateError):
            self.coordinator.renew_provisional(
                bound.reservation_id, bound.lease_token, bound.generation
            )


if __name__ == "__main__":
    unittest.main()
