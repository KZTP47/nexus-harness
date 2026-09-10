"""Effectful Nexus tools, owned by an authenticated goal's private agent copy."""
from __future__ import annotations

import copy
from .config import LoadedConfig
from .execution import CommandRunner
from .harness_tools import definition, PATH
from .models import HarnessError

CONTRACT = "nexus-goal-effect-tools/v1"
DEFINITIONS = [
    definition("run_command", "Run an argv command in this agent's private working copy under saved Full project access. Use PowerShell/bash/python/node explicitly for shell or code execution. Nexus captures output, kills timed-out process trees, and collects file changes for review/publication. No background processes survive the call.",
               {"argv": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string", "maxLength": 32000}},
                "cwd": PATH, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}}, ["argv"]),
    definition("write_file", "Write a UTF-8 file in this agent's private copy under Full project access. Nexus later reviews and publishes collected changes. Parent directories are created; this does not publish to the user's project.",
               {"path": PATH, "content": {"type": "string", "maxLength": 100000}}, ["path", "content"]),
]
NAMES = frozenset(one["name"] for one in DEFINITIONS)


def execute(config, root, name, arguments):
    from .changes import FileTransaction
    from .swarm_work import _validated_changes
    if not isinstance(arguments, dict):
        raise HarnessError("Nexus execution tool arguments must be an object")
    if name == "write_file":
        if set(arguments) != {"path", "content"} or not isinstance(arguments["path"], str) \
                or not isinstance(arguments["content"], str) or len(arguments["content"]) > 100000:
            raise HarnessError("write_file needs a bounded path and content")
        plans = _validated_changes(root, [{**arguments, "reason": "Nexus write_file tool in the agent copy"}])
        receipt = FileTransaction(root, max_files=1, max_bytes=400000).apply(plans)
        return {"applied_to": "private_agent_copy", "published": False, "path": arguments["path"],
                "transaction_id": receipt.get("transaction_id", "") if isinstance(receipt, dict) else ""}
    if name != "run_command" or set(arguments) - {"argv", "cwd", "timeout_seconds"}:
        raise HarnessError("Unknown execution tool or argument")
    argv = arguments.get("argv")
    timeout = arguments.get("timeout_seconds", 30)
    cwd = arguments.get("cwd", ".")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100 \
            or any(not isinstance(one, str) or not one or len(one) > 32000 or "\x00" in one for one in argv) \
            or type(timeout) is not int or not 1 <= timeout <= 60 or not isinstance(cwd, str) or len(cwd) > 240:
        raise HarnessError("run_command needs bounded argv, relative cwd and a 1–60 second timeout")
    rebound = LoadedConfig(copy.deepcopy(config.data), root.resolve(), list(config.sources), dict(config.provenance), copy.deepcopy(config.trusted_floor))
    result = CommandRunner(rebound).run(argv, cwd=cwd, timeout=timeout, max_output_bytes=10000)
    return {"execution_contract": CONTRACT, "working_copy": "private_agent_copy", "published": False,
            "result": result.to_dict(), "verification_claim": "Command output is evidence, not a goal-completion verdict."}
