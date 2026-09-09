"""Native tool permission profiles for engine-owned project copies."""
from pathlib import Path

from ..models import HarnessError
from ..goal_workspaces import _direct

CONTRACT = "nexus-native-agent/v1"


def workspace(request):
    if not request.native_execution:
        return None
    if request.native_execution not in {"inspect", "work"} or request.workspace_context is None:
        raise HarnessError("Native execution requires an engine-owned workspace and permission profile")
    context = request.workspace_context
    root = _direct(Path(request.working_directory))
    source = _direct(Path(context.project_path))
    if not request.working_directory or not Path(request.working_directory).is_absolute() \
            or root != _direct(Path(context.execution_path)) or not root.is_dir() \
            or root == source or source in root.parents or root in source.parents:
        raise HarnessError("Native execution must use an independent agent copy outside the real project")
    return root


def instructions(request):
    root = workspace(request)
    if root is None:
        return ""
    writable = request.native_execution == "work"
    return (
        "NATIVE AGENT EXECUTION\nYour working directory is your own project copy: " + str(root) + ". "
        "Use your native file, search, web and skill tools to investigate the task. "
        + ("Use native commands, scripts and edits in this copy. Nexus collects the actual changed files; "
           "return changes=[] for files already edited with native tools. " if writable else
           "This turn permits native inspection. Propose edits in the changes field and request commands "
           "through Nexus tools under the current user access setting. ")
        + "Nexus context tools also refer to this copy. Tool permission denials are real constraints: report them "
        "or ask a concrete question; do not bypass them. Follow the USER-SELECTED COLLABORATION workspace permissions when present; otherwise edit only your own copy. "
        "Nexus reviews and verifies candidates before publishing to the original project. "
        "Keep temporary downloads and dependencies separate from deliverables. "
        "A native test result is useful evidence, but Nexus performs its own final verification. "
        "Always return the requested structured final action. Use Nexus tool_calls for team communication."
    )
