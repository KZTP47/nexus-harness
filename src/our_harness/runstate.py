from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, replace
from typing import Any

from .models import HarnessError


RUN_CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINT_JSON_BYTES = 2_000_000
MAX_VALUE_DEPTH = 32
MAX_VALUE_NODES = 100_000

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "private_key",
    "cookie",
    "token",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:^|[\s\"'=(:,])(?:[A-Z]:[\\/])")
_UNC_ABSOLUTE = re.compile(r"(?:^|[\s\"'=(:,])(?:\\\\|//)[^\s\\/]+[\\/][^\s\\/]+")
_POSIX_ABSOLUTE = re.compile(r"(?:^|[\s\"'=(:,])/(?!/|\s)[^\s\"',)]+")


class RunCheckpointConflict(HarnessError):
    """A checkpoint writer lost a compare-and-swap race."""


def canonical_json(value: Any) -> str:
    """Serialize JSON evidence once, with stable Unicode-preserving bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def graph_sha256(graph: dict[str, Any]) -> str:
    validate_checkpoint_value(graph, "frozen_graph")
    return canonical_json_sha256(graph)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _is_sensitive_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return normalized in _SENSITIVE_KEYS or any(normalized.endswith("_" + name) for name in _SENSITIVE_KEYS)


def _validate_string(value: str, location: str) -> None:
    if "\x00" in value:
        raise HarnessError(f"Run checkpoint contains a NUL character at {location}")
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise HarnessError(f"Run checkpoint contains credential-like text at {location}")
    url_match = re.fullmatch(r"([A-Za-z][A-Za-z0-9+.-]*)://[^\s]+", value.strip())
    is_file_url = bool(url_match and url_match.group(1).casefold() == "file")
    is_network_url = bool(url_match and not is_file_url)
    if is_file_url or (
        not is_network_url
        and (
            _WINDOWS_ABSOLUTE.search(value)
            or _UNC_ABSOLUTE.search(value)
            or _POSIX_ABSOLUTE.search(value)
            or value.strip().startswith("/")
        )
    ):
        raise HarnessError(f"Run checkpoint contains an absolute filesystem path at {location}")


def validate_checkpoint_value(value: Any, location: str = "state") -> None:
    nodes = 0

    def visit(item: Any, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_VALUE_NODES:
            raise HarnessError("Run checkpoint contains too many values")
        if depth > MAX_VALUE_DEPTH:
            raise HarnessError("Run checkpoint nesting exceeds the supported depth")
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise HarnessError(f"Run checkpoint contains a non-finite number at {path}")
            return
        if isinstance(item, str):
            _validate_string(item, path)
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]", depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise HarnessError(f"Run checkpoint object keys must be strings at {path}")
                if _is_sensitive_key(key):
                    raise HarnessError(f"Run checkpoint contains a sensitive field at {path}.{key}")
                _validate_string(key, f"{path}.<key>")
                visit(child, f"{path}.{key}", depth + 1)
            return
        raise HarnessError(f"Run checkpoint contains an unsupported value type at {path}: {type(item).__name__}")

    visit(value, location, 0)


def checkpoint_safe_copy(value: Any) -> Any:
    """Copy workflow evidence while omitting values forbidden in retained checkpoints."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HarnessError("Run checkpoint contains a non-finite number")
        return value
    if isinstance(value, str):
        try:
            _validate_string(value, "workflow_state")
            return value
        except HarnessError:
            return "[omitted from retained checkpoint]"
    if isinstance(value, list):
        return [checkpoint_safe_copy(item) for item in value]
    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise HarnessError("Run checkpoint object keys must be strings")
            if _is_sensitive_key(key):
                continue
            try:
                _validate_string(key, "workflow_state.<key>")
            except HarnessError:
                continue
            copied[key] = checkpoint_safe_copy(item)
        return copied
    raise HarnessError(f"Run checkpoint contains an unsupported value type: {type(value).__name__}")


@dataclass(frozen=True)
class RunCheckpoint:
    run_id: str
    task: str
    frozen_graph: dict[str, Any]
    graph_sha256: str
    current_node: str
    state: dict[str, Any]
    transaction_ids: tuple[str, ...]
    transaction_manifests: tuple[dict[str, Any], ...]
    remaining_deadline_seconds: float
    pending_approval: dict[str, Any] | None
    sequence: int
    version: int = 0
    updated_at_ms: int = 0

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        task: str,
        frozen_graph: dict[str, Any],
        current_node: str,
        state: dict[str, Any],
        transaction_ids: list[str] | tuple[str, ...] = (),
        transaction_manifests: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        remaining_deadline_seconds: float,
        pending_approval: dict[str, Any] | None = None,
        sequence: int = 0,
    ) -> "RunCheckpoint":
        return cls(
            run_id=run_id,
            task=task,
            frozen_graph=frozen_graph,
            graph_sha256=graph_sha256(frozen_graph),
            current_node=current_node,
            state=state,
            transaction_ids=tuple(transaction_ids),
            transaction_manifests=tuple(transaction_manifests),
            remaining_deadline_seconds=remaining_deadline_seconds,
            pending_approval=pending_approval,
            sequence=sequence,
        )

    def validate(self) -> None:
        if not self.run_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.run_id):
            raise HarnessError("Run checkpoint run_id is invalid")
        if not self.current_node or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.current_node):
            raise HarnessError("Run checkpoint current_node is invalid")
        if not isinstance(self.task, str) or not self.task.strip():
            raise HarnessError("Run checkpoint task must not be empty")
        if not isinstance(self.state, dict):
            raise HarnessError("Run checkpoint state must be an object")
        if not isinstance(self.frozen_graph, dict):
            raise HarnessError("Run checkpoint frozen_graph must be an object")
        if self.graph_sha256 != graph_sha256(self.frozen_graph):
            raise HarnessError("Run checkpoint frozen graph hash does not match its canonical graph")
        if len(self.transaction_ids) != len(self.transaction_manifests):
            raise HarnessError("Run checkpoint transaction IDs and manifests must have equal lengths")
        if len(set(self.transaction_ids)) != len(self.transaction_ids):
            raise HarnessError("Run checkpoint contains duplicate transaction IDs")
        if not all(isinstance(item, dict) for item in self.transaction_manifests):
            raise HarnessError("Run checkpoint transaction manifests must be objects")
        for transaction_id in self.transaction_ids:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", transaction_id):
                raise HarnessError("Run checkpoint transaction ID is invalid")
        for transaction_id, manifest in zip(self.transaction_ids, self.transaction_manifests):
            if manifest.get("transaction_id") != transaction_id:
                raise HarnessError("Run checkpoint transaction manifest does not match its transaction ID")
        if not isinstance(self.remaining_deadline_seconds, (int, float)) or isinstance(
            self.remaining_deadline_seconds, bool
        ):
            raise HarnessError("Run checkpoint remaining deadline must be numeric")
        if not math.isfinite(float(self.remaining_deadline_seconds)) or self.remaining_deadline_seconds < 0:
            raise HarnessError("Run checkpoint remaining deadline must be finite and non-negative")
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or not 0 <= self.sequence <= 9_223_372_036_854_775_807
        ):
            raise HarnessError("Run checkpoint sequence must be a non-negative integer")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 0:
            raise HarnessError("Run checkpoint version must be a non-negative integer")
        if not isinstance(self.updated_at_ms, int) or isinstance(self.updated_at_ms, bool) or self.updated_at_ms < 0:
            raise HarnessError("Run checkpoint updated time must be a non-negative integer")
        if self.pending_approval is not None and not isinstance(self.pending_approval, dict):
            raise HarnessError("Run checkpoint pending approval must be an object or null")
        validate_checkpoint_value(self.task, "task")
        validate_checkpoint_value(self.frozen_graph, "frozen_graph")
        validate_checkpoint_value(self.state, "state")
        validate_checkpoint_value(list(self.transaction_ids), "transaction_ids")
        validate_checkpoint_value(list(self.transaction_manifests), "transaction_manifests")
        validate_checkpoint_value(self.pending_approval, "pending_approval")
        if len(canonical_json_bytes(self.payload())) > MAX_CHECKPOINT_JSON_BYTES:
            raise HarnessError("Run checkpoint exceeds the serialized size limit")

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": RUN_CHECKPOINT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task": self.task,
            "frozen_graph": self.frozen_graph,
            "graph_sha256": self.graph_sha256,
            "current_node": self.current_node,
            "state": self.state,
            "transaction_ids": list(self.transaction_ids),
            "transaction_manifests": list(self.transaction_manifests),
            "remaining_deadline_seconds": float(self.remaining_deadline_seconds),
            "pending_approval": self.pending_approval,
            "sequence": self.sequence,
        }

    def payload_sha256(self) -> str:
        return canonical_json_sha256(self.payload())

    def with_storage_metadata(self, *, version: int, updated_at_ms: int) -> "RunCheckpoint":
        return replace(self, version=version, updated_at_ms=updated_at_ms)

    def with_elapsed_deadline(self, now_ms: int | None = None) -> "RunCheckpoint":
        if not self.updated_at_ms:
            return self
        current = int(time.time() * 1000) if now_ms is None else now_ms
        elapsed = max(0.0, (current - self.updated_at_ms) / 1000.0)
        return replace(self, remaining_deadline_seconds=max(0.0, self.remaining_deadline_seconds - elapsed))
