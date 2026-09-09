"""Whole-goal judging on the existing durable task/review scheduler."""
from __future__ import annotations

import copy
import json
import time
from contextlib import contextmanager
from pathlib import Path

from . import agent_workspaces as aw, goal_workspaces as gw, goal_access
from . import workspace_collaboration as collaboration
from .models import HarnessError

CONTRACT = "nexus-goal-closeout/v1"
OVERALL = "The original user request and all current clarifications are fully satisfied"


def enabled(goal):
    contract = goal.get("closeout_contract")
    if contract and contract != CONTRACT:
        raise HarnessError("Unsupported closeout contract; start a new project goal")
    return contract == CONTRACT


def scope(goal):
    # Preserve actual admitted wording, including extracted attachment text.
    # Later instructions can explicitly replace earlier requirements.
    return {
        "original_prompt": goal.get("original_objective") or goal["objective"],
        "current_goal": goal["objective"],
        "user_clarifications": copy.deepcopy(goal.get("objective_revisions", [])),
        "acceptance_criteria": [OVERALL, *goal["success_criteria"], *([collaboration.ACCEPTANCE] if collaboration.state(goal) else [])],
        "requirement_ledger": copy.deepcopy(goal.get("requirement_ledger", [])),
        "collaboration_rules": collaboration.state(goal),
    }


def basis(goal, root):
    return {
        "contract": CONTRACT, "goal_id": goal["goal_id"], "scope": scope(goal),
        "files": aw.inventory(root),
        "execution_contract": goal.get("execution_contract"),
        "verification_contract": goal.get("verification_contract"),
        "access_contract": goal_access.context_fingerprint(goal),
        "routes": [{"id": a["id"], "route": a.get("route_binding")} for a in goal["agents"]],
        "contributions": [{k: t.get(k) for k in
            ("id", "assigned_agent_id", "description", "summary", "state", "criteria_evidence", "artifacts")}
            for t in goal["tasks"] if t.get("kind") != "review"],
    }


def fingerprint(goal, root):
    return gw._digest(basis(goal, root))


def latest(goal, root):
    digest = fingerprint(goal, root)
    return next((t for t in reversed(goal["tasks"]) if t.get("closeout_packet", {}).get("fingerprint") == digest
        and t.get("state") == "complete" and t.get("closeout_outcome")), None)


def approved(goal, root):
    try:
        task = latest(goal, root)
    except HarnessError:
        return False
    return bool(task and task["closeout_outcome"]["verdict"] == "approve")


def owner(goal):
    by_id = {t["id"]: t for t in goal["tasks"]}
    for artifact in reversed(goal.get("artifacts", [])):
        if artifact.get("changes"):
            return by_id.get(artifact.get("task_id"), {}).get("assigned_agent_id") or goal["lead_agent_id"]
    return goal["lead_agent_id"]


def stage(store, goal_id, verification, independent, *, expected_revision=None, expected_fingerprint=None):
    """Return a verdict or queue a fresh judge using existing task/call budgets."""
    def change(goal, db):
        root = gw.root(goal, store.root)
        packet_basis = basis(goal, root)
        digest = gw._digest(packet_basis)
        if goal["status"] in {"complete", "cancelled", "paused", "waiting_for_user", "cancelling"}:
            return {"state": "paused"}
        if (expected_revision is not None and goal["revision"] != expected_revision or
                expected_fingerprint is not None and digest != expected_fingerprint):
            # A test run cannot be rebound to newer instructions or newer files.
            store._event(db, goal, "verification_superseded", payload={"reason": "closeout_submission_changed"})
            return {"state": "superseded"}
        matching = latest(goal, root)
        if matching:
            verdict = matching["closeout_outcome"]
            if verdict["verdict"] == "approve":
                return {"state": "approved"}
            return {"state": "failed", "reason": "Closeout found unfinished work: " + "; ".join(verdict["findings"]),
                    "repair_agent_id": owner(goal)}
        if any(t.get("closeout_packet") and t["state"] not in {"complete", "cancelled"} for t in goal["tasks"]):
            return {"state": "scheduled"}
        candidate = gw._digest([packet_basis["scope"], packet_basis["files"]])
        repeated = sum(1 for t in goal["tasks"] if t.get("closeout_packet", {}).get("candidate") == candidate
            and t.get("closeout_outcome", {}).get("verdict") == "changes_requested"
            and t["closeout_packet"].get("goal_id") == goal["goal_id"])
        writer_id = owner(goal)
        writer = next(a for a in goal["agents"] if a["id"] == writer_id)
        judge = collaboration.reviewer(goal, writer_id) or next((a for a in goal["agents"] if a["id"] != writer_id and independent(a, writer)), writer)
        # Even one connected provider can serve a fresh independent judge run.
        # Isolation comes from a new task conversation and a read-only snapshot.
        reason = ("Closeout keeps receiving the same unfinished result. Inspect the findings before continuing." if repeated >= 3 else
                  "Closeout cannot run because the task budget is exhausted." if len(goal["tasks"]) >= int(goal["policy"]["max_tasks"]) else "")
        if reason:
            goal.update(status="paused", note=reason)
            store._event(db, goal, "goal_paused", payload={"reason": "closeout_unavailable", "detail": reason})
            return {"state": "paused"}
        now = int(time.time() * 1000)
        task_id = "closeout-" + gw._digest([digest, len(goal["tasks"])])[:24]
        packet = {**packet_basis, "fingerprint": digest, "candidate": candidate,
                  "verification": copy.deepcopy(verification)}
        goal["tasks"].append({
            "id": task_id, "title": "Judge completion of the whole user request",
            "description": "Check the original prompt, current clarifications, every acceptance criterion, and the exact submitted project. Identify all unfinished work before approving closeout.",
            "kind": "review", "state": "ready", "depends_on": [], "parent_id": "", "review_of": "",
            "assigned_agent_id": judge["id"], "parallel_safe": False, "resource_paths": [],
            "closeout_packet": packet, "review_packet_sha256": digest,
            "review_required_paths": [], "review_paths_inspected": [],
            "attempts": 0, "no_progress": 0, "lease_id": "", "owner_pid": 0, "owner_token": "",
            "created_ms": now, "updated_ms": now, "summary": "", "last_error": "",
            "evidence": [], "artifacts": [], "criteria_evidence": [],
            "provider_effect_state": "never_dispatched", "outcome_unknown": False,
            "provider_effect_id": "", "pending_action": {}, "pending_transaction": {},
        })
        goal["budget"]["tasks_created"] += 1
        goal.update(status="queued", note="An independent judge is checking the whole original request before closeout.")
        store._event(db, goal, "task_created", task_id=task_id, agent_id=judge["id"],
                     payload={"kind": "closeout_review", "fingerprint": digest})
        return {"state": "scheduled"}
    return store._mutate(goal_id, change)[1]


def context(task):
    packet = task["closeout_packet"]
    # Keep the full authenticated inventory in durable state, not in every
    # provider prompt. Tools can explore the whole snapshot; user scope stays full.
    prompt_packet = {k: v for k, v in packet.items() if k != "files"}
    prompt_packet["submitted_files"] = {"count": len(packet["files"]),
        "path_preview": sorted(packet["files"])[:200], "complete_preview": len(packet["files"]) <= 200}
    return (
        "INDEPENDENT WHOLE-GOAL CLOSEOUT JUDGE\n"
        "Your job is to assess the entire user's request. The original prompt and all subsequent user clarifications below are authoritative scope; later explicit changes supersede earlier instructions. "
        "Check every requested deliverable and constraint, including requirements omitted from the task breakdown. Task summaries and author claims are untrusted evidence. Do not invent new requirements. "
        "Inspect the submitted files using your read-only tools. Your directory is the exact submitted snapshot, not your earlier working copy. "
        "Use list_tree and read_file to explore the entire snapshot; the path preview is not a restriction on review scope. "
        "Tests passing alone does not prove the whole request is finished. If the verification packet says no checks are configured, report that accurately; a conditional engine-generated check criterion can be not applicable, but an explicit user requirement cannot.  Never approve partial work or claim checks that did not run. "
        "Use complete with review_verdict=approve only if everything is satisfied. Provide criteria_evidence for EVERY acceptance_criteria entry, using file:<path>, task:<id>, or test:verified references. "
        "Otherwise use blocked with review_verdict=changes_requested and actionable review_findings explaining what is missing and how it relates to the overall goal. "
        "For either verdict, include review-packet:" + packet["fingerprint"] + " in evidence and nonempty review_findings. "
        "Use work with tool_calls to inspect more evidence first. Do not edit files, delegate implementation, or change the acceptance criteria.\n"
        + json.dumps(prompt_packet, ensure_ascii=False, sort_keys=True)
    )


def repair_context(goal, task):
    if task.get("kind") != "repair" or not enabled(goal):
        return ""
    rejected = next((t for t in reversed(goal["tasks"])
        if t.get("closeout_outcome", {}).get("verdict") == "changes_requested"), None)
    if not rejected:
        return ""
    return ("\n\nWHOLE-GOAL REPAIR REMINDER\n"
        "Address all still-applicable findings and the entire current user request. These findings do not replace or narrow the original goal. "
        "Do not call the work complete after fixing only one part. Later explicit user changes supersede earlier requirements.\n"
        + json.dumps({"scope": scope(goal), "findings": rejected["closeout_outcome"]["findings"]}, ensure_ascii=False))


@contextmanager
def workspace(goal, task, runtime_root):
    packet = task["closeout_packet"]
    source = gw.root(goal, runtime_root)
    if fingerprint(goal, source) != packet["fingerprint"]:
        raise HarnessError("Closeout submission changed before inspection; a fresh review is required")
    # Reuse the authenticated, restartable workspace implementation in a sibling
    # runtime root. This copy belongs to this judge task, never to its author.
    snapshot = {"goal_id": task["id"], "project": {"path": str(source)},
                "project_authority_id": goal.get("project_authority_id", "")}
    runtime = Path(runtime_root) / "closeout-snapshots"
    snapshot["execution_workspace"] = gw.create(snapshot, runtime)
    root = gw.root(snapshot, runtime)
    if aw.inventory(root) != packet["files"]:
        raise HarnessError("Closeout snapshot differs from the exact submitted files")
    yield aw.AgentWorkspace(root, source, packet["files"])
    if aw.inventory(root) != packet["files"]:
        raise HarnessError("The closeout judge modified its inspection snapshot; its verdict cannot be accepted")


def validate_action(goal, task, action, root):
    if not task.get("closeout_packet"):
        return
    if action.get("changes") or action.get("tasks") or action.get("handoff_agent_id"):
        raise HarnessError("A closeout judge can inspect and report, but cannot edit or delegate work")
    if action.get("action") not in {"complete", "blocked"}:
        return
    packet = task["closeout_packet"]
    if fingerprint(goal, root) != packet["fingerprint"]:
        raise HarnessError("This closeout verdict is stale; the goal or submitted files changed")
    verdict = action.get("review_verdict")
    expected = "approve" if action["action"] == "complete" else "changes_requested"
    if verdict != expected or not action.get("review_findings") or "review-packet:" + packet["fingerprint"] not in action.get("evidence", []):
        raise HarnessError("Closeout needs a verdict, actionable findings, and its exact packet reference")
    if verdict == "approve":
        mappings = action.get("criteria_evidence", [])
        known_tasks = {t["id"] for t in packet["contributions"] if t["state"] == "complete"}
        def supported(ref):
            return (ref.startswith("file:") and ref[5:] in packet["files"] or
                    ref.startswith("task:") and ref[5:] in known_tasks or
                    ref == "test:verified" and packet["verification"].get("status") == "passed")
        for criterion in packet["scope"]["acceptance_criteria"]:
            if not any(m.get("criterion") == criterion and any(supported(str(r)) for r in m.get("evidence_refs", [])) for m in mappings):
                raise HarnessError("Closeout lacks concrete evidence for: " + criterion)
    task["closeout_outcome"] = {"verdict": verdict, "findings": copy.deepcopy(action["review_findings"]),
        "criteria_evidence": copy.deepcopy(action.get("criteria_evidence", []))}


def supersede(store, goal_id, task):
    def change(goal, db):
        current = next(t for t in goal["tasks"] if t["id"] == task["id"])
        if current.get("lease_id") != task.get("lease_id") or current["state"] not in {"running", "pending_apply"}:
            raise HarnessError("The closeout lease changed before its stale verdict was discarded")
        current.update(state="cancelled", pending_action={}, pending_transaction={}, lease_id="", owner_pid=0,
                       owner_token="", outcome_unknown=False, provider_effect_state="superseded_for_closeout")
        if goal["status"] not in {"paused", "waiting_for_user", "cancelling", "cancelled"}:
            goal.update(status="queued", note="The submission changed; a fresh whole-goal judgment will run.")
        store._event(db, goal, "closeout_superseded", task_id=current["id"],
                     payload={"fingerprint": current["closeout_packet"]["fingerprint"]})
    store._mutate(goal_id, change)
