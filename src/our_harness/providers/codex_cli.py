from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .. import cancellation
from ..execution import _BoundedCapture, _ProcessTree, _reap_process, _wait_for_terminated_process, _write_stdin
from ..models import CommandResult, HarnessError, ProviderRequest, ProviderResponse
from ..redaction import CredentialRedactor
from .base import Provider


_AUTH_MODE = "chatgpt"
_FALLBACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
}


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
        raise HarnessError("Codex CLI wall-clock deadline expired")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_minimal_codex_environment(also_in_the_environment),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
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
    timed_out = False
    try:
        process.wait(timeout=max(0.001, deadline_at - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
    if not timed_out:
        for worker in (*readers, *((writer,) if writer is not None else ())):
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            worker.join(remaining)
            if worker.is_alive():
                timed_out = True
                break
    if timed_out:
        tree.kill()
    else:
        tree.kill_descendants_after_exit()
    unregister_cancel()
    tree.close()
    cancellation.checkpoint()
    if timed_out:
        _wait_for_terminated_process(process)
    if process.poll() is None:
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
        raise HarnessError("Codex CLI wall-clock deadline expired")
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
        detail = CredentialRedactor().text((result.stderr or result.stdout).strip()[:2_000])
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


def codex_cli_preflight(
    command: list[str],
    *,
    auth_mode: str,
    timeout_seconds: float,
    model: str = "",
    max_output_bytes: int = 32_000,
) -> tuple[str, str]:
    """Execute the binary and auth checks. Path lookup alone is not sufficient."""
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise HarnessError("Codex CLI command must be a non-empty argv list")
    if auth_mode != _AUTH_MODE:
        raise HarnessError("Codex CLI auth_mode must be chatgpt")
    deadline_at = time.monotonic() + max(0.001, float(timeout_seconds))
    with tempfile.TemporaryDirectory(prefix="our-harness-codex-preflight-") as temporary:
        cwd = Path(temporary)
        version = _run_bounded(
            [*command, "--version"], cwd=cwd, stdin_text=None,
            timeout_seconds=_remaining(deadline_at), max_output_bytes=max_output_bytes,
        )
        if version.timed_out:
            raise HarnessError("Codex CLI --version timed out")
        if version.output_truncated:
            raise HarnessError("Codex CLI --version output exceeded its byte limit")
        if version.exit_code != 0:
            detail = CredentialRedactor().text((version.stderr or version.stdout).strip()[:2_000])
            raise HarnessError(f"Codex CLI --version failed ({version.exit_code}): {detail}")
        login = _run_bounded(
            [*command, "login", "status"], cwd=cwd, stdin_text=None,
            timeout_seconds=_remaining(deadline_at), max_output_bytes=max_output_bytes,
        )
        if login.timed_out:
            raise HarnessError("Codex CLI login status timed out")
        if login.output_truncated:
            raise HarnessError("Codex CLI login status output exceeded its byte limit")
        if login.exit_code != 0:
            detail = CredentialRedactor().text((login.stderr or login.stdout).strip()[:2_000])
            raise HarnessError(f"Codex CLI login status failed ({login.exit_code}): {detail}")
        status = f"{login.stdout}\n{login.stderr}".strip()
        if auth_mode == _AUTH_MODE and "chatgpt" not in status.casefold():
            raise HarnessError("Codex CLI is not signed in with ChatGPT")
        _bundled_model_catalog(
            command,
            cwd=cwd,
            model=model,
            timeout_seconds=_remaining(deadline_at),
            max_output_bytes=1_000_000,
        )
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

    def __init__(self, config):  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._preflight_complete = False

    def _command(self) -> list[str]:
        command = self.settings.get("command", [])
        if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
            raise HarnessError("Codex CLI provider requires a non-empty provider command")
        return list(command)

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
        output_limit = min(2_000_000, int(self.config.get("execution.max_output_bytes")))
        if not self._preflight_complete:
            codex_cli_preflight(
                command,
                auth_mode=auth_mode,
                timeout_seconds=_remaining(deadline_at),
                model=request.model,
                max_output_bytes=min(32_000, output_limit),
            )
            self._preflight_complete = True
        fallback = request.response_format is None
        schema = _FALLBACK_SCHEMA if fallback else request.response_format.schema
        with tempfile.TemporaryDirectory(prefix="our-harness-codex-") as temporary:
            cwd = Path(temporary)
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
            if effort and effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
                raise HarnessError("Codex CLI reasoning effort is invalid")
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
                detail = self._redactor.text((result.stderr or result.stdout).strip()[:8_000])
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
            _validate_schema(value, schema)
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
