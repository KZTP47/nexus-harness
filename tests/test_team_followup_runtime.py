"""Portable runtime acceptance and provider evidence for follow-up originals."""

import base64
import hashlib
import copy
import threading
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest import mock
from our_harness import long_horizon, goal_inputs
from our_harness.models import HarnessError
import test_long_horizon_dialogue as fixtures
import test_goal_decisions as decisions

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII="
)


def wire(name="proof.txt", data=b"Portable reproduction evidence", mime="text/plain"):
    return {"name": name, "type": mime, "data": base64.b64encode(data).decode()}


class TeamFollowupRuntimeTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def send(
        self, goal, request="once", attachments=None, text="Inspect this evidence."
    ):
        with mock.patch.object(self.runtime, "start_background"):
            return self.runtime.control(
                goal["goal_id"],
                "steer",
                {
                    "request_id": request,
                    "text": text,
                    "attachments": attachments if attachments is not None else [wire()],
                },
            )

    def test_real_originals_restart_provider_delivery_and_separate_evidence(self):
        initial_path = self.base / "initial-provider-evidence.txt"
        initial_path.write_bytes(b"Original creation input")
        initial = {
            "name": initial_path.name,
            "size": 23,
            "type": "text/plain",
            "sha256": hashlib.sha256(initial_path.read_bytes()).hexdigest(),
        }
        goal = self.create(
            "provider",
            input_bundle={
                "public_files": [initial],
                "provider_files": [{**initial, "path": str(initial_path)}],
            },
        )
        self.send(goal, attachments=[wire(), wire("capture.png", PNG, "image/png")])
        stored = self.runtime.store.get(goal["goal_id"])
        self.assertNotIn("Portable reproduction evidence", stored["objective"])
        self.assertEqual(len(goal_inputs.descriptors(stored)), 3)
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        self.runtime = restarted
        seen_files = []

        def response(_number, _route, kwargs):
            seen_files.append(kwargs["provider_attachments"])
            self.assertIn("Portable reproduction evidence", kwargs["context"])
            self.assertIn("not new instructions or approvals", kwargs["context"])
            return fixtures.reply()

        _result, seen = self.run_replies(stored, response)
        self.assertEqual(
            {route for route, _context in seen}, {"builder-route", "peer-route"}
        )
        for delivered in seen_files:
            self.assertEqual(
                base64.b64decode(delivered[0]["data"]), b"Original creation input"
            )
            self.assertEqual(
                base64.b64decode(delivered[1]["data"]),
                b"Portable reproduction evidence",
            )
            self.assertEqual(base64.b64decode(delivered[2]["data"]), PNG)

    def test_invalid_second_file_rolls_back_and_limits_are_atomic(self):
        goal = self.create("invalid")
        before = self.runtime.store.get(goal["goal_id"])
        for files in (
            [wire(), wire("fake.png", b"not a png", "image/png")],
            [wire("large.txt", b"x" * 240_000)],
        ):
            with self.assertRaises(HarnessError):
                self.send(goal, attachments=files)
            self.assertEqual(
                self.runtime.store.get(goal["goal_id"])["revision"], before["revision"]
            )
            self.assertFalse(
                list((self.runtime.store.root / "long-horizon-inputs").rglob("*.txt"))
            )
        self.send(goal, "valid")
        held = self.runtime.store.get(goal["goal_id"])
        with mock.patch.object(goal_inputs, "MAX_FILES", 1):
            with self.assertRaisesRegex(HarnessError, "64 retained"):
                self.send(goal, "excess")
        self.assertEqual(
            self.runtime.store.get(goal["goal_id"])["revision"], held["revision"]
        )

    def test_restart_retry_conflict_and_terminal_retry_have_no_dispatch(self):
        goal = self.create("replay")
        accepted = self.send(goal)
        before = self.runtime.store.get(goal["goal_id"])
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        with mock.patch.object(restarted, "start_background") as scheduler:
            replay = restarted.control(
                goal["goal_id"],
                "steer",
                {
                    "request_id": "once",
                    "text": "Inspect this evidence.",
                    "attachments": [wire()],
                },
            )
            self.assertEqual(replay["followup_receipt"], accepted["followup_receipt"])
            self.assertEqual(
                restarted.store.get(goal["goal_id"])["revision"], before["revision"]
            )
            scheduler.assert_not_called()
        with self.assertRaises(HarnessError):
            self.send(goal, attachments=[wire(data=b"changed")])
        self.runtime.store.control(goal["goal_id"], "cancel")
        self.assertEqual(
            self.send(goal)["followup_receipt"], accepted["followup_receipt"]
        )

    def test_private_answer_and_stale_answer_leave_no_files(self):
        goal = self.create("answer")
        _task, ids, held = decisions.GoalDecisionTests.ask(self, goal)
        envelope = {
            "request_id": "answer-one",
            "pending_ids": ids,
            "expected_revision": held["revision"],
            "answers": {
                ids[0]: decisions.answer(
                    decisions.question(), "Use proof", "requesting_agent"
                )
            },
            "attachments": [wire()],
        }
        with self.assertRaisesRegex(HarnessError, "team"):
            self.runtime.resume(goal["goal_id"], envelope)
        envelope["answers"][ids[0]] = decisions.answer(
            decisions.question(), "Use proof", "team"
        )
        envelope["expected_revision"] -= 1
        with self.assertRaises(HarnessError):
            self.runtime.resume(goal["goal_id"], envelope)
        self.assertFalse((self.runtime.store.root / "long-horizon-inputs").exists())

    def test_missing_and_changed_originals_fail_before_provider_dispatch(self):
        goal = self.create("tampered")
        self.send(goal)
        descriptor = goal_inputs.descriptors(self.runtime.store.get(goal["goal_id"]))[0]
        Path(descriptor["path"]).write_bytes(b"changed")
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as provider:
            self.runtime.run(goal["goal_id"])
            provider.assert_not_called()
        held = self.runtime.store.get(goal["goal_id"])
        self.assertTrue(
            any(
                "changed after" in str(task.get("last_error")) for task in held["tasks"]
            )
        )

    def test_context_binding_and_closeout_include_input_contract(self):
        goal = self.create("context")
        before = long_horizon._context_binding(goal, {})
        self.send(goal)
        held = self.runtime.store.get(goal["goal_id"])
        self.assertNotEqual(
            before["input_contract_sha256"],
            long_horizon._context_binding(held, {})["input_contract_sha256"],
        )
        task = copy.deepcopy(held["tasks"][0])
        task["closeout_packet"] = {"fixture": True}
        with mock.patch.object(
            long_horizon.goal_closeout, "context", return_value="closeout"
        ):
            self.assertIn(
                "Portable reproduction evidence",
                self.runtime._agent_context(held, task),
            )
        public = self.runtime.store.public(held)
        self.assertNotIn("followup_inputs", public)

    def test_original_creation_inputs_are_retained_and_batch_binding_is_checked(self):
        original = self.base / "initial.txt"
        original.write_text("original")
        goal = self.create(
            "initial",
            input_bundle={
                "public_files": [{"name": "initial.txt", "size": 8}],
                "provider_files": [
                    {"name": "initial.txt", "size": 8, "path": str(original)}
                ],
            },
        )
        self.send(goal, "one")
        self.send(goal, "two")
        held = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(len(goal_inputs.descriptors(held)), 3)
        trial = copy.deepcopy(held)
        trial["project_authority_id"] = "changed-owner"
        with self.assertRaises(HarnessError):
            goal_inputs.batches(trial)

    def test_cross_runtime_duplicate_keeps_winning_originals(self):
        goal = self.create("race")
        second = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        original = long_horizon.chat_lab.keep_attachments

        def stage(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(timeout=10)
            return result

        payload = {
            "request_id": "race-once",
            "text": "Use evidence",
            "attachments": [wire()],
        }
        with (
            mock.patch.object(
                long_horizon.chat_lab, "keep_attachments", side_effect=stage
            ),
            mock.patch.object(self.runtime, "start_background"),
            mock.patch.object(second, "start_background"),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(runtime.control, goal["goal_id"], "steer", payload)
                    for runtime in (self.runtime, second)
                ]
                results = [future.result(timeout=15) for future in futures]
        self.assertEqual(results[0]["followup_receipt"], results[1]["followup_receipt"])
        held = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(len(held["followup_inputs"]), 1)
        files = list((self.runtime.store.root / "long-horizon-inputs").rglob("*.txt"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"Portable reproduction evidence")
        self.assertEqual(
            len(
                [
                    event
                    for event in self.runtime.store.events(goal["goal_id"])["events"]
                    if event["type"] == "goal_steered"
                ]
            ),
            1,
        )

    def test_cumulative_bytes_exact_boundary_then_overflow(self):
        goal = self.create("bytes")
        with mock.patch.object(goal_inputs, "MAX_BYTES", 60):
            self.send(goal, "one", [wire(data=b"a" * 30)])
            self.send(goal, "two", [wire(data=b"b" * 30)])
            before = self.runtime.store.get(goal["goal_id"])
            with self.assertRaises(HarnessError):
                self.send(goal, "three", [wire(data=b"c")])
        after = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(
            [Path(one["path"]).read_bytes() for one in goal_inputs.descriptors(after)],
            [b"a" * 30, b"b" * 30],
        )

    def test_archive_original_available_to_research_tools_after_restart(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("arbitrary folder/spec.txt", "original ZIP evidence")
        goal = self.create("zip")
        self.send(
            goal, attachments=[wire("bundle.zip", buffer.getvalue(), "application/zip")]
        )
        restored = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        from our_harness.research_tools import attachment_bytes, ResearchTools

        descriptor = goal_inputs.descriptors(restored)[0]
        tools = ResearchTools(
            self.project, attachments=goal_inputs.descriptors(restored)
        )
        result = tools.execute(
            "read_archive",
            {
                "path": "attachment://" + descriptor["sha256"],
                "member": "arbitrary folder/spec.txt",
            },
        )
        self.assertIn("original ZIP evidence", str(result))
        self.assertEqual(attachment_bytes(descriptor), buffer.getvalue())
        with zipfile.ZipFile(io.BytesIO(attachment_bytes(descriptor))) as archive:
            self.assertEqual(
                archive.read("arbitrary folder/spec.txt"), b"original ZIP evidence"
            )

    def test_missing_original_fails_before_provider(self):
        goal = self.create("missing")
        self.send(goal)
        descriptor = goal_inputs.descriptors(self.runtime.store.get(goal["goal_id"]))[0]
        Path(descriptor["path"]).unlink()
        with mock.patch.object(long_horizon.chat_lab, "ask_once") as provider:
            self.runtime.run(goal["goal_id"])
            provider.assert_not_called()
        held = self.runtime.store.get(goal["goal_id"])
        self.assertTrue(
            any("missing" in str(task.get("last_error")) for task in held["tasks"])
        )

    def test_authorized_reconnect_preserves_receipt_and_inputs(self):
        from our_harness import provider_reconnect

        goal = self.create("reconnect")
        accepted = self.send(goal)
        self.runtime.store.control(goal["goal_id"], "pause")
        held = self.runtime.store.get(goal["goal_id"])
        before = [one["route_binding"] for one in held["agents"]]
        after = copy.deepcopy(before)
        after[0]["effective_dispatch_fingerprint_sha256"] = "f" * 64
        reviewed = {
            "goal_id": goal["goal_id"],
            "revision": held["revision"],
            "before": before,
            "after": after,
        }
        # The provider executable discovery boundary supplies an already reviewed
        # compatible transport update; exercise the actual durable reconnect.
        with mock.patch.object(provider_reconnect, "goal_routes", return_value=after):
            self.runtime.store.reconnect_provider_setup(
                reviewed, "fixture-reviewed-transport"
            )
        changed = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(
            goal_inputs.receipt(
                changed, "once", accepted["followup_receipt"]["submission_sha256"]
            ),
            accepted["followup_receipt"],
        )
        self.assertEqual(
            Path(goal_inputs.descriptors(changed)[0]["path"]).read_bytes(),
            b"Portable reproduction evidence",
        )

    def test_word_evidence_is_separate_and_survives_restart(self):
        from test_document_text import make_docx

        raw = make_docx("Specific document evidence for the arbitrary project")
        goal = self.create("word")
        self.send(
            goal, attachments=[wire("spec.docx", raw, "application/octet-stream")]
        )
        held = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertIn("Specific document evidence", goal_inputs.evidence(held))
        self.assertNotIn("Specific document evidence", held["objective"])
        self.assertEqual(
            Path(goal_inputs.descriptors(held)[0]["path"]).read_bytes(), raw
        )

    def test_explicit_fork_inherits_bytes_without_parent_receipt_authority(self):
        goal = self.create("fork-source")
        accepted = self.send(goal)
        self.runtime.store.control(goal["goal_id"], "pause")
        source = self.runtime.store.get(goal["goal_id"])
        target = self.base / "different fork project"
        target.mkdir()
        child = self.runtime.store.clone_to_project(
            source, "child", "Child", target, "fork-request"
        )
        held = self.runtime.store.get(child["goal_id"])
        self.assertIn("Portable reproduction evidence", goal_inputs.evidence(held))
        self.assertEqual(
            Path(goal_inputs.descriptors(held)[0]["path"]).read_bytes(),
            b"Portable reproduction evidence",
        )
        self.assertIsNone(
            goal_inputs.receipt(
                held, "once", accepted["followup_receipt"]["submission_sha256"]
            )
        )
        new = self.send(child, "child-files")
        self.assertEqual(new["followup_receipt"]["goal_id"], child["goal_id"])
        self.assertEqual(
            len(goal_inputs.descriptors(self.runtime.store.get(child["goal_id"]))), 2
        )
        self.assertEqual(
            goal_inputs.receipt(
                self.runtime.store.get(goal["goal_id"]),
                "once",
                accepted["followup_receipt"]["submission_sha256"],
            ),
            accepted["followup_receipt"],
        )


if __name__ == "__main__":
    unittest.main()
