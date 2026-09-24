"""Effectful Nexus tools, owned by an authenticated goal's private agent copy.

Limits here protect the machine only and are deliberately generous: long
builds and test suites run for up to an hour, large outputs come back with
their beginning and end, and write_file accepts multi-megabyte files.
"""
from __future__ import annotations

import copy
from .config import LoadedConfig
from .execution import CommandRunner
from .harness_tools import definition, PATH
from .models import HarnessError

# Every v1 request remains valid; the limits only widened.
CONTRACT = "nexus-goal-effect-tools/v1"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
MAX_COMMAND_TIMEOUT_SECONDS = 3600
# Returned, stored and shown to the agent: the beginning and end of up to this
# much output (100,000 characters each for stdout and stderr in its context).
MAX_COMMAND_OUTPUT_BYTES = 200_000
# Nexus captures more than it returns so the returned end is real output.
COMMAND_CAPTURE_BYTES = 16_000_000
MAX_WRITE_FILE_CHARS = 10_000_000
# UTF-8 needs at most four bytes per character.
MAX_WRITE_FILE_BYTES = MAX_WRITE_FILE_CHARS * 4
DEFINITIONS = [
    definition("run_command", "Run an argv command in this agent's private working copy under saved Full project access. Use PowerShell/bash/python/node explicitly for shell or code execution. "
               f"timeout_seconds defaults to {DEFAULT_COMMAND_TIMEOUT_SECONDS} and may be up to {MAX_COMMAND_TIMEOUT_SECONDS}. "
               f"Output beyond {MAX_COMMAND_OUTPUT_BYTES} bytes returns its beginning and end around a truncation marker; redirect to a file and read it for the full log. "
               "Nexus captures output, kills timed-out process trees, and collects file changes for review/publication. No background processes survive the call.",
               {"argv": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string", "maxLength": 32000}},
                "cwd": PATH, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_COMMAND_TIMEOUT_SECONDS}}, ["argv"]),
    definition("write_file", "Write a UTF-8 file in this agent's private copy under Full project access. Nexus later reviews and publishes collected changes. Parent directories are created; this does not publish to the user's project.",
               {"path": PATH, "content": {"type": "string", "maxLength": MAX_WRITE_FILE_CHARS}}, ["path", "content"]),
]
NAMES = frozenset(one["name"] for one in DEFINITIONS)
FACILITATOR_DEFINITIONS = copy.deepcopy(DEFINITIONS)
FACILITATOR_DEFINITIONS[0]["description"] = (
    "Run an argv command in the selected project under saved command permissions. "
    "Use PowerShell/bash/python/node explicitly for shell or code execution. "
    "Use cwd for a nested project directory. "
    f"timeout_seconds defaults to {DEFAULT_COMMAND_TIMEOUT_SECONDS} and may be up to {MAX_COMMAND_TIMEOUT_SECONDS}. "
    f"Output beyond {MAX_COMMAND_OUTPUT_BYTES} bytes returns its beginning and end around a truncation marker. "
    "Nexus captures output and cleans up "
    "timed-out process trees. File changes are immediately visible in the project."
)
FACILITATOR_DEFINITIONS[1]["description"] = (
    "Write a UTF-8 file directly in the selected project under saved write access. "
    "Parent directories are created and the file is immediately visible."
)


def command_timeout(arguments):
    """Validate the requested timeout; omitted means the generous default."""
    timeout = arguments.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS)
    if type(timeout) is not int or not 1 <= timeout <= MAX_COMMAND_TIMEOUT_SECONDS:
        raise HarnessError(
            f"run_command timeout_seconds must be a whole number from 1 through {MAX_COMMAND_TIMEOUT_SECONDS}")
    return timeout


def command_config(config, root, timeout):
    """Rebind config to ``root`` with runner ceilings that admit this request.

    The configured runner timeout and output ceiling are defaults for other
    callers; an agent's explicitly bounded tool request may use the tool maxima.
    """
    data = copy.deepcopy(config.data)
    execution = data.setdefault("execution", {})
    execution["timeout_seconds"] = max(int(execution.get("timeout_seconds") or 0), int(timeout))
    execution["max_output_bytes"] = max(int(execution.get("max_output_bytes") or 0), COMMAND_CAPTURE_BYTES)
    return LoadedConfig(data, root.resolve(), list(config.sources), dict(config.provenance),
                        copy.deepcopy(config.trusted_floor))


def _head_tail(text, budget, captured_all):
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= budget:
        return text, False
    head = max(1, budget // 4)
    tail = max(1, budget - head)
    omitted = len(encoded) - head - tail
    marker = (f"\n\n[... Nexus omitted {omitted} bytes of output here; showing the first {head} and last {tail} bytes"
              + ("" if captured_all else f" of the first {COMMAND_CAPTURE_BYTES} captured bytes")
              + ". Redirect output to a file and read it for the complete log. ...]\n\n")
    return (encoded[:head].decode("utf-8", errors="ignore") + marker
            + encoded[-tail:].decode("utf-8", errors="ignore")), True


def bounded_output(result, limit=MAX_COMMAND_OUTPUT_BYTES):
    """Return at most about ``limit`` bytes of stdout+stderr, keeping head and tail."""
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    captured_all = not result.get("output_truncated")
    sizes = [len(stdout.encode("utf-8", errors="replace")), len(stderr.encode("utf-8", errors="replace"))]
    if sum(sizes) <= limit:
        return result
    # Each stream gets what it needs; the smaller one never takes over half.
    small_index = 0 if sizes[0] <= sizes[1] else 1
    small_share = min(sizes[small_index], limit // 2)
    shares = [limit - small_share, limit - small_share]
    shares[small_index] = small_share
    trimmed = dict(result)
    trimmed["stdout"], cut_out = _head_tail(stdout, shares[0], captured_all)
    trimmed["stderr"], cut_err = _head_tail(stderr, shares[1], captured_all)
    trimmed["output_truncated"] = bool(result.get("output_truncated") or cut_out or cut_err)
    trimmed["output_bytes"] = {"stdout": sizes[0], "stderr": sizes[1], "returned_limit": limit}
    return trimmed


def execute(config, root, name, arguments, *, facilitator=False, expected_baselines=None, runtime_root=None):
    if not isinstance(arguments, dict):
        raise HarnessError("Nexus execution tool arguments must be an object")
    if not facilitator:
        return _execute(config, root, name, arguments)
    from . import project_operations as operations, long_horizon as lh
    try:
        # Queue behind a teammate's write for a while instead of refusing it.
        with operations.transaction(root, runtime_root, wait_seconds=operations.TOOL_LEASE_WAIT_SECONDS,
                                    max_files=1, max_bytes=MAX_WRITE_FILE_BYTES) as transaction:
            path = str(arguments.get("path") or "").replace("\\", "/").strip() if name == "write_file" else ""
            if name == "write_file" and expected_baselines is not None:
                if lh._observed_baseline(root, expected_baselines, path) != lh._path_baseline_marker(root, path):
                    return {"status": "conflict", "executed": False,
                            "reason": "File changed since this turn observed it: " + path + ". Return work to replan from the current files before writing."}
            # Reuse hashes only before the effect; the after-scan re-hashes all.
            before = lh._project_effect_manifest(root, reuse_hashes=True)
            target_before = lh._path_baseline_marker(root, path) if path else ""
            result = _execute(config, root, name, arguments, facilitator=True, transaction=transaction)
            observed = operations.observed_changes(before, lh._project_effect_manifest(root))
            if path and path not in {one["path"] for one in observed}:
                # The written file itself is always recorded, even where the
                # project scan does not look (for example under node_modules).
                target_after = lh._path_baseline_marker(root, path)
                observed.extend(operations.observed_changes(
                    {path: target_before} if target_before.startswith("file:") else {},
                    {path: target_after} if target_after.startswith("file:") else {}))
            result["observed_changes"] = observed
            return result
    except operations.OperationBusy as exc:
        return operations.unavailable(exc)


def _execute(config, root, name, arguments, *, facilitator=False, transaction=None):
    from .changes import FileTransaction
    from .swarm_work import _validated_changes
    if not isinstance(arguments, dict):
        raise HarnessError("Nexus execution tool arguments must be an object")
    if name == "write_file":
        if set(arguments) != {"path", "content"} or not isinstance(arguments["path"], str) \
                or not isinstance(arguments["content"], str):
            raise HarnessError("write_file needs a path and text content")
        if len(arguments["content"]) > MAX_WRITE_FILE_CHARS:
            raise HarnessError(
                f"write_file content has {len(arguments['content'])} characters; the machine limit is "
                f"{MAX_WRITE_FILE_CHARS}. Write the file in parts or generate it with run_command.")
        plans = _validated_changes(root, [{**arguments, "reason": "Nexus write_file tool in the agent copy"}])
        receipt = (transaction or FileTransaction(root, max_files=1, max_bytes=MAX_WRITE_FILE_BYTES)).apply(plans)
        return {"applied_to": "selected_project" if facilitator else "private_agent_copy", "published": facilitator, "path": arguments["path"],
                "transaction_id": receipt.get("transaction_id", "") if isinstance(receipt, dict) else ""}
    if name != "run_command" or set(arguments) - {"argv", "cwd", "timeout_seconds"}:
        raise HarnessError("Unknown execution tool or argument")
    argv = arguments.get("argv")
    cwd = arguments.get("cwd", ".")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100 \
            or any(not isinstance(one, str) or not one or len(one) > 32000 or "\x00" in one for one in argv) \
            or not isinstance(cwd, str) or len(cwd) > 240:
        raise HarnessError("run_command needs bounded argv and a relative cwd")
    timeout = command_timeout(arguments)
    if facilitator:
        from .facilitator_commands import run_command
        result = run_command(config, root, argv, cwd=cwd, timeout=timeout, max_output_bytes=COMMAND_CAPTURE_BYTES)
    else:
        result = CommandRunner(command_config(config, root, timeout)).run(
            argv, cwd=cwd, timeout=timeout, max_output_bytes=COMMAND_CAPTURE_BYTES)
    return {"execution_contract": CONTRACT, "working_copy": "selected_project" if facilitator else "private_agent_copy", "published": facilitator,
            "result": bounded_output(result.to_dict()), "verification_claim": "Command output is evidence, not a goal-completion verdict."}
