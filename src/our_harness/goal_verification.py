"""Carry a selected project's verification settings through durable goal work.

The snapshot retains the user's exact commands/approval. It grants no new
execution authority: the existing verifier still validates discovered command
fingerprints and runs its containment checks.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .models import HarnessError


SCHEMA_VERSION = 3
SHARED_GOAL_PROFILE = "shared_goal_v1"
LEGACY_CHECK_POLICY = {
    "schema_version": 1,
    "configured_or_discovered_checks": "required",
    "no_selected_checks": "report_not_configured_require_task_evidence",
    "existing_artifacts": "require_authenticated_current_snapshot",
}
CHECK_POLICY = {
    **LEGACY_CHECK_POLICY,
    "schema_version": 2,
    "request_intent_contract": "polite-and-plural-action-requests/v2",
}


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root.resolve()))


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _commands(value: object) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(command, list) or not command
        or any(not isinstance(arg, str) or "\0" in arg for arg in command)
        for command in value
    ):
        raise HarnessError("Project test commands must be nonempty argument lists")
    return copy.deepcopy(value)


def capture_verification_contract(
    config: LoadedConfig, project: dict[str, Any], root: Path,
) -> dict[str, Any]:
    same_root = config.project_root.resolve() == root.resolve()
    contract = {
        "schema_version": SCHEMA_VERSION,
        "verification_profile": SHARED_GOAL_PROFILE,
        "check_policy": copy.deepcopy(CHECK_POLICY),
        "project_root": _root_key(root),
        "test_commands": _commands(project.get("test_commands", [])),
        "test_evidence_contracts": copy.deepcopy(project.get("test_evidence_contracts", [])),
        "approved_test_command_digest": str(project.get("approved_test_command_digest") or ""),
        "uses_project_config": same_root,
        "project_config_commands_sha256": _fingerprint({
            "commands": _commands(config.get("project.test_commands", [])) if same_root else [],
            "evidence_contracts": config.get("project.test_evidence_contracts", []) if same_root else [],
        }),
    }
    return {**contract, "fingerprint_sha256": _fingerprint(contract)}


def verification_project(config: LoadedConfig, goal: dict[str, Any]) -> dict[str, Any]:
    project = copy.deepcopy(goal["project"])
    project["tasks"] = [goal["objective"]]
    contract = goal.get("verification_contract")
    if contract is None:
        # Legacy goals did not retain these settings. Never fabricate approval.
        return project
    if not isinstance(contract, dict):
        raise HarnessError("The saved project verification settings are malformed")
    unsigned = {key: value for key, value in contract.items() if key != "fingerprint_sha256"}
    # Legacy contracts retain their exact saved command authority. Resume
    # captures the current contract and invalidates context-tool results through the new
    # contract fingerprint; reading legacy state must not fabricate approval.
    if contract.get("schema_version") not in {1, 2, SCHEMA_VERSION} or contract.get("verification_profile") != SHARED_GOAL_PROFILE or (
        contract.get("fingerprint_sha256") != _fingerprint(unsigned)
        or contract.get("project_root") != _root_key(Path(project["path"]))
        or (contract.get("schema_version") == 2 and contract.get("check_policy") != LEGACY_CHECK_POLICY)
        or (contract.get("schema_version") == SCHEMA_VERSION and contract.get("check_policy") != CHECK_POLICY)
    ):
        raise HarnessError("The saved project verification contract changed; start a new goal")
    same_root = config.project_root.resolve() == Path(project["path"]).resolve()
    current_config = _fingerprint({
        "commands": _commands(config.get("project.test_commands", [])) if same_root else [],
        "evidence_contracts": config.get("project.test_evidence_contracts", []) if same_root else [],
    })
    if contract.get("uses_project_config") != same_root or (
        contract.get("project_config_commands_sha256") != current_config
    ):
        raise HarnessError("Project test commands changed after this goal started; start a new goal")
    project["test_commands"] = _commands(contract.get("test_commands"))
    project["test_evidence_contracts"] = copy.deepcopy(contract.get("test_evidence_contracts", []))
    project["approved_test_command_digest"] = str(contract.get("approved_test_command_digest") or "")
    return project


def run_configured_goal_verification(
    config: LoadedConfig, root: Path, project: dict[str, Any], goal: str,
    changed: list[str], progress=None, *, deadline=None, verification_session_id: str = "",
    require_changes: bool = True,
) -> dict[str, Any]:
    """Verify actual selected checks; agents own interpretation of the goal.

    No finite English-to-test template compiler can verify an arbitrary game,
    website, or repository goal. The shared task ledger owns those requirements.
    This boundary reports only observed file safety and executed test evidence.
    """
    from . import cancellation, swarm_work as work
    from .models import DeadlineExpired
    from .verification import analyze_verification

    results: list[dict[str, Any]] = []

    def outcome(status: str, basis: str, reason: str, **extra):
        return {"status": status, "basis": basis, "reason": reason,
                "commands": results, "verification_profile": SHARED_GOAL_PROFILE,
                "check_policy": copy.deepcopy(CHECK_POLICY),
                "verification_session_id": verification_session_id, **extra}

    # Retain explicit read-only and protected-file constraints. They are path
    # authority, not invented behavioral acceptance predicates.
    # Merely mentioning/using a file does not forbid editing it. The legacy
    # role parser classifies "use Three.js in index.html" as a protected HTML
    # reference, which contradicts ordinary project-creation requests. Retain
    # explicit prohibitions/preservation clauses and the confined file layer.
    protected = []
    explicit_effects = []
    exception_limits = []
    for clause in re.split(r"[;\n]|[.!?]\s+(?=[A-Z])", goal):
        if re.search(
            r"\b(?:do\s+not|don't|never)\s+(?:create|write|change|modify|edit|delete|touch|overwrite)\b"
            r"|\bpreserv(?:e|ing)\b|\bkeep\b[^;\n]*\bunchanged\b|\bread[- ]only\b",
            clause, re.I,
        ):
            # A preservation target does not absorb the positive action after
            # it: "keep settings.json unchanged while updating game.py".
            scope = re.search(
                r"\b(?:while|and|but|then|except)\s+(?:to\s+)?"
                r"(?:creat(?:e|ing)|writ(?:e|ing)|chang(?:e|ing)|modify(?:ing)?|"
                r"edit(?:ing)?|delet(?:e|ing)|updat(?:e|ing)|fix(?:ing)?)\b",
                clause, re.I,
            )
            constraint = clause[:scope.start()] if scope else clause
            effects = work._goal_named_paths(clause[scope.end():]) if scope else []
            explicit_effects.extend(effects)
            protected.extend(work._goal_path_roles(constraint)["protected"])
            if scope and scope.group().lower().startswith("except") and re.search(
                r"\b(?:any|all)\s+(?:project\s+)?files\b", constraint, re.I,
            ):
                # An exception to a project-wide prohibition permits only its
                # named targets, not every other file in the project.
                exception_limits.append(effects)
    violated = [path for path in changed if (
        any(work._paths_overlap(path, one) for one in protected)
        or any(not any(work._paths_overlap(path, one) for one in allowed) for allowed in exception_limits)
    )]
    if violated:
        return outcome("failed", "protected_path", "Protected/read-only paths were changed: " + ", ".join(violated))
    if work._goal_intent(goal) == "read_only" and not explicit_effects:
        if changed:
            return outcome("failed", "read_only_tree_drift", "Read-only work cannot change project files.")
        merkle, _ = work._project_tree_merkle(root)
        return outcome("passed", "read_only_zero_write", "Read-only work retained the project tree.", current_tree_merkle=merkle)
    if require_changes and not changed:
        return outcome("failed", "goal_effect", "The requested project work has not produced a recorded file change.")
    commands, source = work._verification_commands(config, root, project)
    if not commands:
        if str(project.get("approved_test_command_digest") or ""):
            return outcome(
                "unavailable", "approved_checks_unavailable",
                "Previously approved project checks are no longer discoverable. "
                "Restore or explicitly refresh the selected checks before completing this goal.",
            )
        merkle, manifest = work._project_tree_merkle(root)
        return outcome(
            "not_configured", "no_selected_checks",
            "No deterministic project checks are configured or discoverable; no tests ran. "
            "This is not a missing-runner failure. Complete the task only when its actual "
            "requirements are supported by inspected artifacts and team agreement. "
            "Any explicitly required testing still needs real execution evidence.",
            current_tree_merkle=merkle, file_count=len(manifest),
        )
    if source == "discovered":
        try:
            digest = work._command_approval_digest(root, commands, declared_path=str(project.get("path") or ""))
        except (OSError, RuntimeError) as exc:
            return outcome("unavailable", "command_approval_fingerprint_unavailable", str(exc), proposed_commands=commands)
        if str(project.get("approved_test_command_digest") or "") != digest:
            return outcome("unavailable", "discovered_command_approval_required",
                           "Review and approve Project test commands before Nexus runs them.",
                           proposed_commands=commands, approval_digest=digest)
    command_config = LoadedConfig(copy.deepcopy(config.data), root.resolve(), list(config.sources),
                                  dict(config.provenance), copy.deepcopy(config.trusted_floor))
    before, _ = work._project_tree_merkle(root)
    for command in commands:
        executable = str(command[0]) if command else ""
        path = Path(executable)
        available = work._containment_owns_runner_availability(command) or (
            path.is_file() if path.is_absolute() or path.parent != Path(".") else shutil.which(executable) is not None
        )
        if not available:
            return outcome("unavailable", "missing_runner", "The selected test runner is unavailable: " + executable)
        work._report(progress, "Running project checks", "Nexus is running: " + " ".join(command))
        timeout = None
        limited = False
        if deadline is not None:
            configured = float(command_config.get("execution.timeout_seconds"))
            timeout = deadline.remaining_seconds("before a selected-project verification command", configured)
            limited = deadline.limits(configured) if isinstance(deadline, work._SwarmToolExecutionBudget) else timeout <= configured
        try:
            payload = work._run_disposable_verification_command(command_config, root, command, timeout=timeout)
            if payload.get("timed_out") and limited:
                raise work.ContextToolBudgetExhausted("Project context-tool execution budget exhausted during verification")
            if deadline is not None:
                deadline.check("during selected-project verification")
        except (cancellation.ChatCancelled, DeadlineExpired):
            raise
        except (HarnessError, OSError) as exc:
            return outcome("unavailable", "verification_runtime_unavailable", str(exc))
        results.append(payload)
        if payload.get("containment_unavailable"):
            return outcome("unavailable", "verification_containment_unavailable", "Project checks could not start in the protected runtime. " + str(payload.get("stderr") or ""))
        after, _ = work._project_tree_merkle(root)
        if payload.get("verification_escape_detected") or after != before:
            return outcome("failed", "verification_escape_detected", "Project checks attempted to change files outside their disposable copy.")
        combined = str(payload.get("stdout") or "") + "\n" + str(payload.get("stderr") or "")
        if "Nexus verification containment denied" in combined:
            return outcome("failed", "verification_containment_denied", "Project checks attempted an operation outside their permitted scope.")
        if payload.get("exit_code") == -1:
            return outcome("unavailable", "missing_runner", "The selected test runner could not start: " + str(payload.get("stderr") or ""))
        if re.search(r"no module named|module not found|cannot find module|command not found|is not recognized", combined, re.I):
            return outcome("failed", "missing_test_dependency", "A project test dependency is missing: " + combined.strip()[:2000])
        if payload.get("exit_code") != 0 or payload.get("timed_out") or work._EMPTY_TEST_OUTPUT.search(combined):
            return outcome("failed", source, "A project check failed, timed out, or ran zero tests.")
    contracts = project.get("test_evidence_contracts", [])
    if not contracts and config.project_root.resolve() == root.resolve():
        contracts = config.get("project.test_evidence_contracts", [])
    analysis = analyze_verification(commands, results, evidence_contracts=contracts if isinstance(contracts, list) else [])
    if not analysis["passed"]:
        return outcome("failed", "positive_test_evidence", "Checks did not provide complete positive evidence that tests executed.", verification_analysis=analysis)
    return outcome("passed", source, "All selected project checks passed. The agents' task evidence records how the goal was fulfilled.", verification_analysis=analysis)
