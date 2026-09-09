"""Engine-owned delivery evidence, separate from a provider's public claims."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import HarnessError
from .safety import confined_path


CONTRACT = "selected-project-delivery/v1"


def available_files(manifest: dict[str, str]) -> set[str]:
    return {path for path, identity in manifest.items()
            if re.fullmatch(r"file:[0-9a-f]{64}", identity)}


def file_references(goal: dict[str, Any]) -> set[str]:
    return {
        ref[5:] for task in goal.get("tasks", []) if task.get("state") != "cancelled"
        for evidence in task.get("criteria_evidence", [])
        for ref in evidence.get("evidence_refs", [])
        if isinstance(ref, str) and ref.startswith("file:")
    }


def report_metadata(goal: dict[str, Any]) -> dict[str, str]:
    # Provider turns precede the final verification/publication transaction.
    # This remains a historical fact when reopening an already completed goal.
    if not goal.get("execution_workspace"):
        return {}
    return {"delivery_contract": CONTRACT, "delivery_state": "working_copy_report",
            "delivery_project": str(goal.get("project", {}).get("path") or "")}


def status_metadata(goal: dict[str, Any]) -> dict[str, str]:
    held = goal.get("delivery_receipt") or {}
    if held.get("contract") != CONTRACT or held.get("state") != "delivered" or goal.get("delivery_problem"):
        return {}
    root = str(held["project_path"])
    locations = [root, *[str(Path(root) / item["path"]) for item in held.get("files", [])[:40]]]
    return {"delivery_contract": CONTRACT, "delivery_locations": json.dumps(locations, ensure_ascii=False)}


def receipt(goal: dict[str, Any], manifest: dict[str, str]) -> dict[str, Any]:
    """Read back named deliverables from the destination before reporting done.

    The caller holds the project's publication lock. Hashes must match the
    verified execution tree; a working-copy transaction is never a receipt.
    """
    from .goal_workspaces import _direct

    source = _direct(Path(goal["project"]["path"]))
    paths = file_references(goal)
    paths.update(str(change["path"]) for artifact in goal.get("artifacts", [])
                 for change in artifact.get("changes", [])
                 if change.get("path") and change.get("after_sha256"))
    paths.update(str(change["path"]) for change in (goal.get("workspace_publication") or {}).get("changes", [])
                 if change.get("path") and change.get("after_sha256"))
    # A later intentional deletion supersedes an earlier write. Explicit file
    # evidence, however, must always identify a file that still exists.
    missing = sorted(file_references(goal) - available_files(manifest))
    if missing:
        raise HarnessError("Delivery evidence names missing files: " + ", ".join(missing))
    files = []
    for relative in sorted(paths & available_files(manifest)):
        target = confined_path(source, relative)
        try:
            with target.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError as exc:
            raise HarnessError("Delivered file cannot be read from the selected project: " + relative) from exc
        if "file:" + digest != manifest[relative]:
            raise HarnessError("Delivered file differs from the verified working copy: " + relative)
        files.append({"path": relative, "sha256": digest})
    binding = {"contract": CONTRACT, "goal_id": goal["goal_id"],
               "objective_epoch": goal.get("objective_epoch", 1), "project": str(source)}
    return {"schema_version": 1, "contract": CONTRACT, "state": "delivered",
            "binding_sha256": hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest(),
            "project_path": str(source), "files": files}
