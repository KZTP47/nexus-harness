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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .models import HarnessError


SCHEMA_VERSION = 4
SHARED_GOAL_PROFILE = "shared_goal_v1"
LEGACY_CHECK_POLICY = {
    "schema_version": 1,
    "configured_or_discovered_checks": "required",
    "no_selected_checks": "report_not_configured_require_task_evidence",
    "existing_artifacts": "require_authenticated_current_snapshot",
}
PREVIOUS_CHECK_POLICY = {
    **LEGACY_CHECK_POLICY,
    "schema_version": 2,
    "request_intent_contract": "polite-and-plural-action-requests/v2",
}
CHECK_POLICY = {
    **PREVIOUS_CHECK_POLICY,
    "schema_version": 3,
    "runtime_deliverables": "executed-checks-required/v1",
}

# This identifies an executable deliverable, not an English-to-test compiler.
# Its behavior still needs task-specific checks authored and inspected by the
# team. Static prose/data retain the authenticated no-change completion path.
_RUNTIME_SUFFIXES = frozenset({
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".py", ".rb", ".go", ".rs", ".c", ".cc", ".cpp", ".cxx", ".cs",
    ".java", ".kt", ".swift", ".php", ".lua", ".sh", ".ps1", ".bat", ".cmd",
})
_HTML_RUNTIME = re.compile(r"<script\b|\bon[a-z]+\s*=|javascript\s*:", re.I)
_REQUEST_ACTION = re.compile(r"\b(?:create|build|make|implement|fix|repair|improve|update|add|write)\b", re.I)
_RUNTIME_OBJECT = re.compile(r"\b(?:game|application|app|website|web\s+site|api|script|program|service|server|plugin)\b", re.I)
_DOCUMENT_OBJECT = re.compile(r"\b(?:documentation|docs|readme|guide|report|instructions|notes|plan)\b|\.(?:md|txt)\b", re.I)


def _request_objects(goal: str) -> list[str]:
    """Keep complete object phrases, rather than stopping at a runtime noun.

    In 'create an app user guide', app modifies the guide. In 'create an app
    and a guide', those are separate objects and the app still needs execution.
    Qualifiers such as 'for the app' do not change a guide into an application.
    """
    actions = list(_REQUEST_ACTION.finditer(goal))
    objects = []
    for index, action in enumerate(actions):
        end = actions[index + 1].start() if index + 1 < len(actions) else len(goal)
        scope = re.split(r"[;\n]|[.!?](?:\s|$)", goal[action.end():end], maxsplit=1)[0]
        # Reference modifiers describe an object; they do not replace its
        # type. A 'game described in README.md' is still a game, while an
        # 'app user guide based on the plan' is still a guide.
        direct = re.split(
            r"(?<!-)\b(?:for|about|with|using|where|that|which|in|on|from|by|following|according\s+to)\b(?!-)",
            scope, maxsplit=1, flags=re.I,
        )[0]
        objects.extend(re.split(r"\b(?:and|plus)\b|,", direct, flags=re.I))
    return [one.strip() for one in objects if one.strip()]


def requested_runtime_verification(goal: str) -> bool:
    """Reject obvious runtime requests satisfied only by placeholder files.

    This deliberately does not invent gameplay rules or tests from prose. It
    only recognizes direct runtime creation/repair requests; the task ledger
    and meaningful executed checks still own their detailed acceptance.
    """
    from . import swarm_work as work

    if work._goal_intent(goal) == "read_only":
        return False
    return any(_RUNTIME_OBJECT.search(one) and not _DOCUMENT_OBJECT.search(one)
               for one in _request_objects(goal))


def runtime_verification_paths(
    root: Path, goal: str, changed: list[str], manifest: dict[str, Any],
) -> list[str]:
    """Find in-scope runtime files that snapshots cannot functionally verify.

    Applied paths bound an edit's scope. For an existing-result/no-op claim,
    use the named deliverable if present, otherwise inspect the project tree.
    Never follow project paths outside the existing confined file boundary.
    """
    from . import swarm_work as work

    if work._goal_intent(goal) == "read_only":
        return []
    named = work._goal_named_paths(goal)
    named_candidates = [
        path for path in manifest
        if any(work._paths_overlap(path, one) for one in named)
    ]
    candidates = list(dict.fromkeys([*changed, *named_candidates]))
    if not candidates:
        objects = _request_objects(goal)
        documentation_only = bool(objects) and all(_DOCUMENT_OBJECT.search(one) for one in objects)
        # A library name such as Three.js is not necessarily a project path.
        # Unresolved existing-runtime claims still inspect the project, whereas
        # prose-only work must not absorb unrelated code into its changed scope.
        candidates = [] if documentation_only else list(manifest)
    answer = []
    for relative in sorted(candidates):
        suffix = Path(relative).suffix.lower()
        if suffix in _RUNTIME_SUFFIXES:
            answer.append(relative)
        elif suffix in {".html", ".htm", ".xhtml"}:
            try:
                path = work.confined_path(root, relative, allow_missing=True)
                # A removed entry point or oversized/unreadable document must
                # not turn executable changes into a no-check completion.
                if not path.is_file() or path.stat().st_size > 1_000_000:
                    answer.append(relative)
                elif _HTML_RUNTIME.search(path.read_text(encoding="utf-8", errors="replace")):
                    answer.append(relative)
            except (HarnessError, OSError):
                answer.append(relative)
    return answer


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


def _selected_verification_project(config: LoadedConfig, goal: dict[str, Any]) -> dict[str, Any]:
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
    if contract.get("schema_version") not in {1, 2, 3, SCHEMA_VERSION} or contract.get("verification_profile") != SHARED_GOAL_PROFILE or (
        contract.get("fingerprint_sha256") != _fingerprint(unsigned)
        or contract.get("project_root") != _root_key(Path(project["path"]))
        or (contract.get("schema_version") == 2 and contract.get("check_policy") != LEGACY_CHECK_POLICY)
        or (contract.get("schema_version") == 3 and contract.get("check_policy") != PREVIOUS_CHECK_POLICY)
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


@dataclass(frozen=True)
class _WorkspaceVerificationAuthority:
    """In-process authority; JSON supplied by a project cannot construct it."""

    config: LoadedConfig
    goal: dict[str, Any]
    runtime_root: Path
    execution_root: Path


def verification_project(
    config: LoadedConfig, goal: dict[str, Any], *, runtime_root: Path | None = None,
) -> dict[str, Any]:
    project = _selected_verification_project(config, goal)
    if "execution_workspace" in goal:
        from . import goal_workspaces
        from .swarm_runs import _base

        runtime_root = runtime_root if runtime_root is not None else _base()
        execution_root = goal_workspaces.root(goal, runtime_root)
        binding = copy.deepcopy({key: goal[key] for key in (
            "goal_id", "project", "project_authority_id", "execution_workspace",
            "verification_contract", "objective",
        ) if key in goal})
        project["_nexus_workspace_verification"] = _WorkspaceVerificationAuthority(
            config, binding, runtime_root, execution_root,
        )
    return project


def verification_authority(
    config: LoadedConfig, root: Path, project: dict[str, Any],
) -> tuple[LoadedConfig, Path]:
    """Validate an owned copy while retaining its selected command authority.

    Discovery still reads the execution copy. Only its approval identity and
    selected configuration come from the original project, never from a path
    field supplied by the board or an agent.
    """
    authority = project.get("_nexus_workspace_verification")
    if authority is None:
        return config, root
    if not isinstance(authority, _WorkspaceVerificationAuthority):
        raise HarnessError("Workspace verification authority is not an engine-issued binding")
    from . import goal_workspaces

    expected_root = goal_workspaces.root(authority.goal, authority.runtime_root)
    if root.resolve() != expected_root or authority.execution_root != expected_root:
        raise HarnessError("Workspace verification was redirected to a different execution root")
    selected = _selected_verification_project(authority.config, authority.goal)
    fields = ("path", "test_commands", "test_evidence_contracts", "approved_test_command_digest")
    if any(project.get(field) != selected.get(field) for field in fields):
        raise HarnessError("Workspace verification commands or selected project authority changed")
    return authority.config, Path(selected["path"])


def workspace_verification_approval(
    config: LoadedConfig, goal: dict[str, Any], *, runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Preview one goal's unpublished checks under its canonical project identity."""
    if "execution_workspace" not in goal:
        raise HarnessError("This goal has no isolated workspace; use its selected project's test settings")
    return goal_command_approval(config, goal, runtime_root=runtime_root)


def goal_command_approval(config: LoadedConfig, goal: dict[str, Any], *, runtime_root: Path | None = None) -> dict[str, Any]:
    """Preview commands for either an owned chat copy or a board goal."""
    from . import swarm_work

    project = verification_project(config, goal, runtime_root=runtime_root)
    proposal = swarm_work.verification_command_approval(config, project)
    from .verification_scripts import resolve_package_command
    authority = project.get("_nexus_workspace_verification")
    execution_root = authority.execution_root if authority else Path(project["path"])
    try:
        proposal["resolved_commands"] = [resolve_package_command(execution_root, command) for command in proposal.get("commands", [])]
    except HarnessError as exc:
        proposal["runner_note"] = str(exc)
    if proposal.get("commands") and not proposal.get("approval_digest"):
        proposal["approval_digest"] = swarm_work._command_approval_digest(
            execution_root, proposal["commands"], declared_path=project["path"],
            authority_root=Path(project["path"]),
        )
    settled = goal.get("status") in {"paused", "waiting_for_user"} and not any(
        task.get("state") == "running" for task in goal.get("tasks", []) if isinstance(task, dict)
    )
    return {
        **proposal,
        "goal_id": str(goal.get("goal_id") or ""),
        "revision": int(goal.get("revision") or 0),
        "can_approve": bool(proposal.get("can_approve") and settled),
        **({"reason": "Pause this goal and let its current turn settle before approving its checks."}
           if not settled else {}),
    }


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

    authority_config, authority_root = verification_authority(config, root, project)
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
        runtime_paths = runtime_verification_paths(root, goal, changed, manifest)
        runtime_requested = requested_runtime_verification(goal)
        if runtime_paths or runtime_requested:
            return outcome(
                "failed", "runtime_verification_required",
                "Executable deliverables have no selected or discoverable checks; no tests ran. "
                "File existence, code review, team agreement and an unchanged snapshot cannot prove that "
                "the result launches or works. Add meaningful automated checks for the requested behavior "
                "and launch method, expose a test command at the selected project root (including tests "
                "in a new subfolder), then call run_selected_verification. If discovered commands need "
                "approval, report the exact proposed command through the existing verification flow. "
                "Do not fabricate a test pass or change files solely to obtain an artifact.",
                current_tree_merkle=merkle, runtime_paths=runtime_paths[:100],
                runtime_requested=runtime_requested,
                file_count=len(manifest),
            )
        return outcome(
            "not_configured", "no_selected_checks",
            "No deterministic project checks are configured or discoverable; no tests ran. "
            "The in-scope deliverables contain no executable source. Complete the task only when its actual "
            "requirements are supported by inspected artifacts and team agreement. "
            "Any explicitly required testing still needs real execution evidence.",
            current_tree_merkle=merkle, file_count=len(manifest),
        )
    from .goal_access import command_gate
    access = command_gate(project, commands, work._command_approval_digest(
        root, commands, declared_path=str(project.get("path") or ""), authority_root=authority_root,
    ), source) if project.get("_nexus_command_access") else None
    if isinstance(access, dict):
        return outcome(access["status"], access["basis"], access["reason"],
                       proposed_commands=commands, approval_digest=access["approval_digest"])
    if source == "discovered" and access is not True:
        try:
            digest = work._command_approval_digest(
                root, commands, declared_path=str(project.get("path") or ""),
                authority_root=authority_root,
            )
        except (OSError, RuntimeError) as exc:
            return outcome("unavailable", "command_approval_fingerprint_unavailable", str(exc), proposed_commands=commands)
        if str(project.get("approved_test_command_digest") or "") != digest:
            return outcome("unavailable", "discovered_command_approval_required",
                           "Review and approve Project test commands before Nexus runs them.",
                           proposed_commands=commands, approval_digest=digest)
    command_config = LoadedConfig(copy.deepcopy(authority_config.data), root.resolve(), list(authority_config.sources),
                                  dict(authority_config.provenance), copy.deepcopy(authority_config.trusted_floor))
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
            payload = work._run_disposable_verification_command(
                command_config, root, command, timeout=timeout, command_root=authority_root,
            )
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
    if not contracts and authority_config.project_root.resolve() == authority_root.resolve():
        contracts = authority_config.get("project.test_evidence_contracts", [])
    analysis = analyze_verification(commands, results, evidence_contracts=contracts if isinstance(contracts, list) else [])
    if not analysis["passed"]:
        return outcome("failed", "positive_test_evidence", "Checks did not provide complete positive evidence that tests executed.", verification_analysis=analysis)
    return outcome("passed", source, "All selected project checks passed. The agents' task evidence records how the goal was fulfilled.", verification_analysis=analysis)
