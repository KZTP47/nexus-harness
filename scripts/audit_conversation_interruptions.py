"""Produce a first-party source census for manual interruption-path review.

This is a coverage aid, not a claim that keyword absence proves correctness.
Python transition sites include enclosing symbols so reviewers can trace their
callers; JavaScript sites include line numbers. Generated/vendor files and tests
are excluded, but every checked-in source/configuration file is accounted for.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TOKENS = re.compile(r"paus|interrupt|block|stall|waiting|awaiting|outcome.?unknown|cancel|timeout|deadline|stop", re.I)
STOPS = {"paused", "blocked", "failed", "waiting_for_user", "cancelled", "cancelling", "stalled", "outcome_unknown"}


def census(root=ROOT):
    listed = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
    names = sorted(set(listed.decode().split("\0")) - {""})
    files = []
    for name in names:
        path = root / name
        if name.startswith(("tests/", "docs/")) or path.suffix not in {
            ".py", ".js", ".html", ".css", ".ps1", ".json", ".cmd", ".bat", ".sh", ".yml", ".yaml", ".toml",
        }:
            continue
        if name.endswith((".test.js", ".smoke.js", ".e2e.js", "package-lock.json")) or name == "desktop/smoke.js":
            continue
        raw = path.read_bytes()
        source = raw.decode("utf-8-sig").replace("\r\n", "\n")
        hits = [{"line": number, "text": line.strip()[:240]} for number, line in enumerate(source.splitlines(), 1) if TOKENS.search(line)]
        transitions = []
        if path.suffix == ".py":
            tree = ast.parse(source, name)
            def visit(node, owner="module"):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner += "." + node.name
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and any(
                    isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value in STOPS
                    for value in ast.walk(node.value)
                ):
                    transitions.append({"line": node.lineno, "symbol": owner,
                        "expression": ast.get_source_segment(source, node)[:700]})
                for child in ast.iter_child_nodes(node):
                    visit(child, owner)
            visit(tree)
        files.append({"path": name, "lines": len(source.splitlines()),
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "candidate_count": len(hits),
            "transition_sites": transitions,
            "candidate_lines": [hit["line"] for hit in hits]})
    return {"schema_version": 1, "scope": "First-party source/configuration and example tools across the repository; vendor, generated, docs and tests excluded",
        "method": "Full file census, lexical candidate discovery, Python AST transition discovery, followed by manual owner/caller review",
        "files": files}


if __name__ == "__main__":
    document = census()
    output = ROOT / "docs/audits/conversation-interruptions-inventory.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(document["files"]),
        "candidate_files": sum(bool(one["candidate_count"]) for one in document["files"]),
        "transition_sites": sum(len(one["transition_sites"]) for one in document["files"])}))
