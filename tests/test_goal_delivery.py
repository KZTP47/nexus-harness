from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import chat, goal_delivery, goal_workspaces, swarm_work
from our_harness.models import HarnessError


class GoalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "another user's selected project"
        self.source.mkdir()
        self.goal = {"goal_id": "portable-goal", "project_authority_id": "portable-authority",
                     "project": {"path": str(self.source)}, "tasks": [{"criteria_evidence": [
                         {"evidence_refs": ["file:arbitrary game/start.html"]}]}]}
        self.runtime = self.base / "runtime"
        self.goal["execution_workspace"] = goal_workspaces.create(self.goal, self.runtime)
        self.work = goal_workspaces.root(self.goal, self.runtime)
        (self.work / "arbitrary game").mkdir()
        (self.work / "arbitrary game/start.html").write_text("<main>Delivered game</main>")
        _, self.manifest = swarm_work._project_tree_merkle(self.work)

    def test_working_copy_cannot_supply_a_delivery_receipt_but_published_readback_can(self):
        with self.assertRaisesRegex(HarnessError, "cannot be read"):
            goal_delivery.receipt(self.goal, self.manifest)
        with goal_workspaces.publication(self.goal, self.runtime):
            pending = goal_workspaces.prepare_publish(self.goal, self.runtime)
            goal_workspaces.publish(self.goal, self.runtime, pending)
            receipt = goal_delivery.receipt(self.goal, self.manifest)
        recovered_manifest = goal_workspaces.published_file_manifest(json.loads(json.dumps(self.goal)), self.runtime)
        self.assertEqual(recovered_manifest, self.manifest)
        self.assertEqual(goal_delivery.receipt(self.goal, recovered_manifest), receipt)
        self.assertEqual(receipt["state"], "delivered")
        self.assertEqual(receipt["files"], [{"path": "arbitrary game/start.html",
                                           "sha256": self.manifest["arbitrary game/start.html"][5:]}])
        reopened = json.loads(json.dumps({**self.goal, "delivery_receipt": receipt, "status": "complete"}))
        self.assertIn(str(self.source / "arbitrary game/start.html"), chat._long_horizon_status_text(reopened))
        # Reopening completed history must not endorse an earlier agent claim.
        self.assertEqual(goal_delivery.report_metadata(reopened)["delivery_state"], "working_copy_report")
        (self.source / "arbitrary game/start.html").write_text("changed after publication")
        with self.assertRaisesRegex(HarnessError, "differs"):
            goal_delivery.receipt(reopened, self.manifest)
        (self.source / "arbitrary game/start.html").unlink()
        with self.assertRaisesRegex(HarnessError, "cannot be read"):
            goal_delivery.receipt(reopened, self.manifest)

    def test_missing_and_escaping_references_cannot_be_receipts(self):
        for path in ["missing.html", "../outside.html", "C:/another-project/result.html"]:
            with self.subTest(path=path):
                goal = copy.deepcopy(self.goal)
                goal["tasks"][0]["criteria_evidence"][0]["evidence_refs"] = ["file:" + path]
                with self.assertRaises(HarnessError):
                    goal_delivery.receipt(goal, self.manifest)

    def test_changed_destination_does_not_reuse_binding(self):
        first = {**self.goal, "tasks": [], "execution_workspace": None}
        one = goal_delivery.receipt(first, {})
        other = self.base / "different machine folder"
        other.mkdir()
        two = goal_delivery.receipt({**first, "project": {"path": str(other)}}, {})
        self.assertNotEqual(one["binding_sha256"], two["binding_sha256"])
        self.assertEqual(goal_delivery.report_metadata(first), {})

    def test_publication_files_are_listed_even_without_provider_file_references(self):
        self.goal["tasks"] = []
        with goal_workspaces.publication(self.goal, self.runtime):
            pending = goal_workspaces.prepare_publish(self.goal, self.runtime)
            self.goal["workspace_publication"] = goal_workspaces.publish(self.goal, self.runtime, pending)
            receipt = goal_delivery.receipt(self.goal, self.manifest)
        self.assertEqual([one["path"] for one in receipt["files"]], ["arbitrary game/start.html"])
