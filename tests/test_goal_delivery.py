from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import shutil
import subprocess
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
        self.assertIn(str((self.source / "arbitrary game/start.html").resolve()),
                      chat._long_horizon_status_text(reopened))
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

    def test_facilitator_completion_and_historical_folder_controls_render_actual_locations(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the panel behavior check")
        direct = {**self.goal, "execution_mode": "facilitator", "status": "complete",
                  "verification": {"status": "failed", "reason": "A check failed"}}
        direct.pop("execution_workspace")
        text = chat._long_horizon_status_text(direct)
        metadata = goal_delivery.status_metadata(direct)
        source = Path(__file__).resolve().parents[1] / "src/our_harness/ui/app.js"
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const payload = JSON.parse(process.argv[2]), opened = [];
const element = () => ({dataset:{}, append(){}, setAttribute(){}});
const context = {make:element, aChatTurnFace:element, appendLongHorizonGoalLink(){}, appendChatText(){},
  locationOpenButton(path){opened.push(path); return element();}};
vm.createContext(context);
for(const name of ['aChatGoalCompletionRow','goalCommandLocation','historicalWorkingFolder','facilitatorCompletionDetail']) {
 const start = source.indexOf('function '+name+'('); assert(start >= 0);
 const end = source.indexOf('\n}',start)+2;
 vm.runInContext(source.slice(start,end),context);
}
const correlation = {goalId:'portable',executionMode:'facilitator',verificationStatus:'failed',deliveryLocations:JSON.parse(payload.meta.delivery_locations)};
context.aChatGoalCompletionRow(payload.text,'',correlation,'chat');
assert.deepEqual(opened,[payload.project]);
opened.length=0;
context.aChatGoalCompletionRow('Working folder: an untrusted path','',correlation,'chat');
assert.deepEqual(opened,[]);
context.aChatGoalCompletionRow('Delivery checked in: '+payload.project,'',{goalId:'old',deliveryLocations:[payload.project]},'chat');
assert.deepEqual(opened,[payload.project]);
const preview = context.goalCommandLocation({project_path:payload.project,cwd:'subdir',timeout_seconds:12},{execution_mode:'facilitator'});
assert(preview.includes(payload.project) && preview.includes('subdir') && preview.includes('12 seconds') && !preview.includes('undefined'));
assert(context.facilitatorCompletionDetail({verification:{status:'failed'},workspace_path:payload.project}).includes('Checks failed'));
const evidence = {goal_id:'old',delivery_state:'working_copy_report'};
assert.equal(context.historicalWorkingFolder(evidence,[{goal_id:'old',workspace_path:'old copy'}]),'old copy');
assert.equal(context.historicalWorkingFolder(evidence,[{goal_id:'old',execution_mode:'facilitator',workspace_path:'real project',retained_workspaces:[{path:'retained copy'}]}]),'retained copy');
console.log('FACILITATOR_UI_BEHAVIOR_PASSED');
"""
        result = subprocess.run([node, "-e", script, str(source), json.dumps({
            "text": text, "meta": metadata, "project": str(self.source)})], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FACILITATOR_UI_BEHAVIOR_PASSED", result.stdout)
