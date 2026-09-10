"""User-selected collaboration and exact, independently runnable submissions.

Native permissions are not OS containment. Nexus enforces its own mutations and
acceptance boundary; fixed reviewers use the provider's inspection profile.
"""
from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path

from . import agent_workspaces as aw, goal_workspaces as gw, goal_access
from .changes import FileTransaction
from .models import HarnessError
from .safety import ProjectTransactionLock

CONTRACT = "workspace-collaboration/v1"
ACCEPTANCE = "The selected collaboration roles and workspace editing rules are satisfied"
TOOLS = {"workspace_catalog", "workspace_read", "workspace_edit", "workspace_snapshot", "workspace_verify"}


class SupersededReview(HarnessError):
    pass


def supersede_review(store, goal_id, task):
    def change(goal, db):
        review = next(t for t in goal["tasks"] if t["id"] == task["id"])
        if review["lease_id"] != task["lease_id"]:
            raise HarnessError("The review lease changed")
        parent = next(t for t in goal["tasks"] if t["id"] == review["review_of"])
        for current, state in [(review, "cancelled"), (parent, "ready")]:
            current.update(state=state, pending_action={}, pending_transaction={}, lease_id="", owner_pid=0,
                owner_token="", outcome_unknown=False, provider_effect_state="superseded_for_review")
        parent.pop("review_approved_effect_id", None)
        parent["evidence"].append("The submitted project changed after review began. Reconcile current files and submit a fresh version for review.")
        store._event(db, goal, "review_superseded", task_id=review["id"], payload={"author_task":parent["id"]})
    store._mutate(goal_id, change)


def normalize(goal, value):
    value = value or {}
    if not isinstance(value, dict):
        raise HarnessError("Collaboration settings must be an object")
    mode = value.get("mode", "flexible")
    ids = [a["id"] for a in goal["agents"]]
    writer = value.get("writer_id") or goal["lead_agent_id"]
    reviewer = value.get("reviewer_id") or next((i for i in ids if i != writer), "")
    if not isinstance(mode, str) or mode not in {"flexible", "fixed"} or writer not in ids or reviewer and reviewer not in ids:
        raise HarnessError("Choose a supported collaboration mode and members of this team")
    if mode == "fixed" and (not reviewer or reviewer == writer):
        raise HarnessError("Fixed roles require a different writer and reviewer")
    direct = value.get("allow_direct_real_edits", False)
    if not isinstance(direct, bool):
        raise HarnessError("Direct project editing must be an explicit checkbox choice")
    return {"contract": CONTRACT, "mode": mode, "writer_id": writer, "reviewer_id": reviewer,
            "allow_direct_real_edits": direct}


def state(goal):
    held = goal.get("workspace_collaboration")
    if held is None:
        return None
    value = normalize(goal, held)
    if held.get("contract") != CONTRACT or held.get("fingerprint") != gw._digest({
            "goal_id": goal["goal_id"], "project": goal["project"], "agents": [{k: a.get(k) for k in ("id", "who", "route_binding")} for a in goal["agents"]], "execution_contract": goal.get("execution_contract"), "settings": value}):
        raise HarnessError("The collaboration settings changed ownership or contract; configure this goal again")
    return value


def install(goal, value=None):
    settings = normalize(goal, value)
    goal["workspace_collaboration"] = {**settings, "fingerprint": gw._digest({
        "goal_id": goal["goal_id"], "project": goal["project"], "agents": [{k: a.get(k) for k in ("id", "who", "route_binding")} for a in goal["agents"]], "execution_contract": goal.get("execution_contract"), "settings": settings})}


def can_write(goal, task, workspace_id=None):
    if goal_access.state(goal)["mode"] == "read_only" or task.get("kind") == "review":
        return False
    policy = state(goal)
    actor = task["assigned_agent_id"]
    if not policy:
        return workspace_id in {None, actor}
    if policy["mode"] == "fixed" and actor != policy["writer_id"]:
        return False
    if workspace_id == "real":
        return policy["allow_direct_real_edits"]
    return workspace_id is None or workspace_id == actor or policy["mode"] == "flexible"


def validate_action(goal, task, action):
    if state(goal) and action.get("changes") and not can_write(goal, task):
        raise HarnessError("This agent's selected role permits inspection and feedback, not file changes")


def reviewer(goal, author_id):
    policy = state(goal)
    if policy and policy["mode"] == "fixed":
        return next(a for a in goal["agents"] if a["id"] == policy["reviewer_id"])
    return None


def prompt(goal, task, runtime_root):
    policy = state(goal)
    if not policy:
        return ""
    catalog = [{**one, "may_edit": can_write(goal, task, one["id"])} for one in aw.descriptors(goal, runtime_root)]
    return ("\n\nUSER-SELECTED COLLABORATION\n" + json.dumps({"settings": policy, "workspaces": catalog}, ensure_ascii=False)
        + "\nAll team members may inspect these project copies. Use workspace_catalog, workspace_read and workspace_snapshot without waiting for a formal handoff. "
        "workspace_edit changes a permitted draft using its exact fingerprint; workspace_verify tests an immutable snapshot in a disposable verification directory. "
        "Use shared conversation to exchange feedback, request tasks, and agree who is editing which files. Flexible roles may change as the task requires. "
        "Investigate routine technical blockers together before asking the user. Inspect the real project through workspace_catalog and workspace_read when files added after the initial copy are missing. "
        "Read referenced documents and inspect or extract archives with the available project tools in a permitted workspace, then continue the task. "
        "Do not ask the user to copy, synchronize, or extract files that the team can access and prepare itself. Ask only for a decision, missing information, or permission that the tools and existing instructions cannot supply. "
        "Fixed roles reserve writing for the selected writer and approval for the reviewer. These user-selected roles cannot be changed by an agent action. "
        "Only edit workspaces marked may_edit. With direct real-project editing enabled, those edits reach the user before final verification; report that accurately. "
        "Without it, keep changes in the agent copies until Nexus publishes. Native permission checks still apply; copies are not OS security sandboxes. "
        "The final judge checks these settings together with the original prompt and subsequent clarifications.\n")


def update(store, goal_id, expected_revision, value):
    def change(goal, db):
        if goal["revision"] != expected_revision:
            raise HarnessError("The goal changed; refresh the collaboration settings")
        if goal["status"] not in {"paused", "queued", "waiting_for_user"} or store._scheduler_live(goal) or any(
                t["state"] in {"running", "pending_apply"} or t.get("pending_action") for t in goal["tasks"]):
            raise HarnessError("Pause the team and let pending work settle before changing collaboration")
        if not goal.get("agent_workspace_contract"):
            raise HarnessError("Start a new project goal to use collaboration workspaces")
        install(goal, value)
        store._event(db, goal, "workspace_collaboration_updated", payload=state(goal))
    return store.public(store._mutate(goal_id, change)[0])


def owned_root(goal, runtime_root, workspace_id):
    if workspace_id == "real":
        return gw._direct(Path(goal["project"]["path"]))
    agent = next((a for a in goal["agents"] if a["id"] == workspace_id), None)
    if agent is None:
        raise HarnessError("Choose a workspace belonging to this goal")
    root = aw.existing_root(goal, agent, runtime_root)
    if root is None:
        with aw.workspace(goal, agent, runtime_root) as created:
            root = created.root
    return root


def snapshot(goal, runtime_root, source, *, changes=None, identity=""):
    """A content-addressed private copy; signed receipts reject later tampering."""
    from .swarm_work import _validated_changes
    manifest = aw.inventory(source)
    key = gw._digest([CONTRACT, goal["goal_id"], str(source), manifest, changes or [], identity])
    runtime = Path(runtime_root) / "collaboration-snapshots"
    document = {"goal_id": "snapshot-" + key[:40], "project": {"path": str(source)},
                "project_authority_id": goal.get("project_authority_id", "")}
    document["execution_workspace"] = gw.create(document, runtime)
    root = gw.root(document, runtime)
    home, folder = root.parent.parent, root.parent
    receipt_path = folder / "submission.json"
    with ProjectTransactionLock(folder).held():
        if receipt_path.exists():
            receipt = gw._verify(gw._key(home), json.loads(receipt_path.read_text(encoding="utf-8")))
            if receipt.get("key") != key or receipt.get("files") != aw.inventory(root):
                raise HarnessError("The submitted snapshot changed; its review cannot be accepted")
        else:
            # A crash during the transaction is recoverable only through its
            # exact recorded changes; unrelated files are never accepted.
            before = aw.inventory(root)
            if before != manifest:
                raise HarnessError("Snapshot preparation was interrupted; retain it for recovery")
            if changes:
                FileTransaction(root, max_files=100_000, max_bytes=2_000_000_000).apply(_validated_changes(root, changes))
            if aw.inventory(source) != manifest:
                raise HarnessError("Workspace changed during snapshot creation; request a fresh snapshot")
            receipt = {"contract": CONTRACT, "goal_id": goal["goal_id"], "key": key, "files": aw.inventory(root), "source": str(source)}
            from .changes import atomic_write
            atomic_write(receipt_path, gw._canonical(gw._sign(gw._key(home), receipt)))
    return {"snapshot_id": document["goal_id"], "path": str(root), "fingerprint": gw._digest(receipt["files"]), "files": receipt["files"]}


def load_snapshot(goal, runtime_root, snapshot_id):
    # IDs are looked up in task-owned durable tool receipts, never accepted as paths.
    for task in goal["tasks"]:
        for step in task.get("context_steps", []):
            for observation in step.get("results", []):
                value = observation.get("result") or {}
                if value.get("snapshot_id") == snapshot_id and value.get("path"):
                    root = Path(value["path"])
                    home = Path(runtime_root) / "collaboration-snapshots"
                    if root.resolve().parent.parent.parent != home.resolve():
                        raise HarnessError("Snapshot ownership changed")
                    receipt = gw._verify(gw._key(home / "goal-workspaces"), json.loads((root.parent / "submission.json").read_text(encoding="utf-8")))
                    if receipt.get("goal_id") != goal["goal_id"] or receipt.get("contract") != CONTRACT or gw._digest(aw.inventory(root)) != value["fingerprint"] or receipt["files"] != aw.inventory(root):
                        raise HarnessError("Snapshot changed since it was captured")
                    return root
    raise HarnessError("Request a snapshot from this goal before verifying it")


def execute(runtime, goal, task, name, args):
    if not state(goal):
        raise HarnessError("Start a new project goal for shared workspace tools")
    if name == "workspace_catalog":
        return {"settings": state(goal), "workspaces": aw.descriptors(goal, runtime.store.root)}
    if name == "workspace_verify":
        from . import swarm_work, goal_verification
        root = load_snapshot(goal, runtime.store.root, args["snapshot_id"])
        project = goal_verification.inspection_verification_project(runtime.config, goal, runtime.store.root, root, runtime.store.access_project(goal))
        result = swarm_work._run_selected_project_verification(runtime.config, root, project,
            goal["objective"], [], None, verification_session_id=goal["goal_id"], verification_profile="shared_goal_v1", context_check=True)
        load_snapshot(goal, runtime.store.root, args["snapshot_id"])
        return result
    target = args["workspace_id"]
    root = owned_root(goal, runtime.store.root, target)
    if name == "workspace_read":
        result = aw.inspect(goal, runtime.store.root, target, args.get("path", ""), cursor=args.get("cursor", ""))
        return {**result, "workspace_id": target, "fingerprint": gw._digest(aw.inventory(root))}
    if name == "workspace_snapshot":
        value = snapshot(goal, runtime.store.root, root)
        return {k: v for k, v in value.items() if k != "files"}
    if name == "workspace_edit":
        from .swarm_work import _validated_changes
        if not can_write(goal, task, target):
            raise HarnessError("The selected role or project access does not allow editing this workspace")
        # No nested agent locks: a peer's live turn never causes an A/B lock cycle.
        transaction = FileTransaction(root, max_files=int(runtime.config.get("execution.max_changed_files")),
            max_bytes=int(runtime.config.get("execution.max_changed_bytes")))
        with transaction.locked():
            if gw._digest(aw.inventory(root)) != args["expected_fingerprint"]:
                raise HarnessError("The workspace changed; read it again before editing")
            artifact = transaction.apply(_validated_changes(root, args["changes"]))
        return {"workspace_id": target, "fingerprint": gw._digest(aw.inventory(root)), "artifact": artifact,
                "direct_real_edit": target == "real"}
    raise HarnessError("Unknown collaboration workspace tool")


def prepare_review(store, goal, task):
    if task.get("review_submission"):
        return
    parent = next(t for t in goal["tasks"] if t["id"] == task["review_of"])
    source = gw.root(goal, store.root)
    baseline = gw._digest(aw.inventory(source))
    value = snapshot(goal, store.root, source, changes=(parent.get("pending_action") or {}).get("changes", []),
                     identity=task["review_packet_sha256"])
    def save(current, db):
        held = next(t for t in current["tasks"] if t["id"] == task["id"])
        if held["lease_id"] != task["lease_id"] or gw._digest(aw.inventory(source)) != baseline:
            raise HarnessError("Submission changed while preparing its review")
        held["review_submission"] = {**value, "source_fingerprint": baseline}
    store._mutate(goal["goal_id"], save)


@contextmanager
def review_workspace(goal, task, runtime_root):
    parent = next(t for t in goal["tasks"] if t["id"] == task["review_of"])
    action = parent.get("pending_action") or {}
    if task.get("review_packet_sha256") != parent.get("review_packet_sha256"):
        raise SupersededReview("The author submission changed before review")
    value = task.get("review_submission")
    if not value or value["source_fingerprint"] != gw._digest(aw.inventory(gw.root(goal, runtime_root))):
        raise SupersededReview("The project changed after the submitted review snapshot; request a fresh review")
    root = Path(value["path"])
    if aw.inventory(root) != value["files"]:
        raise HarnessError("Review snapshot was modified after capture")
    yield aw.AgentWorkspace(root, gw.root(goal, runtime_root), value["files"])
    if aw.inventory(root) != value["files"]:
        raise HarnessError("Review modified its submitted snapshot; its approval cannot be accepted")
