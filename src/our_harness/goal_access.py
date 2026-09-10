"""User-owned, project-bound access decisions for durable agent goals.

The engine retains file confinement and command containment in every mode.
Neither provider text nor serialized tool arguments can mint execution authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from .models import HarnessError

CONTRACT = "goal-project-access/v1"
COMMAND_WAIT_NOTE = "Command permission needed. Choose Deny, Run once, or Always allow in this chat."
MODES = {"read_only", "ask", "full"}
BLOCKS = {"discovered_command_approval_required", "command_access_denied", "read_only_access"}
REVIEW_CONTRACT = "full-access-deterministic-review/v1"


def authorize_review_fallback(document, task, packet_sha):
    """Apply the saved user grant to an exact proposal, never model authority."""
    access = state(document)
    if access["mode"] != "full" or not task.get("provider_effect_id"):
        return None
    receipt = {"schema_version": 1, "contract": REVIEW_CONTRACT,
               "access_fingerprint": context_fingerprint(document),
               "review_packet_sha256": packet_sha,
               "provider_effect_id": task["provider_effect_id"]}
    receipt["fingerprint"] = fingerprint(receipt)
    task["full_access_review"] = receipt
    task["review_approved_effect_id"] = task["provider_effect_id"]
    return receipt


def review_fallback_current(document, task, packet_sha):
    receipt = task.get("full_access_review")
    if receipt is None:
        return True  # Existing independent review or exact user decision.
    return isinstance(receipt, dict) and receipt.get("schema_version") == 1 \
        and receipt.get("contract") == REVIEW_CONTRACT \
        and state(document)["mode"] == "full" \
        and receipt.get("access_fingerprint") == context_fingerprint(document) \
        and receipt.get("review_packet_sha256") == packet_sha \
        and receipt.get("provider_effect_id") == task.get("provider_effect_id") \
        and receipt.get("fingerprint") == fingerprint({k: v for k, v in receipt.items() if k != "fingerprint"})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def binding(goal):
    return fingerprint({"contract": CONTRACT, **{key: goal.get(key) for key in (
        "goal_id", "project", "project_authority_id", "execution_contract",
    )}, "agents": [{key: agent.get(key) for key in ("id", "who", "route_binding")}
        for agent in goal.get("agents", []) if isinstance(agent, dict)]})


def state(goal):
    held = goal.get("agent_access")
    if held is None:
        return {"schema_version": 1, "binding": binding(goal), "mode": "ask", "grants": {}}
    if not isinstance(held, dict) or held.get("schema_version") != 1 or held.get("binding") != binding(goal) \
            or not isinstance(held.get("mode"), str) or held.get("mode") not in MODES \
            or not isinstance(held.get("grants"), dict):
        return {"schema_version": 1, "binding": binding(goal), "mode": "read_only", "grants": {}, "stale": True}
    return copy.deepcopy(held)


def context_fingerprint(goal):
    access = state(goal)
    # Consuming a one-use grant must not invalidate the result of that very
    # execution. Only a new user decision changes the context authority.
    for grant in access.get("grants", {}).values():
        grant.pop("remaining", None)
    return fingerprint(access)


@dataclass
class CommandAuthority:
    decide: Callable
    revision: int | None = None


def command_gate(project, commands, digest, source):
    authority = project.get("_nexus_command_access")
    if authority is None:
        return None
    if not isinstance(authority, CommandAuthority):
        raise HarnessError("Command access requires an engine-issued authority")
    result, authority.revision = authority.decide(commands, digest, source)
    return result


def record_block(document, result, agent_id=""):
    if isinstance(result, dict) and result.get("basis") in BLOCKS:
        document["command_request"] = {
            "schema_version": 1,
            "state": "pending" if result.get("basis") == "discovered_command_approval_required" else "denied",
            "agent_id": agent_id,
            "commands": copy.deepcopy(result.get("proposed_commands") or []),
            "approval_digest": result.get("approval_digest", ""),
            "reason": result.get("reason", "Command permission is required."),
        }
        # Existing denials are answers, not new requests for permission. Return
        # the refused tool result so the team can adapt within its access mode.
        # Final verification still cannot claim success without required checks.
        if result.get("basis") == "discovered_command_approval_required" and document.get("status") not in {
            "complete", "cancelled", "cancelling", "waiting_for_user", "paused",
        }:
            document["status"] = "paused"
            document["note"] = COMMAND_WAIT_NOTE


class AccessStoreMixin:
    def access_project(self, document):
        from .goal_verification import verification_project
        project = verification_project(self.config, document, runtime_root=self.root)
        project["_nexus_command_access"] = CommandAuthority(
            lambda commands, digest, source: self.authorize_commands(document["goal_id"], commands, digest, source))
        return project

    def authorize_commands(self, goal_id, commands, digest, source):
        def change(document, db):
            access = state(document)
            grant = access.get("grants", {}).get(digest, {})
            if access["mode"] == "read_only":
                basis = "read_only_access"
                reason = "Read only access does not run project commands. Change access to permit execution."
            elif grant.get("decision") == "deny":
                basis = "command_access_denied"
                reason = "You denied this command. Review its request to allow it."
            elif access["mode"] == "full" or grant.get("decision") == "always":
                return True
            elif grant.get("decision") == "once" and grant.get("remaining") == 1:
                access["grants"][digest]["remaining"] = 0
                document["agent_access"] = access
                self._event(db, document, "command_permission_consumed", payload={"approval_digest": digest})
                return True
            elif digest not in access.get("grants", {}) and (
                source != "discovered" or (document.get("verification_contract") or {}).get("approved_test_command_digest") == digest
            ):
                # Preserve explicitly configured commands and exact legacy grants.
                return True
            else:
                basis = "discovered_command_approval_required"
                reason = "This command needs your permission in the chat before Nexus can run it."
            return {"status": "unavailable", "basis": basis, "reason": reason,
                    "commands": [], "proposed_commands": commands, "approval_digest": digest}
        document, result = self._mutate(goal_id, change)
        return result, document["revision"]

    def update_access(self, goal_id, *, expected_revision, mode=None, decision=None, command_digest=""):
        from .goal_verification import goal_command_approval
        def change(document, db):
            if type(expected_revision) is not int or document["revision"] != expected_revision:
                raise HarnessError("This chat changed; refresh its permissions before deciding")
            if document["status"] not in {"paused", "waiting_for_user", "queued"} or self._scheduler_live(document) or any(
                task.get("state") == "running" or task.get("pending_transaction") for task in document["tasks"]
            ):
                raise HarnessError("Pause the team and let its current turn settle before changing access")
            access = state(document)
            revision = int(access.get("revision") or 0) + 1
            if mode is not None:
                if not isinstance(mode, str) or mode not in MODES or decision is not None:
                    raise HarnessError("Choose a supported agent access mode")
                access = {"schema_version": 1, "binding": binding(document), "mode": mode, "grants": {}}
                document.pop("command_request", None)
            elif isinstance(decision, str) and decision in {"deny", "once", "always"}:
                if access["mode"] == "read_only":
                    raise HarnessError("Change Read only access before allowing or denying commands")
                preview = goal_command_approval(self.config, document, runtime_root=self.root)
                if not preview.get("commands") or not command_digest or preview.get("approval_digest") != command_digest:
                    raise HarnessError("The command changed; review the current command before deciding")
                held_request = document.get("command_request") or {}
                resume_after_decision = (
                    document["status"] == "paused" and document.get("note") == COMMAND_WAIT_NOTE
                    and held_request.get("state") == "pending"
                    and held_request.get("approval_digest") == command_digest
                    and not any(one.get("state") == "pending" for one in document.get("interrupts", []))
                )
                access.setdefault("grants", {})[command_digest] = {"decision": decision, "remaining": 1 if decision == "once" else 0}
                document["command_request"] = {"schema_version": 1, "state": "denied" if decision == "deny" else "approved",
                    "commands": preview["commands"], "approval_digest": command_digest,
                    "resume_after_decision": resume_after_decision}
            else:
                raise HarnessError("Choose Deny, Run once, or Always allow")
            access["revision"] = revision
            document["agent_access"] = access
            if mode == "full":
                self._recover_full_access_reviews(document, db)
            for task in document["tasks"]:
                for step in task.get("context_steps", []):
                    if any(call.get("name") == "run_selected_verification" for call in step.get("calls", [])):
                        step["state"] = "superseded"
            self._event(db, document, "agent_access_updated", payload={"mode": access["mode"],
                "decision": decision, "approval_digest": command_digest})
        return self.public(self._mutate(goal_id, change)[0])
