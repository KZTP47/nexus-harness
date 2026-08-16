from __future__ import annotations

import hashlib
import difflib
import json
import os
import platform
import random
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .changes import FileTransaction, file_sha256
from .config import load_isolated_config
from .context import CompiledContext, ContextCompiler, fit_request_context, stable_prefix
from .execution import CommandRunner
from .graphs import ProductionGraphInterpreter, built_in_workflow_graph, resolve_graph_execution_policy, validate_graph
from .indexer import WorkspaceIndexer
from .memory import MemoryStore
from .models import ChangePlan, HarnessError, ProviderRequest
from .providers.base import StreamDecoder, collect_stream
from .redaction import CredentialRedactor
from .safety import confined_path
from .workflow import HarnessApplication


AGENTIC_TASK_TIMEOUT_SECONDS = 420
BENCHMARK_ID = "our-harness-deterministic"
BENCHMARK_VERSION = 3
DEFAULT_SEED = 20_260_814
MIN_REPETITIONS = 1
MAX_REPETITIONS = 10
MAX_DIAGNOSTIC_CHARS = 12_000


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _diagnostic_text(
    value: str,
    redactor: CredentialRedactor,
    replacements: dict[str, str],
    limit: int = MAX_DIAGNOSTIC_CHARS,
) -> str:
    safe = redactor.text(value)
    for source, replacement in sorted(replacements.items(), key=lambda item: -len(item[0])):
        if source:
            safe = safe.replace(source, replacement)
    if len(safe) <= limit:
        return safe
    marker = f"\n[diagnostic truncated; {len(safe) - limit} chars omitted]\n"
    available = max(0, limit - len(marker))
    head = int(available * 0.62)
    tail = available - head
    return safe[:head] + marker + (safe[-tail:] if tail else "")


def _candidate_diff(task: dict[str, Any], submitted: dict[str, bytes]) -> str:
    blocks: list[str] = []
    initial = task.get("initial_files", {})
    for name in sorted(set(task.get("allowed_paths", []))):
        before = str(initial.get(name, "")).splitlines(keepends=True)
        after_raw = submitted.get(name)
        after = [] if after_raw is None else after_raw.decode("utf-8", errors="replace").splitlines(keepends=True)
        blocks.extend(
            difflib.unified_diff(before, after, fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="")
        )
    return "\n".join(blocks)


def _trajectory_artifact(events: list[dict[str, Any]], redactor: CredentialRedactor, replacements: dict[str, str]) -> dict[str, Any]:
    retained = [
        {
            "sequence": event.get("sequence"),
            "kind": event.get("kind"),
            "node_id": event.get("node_id"),
            "payload": redactor.value(event.get("payload", {})),
        }
        for event in events
    ]
    raw = _canonical(retained)
    return {
        "event_count": len(retained),
        "failure_count": sum(1 for event in retained if event.get("kind") == "failure"),
        "sha256": _sha256(raw),
        "excerpt": _diagnostic_text(raw.decode("utf-8"), redactor, replacements),
    }


def _resource_json(name: str) -> tuple[dict[str, Any], bytes]:
    raw = files("our_harness.templates").joinpath(name).read_bytes()
    return json.loads(raw), raw


def benchmark_manifest() -> dict[str, Any]:
    manifest, _ = _resource_json("benchmark_manifest.json")
    return manifest


def result_schema() -> dict[str, Any]:
    schema, _ = _resource_json("benchmark_result.schema.json")
    return schema


def agentic_fixtures() -> dict[str, Any]:
    fixtures, _ = _resource_json("benchmark_agentic_fixtures.json")
    return fixtures


def _expect_error(operation: Callable[[], Any], contains: str | None = None) -> str:
    try:
        operation()
    except HarnessError as exc:
        message = str(exc)
        if contains and contains.casefold() not in message.casefold():
            raise AssertionError(f"Expected error containing {contains!r}, received {message!r}") from exc
        return message
    raise AssertionError("Expected a HarnessError")


def _case_safety_paths(rng: random.Random) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-safety-") as temporary:
        base = Path(temporary)
        root = base / "fixture"
        root.mkdir()
        canary = base / f"outside-{rng.getrandbits(48):012x}.txt"
        canary.write_text("unchanged", encoding="utf-8")
        rejected = []
        for target in ("../outside.txt", str(canary), ".git/config", ".harness/config.json"):
            if target.startswith(".") and not target.startswith(".."):
                rejected.append(_expect_error(lambda target=target: FileTransaction(root).apply([ChangePlan(target, None, "x")]), "reserved"))
            else:
                rejected.append(_expect_error(lambda target=target: confined_path(root, target)))
        return {"rejected": len(rejected), "outside_sha256": file_sha256(canary)}


def _case_safety_baseline_rollback(rng: random.Random) -> dict[str, Any]:
    del rng
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-baseline-") as temporary:
        root = Path(temporary)
        target = root / "value.txt"
        target.write_text("one\n", encoding="utf-8")
        transaction = FileTransaction(root)
        stale = "0" * 64
        _expect_error(lambda: transaction.apply([ChangePlan("value.txt", stale, "wrong\n")]), "baseline conflict")
        applied = transaction.apply([ChangePlan("value.txt", file_sha256(target), "two\n")])
        target.write_text("user\n", encoding="utf-8")
        _expect_error(lambda: transaction.rollback(str(applied["transaction_id"])), "rollback conflict")
        if target.read_text(encoding="utf-8") != "user\n":
            raise AssertionError("Rollback changed a later user edit")
        return {"stale_rejected": True, "later_edit_preserved": True}


def _prepared_manifest(root: Path, transaction_id: str) -> Path:
    path = root / ".harness" / "backups" / transaction_id / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["state"] = "prepared"
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def _case_recovery_states(rng: random.Random) -> dict[str, Any]:
    del rng
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-recovery-") as temporary:
        root = Path(temporary)
        target = root / "state.txt"
        target.write_text("before", encoding="utf-8")
        transaction = FileTransaction(root)

        applied = transaction.apply([ChangePlan("state.txt", file_sha256(target), "after")])
        txid = str(applied["transaction_id"])
        _prepared_manifest(root, txid)
        if transaction.reconcile()[0]["status"] != "applied_after_crash":
            raise AssertionError("Fully applied prepared transaction was not classified")
        finalized = transaction.recover(txid, "finalize")
        if finalized["status"] != "applied" or target.read_text(encoding="utf-8") != "after":
            raise AssertionError("Finalization changed applied content")

        mixed = transaction.apply([ChangePlan("state.txt", file_sha256(target), "new")])
        mixed_id = str(mixed["transaction_id"])
        _prepared_manifest(root, mixed_id)
        target.write_text("unknown", encoding="utf-8")
        statuses = {item["transaction_id"]: item["status"] for item in transaction.reconcile()}
        if statuses.get(mixed_id) != "in_doubt":
            raise AssertionError("Unknown content was not classified as in_doubt")
        _expect_error(lambda: transaction.recover(mixed_id, "rollback"), "cannot be recovered automatically")
        if target.read_text(encoding="utf-8") != "unknown":
            raise AssertionError("Ambiguous recovery changed content")
        return {"finalized": True, "in_doubt_refused": True}


def _case_context_bounds(rng: random.Random) -> dict[str, Any]:
    del rng
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-context-") as temporary:
        root = Path(temporary)
        config = load_isolated_config(root, {"context": {"max_chars": 5000, "reserve_chars": 500}})
        first, first_hash = stable_prefix()
        second, second_hash = stable_prefix()
        if (first, first_hash) != (second, second_hash):
            raise AssertionError("Static prefix changed between calls")
        fitted = fit_request_context(config, CompiledContext(first, first_hash, "D" * 10_000, {}), "P" * 10_000)
        if fitted.total_chars > 4500 or fitted.total_chars != len(first) + len(fitted.dynamic) + len(fitted.prompt):
            raise AssertionError("Complete provider request exceeded its bound")
        return {"prefix_sha256": first_hash, "total_chars": fitted.total_chars, "limit_chars": fitted.limit_chars}


def _case_context_manifest(rng: random.Random) -> dict[str, Any]:
    token = f"rule-{rng.getrandbits(40):010x}"
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-evidence-") as temporary:
        root = Path(temporary)
        (root / "README.md").write_text(f"Use {token} for exact totals.\n", encoding="utf-8")
        (root / "invoice.py").write_text(f"RULE = {token!r}\n", encoding="utf-8")
        config = load_isolated_config(root)
        with MemoryStore(config) as memory:
            WorkspaceIndexer(config, memory).scan()
            compiled = ContextCompiler(config, memory).compile(token, [{"stack": "python"}])
        standards = {item["path"]: item["sha256"] for item in compiled.manifest["standards"]}
        expected = _sha256((root / "README.md").read_text(encoding="utf-8").encode("utf-8"))
        if standards.get("README.md") != expected or compiled.manifest["prefix_sha256"] != compiled.prefix_sha256:
            raise AssertionError("Context evidence hashes do not match source bytes")
        return {"standards": len(standards), "workspace_hits": len(compiled.manifest["workspace"])}


def _case_index_lifecycle(rng: random.Random) -> dict[str, Any]:
    token = f"INDEX_{rng.getrandbits(32):08x}"
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-index-") as temporary:
        root = Path(temporary)
        target = root / "sample.py"
        target.write_text(f"{token} = 1\n", encoding="utf-8")
        config = load_isolated_config(root)
        with MemoryStore(config) as memory:
            indexer = WorkspaceIndexer(config, memory)
            counts = [indexer.scan()["updated"], indexer.scan()["updated"]]
            target.write_text(f"{token} = 2\n", encoding="utf-8")
            counts.append(indexer.scan()["updated"])
            hits = memory.search_documents(token)
            target.unlink()
            indexer.scan()
            removed = memory.document_hash("sample.py") is None
        if counts != [1, 0, 1] or not hits or not removed:
            raise AssertionError("Incremental index lifecycle was inconsistent")
        return {"updated_sequence": counts, "removed": removed}


def _case_index_dependencies(rng: random.Random) -> dict[str, Any]:
    token = f"parse_{rng.getrandbits(32):08x}"
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-dependency-") as temporary:
        root = Path(temporary)
        (root / "parser.py").write_text(f"from tokens import Token\n\ndef {token}(value): return Token(value)\n", encoding="utf-8")
        (root / "tokens.py").write_text("class Token:\n    def __init__(self, value): self.value = value\n", encoding="utf-8")
        (root / "other.py").write_text("UNRELATED = True\n", encoding="utf-8")
        config = load_isolated_config(root)
        with MemoryStore(config) as memory:
            WorkspaceIndexer(config, memory).scan()
            direct = memory.search_documents(token)
            related = memory.dependency_documents([direct[0].key], 8)
        keys = [item.key for item in related]
        if not direct or direct[0].key != "parser.py" or "tokens.py" not in keys:
            raise AssertionError("Dependency expansion did not return the imported module")
        return {"direct": direct[0].key, "dependencies": keys}


def _bypass_graph() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "benchmark-bypass",
        "entry": "start",
        "nodes": [
            {"id": "start", "type": "start"}, {"id": "planner", "type": "planner"},
            {"id": "coder", "type": "coder"}, {"id": "syntax", "type": "tool", "config": {"role": "syntax"}},
            {"id": "unit", "type": "tool", "config": {"role": "unit_test"}},
            {"id": "review", "type": "evaluator"}, {"id": "end", "type": "end"},
        ],
        "edges": [
            {"source": "start", "target": "planner"}, {"source": "planner", "target": "coder"},
            {"id": "shortcut", "source": "coder", "target": "end"}, {"source": "coder", "target": "syntax"},
            {"source": "syntax", "target": "unit"}, {"source": "unit", "target": "review"}, {"source": "review", "target": "end"},
        ],
    }


def _case_graph_invariants(rng: random.Random) -> dict[str, Any]:
    del rng
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-graph-") as temporary:
        root = Path(temporary)
        _expect_error(lambda: resolve_graph_execution_policy(load_isolated_config(root), _bypass_graph()), "bypasses required")
        graph = _bypass_graph()
        graph["edges"] = [edge for edge in graph["edges"] if edge.get("id") != "shortcut"]
        graph["edges"][4] = {"source": "unit", "target": "end", "condition": "stage_passed == false"}
        _expect_error(lambda: resolve_graph_execution_policy(load_isolated_config(root), graph), "routes failure state")
        return {"shortcut_rejected": True, "failure_route_rejected": True}


def _case_graph_order_and_loops(rng: random.Random) -> dict[str, Any]:
    del rng
    order_graph = {
        "entry": "start",
        "nodes": [{"id": "start", "type": "start"}, {"id": "first", "type": "end"}, {"id": "second", "type": "end"}],
        "edges": [
            {"id": "preferred", "source": "start", "target": "first", "condition": "choice == 1", "variables": ["payload.value"]},
            {"id": "fallback", "source": "start", "target": "second"},
        ],
    }
    state = {"choice": 1, "payload": {"value": "typed"}}
    transition = ProductionGraphInterpreter(order_graph).advance(state)
    cycle = {"entry": "a", "nodes": [{"id": "a", "type": "coder"}, {"id": "b", "type": "tool"}], "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]}
    issues = validate_graph(cycle)
    if transition is None or transition["edge"] != "preferred" or state["edge_inputs"] != {"payload.value": "typed"}:
        raise AssertionError("Declared graph edge order or variable transfer changed")
    if not any("max_iterations" in issue.message for issue in issues):
        raise AssertionError("Unbounded graph cycle was accepted")
    return {"selected_edge": transition["edge"], "cycle_rejected": True}


def _case_stream_decoder(rng: random.Random) -> dict[str, Any]:
    token = f"å-{rng.getrandbits(24):06x}"
    raw = (json.dumps({"text": token}, ensure_ascii=False) + "\r\n" + json.dumps({"done": True}) + "\n").encode("utf-8")
    decoder = StreamDecoder()
    lines: list[str] = []
    for value in raw:
        lines.extend(decoder.feed(bytes([value])))
    lines.extend(decoder.feed(b"", final=True))
    parsed = [json.loads(line) for line in lines]
    if parsed != [{"text": token}, {"done": True}]:
        raise AssertionError("Fragmented UTF-8 frames were not reconstructed")
    _expect_error(lambda: StreamDecoder(max_buffer_bytes=5).feed(b"123456"), "limit")
    return {"frames": len(lines), "unicode_sha256": _sha256(token.encode("utf-8"))}


def _case_stream_protocol(rng: random.Random) -> dict[str, Any]:
    del rng
    request = ProviderRequest("prefix", "dynamic", [], "fixture")

    class Good:
        def stream(self, _request: ProviderRequest):
            yield {"type": "text_delta", "text": "hello"}
            yield {"type": "usage", "input_tokens": 4, "output_tokens": 2}
            yield {"type": "done", "finish_reason": "stop"}

    class Missing:
        def stream(self, _request: ProviderRequest):
            yield {"type": "text_delta", "text": "partial"}

    class AfterDone:
        def stream(self, _request: ProviderRequest):
            yield {"type": "done"}
            yield {"type": "text_delta", "text": "late"}

    response = collect_stream(Good(), request, max_text_chars=5)
    _expect_error(lambda: collect_stream(Missing(), request), "completion")
    _expect_error(lambda: collect_stream(AfterDone(), request), "after completion")
    _expect_error(lambda: collect_stream(Good(), request, max_text_chars=4), "character limit")
    return {"text_sha256": _sha256(response.text.encode("utf-8")), "input_tokens": response.input_tokens}


def _case_execution_bounds(rng: random.Random) -> dict[str, Any]:
    del rng
    with tempfile.TemporaryDirectory(prefix="harness-benchmark-execution-") as temporary:
        root = Path(temporary)
        runner = CommandRunner(load_isolated_config(root, {"execution": {"timeout_seconds": 2, "max_output_bytes": 2048}}))
        output = runner.run([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'o'*20000);sys.stderr.buffer.write(b'e'*20000)"])
        timeout = runner.run([sys.executable, "-c", "import time;time.sleep(3)"], timeout=0.2)
        captured = len(output.stdout.encode()) + len(output.stderr.encode())
        if not output.passed or not output.output_truncated or captured > 2048:
            raise AssertionError("Command output was not drained within its combined cap")
        if not timeout.timed_out or timeout.exit_code != 124:
            raise AssertionError("Command timeout was not reported as a structured failure")
        return {"captured_bytes": captured, "timed_out": timeout.timed_out, "timeout_duration_ms": timeout.duration_ms}


CASE_FUNCTIONS: dict[str, Callable[[random.Random], dict[str, Any]]] = {
    "SAF-001": _case_safety_paths,
    "SAF-002": _case_safety_baseline_rollback,
    "REC-001": _case_recovery_states,
    "CTX-001": _case_context_bounds,
    "CTX-002": _case_context_manifest,
    "IDX-001": _case_index_lifecycle,
    "IDX-002": _case_index_dependencies,
    "GRF-001": _case_graph_invariants,
    "GRF-002": _case_graph_order_and_loops,
    "STR-001": _case_stream_decoder,
    "STR-002": _case_stream_protocol,
    "EXE-001": _case_execution_bounds,
}


def _provider_profile(path: str | Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    try:
        profile_path = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HarnessError("Benchmark provider profile does not exist") from exc
    if not profile_path.is_file():
        raise HarnessError("Benchmark provider profile must be a JSON file")
    raw = profile_path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"Benchmark provider profile is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError("Benchmark provider profile root must be an object")
    route_ref = value.get("benchmark_provider_route")
    if route_ref is not None:
        if not isinstance(route_ref, str) or not route_ref:
            raise HarnessError("Benchmark provider route must be a non-empty profile ID")
        profiles = value.get("providers")
        if not isinstance(profiles, dict) or not isinstance(profiles.get(route_ref), dict):
            raise HarnessError("Benchmark provider route must name a configured provider profile")
        selected = profiles[route_ref]
        if not selected.get("allow_project_graphs"):
            raise HarnessError("Benchmark named provider route must allow the benchmark graph")
        provider_config = {"providers": profiles, "benchmark_provider_route": route_ref}
    else:
        provider = value.get("provider", value)
        if not isinstance(provider, dict) or not provider:
            raise HarnessError("Benchmark provider profile must define a provider object")
        if "name" not in provider or "model" not in provider:
            raise HarnessError("Benchmark provider profile must explicitly define provider.name and provider.model")
        provider_config = {"provider": provider}
    workflow = value.get("workflow", {})
    if not isinstance(workflow, dict):
        raise HarnessError("Benchmark provider profile workflow must be an object")
    unknown_workflow = sorted(set(workflow) - {"max_elapsed_seconds", "require_executable_counterexamples"})
    if unknown_workflow:
        raise HarnessError(f"Unsupported benchmark workflow profile key: {unknown_workflow[0]}")
    if "max_elapsed_seconds" in workflow and (
        isinstance(workflow["max_elapsed_seconds"], bool)
        or not isinstance(workflow["max_elapsed_seconds"], int)
        or not 1 <= workflow["max_elapsed_seconds"] <= 3_600
    ):
        raise HarnessError("Benchmark workflow max_elapsed_seconds must be from 1 through 3600")
    if "require_executable_counterexamples" in workflow and not isinstance(
        workflow["require_executable_counterexamples"], bool
    ):
        raise HarnessError("Benchmark workflow require_executable_counterexamples must be a boolean")
    return provider_config, workflow, _sha256(raw)


def _materialize_agentic_fixture(root: Path, task: dict[str, Any], fixture_seed: int) -> dict[str, bytes]:
    initial = task.get("initial_files")
    expected = task.get("expected_files")
    if not isinstance(initial, dict) or not isinstance(expected, dict) or set(initial) != set(expected):
        raise HarnessError(f"Agentic fixture {task.get('id')} has inconsistent file maps")
    expected_bytes: dict[str, bytes] = {}
    for name, content in initial.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise HarnessError(f"Agentic fixture {task.get('id')} contains a malformed source file")
        path = confined_path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        expected_content = expected[name]
        if not isinstance(expected_content, str):
            raise HarnessError(f"Agentic fixture {task.get('id')} contains malformed expected content")
        expected_bytes[name] = expected_content.encode("utf-8")
    seed_text = f"{fixture_seed:016x}\n".encode("ascii")
    (root / "fixture_seed.txt").write_bytes(seed_text)
    expected_bytes["fixture_seed.txt"] = seed_text
    return expected_bytes


def _capture_authored_tree(root: Path) -> tuple[dict[str, str], dict[str, bytes], list[str]]:
    files: dict[str, str] = {}
    contents: dict[str, bytes] = {}
    unsafe: list[str] = []
    pending = [root]
    ignored = {".harness", "__pycache__"}
    while pending:
        directory = pending.pop()
        for entry in sorted(os.scandir(directory), key=lambda item: item.name, reverse=True):
            if entry.name in ignored or entry.name.endswith(".pyc"):
                continue
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            linked = entry.is_symlink() or bool(
                getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            if linked:
                unsafe.append(relative)
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                content = path.read_bytes()
                files[relative] = _sha256(content)
                contents[relative] = content
            else:
                unsafe.append(relative)
    return dict(sorted(files.items())), dict(sorted(contents.items())), sorted(unsafe)


def _graded_tree(root: Path) -> tuple[dict[str, str], list[str]]:
    files, _, unsafe = _capture_authored_tree(root)
    return files, unsafe


def _materialize_snapshot(root: Path, contents: dict[str, bytes]) -> None:
    root.mkdir()
    for relative, content in contents.items():
        path = confined_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _command_result(config: Any, command: list[str], *, cwd: str | Path = ".") -> dict[str, Any]:
    try:
        return CommandRunner(config).run(command, cwd=cwd, timeout=min(30, int(config.get("execution.timeout_seconds")))).to_dict()
    except Exception as exc:
        return {"argv": command, "exit_code": 1, "timed_out": False, "duration_ms": 0, "error": f"{type(exc).__name__}: {exc}"}


def _unchanged_file(root: Path, name: str, expected_sha256: str) -> bool:
    try:
        path = confined_path(root, name, allow_missing=False)
        return path.is_file() and file_sha256(path) == expected_sha256
    except (HarnessError, OSError):
        return False


def _agentic_attempt(
    task: dict[str, Any],
    provider_profile: dict[str, Any],
    workflow_profile: dict[str, Any],
    profile_sha256: str,
    seed: int,
    repetition: int,
) -> dict[str, Any]:
    started = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix="harness-benchmark-agentic-") as temporary,
        tempfile.TemporaryDirectory(prefix="harness-benchmark-public-evaluator-") as public_temporary,
        tempfile.TemporaryDirectory(prefix="harness-benchmark-hidden-evaluator-") as hidden_temporary,
    ):
        outer = Path(temporary)
        root = outer / "workspace"
        root.mkdir()
        expected_bytes = _materialize_agentic_fixture(root, task, seed)
        hidden = task.get("hidden_evaluator")
        if not isinstance(hidden, str):
            raise HarnessError(f"Agentic fixture {task.get('id')} has no hidden evaluator")
        public_evaluator_root = Path(public_temporary)
        hidden_evaluator_root = Path(hidden_temporary)
        public_safe_cwd = public_evaluator_root / "safe-cwd"
        hidden_safe_cwd = hidden_evaluator_root / "safe-cwd"
        public_safe_cwd.mkdir()
        hidden_safe_cwd.mkdir()
        public_path = public_evaluator_root / "public_evaluator.py"
        public_path.write_text(
            "import pathlib, sys, unittest\n"
            "root = pathlib.Path(sys.argv[1]).resolve()\n"
            "sys.path.insert(0, str(root))\n"
            "suite = unittest.defaultTestLoader.discover(str(root / 'tests_public'))\n"
            "result = unittest.TextTestRunner(verbosity=0).run(suite)\n"
            "raise SystemExit(0 if result.wasSuccessful() else 1)\n",
            encoding="utf-8",
            newline="",
        )
        control_evaluator = root / ".harness" / "benchmark_public_evaluator.py"
        control_evaluator.parent.mkdir()
        control_evaluator.write_bytes(public_path.read_bytes())
        hidden_path = hidden_evaluator_root / "hidden_evaluator.py"
        hidden_path.write_text(hidden, encoding="utf-8", newline="")
        hidden_sha256 = file_sha256(hidden_path)

        # Run the visible evaluator after every candidate so its exact failure
        # reaches the repair loop. The hidden evaluator remains outside the
        # workflow and is never placed in model context or retained diagnostics.
        workflow_command = [
            sys.executable,
            "-I",
            "-S",
            ".harness/benchmark_public_evaluator.py",
            ".",
        ]
        workflow = {
            "name": "planner-coder-reviewer", "max_iterations": 3,
            "max_elapsed_seconds": AGENTIC_TASK_TIMEOUT_SECONDS, "repeat_failure_limit": 2,
            "reviewers": 1, "review_parallelism": 1, "reviewer_lenses": [],
        }
        workflow.update(workflow_profile)
        overrides = {
            "project": {"test_commands": [workflow_command], "lint_commands": [], "build_commands": []},
            "execution": {"mode": "process", "timeout_seconds": 30, "max_output_bytes": 100_000},
            "git": {"enabled": False, "allow_commit": False, "allow_push": False, "allow_merge": False},
            "memory": {"embedding_provider": "", "embedding_model": ""},
            "workflow": workflow,
            "mcp": {"servers": []},
            "plugins": {"enabled": [], "paths": []},
        }
        if "provider" in provider_profile:
            overrides["provider"] = provider_profile["provider"]
        else:
            overrides["providers"] = provider_profile["providers"]
        config = load_isolated_config(root, overrides)
        benchmark_config_sha256 = _sha256(_canonical(config.data))
        forbidden = list(task.get("forbidden_paths", [])) + ["fixture_seed.txt"]
        forbidden_before = {name: file_sha256(confined_path(root, name, allow_missing=False)) for name in forbidden}
        run_result: dict[str, Any] | None = None
        run_error: str | None = None
        failed_provider_usage: dict[str, Any] = {}
        failed_tool_usage: dict[str, Any] = {}
        trajectory: list[dict[str, Any]] = []
        redactor = CredentialRedactor(config)
        replacements = {
            str(outer): "<agent-workspace>",
            str(public_evaluator_root): "<public-evaluator>",
            str(hidden_evaluator_root): "<hidden-evaluator>",
        }
        try:
            with HarnessApplication(config) as application:
                try:
                    graph = None
                    route_ref = provider_profile.get("benchmark_provider_route")
                    if isinstance(route_ref, str) and route_ref:
                        graph = built_in_workflow_graph(config)
                        for node in graph.get("nodes", []):
                            if isinstance(node, dict) and node.get("type") in {"planner", "coder", "evaluator", "merge"}:
                                settings = node.setdefault("config", {})
                                if isinstance(settings, dict):
                                    settings["provider_route"] = route_ref
                    run_result = application.run_task(str(task["task"]), dry_run=False, graph=graph)
                finally:
                    snapshot = application.runtime_metrics()
                    failed_provider_usage = snapshot["provider_usage"]
                    failed_tool_usage = snapshot["agent_tools"]
                    run_id = run_result.get("run_id") if isinstance(run_result, dict) else None
                    if not isinstance(run_id, str):
                        row = application.memory.connection.execute(
                            "SELECT id FROM runs ORDER BY updated_at DESC, started_at DESC LIMIT 1"
                        ).fetchone()
                        run_id = str(row["id"]) if row is not None else None
                    if run_id:
                        trajectory = application.memory.events(run_id)
        except Exception as exc:
            run_error = _diagnostic_text(f"{type(exc).__name__}: {exc}", redactor, replacements, 4_000)

        actual_tree, submitted_contents, unsafe_paths = _capture_authored_tree(root)
        initial_bytes = {
            name: content.encode("utf-8")
            for name, content in task.get("initial_files", {}).items()
            if isinstance(name, str) and isinstance(content, str)
        }
        initial_bytes["fixture_seed.txt"] = f"{seed:016x}\n".encode("ascii")
        initial_tree = {name: _sha256(content) for name, content in initial_bytes.items()}
        expected_tree = {name: _sha256(content) for name, content in expected_bytes.items()}
        allowed = set(task.get("allowed_paths", []))
        exact_patch = all(actual_tree.get(name) == expected_tree.get(name) for name in allowed)
        exact_tree = actual_tree == dict(sorted(expected_tree.items())) and not unsafe_paths
        allowed_scope_only = all(
            (name in allowed) or actual_tree.get(name) == digest
            for name, digest in initial_tree.items()
        ) and not (set(actual_tree) - set(initial_tree) - allowed)
        forbidden_unchanged = all(_unchanged_file(root, name, digest) for name, digest in forbidden_before.items())

        public_workspace = public_evaluator_root / "workspace"
        hidden_workspace = hidden_evaluator_root / "workspace"
        _materialize_snapshot(public_workspace, submitted_contents)
        _materialize_snapshot(hidden_workspace, submitted_contents)
        public_pre_tree, public_pre_unsafe = _graded_tree(public_workspace)
        hidden_pre_tree, hidden_pre_unsafe = _graded_tree(hidden_workspace)
        public_config = load_isolated_config(
            public_evaluator_root,
            {"execution": {"mode": "process", "timeout_seconds": 30, "max_output_bytes": 100_000}},
        )
        hidden_config = load_isolated_config(
            hidden_evaluator_root,
            {"execution": {"mode": "process", "timeout_seconds": 30, "max_output_bytes": 100_000}},
        )
        public = _command_result(
            public_config,
            [sys.executable, "-I", "-S", str(public_path), str(public_workspace)],
            cwd="safe-cwd",
        )
        public_post_tree, public_post_unsafe = _graded_tree(public_workspace)
        hidden_result = _command_result(
            hidden_config,
            [sys.executable, "-I", "-S", str(hidden_path), str(hidden_workspace)],
            cwd="safe-cwd",
        )
        hidden_post_tree, hidden_post_unsafe = _graded_tree(hidden_workspace)
        public_tree_unchanged = public_pre_tree == public_post_tree and public_pre_unsafe == public_post_unsafe
        hidden_tree_unchanged = hidden_pre_tree == hidden_post_tree and hidden_pre_unsafe == hidden_post_unsafe
        completed = isinstance(run_result, dict) and run_result.get("state") == "complete"
        public_passed = public.get("exit_code") == 0 and not public.get("timed_out")
        hidden_passed = hidden_result.get("exit_code") == 0 and not hidden_result.get("timed_out")
        resolved = bool(
            completed
            and public_passed
            and hidden_passed
            and allowed_scope_only
            and forbidden_unchanged
            and public_tree_unchanged
            and hidden_tree_unchanged
            and not unsafe_paths
        )
        provider_usage = run_result.get("provider_usage", {}) if run_result else failed_provider_usage
        totals = provider_usage.get("totals", {}) if isinstance(provider_usage, dict) else {}
        tool_usage = run_result.get("agent_tools", {}) if run_result else failed_tool_usage
        metrics = {
            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
            "provider_calls": int(provider_usage.get("requests", 0) or 0) if isinstance(provider_usage, dict) else 0,
            "input_tokens": int(totals.get("input_tokens", 0) or 0) if isinstance(totals, dict) else 0,
            "output_tokens": int(totals.get("output_tokens", 0) or 0) if isinstance(totals, dict) else 0,
            "cached_input_tokens": int(totals.get("cached_input_tokens", 0) or 0) if isinstance(totals, dict) else 0,
            "tool_calls": int(tool_usage.get("calls", 0) or 0) if isinstance(tool_usage, dict) else 0,
            "tool_output_bytes": int(tool_usage.get("output_bytes", 0) or 0) if isinstance(tool_usage, dict) else 0,
        }
        candidate_diff = _candidate_diff(task, submitted_contents)
        redacted_diff = _diagnostic_text(candidate_diff, redactor, replacements)
        diagnostics = {
            "schema_version": 1,
            "trajectory": _trajectory_artifact(trajectory, redactor, replacements),
            "candidate_patch_sha256": _sha256(candidate_diff.encode("utf-8")),
            "candidate_patch_excerpt": redacted_diff,
            "public_stdout": _diagnostic_text(str(public.get("stdout", "")), redactor, replacements, 4_000),
            "public_stderr": _diagnostic_text(str(public.get("stderr", "")), redactor, replacements, 4_000),
            "public_output_truncated": bool(public.get("output_truncated", False)),
        }
        result = {
            "id": str(task["id"]),
            "repetition": repetition,
            "seed": seed,
            "status": "resolved" if resolved else "failed",
            "score": 100 if resolved else 0,
            "checks": {
                "workflow_complete": completed,
                "public_tests": public_passed,
                "hidden_tests": hidden_passed,
                "exact_patch": exact_patch,
                "exact_tree": exact_tree,
                "allowed_scope_only": allowed_scope_only,
                "forbidden_paths_unchanged": forbidden_unchanged,
                "public_tree_unchanged": public_tree_unchanged,
                "hidden_tree_unchanged": hidden_tree_unchanged,
            },
            "metrics": metrics,
            "diagnostics": diagnostics,
            "evidence": {
                "expected_tree_sha256": _sha256(_canonical(expected_tree)),
                "actual_tree_sha256": _sha256(_canonical(actual_tree)),
                "public_post_tree_sha256": _sha256(_canonical(public_post_tree)),
                "hidden_post_tree_sha256": _sha256(_canonical(hidden_post_tree)),
                "hidden_evaluator_sha256": hidden_sha256,
                "unsafe_paths": unsafe_paths,
                "public_exit_code": public.get("exit_code"),
                "hidden_exit_code": hidden_result.get("exit_code"),
                "provider_profile_sha256": profile_sha256,
                "benchmark_config_sha256": benchmark_config_sha256,
            },
        }
        if run_error:
            result["error"] = run_error
        return result


def run_agentic_benchmark(provider_profile: str | Path, seed: int, repetitions: int = 1) -> dict[str, Any]:
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not MIN_REPETITIONS <= repetitions <= MAX_REPETITIONS:
        raise HarnessError(f"Benchmark repetitions must be from {MIN_REPETITIONS} through {MAX_REPETITIONS}")
    provider_config, workflow_profile, profile_sha256 = _provider_profile(provider_profile)
    fixtures, fixture_raw = _resource_json("benchmark_agentic_fixtures.json")
    tasks = fixtures.get("tasks", [])
    if fixtures.get("suite_id") != "our-harness-agentic-resolution" or len(tasks) < 3:
        raise HarnessError("Agentic fixture suite identity or task count is invalid")
    attempts: list[dict[str, Any]] = []
    for repetition in range(1, repetitions + 1):
        ordered = sorted(tasks, key=lambda task: hashlib.sha256(f"{seed}:{repetition}:{task['id']}:order".encode()).digest())
        for task in ordered:
            task_seed = int.from_bytes(hashlib.sha256(f"{seed}:{repetition}:{task['id']}".encode()).digest()[:8], "big")
            attempts.append(_agentic_attempt(task, provider_config, workflow_profile, profile_sha256, task_seed, repetition))
    resolved = sum(item["status"] == "resolved" for item in attempts)
    score = round(100 * resolved / len(attempts), 2)
    metric_names = ("elapsed_ms", "provider_calls", "input_tokens", "output_tokens", "cached_input_tokens", "tool_calls", "tool_output_bytes")
    metrics = {name: sum(int(item["metrics"][name]) for item in attempts) for name in metric_names}
    return {
        "status": "completed",
        "reason": f"{resolved} of {len(attempts)} isolated task attempts resolved.",
        "suite_id": fixtures["suite_id"],
        "fixture_version": fixtures["version"],
        "fixture_sha256": _sha256(fixture_raw),
        "provider_profile_sha256": profile_sha256,
        "repetitions": repetitions,
        "attempts": len(attempts),
        "resolved": resolved,
        "score": score,
        "metrics": metrics,
        "tasks": attempts,
    }


def _source_hashes() -> dict[str, str]:
    result: dict[str, str] = {}

    def visit(node: Any, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            name = str(child.name)
            if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                continue
            relative = f"{prefix}/{name}" if prefix else name
            if child.is_dir():
                visit(child, relative)
            elif child.is_file():
                result[relative] = _sha256(child.read_bytes())

    visit(files("our_harness"), "our_harness")
    return dict(sorted(result.items()))


def run_benchmark(seed: int = DEFAULT_SEED, provider_profile: str | None = None, repetitions: int = 1) -> dict[str, Any]:
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not MIN_REPETITIONS <= repetitions <= MAX_REPETITIONS:
        raise HarnessError(f"Benchmark repetitions must be from {MIN_REPETITIONS} through {MAX_REPETITIONS}")
    manifest, manifest_raw = _resource_json("benchmark_manifest.json")
    _, schema_raw = _resource_json("benchmark_result.schema.json")
    _, agentic_fixture_raw = _resource_json("benchmark_agentic_fixtures.json")
    cases = manifest.get("cases", [])
    if manifest.get("benchmark_id") != BENCHMARK_ID or manifest.get("version") != BENCHMARK_VERSION:
        raise HarnessError("Benchmark manifest identity is not supported")
    if sum(int(case["weight"]) for case in cases) != 100:
        raise HarnessError("Benchmark manifest weights must total 100")
    if set(CASE_FUNCTIONS) != {str(case["id"]) for case in cases}:
        raise HarnessError("Benchmark manifest cases do not match the evaluator")

    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    ordered = sorted(cases, key=lambda case: hashlib.sha256(f"{seed}:{case['id']}:order".encode()).digest())
    results: list[dict[str, Any]] = []
    for position, case in enumerate(ordered):
        case_started = time.monotonic()
        status = "pass"
        evidence: dict[str, Any] = {}
        error: str | None = None
        case_seed = int.from_bytes(hashlib.sha256(f"{seed}:{case['id']}".encode()).digest()[:8], "big")
        try:
            evidence = CASE_FUNCTIONS[str(case["id"])](random.Random(case_seed))
        except Exception as exc:
            status = "fail"
            error = f"{type(exc).__name__}: {exc}"
        result = {
            "id": case["id"],
            "domain": case["domain"],
            "weight": int(case["weight"]),
            "critical": bool(case.get("critical", False)),
            "status": status,
            "elapsed_ms": max(0, round((time.monotonic() - case_started) * 1000)),
            "evidence": evidence,
        }
        if error is not None:
            result["error"] = error
        results.append(result)

    passed_weight = sum(item["weight"] for item in results if item["status"] == "pass")
    critical_failures = [item["id"] for item in results if item["critical"] and item["status"] != "pass"]
    score = min(passed_weight, 49) if critical_failures else passed_weight
    source_hashes = _source_hashes()
    artifact_inputs = {
        "manifest": _sha256(manifest_raw),
        "result_schema": _sha256(schema_raw),
        "agentic_fixtures": _sha256(agentic_fixture_raw),
        "sources": source_hashes,
    }
    agentic = (
        run_agentic_benchmark(provider_profile, seed, repetitions)
        if provider_profile
        else {"status": "not_run", "reason": "No provider profile was supplied."}
    )
    agentic_score: str | float = agentic["score"] if agentic["status"] == "completed" else "not_run"
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    result = {
        "schema_version": 3,
        "benchmark": {
            "id": BENCHMARK_ID,
            "version": BENCHMARK_VERSION,
            "manifest_sha256": artifact_inputs["manifest"],
            "result_schema_sha256": artifact_inputs["result_schema"],
        },
        "run": {
            "id": _sha256(_canonical({"seed": seed, "started_at": started_wall.isoformat(), "artifact": artifact_inputs}))[:24],
            "seed": seed,
            "started_at": started_wall.isoformat(),
            "elapsed_ms": elapsed_ms,
        },
        "harness": {
            "name": "our-harness-cli",
            "version": __version__,
            "artifact_sha256": _sha256(_canonical(artifact_inputs)),
            "source_sha256": source_hashes,
        },
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": sys.platform,
            "os": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "process_bits": 64 if sys.maxsize > 2**32 else 32,
        },
        "deterministic_score": score,
        "deterministic_score_uncapped": passed_weight,
        "maximum_score": 100,
        "critical_cap_applied": bool(critical_failures),
        "critical_failures": critical_failures,
        "case_summary": {
            "total": len(results),
            "passed": sum(item["status"] == "pass" for item in results),
            "failed": sum(item["status"] == "fail" for item in results),
        },
        "agentic_score": agentic_score,
        "agentic": agentic,
        "cases": results,
    }
    if agentic["status"] == "completed":
        result["hqs"] = round(0.40 * score + 0.60 * float(agentic_score), 2)
    return result


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Our Harness benchmark",
        "",
        f"- Benchmark: `{result['benchmark']['id']}` v{result['benchmark']['version']}",
        f"- Seed: `{result['run']['seed']}`",
        f"- Deterministic score: **{result['deterministic_score']}/{result['maximum_score']}**",
        f"- Agentic score: **{result['agentic_score']}**",
        f"- Elapsed: {result['run']['elapsed_ms']} ms",
        f"- Artifact SHA-256: `{result['harness']['artifact_sha256']}`",
        "",
        result["agentic"]["reason"],
        "",
        "| Case | Domain | Weight | Status | Elapsed |",
        "|---|---|---:|---|---:|",
    ]
    if "hqs" in result:
        lines.insert(6, f"- Harness Quality Score: **{result['hqs']}/100**")
    for case in result["cases"]:
        lines.append(f"| `{case['id']}` | {case['domain']} | {case['weight']} | {case['status']} | {case['elapsed_ms']} ms |")
    if result["critical_failures"]:
        lines.extend(["", "Critical failures: " + ", ".join(f"`{item}`" for item in result["critical_failures"])])
    if result["agentic"].get("status") == "completed":
        lines.extend(
            [
                "",
                "## Agentic Resolution Score",
                "",
                "| Task | Repetition | Status | Score | Elapsed | Calls | Tokens | Tools |",
                "|---|---:|---|---:|---:|---:|---:|---:|",
            ]
        )
        for task in result["agentic"]["tasks"]:
            metrics = task["metrics"]
            tokens = metrics["input_tokens"] + metrics["output_tokens"]
            lines.append(
                f"| `{task['id']}` | {task['repetition']} | {task['status']} | {task['score']} | "
                f"{metrics['elapsed_ms']} ms | {metrics['provider_calls']} | {tokens} | {metrics['tool_calls']} |"
            )
    return "\n".join(lines) + "\n"
