"""Host execution after the goal service has obtained the user's command grant.

This is deliberately not an authorization boundary. Callers must use goal_access
before calling it. The process runner still owns output limits, cancellation and
process-tree cleanup; it does not second-guess an authorized shell program.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil

from .config import LoadedConfig
from .execution import CommandRunner
from .models import HarnessError
from .safety import confined_path


def tool_command_digest(root, arguments):
    """Identify the exact native request, including cwd and timeout authority."""
    if not isinstance(arguments, dict) or set(arguments) - {"argv", "cwd", "timeout_seconds"}:
        raise HarnessError("Unknown command tool argument")
    argv = arguments.get("argv")
    cwd = arguments.get("cwd", ".")
    timeout = arguments.get("timeout_seconds", 30)
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100 or any(
        not isinstance(one, str) or not one or len(one) > 32000 or "\0" in one for one in argv
    ) or not isinstance(cwd, str) or len(cwd) > 240 or type(timeout) is not int or not 1 <= timeout <= 60:
        raise HarnessError("run_command needs bounded argv, relative cwd and a 1–60 second timeout")
    working = confined_path(root, cwd, allow_missing=False)
    if not working.is_dir():
        raise HarnessError("Command cwd must be an existing directory")
    payload = {"contract": "facilitator-native-command/v1",
               "root": os.path.normcase(str(root.resolve())),
               "cwd": os.path.normcase(str(working.resolve())), "argv": argv, "timeout_seconds": timeout}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def authorize_tool(store, goal, root, arguments):
    """Ask the existing access store; provider text cannot grant execution."""
    digest = tool_command_digest(root, arguments)
    result, _revision = store.authorize_commands(goal["goal_id"], [arguments["argv"]], digest, "discovered")
    if isinstance(result, dict):
        return {**result, "tool_arguments": copy.deepcopy(arguments), "command_kind": "run_command"}
    return result


class _GrantedCommandRunner(CommandRunner):
    def _check(self, argv):
        if not isinstance(argv, list) or not argv or any(
            not isinstance(part, str) or not part or "\0" in part for part in argv
        ):
            raise HarnessError("Command must be a non-empty argv list")


def run_command(config, root: Path, argv, *, cwd=".", timeout=None, max_output_bytes=10000):
    """Run granted argv at the selected root, including explicit shell argv."""
    data = copy.deepcopy(config.data)
    data["execution"]["mode"] = "process"
    rebound = LoadedConfig(data, root.resolve(), list(config.sources),
                           dict(config.provenance), copy.deepcopy(config.trusted_floor))
    actual = list(argv)
    # CreateProcess does not search PATHEXT for bare npm/pnpm/yarn names.
    # Supply the installed executable spelling without expanding npm scripts.
    if os.name == "nt" and actual:
        resolved = shutil.which(actual[0])
        if resolved:
            actual[0] = resolved
    result = _GrantedCommandRunner(rebound).run(
        actual, cwd=cwd, timeout=timeout, max_output_bytes=max_output_bytes,
    )
    return replace(result, argv=list(argv))
