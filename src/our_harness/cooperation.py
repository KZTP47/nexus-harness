from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any

from .graphs import condition_matches, migrate_graph, validate_graph
from .models import HarnessError


COOPERATIVE_SNAPSHOT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CooperativeDispatch:
    node_id: str
    node_type: str
    inputs: dict[str, Any]
    attempt: int


class CooperativeScheduler:
    """Deterministic fan-out/fan-in state for a cooperative graph.

    The scheduler does not execute providers. A runtime requests ready work,
    dispatches it through its own bounded provider pool, then commits results.
    """

    def __init__(
        self,
        graph: dict[str, Any],
        *,
        max_parallelism: int = 4,
        max_dispatches: int = 256,
        timeout_seconds: float = 1800,
    ) -> None:
        normalized = migrate_graph(graph)
        issues = validate_graph(normalized)
        if issues:
            raise HarnessError("Cooperative graph is invalid: " + "; ".join(item.message for item in issues))
        if not 1 <= max_parallelism <= 32 or not 1 <= max_dispatches <= 10_000 or timeout_seconds <= 0:
            raise HarnessError("Cooperative scheduler limits are invalid")
        self.graph = normalized
        self.nodes = {str(node["id"]): node for node in normalized["nodes"]}
        self.node_order = {str(node["id"]): index for index, node in enumerate(normalized["nodes"])}
        self.outgoing: dict[str, list[dict[str, Any]]] = {}
        self.incoming: dict[str, list[dict[str, Any]]] = {}
        for edge in normalized["edges"]:
            self.outgoing.setdefault(str(edge["source"]), []).append(edge)
            self.incoming.setdefault(str(edge["target"]), []).append(edge)
        self.max_parallelism = max_parallelism
        self.max_dispatches = max_dispatches
        self.started_at = time.monotonic()
        self.deadline_at = self.started_at + timeout_seconds
        self.dispatches = 0
        self.attempts: dict[str, int] = {}
        self.running: set[str] = set()
        self.completed: dict[str, dict[str, Any]] = {}
        self.failed: dict[str, str] = {}
        self.state: dict[str, Any] = {}
        self.loop_counts: dict[str, int] = {}
        self.loop_started: dict[str, float] = {}
        self._redispatch_attempts: dict[str, int] = {}
        self._delegate_returns: dict[str, dict[str, list[str]]] = {}
        self._available: dict[str, dict[str, Any]] = {
            str(normalized["entry"]): {"task": None}
        }
        self._merge_inputs: dict[str, dict[str, Any]] = {}

    def _check_budget(self, now: float | None = None, *, dispatch: bool = True) -> None:
        if (time.monotonic() if now is None else now) >= self.deadline_at:
            raise HarnessError("Cooperative scheduler reached its timeout")
        if dispatch and self.dispatches >= self.max_dispatches:
            raise HarnessError("Cooperative scheduler reached its dispatch limit")

    def set_entry_state(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise HarnessError("Cooperative entry state must be an object")
        self.state = copy.deepcopy(state)
        self._available[str(self.graph["entry"])] = copy.deepcopy(state)

    def ready(self, now: float | None = None) -> tuple[CooperativeDispatch, ...]:
        slots = max(0, self.max_parallelism - len(self.running))
        candidates = self._ready_candidates()
        replay_ids = set(self._redispatch_attempts)
        replay_ready = any(node_id in replay_ids for _, node_id, _ in candidates)
        self._check_budget(now, dispatch=not replay_ready)
        if self.dispatches >= self.max_dispatches:
            candidates = [item for item in candidates if item[1] in replay_ids]
        candidates.sort(key=lambda item: (item[1] not in replay_ids, item[0], item[1]))
        dispatches: list[CooperativeDispatch] = []
        for _, node_id, inputs in candidates[:slots]:
            replay_attempt = self._redispatch_attempts.pop(node_id, None)
            if replay_attempt is None:
                self._check_budget(now)
                self.dispatches += 1
                self.attempts[node_id] = self.attempts.get(node_id, 0) + 1
            else:
                self._check_budget(now, dispatch=False)
                self.attempts[node_id] = replay_attempt
            self.running.add(node_id)
            dispatches.append(
                CooperativeDispatch(
                    node_id,
                    str(self.nodes[node_id]["type"]),
                    copy.deepcopy(inputs),
                    self.attempts[node_id],
                )
            )
        return tuple(dispatches)

    def _ready_candidates(self) -> list[tuple[int, str, dict[str, Any]]]:
        candidates = []
        for node_id, inputs in self._available.items():
            if node_id in self.running or node_id in self.completed or node_id in self.failed:
                continue
            node = self.nodes[node_id]
            if node.get("type") == "end" and any(
                str(edge["source"]) in self.running
                or (
                    str(edge["source"]) in self._available
                    and str(edge["source"]) not in self.completed
                    and str(edge["source"]) not in self.failed
                )
                for edge in self.incoming.get(node_id, [])
            ):
                # A shared end node is an implicit fan-in barrier. Do not let a
                # crash after one branch commit orphan another activated branch.
                continue
            if node.get("type") == "merge":
                required = set(node.get("config", {}).get("required_slots", []))
                if not required.issubset(self._merge_inputs.get(node_id, {})):
                    continue
                inputs = {**inputs, "merge_inputs": copy.deepcopy(self._merge_inputs[node_id])}
            candidates.append((self.node_order[node_id], node_id, inputs))
        return candidates

    def complete(self, node_id: str, output: dict[str, Any], now: float | None = None) -> None:
        self._check_budget(now, dispatch=False)
        if node_id not in self.running:
            raise HarnessError(f"Cooperative node is not running: {node_id}")
        if not isinstance(output, dict):
            raise HarnessError("Cooperative node output must be an object")
        contracts = self._delegate_returns.get(node_id, {})
        missing_returns = sorted({
            field
            for fields in contracts.values()
            for field in fields
            if field not in output
        })
        if missing_returns:
            raise HarnessError(
                f"Cooperative delegate {node_id} omitted declared return fields: {', '.join(missing_returns)}"
            )
        self.running.remove(node_id)
        self._delegate_returns.pop(node_id, None)
        safe_output = copy.deepcopy(output)
        self.state.update(copy.deepcopy(safe_output))
        self.completed[node_id] = safe_output
        combined = {**self.state, **safe_output}
        for edge in self.outgoing.get(node_id, []):
            if not condition_matches(str(edge.get("condition", "")), combined):
                continue
            target = str(edge["target"])
            loop = edge.get("loop")
            if loop:
                edge_id = str(edge.get("id") or f"{edge['source']}->{edge['target']}")
                count = self.loop_counts.get(edge_id, 0) + 1
                if count > int(loop["max_iterations"]):
                    raise HarnessError(f"Cooperative graph loop limit reached: {edge_id}")
                started = self.loop_started.setdefault(edge_id, time.monotonic() if now is None else now)
                timeout = int(loop.get("timeout_seconds", 0))
                current_time = time.monotonic() if now is None else now
                if timeout and current_time - started > timeout:
                    raise HarnessError(f"Cooperative graph loop timeout reached: {edge_id}")
                self.loop_counts[edge_id] = count
                self.state["temperature"] = float(self.state.get("temperature", 0.2)) * float(loop.get("temperature_decay", 1.0))
            # A second activation represents a new generation of downstream
            # work. This is required for bounded repair and delegation loops.
            self.completed.pop(target, None)
            self.failed.pop(target, None)
            values = {
                name: copy.deepcopy(_state_value(combined, name))
                for name in edge.get("variables", [])
            }
            if edge.get("mode") == "merge_input":
                slot = str(edge["target_slot"])
                self._merge_inputs.setdefault(target, {})[slot] = values
                self._available.setdefault(target, {})
            else:
                destination = self._available.setdefault(target, {})
                destination.update(values)
                if edge.get("mode") == "delegate":
                    destination["delegated_by"] = node_id
                    edge_id = str(edge.get("id") or f"{edge['source']}->{edge['target']}")
                    self._delegate_returns.setdefault(target, {})[edge_id] = list(edge.get("return_fields", []))

    def fail(self, node_id: str, error: str) -> None:
        if node_id not in self.running:
            raise HarnessError(f"Cooperative node is not running: {node_id}")
        self.running.remove(node_id)
        self.failed[node_id] = str(error)[:8000]

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        current_time = time.monotonic() if now is None else float(now)
        if not math.isfinite(current_time):
            raise HarnessError("Cooperative snapshot time must be finite")
        elapsed = max(0.0, current_time - self.started_at)
        remaining = max(0.0, self.deadline_at - current_time)
        payload = {
            "schema_version": COOPERATIVE_SNAPSHOT_SCHEMA_VERSION,
            "graph_sha256": _graph_sha256(self.graph),
            "limits": {
                "max_parallelism": self.max_parallelism,
                "max_dispatches": self.max_dispatches,
                "elapsed_seconds": elapsed,
                "remaining_deadline_seconds": remaining,
            },
            "dispatches": self.dispatches,
            "attempts": dict(self.attempts),
            "running": sorted(self.running, key=self.node_order.get),
            "ready": [node_id for _, node_id, _ in sorted(self._ready_candidates())],
            "redispatch_attempts": dict(self._redispatch_attempts),
            "completed": copy.deepcopy(self.completed),
            "failed": dict(self.failed),
            "available": copy.deepcopy(self._available),
            "merge_inputs": copy.deepcopy(self._merge_inputs),
            "delegate_returns": copy.deepcopy(self._delegate_returns),
            "state": copy.deepcopy(self.state),
            "loop_counts": dict(self.loop_counts),
            "loop_elapsed_seconds": {
                edge_id: max(0.0, current_time - started)
                for edge_id, started in self.loop_started.items()
            },
        }
        return _json_detached(payload, "Cooperative scheduler snapshot")

    @classmethod
    def restore(
        cls,
        graph: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        now: float | None = None,
    ) -> "CooperativeScheduler":
        """Restore a validated snapshot and requeue interrupted dispatches."""
        normalized = migrate_graph(graph)
        value = _json_detached(snapshot, "Cooperative scheduler snapshot")
        if not isinstance(value, dict):
            raise HarnessError("Cooperative scheduler snapshot must be an object")
        if value.get("schema_version") != COOPERATIVE_SNAPSHOT_SCHEMA_VERSION:
            raise HarnessError("Cooperative scheduler snapshot has an unsupported schema")
        if value.get("graph_sha256") != _graph_sha256(normalized):
            raise HarnessError("Cooperative scheduler snapshot graph hash does not match")
        limits = value.get("limits")
        if not isinstance(limits, dict):
            raise HarnessError("Cooperative scheduler snapshot limits are invalid")
        max_parallelism = _snapshot_int(limits, "max_parallelism", 1, 32)
        max_dispatches = _snapshot_int(limits, "max_dispatches", 1, 10_000)
        elapsed = _snapshot_number(limits, "elapsed_seconds")
        remaining = _snapshot_number(limits, "remaining_deadline_seconds")
        if elapsed + remaining <= 0:
            raise HarnessError("Cooperative scheduler snapshot deadline is invalid")
        current_time = time.monotonic() if now is None else float(now)
        if not math.isfinite(current_time):
            raise HarnessError("Cooperative restore time must be finite")
        scheduler = cls(
            normalized,
            max_parallelism=max_parallelism,
            max_dispatches=max_dispatches,
            timeout_seconds=elapsed + remaining,
        )
        scheduler.started_at = current_time - elapsed
        scheduler.deadline_at = current_time + remaining
        node_ids = set(scheduler.nodes)
        loop_limits = {
            str(edge.get("id") or f"{edge['source']}->{edge['target']}"): int(edge["loop"]["max_iterations"])
            for edge in normalized["edges"]
            if edge.get("loop")
        }
        dispatches = _snapshot_int(value, "dispatches", 0, max_dispatches)
        attempts = _node_int_map(value.get("attempts"), node_ids, "attempts")
        if sum(attempts.values()) != dispatches:
            raise HarnessError("Cooperative scheduler snapshot dispatch count does not match attempts")
        running = _node_list(value.get("running"), node_ids, "running")
        ready = _node_list(value.get("ready"), node_ids, "ready")
        redispatch_attempts = _node_int_map(value.get("redispatch_attempts"), node_ids, "redispatch_attempts")
        completed = _node_object_map(value.get("completed"), node_ids, "completed")
        failed = value.get("failed")
        if (
            not isinstance(failed, dict)
            or not set(failed).issubset(node_ids)
            or not all(isinstance(item, str) and len(item) <= 8000 for item in failed.values())
        ):
            raise HarnessError("Cooperative scheduler snapshot failed nodes are invalid")
        available = _node_object_map(value.get("available"), node_ids, "available")
        if not (set(running) | set(redispatch_attempts)).issubset(available):
            raise HarnessError("Cooperative scheduler snapshot interrupted nodes have no retained inputs")
        if len(running) > max_parallelism:
            raise HarnessError("Cooperative scheduler snapshot has too many running nodes")
        if (
            (set(running) & set(completed))
            or (set(running) & set(failed))
            or (set(completed) & set(failed))
            or (set(redispatch_attempts) & (set(running) | set(completed) | set(failed)))
        ):
            raise HarnessError("Cooperative scheduler snapshot node states overlap")
        state = value.get("state")
        if not isinstance(state, dict):
            raise HarnessError("Cooperative scheduler snapshot shared state is invalid")
        merge_inputs = value.get("merge_inputs")
        if not isinstance(merge_inputs, dict) or not set(merge_inputs).issubset(node_ids):
            raise HarnessError("Cooperative scheduler snapshot merge inputs are invalid")
        for node_id, slots in merge_inputs.items():
            node = scheduler.nodes[node_id]
            required = set(node.get("config", {}).get("required_slots", []))
            if (
                node.get("type") != "merge"
                or not isinstance(slots, dict)
                or not set(slots).issubset(required)
                or not all(isinstance(item, dict) for item in slots.values())
            ):
                raise HarnessError("Cooperative scheduler snapshot merge inputs do not match the graph")
        delegate_returns = value.get("delegate_returns")
        delegate_edges = {
            str(edge.get("id") or f"{edge['source']}->{edge['target']}"): edge
            for edge in normalized["edges"]
            if edge.get("mode") == "delegate"
        }
        if not isinstance(delegate_returns, dict) or not set(delegate_returns).issubset(node_ids):
            raise HarnessError("Cooperative scheduler snapshot delegate returns are invalid")
        for node_id, contracts in delegate_returns.items():
            if not isinstance(contracts, dict):
                raise HarnessError("Cooperative scheduler snapshot delegate returns are invalid")
            for edge_id, fields in contracts.items():
                edge = delegate_edges.get(edge_id)
                if (
                    edge is None
                    or edge["target"] != node_id
                    or fields != edge.get("return_fields", [])
                    or not isinstance(fields, list)
                ):
                    raise HarnessError("Cooperative scheduler snapshot delegate returns do not match the graph")
        loop_counts = value.get("loop_counts")
        if (
            not isinstance(loop_counts, dict)
            or not set(loop_counts).issubset(loop_limits)
            or not all(
                isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= loop_limits[edge_id]
                for edge_id, count in loop_counts.items()
            )
        ):
            raise HarnessError("Cooperative scheduler snapshot loop counts are invalid")
        loop_elapsed = value.get("loop_elapsed_seconds")
        if (
            not isinstance(loop_elapsed, dict)
            or set(loop_elapsed) != set(loop_counts)
            or not all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)) and 0 <= item <= elapsed for item in loop_elapsed.values())
        ):
            raise HarnessError("Cooperative scheduler snapshot loop elapsed times are invalid")
        if not set(attempts).issuperset(set(running) | set(completed) | set(failed) | set(redispatch_attempts)):
            raise HarnessError("Cooperative scheduler snapshot node states have no dispatch attempt")
        if any(attempts[node_id] != attempt for node_id, attempt in redispatch_attempts.items()):
            raise HarnessError("Cooperative scheduler snapshot redispatch attempts do not match")
        scheduler.dispatches = dispatches
        scheduler.attempts = attempts
        # Provider calls may have been interrupted after dispatch but before a
        # durable result. Requeue them with retained inputs for idempotent work.
        scheduler.running = set(running)
        scheduler.completed = copy.deepcopy(completed)
        scheduler.failed = dict(failed)
        scheduler._available = copy.deepcopy(available)
        scheduler._merge_inputs = copy.deepcopy(merge_inputs)
        scheduler._delegate_returns = copy.deepcopy(delegate_returns)
        scheduler.state = copy.deepcopy(state)
        scheduler.loop_counts = dict(loop_counts)
        scheduler.loop_started = {
            edge_id: current_time - float(seconds)
            for edge_id, seconds in loop_elapsed.items()
        }
        expected_ready = [node_id for _, node_id, _ in sorted(scheduler._ready_candidates())]
        if ready != expected_ready:
            raise HarnessError("Cooperative scheduler snapshot ready nodes do not match retained state")
        scheduler.running = set()
        scheduler._redispatch_attempts = {
            **redispatch_attempts,
            **{node_id: attempts[node_id] for node_id in running},
        }
        return scheduler


def _state_value(state: dict[str, Any], dotted: str) -> Any:
    value: Any = state
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _graph_sha256(graph: dict[str, Any]) -> str:
    raw = json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json_detached(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise HarnessError(f"{label} must contain JSON-compatible finite values") from exc


def _snapshot_int(value: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise HarnessError(f"Cooperative scheduler snapshot {key} is invalid")
    return item


def _snapshot_number(value: dict[str, Any], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or item < 0:
        raise HarnessError(f"Cooperative scheduler snapshot {key} is invalid")
    return float(item)


def _node_list(value: Any, node_ids: set[str], label: str) -> list[str]:
    if not isinstance(value, list) or len(value) != len(set(value)) or not all(isinstance(item, str) and item in node_ids for item in value):
        raise HarnessError(f"Cooperative scheduler snapshot {label} nodes are invalid")
    return list(value)


def _node_int_map(value: Any, node_ids: set[str], label: str) -> dict[str, int]:
    if (
        not isinstance(value, dict)
        or not set(value).issubset(node_ids)
        or not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value.values())
    ):
        raise HarnessError(f"Cooperative scheduler snapshot {label} is invalid")
    return dict(value)


def _node_object_map(value: Any, node_ids: set[str], label: str) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or not set(value).issubset(node_ids)
        or not all(isinstance(item, dict) for item in value.values())
    ):
        raise HarnessError(f"Cooperative scheduler snapshot {label} is invalid")
    return copy.deepcopy(value)
