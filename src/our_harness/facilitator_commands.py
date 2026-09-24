"""Project command execution after a goal grant, retaining runner policy."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil

from .execution import CommandRunner
from .models import HarnessError
from .safety import confined_path


def tool_command_digest(root, arguments, config):
    """Identify the exact native request, including cwd and timeout authority."""
    if not isinstance(arguments, dict) or set(arguments) - {"argv", "cwd", "timeout_seconds"}:
        raise HarnessError("Unknown command tool argument")
    from .goal_tools import command_timeout
    argv = arguments.get("argv")
    cwd = arguments.get("cwd", ".")
    command_timeout(arguments)
    # Identity marker only: an omitted timeout keeps the digest it always had,
    # so existing approvals stay valid. The runtime default is generous.
    timeout = arguments.get("timeout_seconds", 30)
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100 or any(
        not isinstance(one, str) or not one or len(one) > 32000 or "\0" in one for one in argv
    ) or not isinstance(cwd, str) or len(cwd) > 240:
        raise HarnessError("run_command needs bounded argv and a relative cwd")
    working = confined_path(root, cwd, allow_missing=False)
    if not working.is_dir():
        raise HarnessError("Command cwd must be an existing directory")
    payload = {"contract": "facilitator-project-command/v2",
               "root": os.path.normcase(str(root.resolve())),
               "cwd": os.path.normcase(str(working.resolve())), "argv": argv, "timeout_seconds": timeout,
               "execution_policy": {key: config.get("execution." + key) for key in (
                   "mode", "deny_executables", "deny_argument_sequences", "docker_image", "docker_network")}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def authorize_tool(store, goal, root, arguments):
    """Ask the existing access store; provider text cannot grant execution."""
    digest = tool_command_digest(root, arguments, store.config)
    result, _revision = store.authorize_commands(goal["goal_id"], [arguments["argv"]], digest, "discovered")
    if isinstance(result, dict):
        return {**result, "tool_arguments": copy.deepcopy(arguments), "command_kind": "run_command"}
    return result


def run_command(config, root: Path, argv, *, cwd=".", timeout=None, max_output_bytes=None):
    """Run granted argv with the configured backend, denials and cleanup."""
    from .goal_tools import COMMAND_CAPTURE_BYTES, command_config
    # Callers without an agent's explicit request keep the configured timeout.
    timeout = float(config.get("execution.timeout_seconds")) if timeout is None else timeout
    max_output_bytes = COMMAND_CAPTURE_BYTES if max_output_bytes is None else max_output_bytes
    rebound = command_config(config, root, timeout)
    actual = list(argv)
    # CreateProcess does not search PATHEXT for bare npm/pnpm/yarn names.
    # Supply the installed executable spelling without expanding npm scripts.
    if os.name == "nt" and actual and config.get("execution.mode") == "process":
        resolved = shutil.which(actual[0])
        if resolved:
            actual[0] = resolved
    result = CommandRunner(rebound).run(
        actual, cwd=cwd, timeout=timeout, max_output_bytes=max_output_bytes,
    )
    return replace(result, argv=list(argv))
