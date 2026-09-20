"""Mandatory, local LangGraph portability gate for distributable engine builds.

The source checkout is supplied by the caller. No developer path or local
configuration can waive this gate. Private project memory is never read.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .audit import audit_distribution
from .models import HarnessError
from .persistent_memory_index import _is_link_or_junction


PRIVATE_NAMES = frozenset({
    ".harness", ".obsidian", ".nexus-memory", ".nexus-vault",
    "private-project-memory", ".nexus-project-memory.json", ".ai-project.json",
    "config.local.json", "memory-index.sqlite3", "AGENTS.md", "CLAUDE.md",
})


class DistributionState(TypedDict, total=False):
    root: str
    privacy_findings: list[str]
    audit: dict[str, Any]
    passed: bool


def _engine_boundary(state: DistributionState) -> dict[str, Any]:
    root = Path(state["root"])
    findings = []
    # These Python files ship verbatim, independent of Git tracking. Refuse
    # private files and linked source before opening any candidate content.
    source = root / "src"
    if _is_link_or_junction(source):
        return {"privacy_findings": ["src"]}
    private_names = {name.casefold() for name in PRIVATE_NAMES}
    for directory, folders, files in os.walk(source, followlinks=False):
        for name in [*folders, *files]:
            candidate = Path(directory) / name
            linked = _is_link_or_junction(candidate)
            if name.casefold() in private_names or linked:
                findings.append(candidate.relative_to(root).as_posix())
                if name in folders:
                    folders.remove(name)
    return {"privacy_findings": findings}


def _audit_engine(state: DistributionState) -> dict[str, Any]:
    return {"audit": audit_distribution(Path(state["root"]))}


def _require_pass(state: DistributionState) -> dict[str, bool]:
    if state.get("privacy_findings"):
        raise HarnessError("Distribution gate rejected private or linked engine source: " + ", ".join(state["privacy_findings"]))
    report = state.get("audit", {})
    if not report.get("passed"):
        findings = report.get("findings", [])
        locations = "; ".join(f"{item['path']}:{item['line']}: {item['message']}" for item in findings[:20])
        raise HarnessError("Distribution portability gate failed: " + (locations or "no valid engine audit"))
    return {"passed": True}


def enforce_distribution_gate(root: Path) -> dict[str, Any]:
    graph = StateGraph(DistributionState)
    graph.add_node("verify_engine_boundary", _engine_boundary)
    graph.add_node("audit_portable_engine", _audit_engine)
    graph.add_node("require_distribution_pass", _require_pass)
    graph.add_edge(START, "verify_engine_boundary")
    graph.add_conditional_edges("verify_engine_boundary", lambda state: "reject" if state["privacy_findings"] else "audit", {
        "reject": "require_distribution_pass", "audit": "audit_portable_engine",
    })
    graph.add_edge("audit_portable_engine", "require_distribution_pass")
    graph.add_edge("require_distribution_pass", END)
    return graph.compile().invoke({"root": str(root.resolve())})
