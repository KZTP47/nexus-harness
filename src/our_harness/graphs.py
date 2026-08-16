from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from .models import HarnessError


NODE_TYPES = {"planner", "coder", "tool", "evaluator", "merge", "gauntlet", "approval_required", "start", "end"}
AGENT_NODE_TYPES = {"planner", "coder", "evaluator", "merge"}
EDGE_MODES = {"state", "delegate", "merge_input"}
GRAPH_MAX_NODES = 256
GRAPH_MAX_EDGES = 1024
GRAPH_MAX_PROMPT_CHARS = 32_000
GRAPH_MAX_CAPABILITIES = 32
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
CONDITION = re.compile(r"^([A-Za-z_][\w.]*)\s*(==|!=)\s*(true|false|null|-?\d+(?:\.\d+)?|\"[^\"]*\")$", re.IGNORECASE)


@dataclass(frozen=True)
class GraphIssue:
    path: str
    message: str


@dataclass(frozen=True)
class WorkflowExecutionPolicy:
    name: str
    max_iterations: int
    max_elapsed_seconds: int
    temperature_decay: float
    include_lint: bool
    include_build: bool
    require_review: bool
    verification_kinds: tuple[str, ...]
    graph_sha256: str | None = None
    event_nodes: tuple[tuple[str, str], ...] = ()


TOOL_KINDS = {
    "syntax": "lint",
    "build": "build",
    "security": "security",
    "performance": "performance",
    "unit_test": "test",
    "generic": "test",
}
MERGE_OUTPUT_CONTRACTS = {"implementation_plan"}
# Every agent kind may write notes to the others. A note is text: reading one
# never runs anything, so it does not widen what an agent can do to the project.
GRAPH_AGENT_CAPABILITIES = {
    "planner": {"workspace.read", "team.message"},
    "coder": {"workspace.read", "workspace.write", "team.message"},
    "evaluator": {"workspace.read", "team.message"},
    "merge": {"workspace.read", "team.message"},
}

PRODUCTION_STATE_ROOTS = {
    "task", "plan_ready", "plan", "candidate", "source_code", "stage_passed",
    "tests_passed", "review_passed", "temperature", "iteration", "verifications",
    "verification", "test_results", "review", "failure", "error_trace", "applied",
    "edge_inputs", "approval_decision",
}

FIXED_NODE_STATE = {
    "start": {"plan_ready": False, "stage_passed": True, "tests_passed": False, "review_passed": False, "iteration": 0},
    "planner": {"plan_ready": True},
    "coder": {"stage_passed": True, "failure": None},
}


class ProductionGraphInterpreter:
    """Traverse one validated graph while retaining typed state and loop budgets."""

    def __init__(self, graph: dict[str, Any]):
        self.graph = migrate_graph(graph)
        self.nodes = {node["id"]: node for node in self.graph["nodes"]}
        self.outgoing: dict[str, list[dict[str, Any]]] = {}
        for edge in self.graph["edges"]:
            self.outgoing.setdefault(edge["source"], []).append(edge)
        self.current = str(self.graph["entry"])
        self.loop_counts: dict[str, int] = {}
        self.loop_started: dict[str, float] = {}
        loop_budget = sum(int(edge.get("loop", {}).get("max_iterations", 0)) for edge in self.graph["edges"])
        self.max_steps = min(10_000, max(64, len(self.nodes) * (loop_budget + 2)))
        self.steps = 0

    @property
    def node(self) -> dict[str, Any]:
        return self.nodes[self.current]

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        current_time = time.monotonic() if now is None else now
        return {
            "schema_version": 1,
            "current": self.current,
            "loop_counts": dict(self.loop_counts),
            "loop_elapsed_seconds": {
                edge_id: max(0.0, current_time - started) for edge_id, started in self.loop_started.items()
            },
            "steps": self.steps,
            "max_steps": self.max_steps,
        }

    def restore(self, snapshot: dict[str, Any], now: float | None = None) -> None:
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
            raise HarnessError("Run checkpoint interpreter state has an unsupported schema")
        current = snapshot.get("current")
        counts = snapshot.get("loop_counts")
        elapsed = snapshot.get("loop_elapsed_seconds")
        steps = snapshot.get("steps")
        if current not in self.nodes:
            raise HarnessError("Run checkpoint interpreter node is not present in the frozen graph")
        if snapshot.get("max_steps") != self.max_steps:
            raise HarnessError("Run checkpoint interpreter step budget does not match the frozen graph")
        if not isinstance(steps, int) or isinstance(steps, bool) or not 0 <= steps <= self.max_steps:
            raise HarnessError("Run checkpoint interpreter step count is invalid")
        loop_ids = {
            str(edge.get("id") or f"{edge['source']}->{edge['target']}")
            for edge in self.graph["edges"]
            if edge.get("loop")
        }
        if not isinstance(counts, dict) or not isinstance(elapsed, dict):
            raise HarnessError("Run checkpoint interpreter loop state is invalid")
        if not set(counts).issubset(loop_ids) or not set(elapsed).issubset(loop_ids):
            raise HarnessError("Run checkpoint interpreter loop state does not match the frozen graph")
        if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts.values()):
            raise HarnessError("Run checkpoint interpreter loop count is invalid")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 for value in elapsed.values()):
            raise HarnessError("Run checkpoint interpreter loop elapsed time is invalid")
        current_time = time.monotonic() if now is None else now
        self.current = str(current)
        self.loop_counts = {str(key): int(value) for key, value in counts.items()}
        self.loop_started = {str(key): current_time - float(value) for key, value in elapsed.items()}
        self.steps = steps

    def advance(self, state: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
        self.steps += 1
        if self.steps > self.max_steps:
            raise HarnessError("Production graph exceeded its bounded step budget")
        edge = next(
            (candidate for candidate in self.outgoing.get(self.current, []) if condition_matches(candidate.get("condition", ""), state)),
            None,
        )
        if edge is None:
            return None
        edge_id = str(edge.get("id") or f"{edge['source']}->{edge['target']}")
        loop = edge.get("loop")
        if loop:
            current_time = time.monotonic() if now is None else now
            count = self.loop_counts.get(edge_id, 0) + 1
            attempts = int(state.get("iteration", 0))
            maximum = int(loop["max_iterations"])
            if (attempts and attempts >= maximum) or (not attempts and count > maximum):
                raise HarnessError(f"Graph loop limit reached: {edge_id}; stopped after {state.get('iteration', 0)} attempts")
            started = self.loop_started.setdefault(edge_id, current_time)
            timeout = int(loop.get("timeout_seconds", 0))
            if timeout and current_time - started > timeout:
                raise HarnessError(f"Graph loop timeout reached: {edge_id}")
            self.loop_counts[edge_id] = count
            state["temperature"] = float(state.get("temperature", 0.2)) * float(loop.get("temperature_decay", 1.0))
        transferred = {name: copy.deepcopy(_value(state, name)) for name in edge.get("variables", [])}
        state["edge_inputs"] = transferred
        source = self.current
        self.current = str(edge["target"])
        return {
            "edge": edge_id,
            "source": source,
            "target": self.current,
            "variables": transferred,
            "loop_count": self.loop_counts.get(edge_id, 0),
        }


def resolve_workflow_policy(config: Any, plugin_nodes: dict[str, Any] | None = None) -> WorkflowExecutionPolicy:
    name = str(config.get("workflow.name"))
    base = {
        "max_iterations": int(config.get("workflow.max_iterations")),
        "max_elapsed_seconds": int(config.get("workflow.max_elapsed_seconds")),
        "temperature_decay": float(config.get("workflow.temperature_decay")),
        "include_lint": True,
        "include_build": False,
        "require_review": bool(config.get("workflow.require_review")),
    }
    if name == "gauntlet":
        graph = json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8"))
        issues = validate_graph(graph)
        if issues:
            raise HarnessError("Built-in Gauntlet graph is invalid: " + "; ".join(item.message for item in issues))
        loop_edges = [edge for edge in graph["edges"] if edge.get("loop")]
        if not loop_edges:
            raise HarnessError("Built-in Gauntlet graph must have bounded repair edges")
        loops = [edge["loop"] for edge in loop_edges]
        base.update(
            {
                "max_iterations": min(int(loop["max_iterations"]) for loop in loops),
                "max_elapsed_seconds": min(
                    base["max_elapsed_seconds"],
                    *(int(loop.get("timeout_seconds", base["max_elapsed_seconds"])) for loop in loops),
                ),
                "temperature_decay": min(
                    float(loop.get("temperature_decay", base["temperature_decay"])) for loop in loops
                ),
                "include_build": True,
                "require_review": True,
            }
        )
    elif name != "planner-coder-reviewer":
        factory = (plugin_nodes or {}).get(name)
        if factory is None:
            raise HarnessError(f"Unknown workflow.name: {name}")
        raw = factory(config)
        if not isinstance(raw, dict):
            raise HarnessError(f"Workflow plugin {name} must return a policy object")
        allowed = set(base)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise HarnessError(f"Workflow plugin {name} returned unknown policy keys: {', '.join(unknown)}")
        base.update(raw)
    for key in ("max_iterations", "max_elapsed_seconds"):
        if isinstance(base[key], bool) or not isinstance(base[key], int):
            raise HarnessError(f"Workflow policy {key} must be an integer")
    if isinstance(base["temperature_decay"], bool) or not isinstance(base["temperature_decay"], (int, float)):
        raise HarnessError("Workflow policy temperature_decay must be a number")
    for key in ("include_lint", "include_build", "require_review"):
        if not isinstance(base[key], bool):
            raise HarnessError(f"Workflow policy {key} must be a boolean")
    if name == "gauntlet":
        verification_kinds = ("lint", "security", "performance", "test")
    else:
        verification_kinds = tuple(
            kind for kind, enabled in (("test", True), ("lint", base["include_lint"]), ("build", base["include_build"])) if enabled
        )
    policy = WorkflowExecutionPolicy(name=name, verification_kinds=verification_kinds, **base)
    if policy.max_iterations <= 0 or policy.max_elapsed_seconds <= 0:
        raise HarnessError("Workflow policy limits must be positive")
    if not 0 < policy.temperature_decay <= 1:
        raise HarnessError("Workflow policy temperature_decay must be greater than 0 and at most 1")
    return policy


def resolve_graph_execution_policy(
    config: Any,
    graph: dict[str, Any],
    resolved_policy: WorkflowExecutionPolicy | None = None,
) -> WorkflowExecutionPolicy:
    """Validate an executable graph and expose its bounded compatibility summary."""
    source_graph = copy.deepcopy(graph)
    graph = migrate_graph(graph)
    issues = validate_graph(graph)
    if issues:
        raise HarnessError("Graph is invalid: " + "; ".join(f"{issue.path}: {issue.message}" for issue in issues))
    expected = resolved_policy or resolve_workflow_policy(config)
    nodes = {node["id"]: node for node in graph["nodes"]}
    outgoing: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        edge_label = edge.get("id") or f"{edge['source']}->{edge['target']}"
        impossible = _impossible_production_condition(nodes[edge["source"]], str(edge.get("condition", "")))
        if impossible:
            raise HarnessError(f"Executable graph edge {edge_label} {impossible}")
        if nodes[edge["target"]]["type"] == "end" and _condition_accepts_failed_completion(str(edge.get("condition", ""))):
            raise HarnessError(
                f"Executable graph edge {edge_label} routes failure state to an end node"
            )
        outgoing.setdefault(edge["source"], []).append(edge["target"])
    reachable = _reachable(outgoing, str(graph["entry"]))
    if nodes[str(graph["entry"])]["type"] != "start":
        raise HarnessError("Executable graph entry must name a start node")
    present = {node["type"] for node_id, node in nodes.items() if node_id in reachable}
    required = {"planner", "coder", "tool", "end"}
    missing = sorted(required - present)
    if missing:
        raise HarnessError(f"Executable graph is missing reachable node types: {', '.join(missing)}")
    if "gauntlet" in present:
        raise HarnessError("Executable graphs must expand gauntlet macros into explicit tool and evaluator nodes")
    if expected.require_review and "evaluator" not in present:
        raise HarnessError("Executable graph must contain a reachable evaluator when review is required")
    profiles = config.get("providers", {}) or {}
    agents = config.get("agents", {}) or {}
    for node_id in reachable:
        node = nodes[node_id]
        if node.get("type") not in AGENT_NODE_TYPES:
            continue
        settings = node.get("config", {})
        route = str(settings.get("provider_route", ""))
        agent_ref = str(settings.get("agent_ref", ""))
        if agent_ref:
            if agent_ref not in agents:
                raise HarnessError(f"Executable graph agent {node_id} names an unknown trusted agent: {agent_ref}")
            if route:
                raise HarnessError(
                    f"Executable graph agent {node_id} must not combine agent_ref with provider_route"
                )
            route = str(agents[agent_ref].get("provider_ref", ""))
        if route and route != "default" and route not in profiles:
            raise HarnessError(f"Executable graph agent {node_id} names an unknown provider route: {route}")
        requested = set(settings.get("capabilities", []))
        denied = sorted(requested - GRAPH_AGENT_CAPABILITIES.get(str(node.get("type")), set()))
        if denied:
            raise HarnessError(f"Executable graph agent {node_id} requests denied capabilities: {', '.join(denied)}")
        if agent_ref:
            assigned = set(agents[agent_ref].get("capabilities", []))
            unassigned = sorted(requested - assigned)
            if unassigned:
                raise HarnessError(
                    f"Executable graph agent {node_id} requests capabilities not assigned to trusted agent "
                    f"{agent_ref}: {', '.join(unassigned)}"
                )
        profile = profiles.get(route, {}) if route and route != "default" else {}
        max_data_class = str(profile.get("max_data_class", "project_private"))
        requested_data_class = str(settings.get("data_class", "project_private"))
        ranks = {"public": 0, "project_private": 1, "restricted": 2}
        context_rank = max(ranks["project_private"], ranks[requested_data_class])
        if ranks.get(max_data_class, -1) < context_rank:
            raise HarnessError(
                f"Executable graph agent {node_id} routes project context above provider {route or 'default'} "
                f"max_data_class {max_data_class}"
            )
    if source_graph.get("schema_version") == 2 and uses_cooperative_execution(graph):
        cooperative_types = [str(nodes[node_id]["type"]) for node_id in reachable]
        if any(kind in {"approval_required", "gauntlet"} for kind in cooperative_types):
            raise HarnessError("Cooperative execution does not support approval or gauntlet macro nodes")
        for kind, reason in (("coder", "so mutations stay serialized"), ("end", "")):
            if cooperative_types.count(kind) != 1:
                suffix = f" {reason}" if reason else ""
                raise HarnessError(f"Cooperative execution requires exactly one {kind} node{suffix}")
    end_nodes = {node_id for node_id, node in nodes.items() if node["type"] == "end" and node_id in reachable}
    if not end_nodes:
        raise HarnessError("Executable graph has no path from entry to an end node")
    stranded = sorted(node_id for node_id in reachable if not _can_reach_any(outgoing, node_id, end_nodes))
    if stranded:
        raise HarnessError(f"Executable graph has reachable nodes with no path to an end node: {', '.join(stranded)}")
    planners = {node_id for node_id in reachable if nodes[node_id]["type"] == "planner"}
    coders = {node_id for node_id in reachable if nodes[node_id]["type"] == "coder"}
    entry = str(graph["entry"])
    if _can_reach_any(outgoing, entry, end_nodes, planners):
        raise HarnessError("Executable graph has a start-to-end route that bypasses the planner")
    if _can_reach_any(outgoing, entry, end_nodes, coders):
        raise HarnessError("Executable graph has a start-to-end route that bypasses the coder")
    for coder in coders:
        if _can_reach_any(outgoing, entry, {coder}, planners):
            raise HarnessError(f"Executable graph can reach coder {coder} before a planner")
    loop_edges = [edge for edge in graph["edges"] if edge.get("loop")]
    roles = []
    for node in graph["nodes"]:
        if node["id"] not in reachable:
            continue
        if node["type"] == "tool":
            roles.append(str(node.get("config", {}).get("role", "generic")))
    unknown_roles = sorted({role for role in roles if role not in TOOL_KINDS})
    if unknown_roles:
        raise HarnessError(f"Executable graph has unsupported tool roles: {', '.join(unknown_roles)}")
    verification_kinds = tuple(dict.fromkeys(TOOL_KINDS[role] for role in roles))
    required_kinds = tuple(dict.fromkeys(expected.verification_kinds))
    missing_kinds = sorted(set(required_kinds) - set(verification_kinds))
    if missing_kinds:
        raise HarnessError(f"Executable graph is missing required verification tool kinds: {', '.join(missing_kinds)}")
    reviewers = {node_id for node_id in reachable if nodes[node_id]["type"] == "evaluator"}
    tools_by_kind = {
        kind: {
            node_id
            for node_id in reachable
            if nodes[node_id]["type"] == "tool"
            and TOOL_KINDS[str(nodes[node_id].get("config", {}).get("role", "generic"))] == kind
        }
        for kind in required_kinds
    }
    for coder in coders:
        if not _can_reach_any(outgoing, coder, end_nodes):
            continue
        for kind, tool_nodes in tools_by_kind.items():
            if _can_reach_any(outgoing, coder, end_nodes, tool_nodes):
                raise HarnessError(f"Executable graph has a coder-to-end route that bypasses required {kind} verification")
            if expected.require_review and _can_reach_any(outgoing, coder, reviewers, tool_nodes):
                raise HarnessError(f"Executable graph can review coder output before required {kind} verification")
        if expected.require_review and _can_reach_any(outgoing, coder, end_nodes, reviewers):
            raise HarnessError("Executable graph has a coder-to-end route that bypasses required review")
    maximum = int(config.get("workflow.max_iterations"))
    elapsed = int(config.get("workflow.max_elapsed_seconds"))
    decay = float(config.get("workflow.temperature_decay"))
    if loop_edges:
        maximum = min(int(edge["loop"]["max_iterations"]) for edge in loop_edges)
        elapsed = min(elapsed, *(int(edge["loop"].get("timeout_seconds", elapsed)) for edge in loop_edges))
        decay = min(float(edge["loop"].get("temperature_decay", decay)) for edge in loop_edges)
    else:
        maximum = 1
    # The workflow freezes and hashes the exact caller graph. Validation uses
    # the migrated view, but the binding must retain the caller's byte-level
    # schema identity so v1 resumes and imported graphs do not mismatch.
    canonical = json.dumps(source_graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return WorkflowExecutionPolicy(
        name=str(graph.get("name") or "visual-graph"),
        max_iterations=maximum,
        max_elapsed_seconds=elapsed,
        temperature_decay=decay,
        include_lint="lint" in verification_kinds,
        include_build="build" in verification_kinds,
        require_review=expected.require_review,
        verification_kinds=verification_kinds,
        graph_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def built_in_workflow_graph(config: Any, plugin_nodes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the explicit graph used by non-UI runs."""
    policy = resolve_workflow_policy(config, plugin_nodes)
    if policy.name == "gauntlet":
        graph = json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8"))
        resolve_graph_execution_policy(config, graph, policy)
        return graph
    role_for_kind = {"lint": "syntax", "build": "build", "test": "unit_test"}
    nodes: list[dict[str, Any]] = [
        {"id": "start", "type": "start", "label": "Start"},
        {"id": "planner", "type": "planner", "label": "Planner"},
        {"id": "coder", "type": "coder", "label": "Coder"},
    ]
    tool_ids: list[str] = []
    for kind in policy.verification_kinds:
        node_id = f"check-{kind}"
        tool_ids.append(node_id)
        nodes.append({"id": node_id, "type": "tool", "label": f"{kind.title()} check", "config": {"role": role_for_kind[kind]}})
    if policy.require_review:
        nodes.append({"id": "reviewer", "type": "evaluator", "label": "Reviewer"})
    nodes.append({"id": "end", "type": "end", "label": "Complete"})
    loop = {
        "max_iterations": policy.max_iterations,
        "temperature_decay": policy.temperature_decay,
        "timeout_seconds": policy.max_elapsed_seconds,
    }
    edges: list[dict[str, Any]] = [
        {"id": "start-plan", "source": "start", "target": "planner", "variables": ["task"]},
        {"id": "plan-code", "source": "planner", "target": "coder", "condition": "plan_ready == true", "variables": ["plan"]},
        {"id": "code-first", "source": "coder", "target": tool_ids[0], "variables": ["candidate"]},
    ]
    for index, node_id in enumerate(tool_ids):
        next_id = tool_ids[index + 1] if index + 1 < len(tool_ids) else "reviewer" if policy.require_review else "end"
        edges.append({"id": f"{node_id}-pass", "source": node_id, "target": next_id, "condition": "stage_passed == true", "variables": ["verification"]})
        edges.append({"id": f"{node_id}-repair", "source": node_id, "target": "coder", "condition": "stage_passed == false", "variables": ["failure"], "loop": dict(loop)})
    if policy.require_review:
        edges.append({"id": "review-pass", "source": "reviewer", "target": "end", "condition": "review_passed == true", "variables": ["review"]})
        edges.append({"id": "review-repair", "source": "reviewer", "target": "coder", "condition": "review_passed == false", "variables": ["failure"], "loop": dict(loop)})
    graph = {"schema_version": 1, "name": policy.name, "entry": "start", "nodes": nodes, "edges": edges}
    resolve_graph_execution_policy(config, graph, policy)
    return graph


def _reachable(adjacency: dict[str, list[str]], start: str, blocked: set[str] | None = None) -> set[str]:
    excluded = blocked or set()
    if start in excluded:
        return set()
    visited: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in visited or current in excluded:
            continue
        visited.add(current)
        pending.extend(adjacency.get(current, []))
    return visited


def _can_reach_any(adjacency: dict[str, list[str]], start: str, targets: set[str], blocked: set[str] | None = None) -> bool:
    return bool(_reachable(adjacency, start, blocked) & targets)


def _has_path(adjacency: dict[str, list[str]], start: str, target: str) -> bool:
    return target in _reachable(adjacency, start)


def _literal_value(literal: str) -> Any:
    lowered = literal.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if literal.startswith('"'):
        return literal[1:-1]
    return float(literal) if "." in literal else int(literal)


def _impossible_production_condition(node: dict[str, Any], expression: str) -> str:
    if not expression:
        return ""
    match = CONDITION.fullmatch(expression.strip())
    if not match:
        return "has an invalid condition"
    key, operator, literal = match.groups()
    if key.split(".", 1)[0] not in PRODUCTION_STATE_ROOTS:
        return f"references unknown production state: {key}"
    fixed = FIXED_NODE_STATE.get(str(node.get("type")), {})
    if key not in fixed:
        return ""
    matches = fixed[key] == _literal_value(literal)
    if matches != (operator == "=="):
        return f"has an impossible condition for {node.get('type')} state: {expression}"
    return ""


def _condition_accepts_failed_completion(expression: str) -> bool:
    match = CONDITION.fullmatch(expression.strip())
    if not match:
        return False
    key, operator, literal = match.groups()
    if key not in {"stage_passed", "tests_passed", "review_passed"}:
        return False
    expected = _literal_value(literal)
    return isinstance(expected, bool) and ((operator == "==" and not expected) or (operator == "!=" and expected))


def validate_graph(graph: dict[str, Any]) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    if not isinstance(graph, dict):
        return [GraphIssue("graph", "must be an object")]
    if graph.get("schema_version") not in {1, 2}:
        issues.append(GraphIssue("schema_version", "must be 1 or 2"))
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return [GraphIssue("graph", "nodes and edges must be arrays")]
    if len(nodes) > GRAPH_MAX_NODES:
        issues.append(GraphIssue("nodes", f"must contain at most {GRAPH_MAX_NODES} nodes"))
    if len(edges) > GRAPH_MAX_EDGES:
        issues.append(GraphIssue("edges", f"must contain at most {GRAPH_MAX_EDGES} edges"))
    schema_v2 = graph.get("schema_version") == 2
    ids: set[str] = set()
    node_map: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            issues.append(GraphIssue(f"nodes[{index}]", "node needs a non-empty id"))
            continue
        if node["id"] in ids:
            issues.append(GraphIssue(f"nodes[{index}].id", "node id must be unique"))
        ids.add(node["id"])
        node_map[node["id"]] = node
        if node.get("type") not in NODE_TYPES:
            issues.append(GraphIssue(f"nodes[{index}].type", f"unknown node type: {node.get('type')}"))
        if node.get("type") == "approval_required" and not isinstance(node.get("config", {}), dict):
            issues.append(GraphIssue(f"nodes[{index}].config", "approval_required config must be an object"))
        if schema_v2 and node.get("type") in AGENT_NODE_TYPES:
            issues.extend(_validate_agent_node(node, index))
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in ids}
    merge_slots: dict[str, set[str]] = {}
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            issues.append(GraphIssue(f"edges[{index}]", "edge must be an object"))
            continue
        source, target = edge.get("source"), edge.get("target")
        if source not in ids or target not in ids:
            issues.append(GraphIssue(f"edges[{index}]", "edge source and target must name existing nodes"))
            continue
        condition = edge.get("condition", "")
        if condition and not CONDITION.fullmatch(condition.strip()):
            issues.append(GraphIssue(f"edges[{index}].condition", "condition must be key == literal or key != literal"))
        if not isinstance(edge.get("variables", []), list) or not all(isinstance(item, str) for item in edge.get("variables", [])):
            issues.append(GraphIssue(f"edges[{index}].variables", "variables must be a string array"))
        if schema_v2:
            mode = edge.get("mode", "state")
            if mode not in EDGE_MODES:
                issues.append(GraphIssue(f"edges[{index}].mode", f"unknown edge mode: {mode}"))
            return_fields = edge.get("return_fields", [])
            if not isinstance(return_fields, list) or not all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in return_fields):
                issues.append(GraphIssue(f"edges[{index}].return_fields", "return_fields must contain valid state field names"))
            if mode == "delegate" and source == target:
                issues.append(GraphIssue(f"edges[{index}]", "delegation target must differ from its source"))
            if mode == "merge_input":
                slot = edge.get("target_slot")
                if node_map.get(target, {}).get("type") != "merge":
                    issues.append(GraphIssue(f"edges[{index}].target", "merge_input must target a merge node"))
                if not isinstance(slot, str) or not IDENTIFIER.fullmatch(slot):
                    issues.append(GraphIssue(f"edges[{index}].target_slot", "merge_input needs a valid target_slot"))
                elif slot in merge_slots.setdefault(str(target), set()):
                    issues.append(GraphIssue(f"edges[{index}].target_slot", f"merge slot is already connected: {slot}"))
                else:
                    merge_slots.setdefault(str(target), set()).add(slot)
        creates_cycle = _has_path(adjacency, target, source)
        loop = edge.get("loop", {})
        if loop and not creates_cycle:
            issues.append(GraphIssue(f"edges[{index}].loop", "loop settings are allowed only on a cyclical edge"))
        if creates_cycle:
            if not isinstance(loop, dict) or not isinstance(loop.get("max_iterations"), int) or loop["max_iterations"] <= 0:
                issues.append(GraphIssue(f"edges[{index}].loop", "cyclical edge needs a positive max_iterations"))
            if loop.get("timeout_seconds", 0) and (not isinstance(loop["timeout_seconds"], int) or loop["timeout_seconds"] <= 0):
                issues.append(GraphIssue(f"edges[{index}].loop.timeout_seconds", "timeout_seconds must be positive"))
            decay = loop.get("temperature_decay", 1.0)
            if not isinstance(decay, (int, float)) or not 0 < decay <= 1:
                issues.append(GraphIssue(f"edges[{index}].loop.temperature_decay", "temperature_decay must be greater than 0 and at most 1"))
        adjacency[source].append(target)
    entry = graph.get("entry")
    if entry not in ids:
        issues.append(GraphIssue("entry", "entry must name an existing node"))
    if schema_v2:
        for index, node in enumerate(nodes):
            if not isinstance(node, dict) or node.get("type") != "merge" or not isinstance(node.get("id"), str):
                continue
            required = node.get("config", {}).get("required_slots", [])
            missing = sorted(set(required) - merge_slots.get(node["id"], set()))
            if missing:
                issues.append(GraphIssue(f"nodes[{index}].config.required_slots", f"merge slots are not connected: {', '.join(missing)}"))
    return issues


def _validate_agent_node(node: dict[str, Any], index: int) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    config = node.get("config", {})
    path = f"nodes[{index}].config"
    if not isinstance(config, dict):
        return [GraphIssue(path, "agent config must be an object")]
    for key in ("provider_route", "model", "role_name"):
        value = config.get(key, "")
        if not isinstance(value, str) or len(value) > 256 or (value and key != "role_name" and not IDENTIFIER.fullmatch(value)):
            issues.append(GraphIssue(f"{path}.{key}", f"{key} must be a valid string of at most 256 characters"))
    agent_ref = config.get("agent_ref", "")
    if not isinstance(agent_ref, str) or (agent_ref and not IDENTIFIER.fullmatch(agent_ref)):
        issues.append(GraphIssue(f"{path}.agent_ref", "agent_ref must be a valid trusted agent ID"))
    if agent_ref and config.get("provider_route"):
        issues.append(GraphIssue(path, "agent_ref and provider_route are mutually exclusive"))
    data_class = config.get("data_class", "project_private")
    if data_class not in {"public", "project_private", "restricted"}:
        issues.append(GraphIssue(f"{path}.data_class", "data_class must be public, project_private, or restricted"))
    prompt = config.get("system_prompt", "")
    if not isinstance(prompt, str) or len(prompt) > GRAPH_MAX_PROMPT_CHARS:
        issues.append(GraphIssue(f"{path}.system_prompt", f"system_prompt must contain at most {GRAPH_MAX_PROMPT_CHARS} characters"))
    capabilities = config.get("capabilities", [])
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > GRAPH_MAX_CAPABILITIES
        or len(capabilities) != len(set(item for item in capabilities if isinstance(item, str)))
        or not all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in capabilities)
    ):
        issues.append(GraphIssue(f"{path}.capabilities", "capabilities must be a unique array of valid capability names"))
    if node.get("type") == "merge":
        slots = config.get("required_slots")
        if (
            not isinstance(slots, list)
            or not 2 <= len(slots) <= 16
            or len(slots) != len(set(item for item in slots if isinstance(item, str)))
            or not all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in slots)
        ):
            issues.append(GraphIssue(f"{path}.required_slots", "merge nodes need 2 to 16 unique required_slots"))
        output = config.get("output_field", "merged_output")
        if not isinstance(output, str) or not IDENTIFIER.fullmatch(output):
            issues.append(GraphIssue(f"{path}.output_field", "output_field must be a valid state field name"))
        contract = config.get("output_contract", "implementation_plan")
        if contract not in MERGE_OUTPUT_CONTRACTS:
            issues.append(GraphIssue(
                f"{path}.output_contract",
                "output_contract must be implementation_plan",
            ))
    return issues


def migrate_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Return a detached schema-v2 graph without changing the caller's value."""
    if not isinstance(graph, dict):
        return copy.deepcopy(graph)
    migrated = copy.deepcopy(graph)
    if migrated.get("schema_version") == 2:
        return migrated
    if migrated.get("schema_version") != 1:
        return migrated
    migrated["schema_version"] = 2
    for node in migrated.get("nodes", []):
        if not isinstance(node, dict) or node.get("type") not in AGENT_NODE_TYPES:
            continue
        config = node.setdefault("config", {})
        if not isinstance(config, dict):
            continue
        config.setdefault("provider_route", "")
        config.setdefault("model", "")
        config.setdefault("role_name", str(node.get("label") or node.get("type") or "agent"))
        config.setdefault("system_prompt", "")
        config.setdefault("capabilities", [])
        if node.get("type") == "merge":
            config.setdefault("output_contract", "implementation_plan")
    for edge in migrated.get("edges", []):
        if isinstance(edge, dict):
            edge.setdefault("mode", "state")
            edge.setdefault("return_fields", [])
    return migrated


def uses_cooperative_execution(graph: dict[str, Any]) -> bool:
    """Select the cooperative engine only for explicit v2 cooperation contracts."""
    if not isinstance(graph, dict) or graph.get("schema_version") != 2:
        return False
    if (
        any(isinstance(node, dict) and node.get("type") == "merge" for node in graph.get("nodes", []))
        or any(
            isinstance(edge, dict) and edge.get("mode") in {"delegate", "merge_input"}
            for edge in graph.get("edges", [])
        )
    ):
        return True
    outgoing: dict[str, list[str]] = {}
    for edge in graph.get("edges", []):
        if isinstance(edge, dict) and isinstance(edge.get("source"), str):
            outgoing.setdefault(str(edge["source"]), []).append(str(edge.get("condition", "")).strip())
    return any(
        _conditions_can_overlap(left, right)
        for conditions in outgoing.values()
        for index, left in enumerate(conditions)
        for right in conditions[index + 1:]
    )


def _conditions_can_overlap(left: str, right: str) -> bool:
    """Return true unless two validated edge conditions are provably exclusive."""
    if not left or not right:
        return True
    left_match = CONDITION.fullmatch(left)
    right_match = CONDITION.fullmatch(right)
    if left_match is None or right_match is None:
        return True
    left_key, left_operator, left_literal = left_match.groups()
    right_key, right_operator, right_literal = right_match.groups()
    if left_key != right_key:
        return True
    left_value = _literal_value(left_literal)
    right_value = _literal_value(right_literal)
    if left_operator == "==" and right_operator == "==":
        return left_value == right_value
    if left_operator == "==" and right_operator == "!=":
        return left_value != right_value
    if left_operator == "!=" and right_operator == "==":
        return left_value != right_value
    return True


def _value(state: dict[str, Any], dotted: str) -> Any:
    value: Any = state
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def condition_matches(expression: str, state: dict[str, Any]) -> bool:
    if not expression:
        return True
    match = CONDITION.fullmatch(expression.strip())
    if not match:
        return False
    key, operator, literal = match.groups()
    expected = _literal_value(literal)
    return (_value(state, key) == expected) == (operator == "==")


def _simulate_node(node: dict[str, Any], state: dict[str, Any]) -> None:
    node_type = node.get("type")
    state["active_node"] = node["id"]
    if node_type == "planner":
        state["plan_ready"] = True
    elif node_type == "coder":
        state["code_version"] = int(state.get("code_version", 0)) + 1
        state["source_code"] = f"candidate-{state['code_version']}"
        state["stage_passed"] = True
    elif node_type == "tool":
        role = node.get("config", {}).get("role", node["id"])
        if role == "unit_test" and int(state.get("test_failures_remaining", 0)) > 0:
            state["test_failures_remaining"] -= 1
            state["stage_passed"] = False
            state["error_trace"] = "Simulated unit test failure"
        else:
            state["stage_passed"] = True
            state["test_results"] = f"{role}: pass"
    elif node_type in {"evaluator", "gauntlet"}:
        state["tests_passed"] = bool(state.get("stage_passed", False))
    elif node_type == "approval_required":
        state["approval_required"] = True
    elif node_type == "end":
        state["complete"] = True


def simulate_graph(graph: dict[str, Any], initial_state: dict[str, Any] | None = None, max_steps: int = 100) -> dict[str, Any]:
    issues = validate_graph(graph)
    if issues:
        raise HarnessError("Graph is invalid: " + "; ".join(f"{issue.path}: {issue.message}" for issue in issues))
    nodes = {node["id"]: node for node in graph["nodes"]}
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in graph["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge)
    state = copy.deepcopy(initial_state or {})
    state.setdefault("test_failures_remaining", 1)
    current = graph["entry"]
    transitions: list[dict[str, Any]] = []
    loop_counts: dict[str, int] = {}
    for step in range(max_steps):
        node = nodes[current]
        _simulate_node(node, state)
        transitions.append({"step": step, "node": current, "status": "complete", "state": copy.deepcopy(state)})
        if node.get("type") == "approval_required":
            return {"state": state, "transitions": transitions, "complete": False, "stopped_at": current, "paused": True}
        candidates = [edge for edge in outgoing.get(current, []) if condition_matches(edge.get("condition", ""), state)]
        if not candidates:
            return {"state": state, "transitions": transitions, "complete": bool(state.get("complete")), "stopped_at": current}
        edge = candidates[0]
        edge_id = edge.get("id", f"{edge['source']}->{edge['target']}")
        if edge.get("loop"):
            loop_counts[edge_id] = loop_counts.get(edge_id, 0) + 1
            if loop_counts[edge_id] > int(edge["loop"]["max_iterations"]):
                return {"state": state, "transitions": transitions, "complete": False, "error": f"Loop limit reached: {edge_id}"}
            state["temperature"] = float(state.get("temperature", 0.2)) * float(edge["loop"].get("temperature_decay", 1.0))
        transferred = {key: _value(state, key) for key in edge.get("variables", [])}
        transitions.append({"step": step, "edge": edge_id, "source": current, "target": edge["target"], "variables": transferred})
        current = edge["target"]
    return {"state": state, "transitions": transitions, "complete": False, "error": "Graph step limit reached"}
