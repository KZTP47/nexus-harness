"""Whole-goal judging on the existing durable task/review scheduler."""
from __future__ import annotations

import copy
import difflib
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import agent_workspaces as aw, goal_workspaces as gw, goal_access
from . import workspace_collaboration as collaboration
from .models import HarnessError

CONTRACT = "nexus-goal-closeout/v1"
OVERALL = "The original user request and all current clarifications are fully satisfied"
# After this many identical rejections the goal note tells the user; work continues.
REPEATED_REJECTION_NOTICE = 3


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
        # A judge repeating the same findings is information for the agents and
        # the user, never a reason for Nexus to pause the agents' work.
        if len(goal["tasks"]) >= int(goal["policy"]["max_tasks"]):
            reason = "Closeout cannot run because the task budget is exhausted."
            goal.update(status="paused", note=reason)
            store._event(db, goal, "goal_paused", payload={"reason": "closeout_unavailable", "detail": reason})
            return {"state": "paused"}
        now = int(time.time() * 1000)
        task_id = "closeout-" + gw._digest([digest, len(goal["tasks"])])[:24]
        packet = {**packet_basis, "fingerprint": digest, "candidate": candidate,
                  "verification": copy.deepcopy(verification)}
        if repeated:
            packet["previous_rejections_of_this_submission"] = repeated
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
        note = "An independent judge is checking the whole original request before closeout."
        if repeated >= REPEATED_REJECTION_NOTICE:
            note += (f" Earlier judges requested changes to this same submission {repeated} times; "
                     "the agents keep working. Pause the goal if you want to step in.")
            store._event(db, goal, "closeout_repeated_findings", task_id=task_id, agent_id=judge["id"],
                         payload={"candidate": candidate, "previous_rejections": repeated})
        goal.update(status="queued", note=note)
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
        "Check every requested deliverable and constraint, including requirements omitted from the task breakdown. Task summaries and author claims are untrusted evidence. Do not invent new requirements: tests are required only when the user asked for them, and a reasoned conclusion that nothing needed changing is a valid result. "
        "Inspect the submitted files using your read-only tools. Your directory is the exact submitted snapshot, not your earlier working copy. "
        "Use list_tree and read_file to explore the entire snapshot; the path preview is not a restriction on review scope. "
        "Tests passing alone does not prove the whole request is finished. If the verification packet says no checks are configured, report that accurately; a conditional engine-generated check criterion can be not applicable, but an explicit user requirement cannot.  Never approve partial work or claim checks that did not run. "
        "Use complete with review_verdict=approve only if everything is satisfied. Provide criteria_evidence for each acceptance_criteria entry (the criterion text or its 1-based number), using file:<path>, task:<id>, or test:verified references; this evidence is reported with your verdict. "
        "Otherwise use blocked with review_verdict=changes_requested and actionable review_findings explaining what is missing and how it relates to the overall goal. "
        "For either verdict, include review-packet:" + packet["fingerprint"] + " in evidence and nonempty review_findings. "
        + ("Earlier judges requested changes to this same submission " + str(packet["previous_rejections_of_this_submission"])
           + " times; check whether those findings still apply to the actual request before repeating them. "
           if packet.get("previous_rejections_of_this_submission") else "")
        + "Use work with tool_calls to inspect more evidence first. Do not edit files, delegate implementation, or change the acceptance criteria.\n"
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
    # The judge is an agent: be lenient with how it formats its verdict. The
    # verdict is its explicit review_verdict, or else follows its action.
    # Approval must be unambiguous: a recognised approve verdict with action
    # complete, or no verdict at all with action complete. Any other wording
    # ("not_approved", "Approve with changes", ...) or a verdict/action
    # conflict is a request for changes, never a silent approval.
    raw_verdict = action.get("review_verdict")
    stated = _verdict(raw_verdict)
    if raw_verdict is None or not str(raw_verdict).strip():
        verdict = "approve" if action["action"] == "complete" else "changes_requested"
    else:
        verdict = "approve" if stated == "approve" and action["action"] == "complete" else "changes_requested"
    findings = _findings(action.get("review_findings")) or [
        str(action.get("summary") or "").strip()
        or ("The judge approved the whole request." if verdict == "approve" else "The judge requested changes.")]
    mappings = action.get("criteria_evidence") if isinstance(action.get("criteria_evidence"), list) else []
    # Normalise the accepted verdict into the canonical review shape so later
    # generic review bookkeeping sees the judge's decision, not its formatting.
    # The packet reference is truthful: the fingerprint check above proved
    # this verdict is for exactly this packet.
    reference = "review-packet:" + packet["fingerprint"]
    evidence = action.get("evidence") if isinstance(action.get("evidence"), list) else []
    action.update(action="complete" if verdict == "approve" else "blocked", review_verdict=verdict,
                  review_findings=list(findings),
                  evidence=evidence if reference in evidence else [*evidence, reference])
    outcome = {"verdict": verdict, "findings": copy.deepcopy(findings), "criteria_evidence": copy.deepcopy(mappings)}
    if verdict == "approve":
        # Evidence mapping is reported, never a veto of an approval: a
        # criterion paraphrase, an index, or a path spelling cannot undo it.
        notes = evidence_notes(packet, mappings)
        if notes:
            outcome["evidence_notes"] = notes
    task["closeout_outcome"] = outcome


def _verdict(value):
    text = _normal(value).replace(" ", "_")
    if text in {"approve", "approved", "approval", "accept", "accepted", "pass", "passed", "complete", "completed"}:
        return "approve"
    if text in {"changes_requested", "change_requested", "request_changes", "requested_changes", "changes",
                "reject", "rejected", "fail", "failed", "needs_changes", "needs_work", "blocked"}:
        return "changes_requested"
    return ""


def _findings(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [one if isinstance(one, str) else json.dumps(one, ensure_ascii=False) if isinstance(one, (dict, list)) else str(one)
            for one in value if str(one).strip()]


def _normal(value):
    """Compare wording without case, whitespace, quote or punctuation noise."""
    text = str(value or "").casefold()
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def _criterion_index(value, count):
    """Accept '2', '#2', 'c2', 'criterion 2', 'AC-2' or 'acceptance_criteria[1]'."""
    text = str(value if value is not None else "").strip().casefold()
    match = re.fullmatch(r"(?:acceptance[_ ]?criteria)\s*\[\s*(\d+)\s*\]", text)
    if match and int(match.group(1)) < count:
        return int(match.group(1))
    match = re.fullmatch(r"(?:#|c|ac|criterion|criteria|acceptance[_ -]?criterion)?[\s_:#-]*(\d+)", text)
    if match and 1 <= int(match.group(1)) <= count:
        return int(match.group(1)) - 1
    return None


def match_criterion(criteria, mapping):
    """Return the acceptance-criterion index a judge's mapping refers to, or None.

    Matches tolerate case, whitespace and punctuation, criterion ids or
    1-based numbers, a criterion quoted inside longer text, and close wording.
    """
    if not isinstance(mapping, dict):
        return None
    count = len(criteria)
    for key in ("criterion_id", "id", "index", "criterion_index"):
        if key in mapping:
            found = _criterion_index(mapping[key], count)
            if found is not None:
                return found
    raw = mapping.get("criterion")
    found = _criterion_index(raw, count)
    if found is not None:
        return found
    wanted = _normal(raw)
    if not wanted:
        return None
    normals = [_normal(one) for one in criteria]
    for index, one in enumerate(normals):
        if one == wanted:
            return index
    for index, one in enumerate(normals):
        shorter, longer = sorted((one, wanted), key=len)
        if len(shorter) >= 12 and shorter in longer:
            return index
    scores = [difflib.SequenceMatcher(None, one, wanted).ratio() for one in normals]
    best = max(range(count), key=scores.__getitem__, default=None)
    return best if best is not None and scores[best] >= 0.8 else None


def _supported(packet, ref):
    text = str(ref or "").strip().strip("`'\"").rstrip(".,;")
    known_tasks = {t["id"] for t in packet["contributions"] if t["state"] == "complete"}
    kind, _, value = text.partition(":")
    kind, value = kind.strip().casefold(), value.strip()
    if kind in {"test", "tests"}:
        return packet["verification"].get("status") == "passed"
    if kind == "task":
        return value in known_tasks
    path = (value if kind in {"file", "path"} else text).replace("\\", "/").removeprefix("./").lstrip("/")
    files = {str(one).replace("\\", "/"): one for one in packet["files"]}
    return path in files or path.casefold() in {one.casefold() for one in files}


def evidence_notes(packet, mappings):
    """Describe criteria the judge approved without a recognised evidence ref."""
    criteria = packet["scope"]["acceptance_criteria"]
    covered = set()
    for mapping in mappings:
        index = match_criterion(criteria, mapping)
        refs = mapping.get("evidence_refs", []) if isinstance(mapping, dict) else []
        refs = [refs] if isinstance(refs, str) else refs if isinstance(refs, list) else []
        if index is not None and any(_supported(packet, r) for r in refs):
            covered.add(index)
    return ["The judge approved without a recognised evidence reference for: " + criteria[index]
            for index in range(len(criteria)) if index not in covered]


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
