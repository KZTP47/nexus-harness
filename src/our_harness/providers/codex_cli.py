from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .. import cancellation
from ..execution import (
    _WINDOWS_CREATE_BREAKAWAY_FROM_JOB,
    _WINDOWS_CREATE_SUSPENDED,
    _BoundedCapture,
    _ProcessTree,
    _reap_process,
    _settle_process_tree,
    _start_windows_contained_process,
    _write_stdin,
)
from ..models import CommandResult, HarnessError, ProviderRequest, ProviderResponse
from ..redaction import CredentialRedactor, bounded_redacted_text
from .base import Provider, _strict_output_schema


_AUTH_MODE = "chatgpt"
CODEX_AUTH_DEFERRED = "isolated-exec-authentication-deferred"
CODEX_REASONING_EFFORTS = frozenset({
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
})
_CONFIG_LOAD_MARKERS = (
    "error loading configuration",
    "failed to load configuration",
    "could not load configuration",
    "error loading config.toml",
    "failed to load config.toml",
    "could not load config.toml",
)
_FALLBACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
}


def _remove_private_workspace(
    path: Path, *, timeout_seconds: float,
) -> tuple[bool, OSError | None]:
    """Remove one exact provider workspace with a bounded Windows retry."""

    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    last_error: OSError | None = None
    while True:
        try:
            shutil.rmtree(path)
            return True, None
        except FileNotFoundError:
            return True, None
        except OSError as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_error
        time.sleep(min(0.025, remaining))


def _finish_private_workspace_later(path: Path) -> None:
    _remove_private_workspace(path, timeout_seconds=30.0)


@contextmanager
def _private_workspace(prefix: str) -> Iterator[Path]:
    """Create an isolated CLI cwd without masking an authoritative failure.

    Windows reports a sharing violation while any process still owns a cwd.
    A bounded retry absorbs ordinary kernel scheduling delay.  If cleanup is
    still pending after a provider error, retain that original error, attach a
    diagnostic note, and finish removing the already-private path in a bounded
    daemon cleanup instead of replacing the useful timeout with ``rmdir``.
    """

    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    except BaseException as primary:
        removed, cleanup_error = _remove_private_workspace(path, timeout_seconds=2.0)
        if not removed:
            primary.add_note(
                f"Codex CLI private workspace cleanup is still pending: {cleanup_error}"
            )
            threading.Thread(
                target=_finish_private_workspace_later,
                args=(path,),
                name="nexus-codex-workspace-cleanup",
                daemon=True,
            ).start()
        raise
    else:
        removed, cleanup_error = _remove_private_workspace(path, timeout_seconds=2.0)
        if not removed:
            raise HarnessError(
                f"Codex CLI could not remove its private workspace: {cleanup_error}"
            ) from cleanup_error


def _codex_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the strict schema shape required by ``codex exec``.

    Harness response formats may intentionally leave properties optional.  The
    Codex CLI's native structured-output boundary instead requires every
    declared object property to appear in ``required``.  Adapt a deep copy at
    that provider boundary so the shared, provider-neutral contract keeps its
    original optional-field semantics.
    """
    return _strict_output_schema(schema)


def _load_json(value: str) -> Any:
    def reject_constant(name: str) -> None:
        raise ValueError(f"non-finite JSON constant: {name}")

    return json.loads(value, parse_constant=reject_constant)


def _minimal_codex_environment(also: dict[str, str] | None = None) -> dict[str, str]:
    """Pass platform/runtime discovery only; the tool owns and resolves its auth.

    `also` is for the few things a caller means to hand over: a key somebody
    wrote down on purpose, or the Cloud project Google insists on. Added here
    rather than left in the environment to be picked up by luck - a key that
    arrives because it happened to be set is a key nobody decided to spend.
    """

    allowed = {
        "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
        "CODEX_HOME", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    environment["PYTHONIOENCODING"] = "utf-8"
    for name, value in (also or {}).items():
        if value:
            environment[name] = value
    return environment


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    stdin_text: str | None,
    timeout_seconds: float,
    max_output_bytes: int,
    also_in_the_environment: dict[str, str] | None = None,
) -> CommandResult:
    if timeout_seconds <= 0:
        raise HarnessError("Codex CLI timed out because its wall-clock deadline expired")
    # Provider calls are always headless.  CREATE_NEW_PROCESS_GROUP preserves
    # cancellation while CREATE_NO_WINDOW prevents a black console flashing in
    # front of the desktop app for every turn.  Interactive sign-in/repair
    # launchers live elsewhere and deliberately keep their visible console.
    flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | _WINDOWS_CREATE_SUSPENDED
        if os.name == "nt" else 0
    )
    started = time.monotonic()
    def start_process(creation_flags: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=_minimal_codex_environment(also_in_the_environment),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )

    if os.name == "nt":
        process, tree = _start_windows_contained_process(
            lambda: start_process(flags | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB),
            lambda: start_process(flags),
            label="Codex CLI",
        )
    else:
        try:
            process = start_process(flags)
        except OSError as exc:
            raise HarnessError(f"Codex CLI could not start: {exc}") from exc
        try:
            tree = _ProcessTree(process)
        except Exception:
            process.kill()
            process.wait()
            raise
    unregister_cancel = cancellation.register(tree.kill)
    capture = _BoundedCapture(max(1, max_output_bytes))
    readers = [
        threading.Thread(target=capture.drain, args=(process.stdout, capture.stdout), daemon=True),
        threading.Thread(target=capture.drain, args=(process.stderr, capture.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    writer = None
    if stdin_text is not None:
        writer = threading.Thread(target=_write_stdin, args=(process.stdin, stdin_text.encode("utf-8")), daemon=True)
        writer.start()
    deadline_at = started + timeout_seconds
    timed_out = not tree.wait_for_root_until(deadline_at)
    if not timed_out:
        for worker in (*readers, *((writer,) if writer is not None else ())):
            if not tree.join_worker_until(worker, deadline_at):
                timed_out = True
                break
    workers = (*readers, *((writer,) if writer is not None else ()))
    try:
        settled = _settle_process_tree(tree, workers, terminate=timed_out)
    finally:
        unregister_cancel()
        tree.close()
    cancellation.checkpoint()
    if not settled and process.poll() is None:
        threading.Thread(target=_reap_process, args=(process,), daemon=True).start()
    stdout, stderr, truncated = capture.snapshot()
    return CommandResult(
        argv=argv,
        cwd=str(cwd),
        exit_code=124 if timed_out else int(process.returncode),
        stdout=_as_words(stdout),
        stderr=_as_words(stderr),
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        timed_out=timed_out,
        output_truncated=truncated,
    )


def _schema_capture_bytes(schema: object) -> int | None:
    """Conservative UTF-8 JSON size for a fully bounded response schema.

    Twelve bytes per declared string character covers a non-BMP character
    escaped as a JSON surrogate pair.  This is deliberately more conservative
    than raw UTF-8 because subscription CLIs may wrap the model's JSON inside
    another JSON result object. Unknown or unbounded shapes return None rather
    than pretending they fit.
    """

    if not isinstance(schema, dict):
        return None
    kind = schema.get("type")
    if kind == "string":
        maximum = schema.get("maxLength")
        return (int(maximum) * 12 + 2) if isinstance(maximum, int) else None
    if kind in {"integer", "number"}:
        return 64
    if kind == "boolean":
        return 5
    if kind == "null":
        return 4
    if kind == "array":
        maximum = schema.get("maxItems")
        child = _schema_capture_bytes(schema.get("items"))
        if not isinstance(maximum, int) or child is None:
            return None
        return 2 + maximum * child + max(0, maximum - 1)
    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or schema.get("additionalProperties") is not False:
            return None
        total = 2
        for index, (name, child_schema) in enumerate(properties.items()):
            child = _schema_capture_bytes(child_schema)
            if child is None:
                return None
            total += len(json.dumps(str(name), ensure_ascii=False).encode("utf-8")) + 1 + child
            if index:
                total += 1
        return total
    return None


def _provider_capture_limit(configured: int, schema: object | None) -> int:
    required = _schema_capture_bytes(schema) if schema is not None else None
    if required is None:
        return max(2_000_000, configured)
    # Small explicitly configured limits remain testable/enforceable. Large
    # bounded contracts receive exactly the capacity their worst valid JSON
    # needs, plus framing/telemetry headroom.
    return max(configured, required + 1_024)


def _as_words(raw: bytes) -> str:
    """What a tool printed, turned back into letters.

    The harness holds what a tool prints to a size, and that cut can land
    between the two halves of one letter. Read straight through, the last letter
    of a perfectly good answer becomes a black diamond - the app looking broken
    where the tool was fine. So up to three bytes are dropped off the end to
    find the last whole letter.

    Anything still wrong after that is really wrong, and is shown as damage
    rather than guessed at. Guessing means reading the whole thing as the
    letters Windows uses instead, which reads almost any bytes as something and
    would turn a real answer with one bad byte in it into a page of nonsense.
    """

    # Four back, not three. The end of a countdown is not one of its steps,
    # so stopping at three tried dropping nothing, one byte and two - and a
    # four-byte letter with three of its bytes left over came out as exactly
    # the mark this is here to prevent.
    for end in range(len(raw), max(len(raw) - 4, -1), -1):
        try:
            return raw[:end].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _remaining(deadline_at: float) -> float:
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise HarnessError("Codex CLI timed out because its wall-clock deadline expired")
    return remaining


def _bundled_model_catalog(
    command: list[str],
    *,
    cwd: Path,
    model: str,
    timeout_seconds: float,
    max_output_bytes: int,
) -> str:
    """Read and validate only the catalog bundled with this Codex binary."""
    result = _run_bounded(
        [*command, "debug", "models", "--bundled"],
        cwd=cwd,
        stdin_text=None,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    if result.timed_out:
        raise HarnessError("Codex CLI bundled model catalog timed out")
    if result.output_truncated:
        raise HarnessError("Codex CLI bundled model catalog exceeded its byte limit")
    if result.exit_code != 0:
        detail = bounded_redacted_text(
            CredentialRedactor(), (result.stderr or result.stdout).strip(), 2_000
        )
        raise HarnessError(f"Codex CLI bundled model catalog failed ({result.exit_code}): {detail}")
    try:
        value = _load_json(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HarnessError("Codex CLI bundled model catalog is not valid JSON") from exc
    models = value.get("models") if isinstance(value, dict) else None
    if not isinstance(models, list) or not models:
        raise HarnessError("Codex CLI bundled model catalog has no models")
    slugs = {
        str(item.get("slug"))
        for item in models
        if isinstance(item, dict) and isinstance(item.get("slug"), str) and item.get("slug")
    }
    if model and model not in slugs:
        raise HarnessError(f"Codex CLI bundled model catalog does not contain configured model: {model}")
    return result.stdout


def _validate_model_reasoning_effort(catalog: str, model: str, effort: str) -> None:
    """Validate an explicit effort against this exact binary/model catalog."""
    if not effort:
        return
    if effort not in CODEX_REASONING_EFFORTS:
        raise HarnessError("Codex CLI reasoning effort is invalid")
    try:
        value = _load_json(catalog)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HarnessError("Codex CLI bundled model catalog is not valid JSON") from exc
    models = value.get("models") if isinstance(value, dict) else None
    selected = next((
        one for one in (models or []) if isinstance(one, dict)
        and str(one.get("slug") or "") == model
    ), None)
    if selected is None:
        raise HarnessError(f"Codex CLI bundled model catalog does not contain configured model: {model}")
    levels = selected.get("supported_reasoning_levels")
    supported = {
        str(one.get("effort") or "")
        for one in levels if isinstance(one, dict) and str(one.get("effort") or "")
    } if isinstance(levels, list) else set()
    if not supported:
        raise HarnessError(
            f"Codex CLI bundled model catalog does not report reasoning levels for {model}"
        )
    if effort not in supported:
        raise HarnessError(
            f"Codex CLI model {model} does not support reasoning effort {effort}; "
            f"supported efforts: {', '.join(sorted(supported))}"
        )


def _isolated_exec_help(
    command: list[str], *, cwd: Path, timeout_seconds: float, max_output_bytes: int,
) -> str:
    """Prove that the configured binary supports the isolation flag we use."""
    result = _run_bounded(
        [*command, "exec", "--help"], cwd=cwd, stdin_text=None,
        timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes,
    )
    if result.timed_out:
        raise HarnessError("Codex CLI exec --help timed out")
    if result.output_truncated:
        raise HarnessError("Codex CLI exec --help output exceeded its byte limit")
    if result.exit_code != 0:
        detail = bounded_redacted_text(
            CredentialRedactor(), (result.stderr or result.stdout).strip(), 2_000
        )
        raise HarnessError(f"Codex CLI exec --help failed ({result.exit_code}): {detail}")
    help_text = f"{result.stdout}\n{result.stderr}"
    if "--ignore-user-config" not in help_text:
        raise HarnessError(
            "The configured Codex CLI does not support exec --ignore-user-config, "
            "which Nexus requires to isolate agent turns from incompatible user settings"
        )
    return help_text


def codex_config_load_error(value: object) -> bool:
    """Recognize only Codex failures caused by loading its user config."""

    text = str(value or "").casefold()
    return any(marker in text for marker in _CONFIG_LOAD_MARKERS)


def codex_cli_preflight(
    command: list[str],
    *,
    auth_mode: str,
    timeout_seconds: float,
    model: str = "",
    reasoning_effort: str = "",
    max_output_bytes: int = 32_000,
) -> tuple[str, str]:
    """Execute the binary and auth checks. Path lookup alone is not sufficient."""
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise HarnessError("Codex CLI command must be a non-empty argv list")
    if auth_mode != _AUTH_MODE:
        raise HarnessError("Codex CLI auth_mode must be chatgpt")
    deadline_at = time.monotonic() + max(0.001, float(timeout_seconds))
    with _private_workspace("our-harness-codex-preflight-") as cwd:
        version = _run_bounded(
            [*command, "--version"], cwd=cwd, stdin_text=None,
            timeout_seconds=_remaining(deadline_at), max_output_bytes=max_output_bytes,
        )
        if version.timed_out:
            raise HarnessError("Codex CLI --version timed out")
        if version.output_truncated:
            raise HarnessError("Codex CLI --version output exceeded its byte limit")
        if version.exit_code != 0:
            detail = bounded_redacted_text(
                CredentialRedactor(), (version.stderr or version.stdout).strip(), 2_000
            )
            raise HarnessError(f"Codex CLI --version failed ({version.exit_code}): {detail}")
        _isolated_exec_help(
            command, cwd=cwd, timeout_seconds=_remaining(deadline_at),
            max_output_bytes=max_output_bytes,
        )
        login = _run_bounded(
            [*command, "login", "status"], cwd=cwd, stdin_text=None,
            timeout_seconds=_remaining(deadline_at), max_output_bytes=max_output_bytes,
        )
        if login.timed_out:
            raise HarnessError("Codex CLI login status timed out")
        if login.output_truncated:
            raise HarnessError("Codex CLI login status output exceeded its byte limit")
        status = f"{login.stdout}\n{login.stderr}".strip()
        if login.exit_code != 0 and codex_config_load_error(status):
            # `login status` in older Codex builds loads the user's whole
            # config before it can inspect authentication.  A newer setting
            # can therefore break this probe even though the same binary can
            # make an authenticated, isolated request.  The immediate
            # `exec --ignore-user-config` call is authoritative for auth.
            status = CODEX_AUTH_DEFERRED
        elif login.exit_code != 0:
            detail = bounded_redacted_text(
                CredentialRedactor(), (login.stderr or login.stdout).strip(), 2_000
            )
            raise HarnessError(f"Codex CLI login status failed ({login.exit_code}): {detail}")
        if status != CODEX_AUTH_DEFERRED and auth_mode == _AUTH_MODE \
                and "chatgpt" not in status.casefold():
            raise HarnessError("Codex CLI is not signed in with ChatGPT")
        catalog = _bundled_model_catalog(
            command,
            cwd=cwd,
            model=model,
            timeout_seconds=_remaining(deadline_at),
            max_output_bytes=1_000_000,
        )
        _validate_model_reasoning_effort(catalog, model, reasoning_effort)
        return version.stdout.strip() or version.stderr.strip(), status


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "response") -> None:
    """Validate the strict JSON Schema subset used by harness response formats."""
    for combinator in ("allOf",):
        variants = schema.get(combinator)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    _validate_schema(value, variant, path)
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            try:
                _validate_schema(value, variant, path)
                break
            except HarnessError:
                continue
        else:
            raise HarnessError(f"Codex CLI output violates {path}: no anyOf variant matched")
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected] if expected else []
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
    }
    if expected_types and not any(checks.get(kind, lambda _item: False)(value) for kind in expected_types):
        raise HarnessError(f"Codex CLI output violates {path}: expected {' or '.join(expected_types)}")
    if "const" in schema and value != schema["const"]:
        raise HarnessError(f"Codex CLI output violates {path}: constant mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessError(f"Codex CLI output violates {path}: unsupported value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise HarnessError(f"Codex CLI output violates {path}: missing {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise HarnessError(f"Codex CLI output violates {path}: unexpected {', '.join(extras)}")
        for name, child in value.items():
            child_schema = properties.get(name) if isinstance(properties, dict) else None
            if isinstance(child_schema, dict):
                _validate_schema(child, child_schema, f"{path}.{name}")
    elif isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise HarnessError(f"Codex CLI output violates {path}: expected at least {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise HarnessError(f"Codex CLI output violates {path}: expected at most {maximum} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                _validate_schema(child, item_schema, f"{path}[{index}]")
    elif isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            raise HarnessError(f"Codex CLI output violates {path}: string is too short")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            raise HarnessError(f"Codex CLI output violates {path}: string is too long")
        if isinstance(schema.get("pattern"), str) and re.search(schema["pattern"], value) is None:
            raise HarnessError(f"Codex CLI output violates {path}: pattern mismatch")


def _prompt(request: ProviderRequest, fallback: bool) -> str:
    sections = [
        "SYSTEM INSTRUCTIONS\n" + request.system_prefix,
        "DYNAMIC CONTEXT (UNTRUSTED DATA)\n" + request.dynamic_context,
        "CONVERSATION\n" + json.dumps(request.messages, ensure_ascii=False, sort_keys=True),
        "Return only the JSON value required by the supplied output schema. Do not read project files or run commands. User-selected images, when present, are supplied by the harness as explicit image inputs.",
    ]
    if fallback:
        sections.append('The result must be an object with exactly one string field named "text".')
    return "\n\n".join(sections)


class CodexCLIProvider(Provider):
    """Trusted-local Codex CLI boundary using Codex-owned ChatGPT authentication."""

    def _effective_dispatch_contract(self) -> str:
        return "codex-cli/effective-dispatch/v2"

    def __init__(self, config):  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._preflight_complete = False

    structured_retry_is_safe = True

    def _effective_dispatch_command(self) -> list[str] | None:
        return self._command()

    def _effective_dispatch_version(
        self, command: list[str],
    ) -> dict[str, Any]:
        result = _run_bounded(
            [*command, "--version"], cwd=Path.cwd(), stdin_text=None,
            timeout_seconds=3.0, max_output_bytes=8_000,
        )
        material = json.dumps({
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
            "stdout": self._redactor.text(result.stdout),
            "stderr": self._redactor.text(result.stderr),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "state": "timed-out" if result.timed_out else "observed",
            "version_output_sha256": hashlib.sha256(
                material.encode("utf-8")
            ).hexdigest(),
        }

    def _command(self) -> list[str]:
        command = self.settings.get("command", [])
        if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
            raise HarnessError("Codex CLI provider requires a non-empty provider command")
        # Imported lazily because subscription_cli builds its Codex recipe from
        # helpers in this module. At dispatch time both modules are complete.
        # Resolving here lets the stable ``codex`` hint follow a desktop update
        # from build A to build B while arbitrary configured commands remain
        # exact authority inside subscription_cli.available().
        from .subscription_cli import available

        resolved = available("codex-cli", list(command))
        if not resolved:
            raise HarnessError(
                "The configured Codex command is not available. Update Codex or "
                "reconnect this route in Nexus Settings."
            )
        return [resolved, *command[1:]]

    @staticmethod
    def _reject_native_contract(request: ProviderRequest) -> None:
        if request.tools or request.responses_continuation or request.function_call_outputs:
            raise HarnessError("Codex CLI provider does not support native tools or continuation")
        if request.chat_continuation or request.chat_function_call_outputs:
            raise HarnessError("Codex CLI provider does not support native tools or continuation")
        if request.native_continuation or request.native_function_call_outputs:
            raise HarnessError("Codex CLI provider does not support native tools or continuation")

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self._reject_native_contract(request)
        timeout = self._timeout(request.timeout_seconds)
        deadline_at = time.monotonic() + timeout
        command = self._command()
        auth_mode = str(self.settings.get("auth_mode") or "")
        fallback = request.response_format is None
        contract_schema = _FALLBACK_SCHEMA if fallback else request.response_format.schema
        # Command/test output and provider-result transport are different
        # budgets.  The old min() silently made a valid multi-file response
        # impossible whenever execution.max_output_bytes kept its 250 KB
        # default. The bounded schema now determines the safe capture size;
        # larger explicitly configured values remain honoured.
        output_limit = _provider_capture_limit(
            int(self.config.get("execution.max_output_bytes")), contract_schema
        )
        if not self._preflight_complete:
            codex_cli_preflight(
                command,
                auth_mode=auth_mode,
                timeout_seconds=_remaining(deadline_at),
                model=request.model,
                reasoning_effort=str(request.reasoning_effort or "").strip(),
                max_output_bytes=min(32_000, output_limit),
            )
            self._preflight_complete = True
        schema = _codex_output_schema(contract_schema)
        with _private_workspace("our-harness-codex-") as cwd:
            schema_path = cwd / "response.schema.json"
            result_path = cwd / "response.json"
            catalog_path = cwd / "models.catalog.json"
            catalog = _bundled_model_catalog(
                command,
                cwd=cwd,
                model=request.model,
                timeout_seconds=_remaining(deadline_at),
                max_output_bytes=1_000_000,
            )
            for path, payload in (
                (schema_path, json.dumps(schema, sort_keys=True)),
                (result_path, ""),
                (catalog_path, catalog),
            ):
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
            effort = str(request.reasoning_effort or "").strip()
            _validate_model_reasoning_effort(catalog, request.model, effort)
            argv = [
                *command,
                "-c", "model_catalog_json=" + json.dumps(str(catalog_path)),
                *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--json",
                "--output-schema", str(schema_path),
                "--output-last-message", str(result_path),
                "--model", request.model,
                *[
                    part
                    for one in request.attachments
                    if isinstance(one, dict)
                    and str(one.get("type") or "").startswith("image/")
                    and str(one.get("path") or "")
                    for part in ("--image", str(one.get("path")))
                ],
                "-",
            ]
            result = _run_bounded(
                argv,
                cwd=cwd,
                stdin_text=self._redactor.text(_prompt(request, fallback)),
                timeout_seconds=_remaining(deadline_at),
                max_output_bytes=output_limit,
            )
            if result.timed_out:
                raise HarnessError("Codex CLI provider timed out at its wall-clock deadline")
            if result.output_truncated:
                raise HarnessError(f"Codex CLI output exceeded its {output_limit}-byte limit")
            if result.exit_code != 0:
                detail = bounded_redacted_text(
                    self._redactor, (result.stderr or result.stdout).strip(), 8_000
                )
                raise HarnessError(f"Codex CLI exited {result.exit_code}: {detail}")
            usage: dict[str, int | None] = {
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
            }
            completed_seen = False
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    event = _load_json(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise HarnessError("Codex CLI JSONL stream contained invalid JSON") from exc
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise HarnessError("Codex CLI JSONL event must be an object with a type")
                event_type = event["type"].casefold()
                if event_type == "error" or event_type.endswith(".failed"):
                    raise HarnessError("Codex CLI reported a failed turn")
                if event_type == "turn.completed":
                    if completed_seen:
                        raise HarnessError("Codex CLI JSONL stream contained duplicate turn.completed events")
                    completed_seen = True
                    raw_usage = event.get("usage")
                    if raw_usage is None:
                        continue
                    if not isinstance(raw_usage, dict):
                        raise HarnessError("Codex CLI turn.completed usage must be an object")
                    aliases = {
                        "input_tokens": ("input_tokens",),
                        "cached_input_tokens": ("cached_input_tokens",),
                        "output_tokens": ("output_tokens",),
                        "reasoning_tokens": ("reasoning_tokens", "reasoning_output_tokens"),
                    }
                    for target, names in aliases.items():
                        present = [name for name in names if name in raw_usage]
                        if len(present) > 1:
                            raise HarnessError(f"Codex CLI usage contains conflicting {target} fields")
                        if not present:
                            continue
                        value = raw_usage[present[0]]
                        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                            raise HarnessError(f"Codex CLI usage {present[0]} must be a non-negative integer")
                        usage[target] = value
            try:
                size = result_path.stat().st_size
            except OSError as exc:
                raise HarnessError("Codex CLI did not produce its result file") from exc
            if size > output_limit:
                raise HarnessError(f"Codex CLI result exceeded its {output_limit}-byte limit")
            if result_path.is_symlink() or not result_path.is_file():
                raise HarnessError("Codex CLI result path is not a regular file")
            raw = result_path.read_text(encoding="utf-8")
            try:
                value = _load_json(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise HarnessError("Codex CLI result is not valid JSON") from exc
            # Validate against both boundaries.  The native Codex schema
            # guarantees its stricter transport shape; the unmodified harness
            # schema remains the provider-neutral application contract.
            _validate_schema(value, schema)
            _validate_schema(value, contract_schema)
            text = value["text"] if fallback else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            return ProviderResponse(
                text=text,
                finish_reason="stop",
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cached_input_tokens=usage["cached_input_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                raw={"auth_mode": auth_mode, "price_status": "subscription-unpriced"},
            )
