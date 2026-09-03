from __future__ import annotations

import copy
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness import cancellation, execution, long_horizon, swarm_work
from our_harness.config import load_isolated_config
from our_harness.doctor import run_doctor
from our_harness.models import HarnessError, ProviderRequest, ResponseFormat
from our_harness.providers import ProviderRegistry, codex_cli
from our_harness.usage import PriceCatalog


FAKE_CODEX = r'''
import ctypes, json, os, pathlib, subprocess, sys, time

args = sys.argv[1:]
def option(name, default=""):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return default

def config_value(name, default=""):
    for index, value in enumerate(args[:-1]):
        if value == "-c" and args[index + 1].startswith(name + "="):
            raw = args[index + 1].split("=", 1)[1]
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    return default

mode = option("--fake-mode", "ok")
record = option("--record")
if "--version" in args:
    print("codex-cli 9.9.9")
    raise SystemExit(0)
if "exec" in args and "--help" in args:
    print("Usage: codex exec [--ignore-rules]" + (
        "" if mode == "config-error-no-isolation" else " [--ignore-user-config]"
    ))
    raise SystemExit(0)
if "login" in args and "status" in args:
    if mode in {"config-error", "config-error-no-isolation", "config-error-auth-failure"}:
        print("Error loading configuration: unknown variant ultra", file=sys.stderr)
        raise SystemExit(1)
    if mode == "signed-out":
        print("Not logged in", file=sys.stderr)
        raise SystemExit(1)
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if "debug" in args and "models" in args and "--bundled" in args:
    print(json.dumps({"models": [{
        "slug": "fixture-codex", "display_name": "Fixture Codex",
        "supported_reasoning_levels": [
            {"effort": one} for one in ("minimal", "low", "medium", "high", "xhigh", "ultra")
        ],
    }]}))
    raise SystemExit(0)
if "exec" not in args:
    raise SystemExit(8)
prompt = sys.stdin.read()
schema_path = pathlib.Path(option("--output-schema"))
result_path = pathlib.Path(option("--output-last-message"))
if record:
    catalog_path = pathlib.Path(config_value("model_catalog_json"))
    pathlib.Path(record).write_text(json.dumps({
        "argv": args,
        "cwd": os.getcwd(),
        "prompt": prompt,
        "schema": json.loads(schema_path.read_text()),
        "schema_mode": schema_path.stat().st_mode & 0o777,
        "result_mode": result_path.stat().st_mode & 0o777,
        "catalog": json.loads(catalog_path.read_text()),
        "catalog_mode": catalog_path.stat().st_mode & 0o777,
        "openai_api_key_present": bool(os.environ.get("OPENAI_API_KEY")),
    }), encoding="utf-8")
if mode == "timeout":
    time.sleep(5)
if mode in {"timeout-tree", "fast-root-tree"}:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=os.getcwd(),
    )
    child_in_job = ctypes.c_int()
    checked_job = ctypes.windll.kernel32.IsProcessInJob(
        int(child._handle), None, ctypes.byref(child_in_job)
    )
    pathlib.Path(str(record) + ".child.pid").write_text(json.dumps({
        "pid": child.pid,
        "in_job": bool(child_in_job.value) if checked_job else None,
    }), encoding="utf-8")
if mode == "timeout-tree":
    time.sleep(30)
if mode == "brokered-grandchild-tree":
    intermediate_code = (
        "import pathlib,subprocess,sys,time; "
        "pathlib.Path(sys.argv[2]).write_text(str(__import__('os').getpid()),encoding='utf-8'); "
        "trigger=pathlib.Path(sys.argv[3]); "
        "list(iter(lambda:(time.sleep(0.005),trigger.exists())[1],True)); "
        "child_code=\"import json,os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()}));time.sleep(30)\"; "
        "subprocess.Popen([sys.executable,'-c',child_code,sys.argv[4]],cwd=sys.argv[1])"
    )
    subprocess.Popen(
        [
            sys.executable, "-c", intermediate_code, os.getcwd(),
            str(record) + ".broker.pid", str(record) + ".spawn-now",
            str(record) + ".child.pid",
        ],
        cwd=os.getcwd(),
    )
    time.sleep(30)
if mode == "nonzero":
    print("fixture failure", file=sys.stderr)
    raise SystemExit(17)
if mode == "config-error-auth-failure":
    print("ChatGPT authentication required", file=sys.stderr)
    raise SystemExit(17)
if mode == "stdout-oversize":
    print("x" * 5000)
    result_path.write_text(json.dumps({"answer": "ok"}), encoding="utf-8")
    raise SystemExit(0)
if mode == "result-oversize":
    result_path.write_text(json.dumps({"answer": "x" * 5000}), encoding="utf-8")
elif mode == "bad-result":
    result_path.write_text(json.dumps({"wrong": "field"}), encoding="utf-8")
else:
    result_path.write_text(json.dumps({"answer": "ok"}), encoding="utf-8")
if mode == "malformed-usage":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": -1}}))
elif mode == "no-usage":
    print(json.dumps({"type": "turn.completed"}))
elif mode == "duplicate-completed":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
    print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 5}}))
else:
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 17, "cached_input_tokens": 3, "output_tokens": 5,
        "reasoning_output_tokens": 2,
    }}))
'''


class CodexCLIProviderTests(unittest.TestCase):
    def test_stable_codex_hint_resolves_the_current_desktop_build_at_dispatch(self) -> None:
        provider = object.__new__(codex_cli.CodexCLIProvider)
        provider.settings = {"command": ["codex"]}
        with patch(
            "our_harness.providers.subscription_cli.available",
            return_value="C:/OpenAI/Codex/bin/build-b/codex.exe",
        ):
            self.assertEqual(
                provider._command(),
                ["C:/OpenAI/Codex/bin/build-b/codex.exe"],
            )

    def test_private_workspace_cleanup_never_masks_authoritative_provider_error(self) -> None:
        workspace: Path | None = None
        cleanup_error = PermissionError(32, "fixture cwd is still locked")
        try:
            with patch.object(
                codex_cli, "_remove_private_workspace",
                return_value=(False, cleanup_error),
            ), patch.object(codex_cli, "_finish_private_workspace_later"):
                with self.assertRaisesRegex(HarnessError, "authoritative timeout") as caught:
                    with codex_cli._private_workspace("nexus-cleanup-mask-test-") as workspace:
                        raise HarnessError("authoritative timeout")
            self.assertTrue(
                any("cleanup is still pending" in note for note in caught.exception.__notes__)
            )
        finally:
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)

    def test_codex_native_schema_requires_optional_properties_without_mutating_contract(self) -> None:
        contract = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "detail": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": [],
                },
            },
            "required": ["answer"],
        }

        native = codex_cli._codex_output_schema(contract)

        self.assertEqual(native["required"], ["answer", "detail"])
        self.assertFalse(native["additionalProperties"])
        self.assertEqual(native["properties"]["detail"]["required"], ["note"])
        self.assertFalse(native["properties"]["detail"]["additionalProperties"])
        self.assertEqual(contract["required"], ["answer"])
        self.assertNotIn("additionalProperties", contract)
        self.assertEqual(contract["properties"]["detail"]["required"], [])

    def test_codex_native_context_tool_schemas_are_strict_without_mutation(self) -> None:
        contracts = (
            swarm_work.WORK_FORMAT.schema,
            long_horizon.AGENT_ACTION_FORMAT.schema,
        )

        def assert_every_object_is_strict(value: object, path: str = "result") -> None:
            if isinstance(value, list):
                for index, item in enumerate(value):
                    assert_every_object_is_strict(item, f"{path}[{index}]")
                return
            if not isinstance(value, dict):
                return
            if value.get("type") == "object":
                properties = value.get("properties")
                self.assertIsInstance(properties, dict, path)
                self.assertIs(value.get("additionalProperties"), False, path)
                self.assertEqual(
                    value.get("required"), list(properties), path,
                )
            for name, child in value.items():
                assert_every_object_is_strict(child, f"{path}.{name}")

        for contract in contracts:
            with self.subTest(schema=contract):
                before = copy.deepcopy(contract)
                native = codex_cli._codex_output_schema(contract)
                assert_every_object_is_strict(native)
                self.assertEqual(contract, before)

        review_arguments = long_horizon.AGENT_ACTION_FORMAT.schema[
            "properties"
        ]["tool_calls"]["items"]["anyOf"][-1]["properties"]["arguments"]
        native_review_arguments = codex_cli._codex_output_schema(
            long_horizon.AGENT_ACTION_FORMAT.schema
        )["properties"]["tool_calls"]["items"]["anyOf"][-1]["properties"]["arguments"]
        self.assertEqual(review_arguments["required"], ["path"])
        self.assertEqual(
            native_review_arguments["required"], ["path", "offset", "limit"],
        )

    def test_stop_terminates_the_active_cli_process_tree(self) -> None:
        token = cancellation.Cancellation()
        errors: list[Exception] = []
        with tempfile.TemporaryDirectory() as temporary:
            def run() -> None:
                try:
                    with cancellation.use(token):
                        codex_cli._run_bounded(
                            [sys.executable, "-c", "import time; time.sleep(30)"],
                            cwd=Path(temporary), stdin_text=None,
                            timeout_seconds=30, max_output_bytes=1000,
                        )
                except Exception as exc:
                    errors.append(exc)

            started = time.monotonic()
            thread = threading.Thread(target=run)
            thread.start()
            time.sleep(0.2)
            token.cancel()
            thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsInstance(errors[0], cancellation.ChatCancelled)

    def make_provider(
        self, root: Path, mode: str = "ok", *, output_limit: int = 250_000,
        reasoning_effort: str | None = None,
    ):
        script = root / "fake_codex.py"
        script.write_text(FAKE_CODEX, encoding="utf-8")
        record = root / "record.json"
        config = load_isolated_config(
            root,
            {
                "providers": {
                    "subscription": {
                        "kind": "codex-cli",
                        "model": "fixture-codex",
                    "command": [sys.executable, str(script), "--fake-mode", mode, "--record", str(record)],
                    "auth_mode": "chatgpt",
                    **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
                    }
                },
                "execution": {"max_output_bytes": output_limit},
            },
        )
        return config, ProviderRegistry(config).create("subscription"), record

    @staticmethod
    def request(timeout: float | None = None) -> ProviderRequest:
        return ProviderRequest(
            "fixed policy",
            "bounded evidence",
            [{"role": "user", "content": "Return the answer."}],
            "fixture-codex",
            timeout_seconds=timeout,
            response_format=ResponseFormat(
                "answer",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string", "maxLength": 20}},
                },
            ),
        )

    def test_fixed_argv_stdin_private_schema_result_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fixture-secret-value"}):
                _config, provider, record = self.make_provider(root)
                response = provider.complete(self.request())
            captured = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(response.text), {"answer": "ok"})
        self.assertEqual(response.input_tokens, 17)
        self.assertEqual(response.cached_input_tokens, 3)
        self.assertEqual(response.output_tokens, 5)
        self.assertEqual(response.reasoning_tokens, 2)
        argv = captured["argv"]
        expected = [
            "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "--skip-git-repo-check", "--json",
        ]
        cursor = 0
        for item in expected:
            cursor = argv.index(item, cursor) + 1
        self.assertEqual(argv[-3:], ["--model", "fixture-codex", "-"])
        self.assertIn("fixed policy", captured["prompt"])
        self.assertIn("bounded evidence", captured["prompt"])
        self.assertNotIn("sk-fixture-secret-value", captured["prompt"])
        self.assertFalse(captured["openai_api_key_present"])
        self.assertEqual(captured["schema"]["required"], ["answer"])
        self.assertEqual(captured["catalog"]["models"][0]["slug"], "fixture-codex")
        self.assertLess(argv.index("-c"), argv.index("exec"))
        if os.name != "nt":
            self.assertEqual(captured["schema_mode"], 0o600)
            self.assertEqual(captured["result_mode"], 0o600)
            self.assertEqual(captured["catalog_mode"], 0o600)
        self.assertFalse(Path(captured["cwd"]).exists())

    def test_reasoning_effort_is_passed_as_a_fixed_codex_config_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, record = self.make_provider(Path(temporary))
            request = ProviderRequest(**{**self.request().__dict__, "reasoning_effort": "high"})
            provider.complete(request)
            argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertLess(argv.index('model_reasoning_effort="high"'), argv.index("exec"))

    def test_reasoning_effort_is_checked_against_the_selected_model_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, record = self.make_provider(Path(temporary))
            request = ProviderRequest(**{**self.request().__dict__, "reasoning_effort": "max"})
            with self.assertRaisesRegex(
                HarnessError, "does not support reasoning effort max.*supported efforts"
            ):
                provider.complete(request)
            self.assertFalse(record.exists())

    def test_new_reasoning_effort_names_are_accepted_then_model_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, provider, record = self.make_provider(root)
            request = ProviderRequest(**{**self.request().__dict__, "reasoning_effort": "ultra"})
            provider.complete(request)
            argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
            self.assertIn('model_reasoning_effort="ultra"', argv)
            self.assertEqual(
                config.data["providers"]["subscription"].get("reasoning_effort"), None
            )

    def test_profile_and_agent_accept_current_effort_names_and_schema_matches(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        schema = json.loads((repository / "harness.schema.json").read_text(encoding="utf-8"))
        profile_enum = set(
            schema["$defs"]["providerProfile"]["properties"]["reasoning_effort"]["enum"]
        )
        agent_enum = set(
            schema["$defs"]["agentSpec"]["properties"]["reasoning_effort"]["enum"]
        )
        self.assertEqual(profile_enum, set(codex_cli.CODEX_REASONING_EFFORTS))
        self.assertEqual(agent_enum, set(codex_cli.CODEX_REASONING_EFFORTS))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for effort in ("minimal", "ultra"):
                config = load_isolated_config(root, {
                    "providers": {"codex": {
                        "kind": "codex-cli", "model": "fixture-codex",
                        "command": ["codex"], "auth_mode": "chatgpt",
                        "reasoning_effort": effort,
                    }},
                    "agents": {"worker": {"provider_ref": "codex", "reasoning_effort": effort}},
                })
                self.assertEqual(config.get("providers.codex.reasoning_effort"), effort)
                self.assertEqual(config.get("agents.worker.reasoning_effort"), effort)
            with self.assertRaisesRegex(HarnessError, "reasoning_effort is invalid"):
                load_isolated_config(root, {
                    "providers": {"codex": {
                        "kind": "codex-cli", "model": "fixture-codex",
                        "command": ["codex"], "auth_mode": "chatgpt",
                        "reasoning_effort": "impossible",
                    }},
                })

    def test_config_broken_login_probe_defers_to_isolated_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config, provider, _record = self.make_provider(Path(temporary), "config-error")
            response = provider.complete(self.request())
            doctor = run_doctor(config)
            check = next(one for one in doctor["checks"] if one["name"] == "provider:subscription")
        self.assertEqual(json.loads(response.text), {"answer": "ok"})
        self.assertEqual(check["level"], "warn")
        self.assertIn("first isolated request", check["message"])

    def test_config_broken_login_probe_fails_closed_without_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, record = self.make_provider(
                Path(temporary), "config-error-no-isolation"
            )
            with self.assertRaisesRegex(HarnessError, "does not support exec --ignore-user-config"):
                provider.complete(self.request())
            self.assertFalse(record.exists())

    def test_an_ordinary_signed_out_status_is_not_bypassed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, record = self.make_provider(Path(temporary), "signed-out")
            with self.assertRaisesRegex(HarnessError, "login status failed.*Not logged in"):
                provider.complete(self.request())
            self.assertFalse(record.exists())

    def test_deferred_authentication_failure_from_exec_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, _record = self.make_provider(
                Path(temporary), "config-error-auth-failure"
            )
            with self.assertRaisesRegex(HarnessError, "exited 17.*authentication required"):
                provider.complete(self.request())

    def test_bundled_catalog_rejects_an_unavailable_model_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, record = self.make_provider(Path(temporary))
            request = ProviderRequest(**{**self.request().__dict__, "model": "missing-model"})
            with self.assertRaisesRegex(HarnessError, "does not contain configured model"):
                provider.complete(request)
            self.assertFalse(record.exists())

    def test_timeout_nonzero_oversize_and_invalid_result_fail_closed(self) -> None:
        cases = (
            ("timeout", 250_000, "timed out", 1.0),
            ("nonzero", 250_000, "exited 17", None),
            ("stdout-oversize", 1_024, "exceeded", None),
            ("result-oversize", 1_024, "result exceeded", None),
            ("bad-result", 250_000, "missing answer", None),
            ("malformed-usage", 250_000, "usage output_tokens must be a non-negative integer", None),
            ("duplicate-completed", 250_000, "duplicate turn.completed", None),
        )
        for mode, limit, message, timeout in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                _config, provider, record = self.make_provider(Path(temporary), mode, output_limit=limit)
                with self.assertRaisesRegex(HarnessError, message):
                    provider.complete(self.request(timeout))
                if record.exists():
                    cwd = json.loads(record.read_text(encoding="utf-8"))["cwd"]
                    self.assertFalse(Path(cwd).exists())

    @unittest.skipUnless(os.name == "nt", "Windows process containment is Windows-specific")
    def test_timeout_reaps_contained_and_brokered_trees_before_workspace_cleanup(self) -> None:
        class BrokerEscapedJob:
            # Models Store/venv redirectors that broker the real executable
            # outside a job even though assignment of the launcher succeeded.
            last_handle = None

            def __init__(self, _process):
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CreateJobObjectW.argtypes = [
                    ctypes.c_void_p, ctypes.c_wchar_p
                ]
                kernel32.CreateJobObjectW.restype = ctypes.c_void_p
                self._kernel32 = kernel32
                self.handle = kernel32.CreateJobObjectW(None, None)
                if not self.handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                BrokerEscapedJob.last_handle = self.handle

            def terminate(self) -> bool:
                return True

            def wait_until_empty(self, _timeout_seconds: float) -> bool:
                return True

            def close(self) -> None:
                if self.handle:
                    self._kernel32.CloseHandle(ctypes.c_void_p(self.handle))
                    self.handle = None

        for mode in (
            "timeout-tree",
            "fast-root-tree",
            "brokered-grandchild-tree",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _config, provider, record = self.make_provider(
                    root, mode, output_limit=250_000
                )
                child_record = Path(str(record) + ".child.pid")
                broker_record = Path(str(record) + ".broker.pid")
                spawn_trigger = Path(str(record) + ".spawn-now")
                inner_cwd: Path | None = None
                drain_counts = [0, 0]
                drain_lock = threading.Lock()
                real_drain = codex_cli._BoundedCapture.drain
                real_snapshot = execution._windows_process_snapshot
                snapshot_state = {
                    "broker_seen": 0,
                    "spawned": False,
                    "child_in_private_job": None,
                }

                def tracked_drain(capture, pipe, destination):
                    with drain_lock:
                        drain_counts[0] += 1
                    try:
                        return real_drain(capture, pipe, destination)
                    finally:
                        with drain_lock:
                            drain_counts[1] += 1

                def snapshot_then_spawn_final_child():
                    snapshot = real_snapshot()
                    if not broker_record.exists():
                        return snapshot
                    try:
                        broker_pid = int(broker_record.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        return snapshot
                    processes, _cutoff = snapshot
                    if broker_pid not in processes:
                        return snapshot
                    snapshot_state["broker_seen"] += 1
                    if snapshot_state["broker_seen"] != 2:
                        return snapshot

                    # The first scan has already retained this broker's process
                    # handle.  Trigger its final child only after this second
                    # snapshot was captured, then wait for the broker to exit.
                    # Returning the now-stale snapshot forces cleanup to rescan
                    # from the retained exited intermediate.
                    broker_handle = execution._windows_open_process_handle(broker_pid)
                    try:
                        spawn_trigger.write_text("spawn", encoding="utf-8")
                        child_deadline = time.monotonic() + 3.0
                        while (
                            not child_record.exists()
                            and time.monotonic() < child_deadline
                        ):
                            time.sleep(0.005)
                        self.assertTrue(
                            child_record.exists(), "fixture final child did not start"
                        )
                        child_pid = int(json.loads(
                            child_record.read_text(encoding="utf-8")
                        )["pid"])
                        child_handle = execution._windows_open_process_handle(
                            child_pid
                        )
                        self.assertIsNotNone(
                            child_handle, "fixture final child was not live"
                        )
                        try:
                            in_job = ctypes.c_int()
                            checker = ctypes.WinDLL(
                                "kernel32", use_last_error=True
                            ).IsProcessInJob
                            checker.argtypes = [
                                ctypes.c_void_p,
                                ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_int),
                            ]
                            checker.restype = ctypes.c_int
                            checked = checker(
                                ctypes.c_void_p(child_handle),
                                ctypes.c_void_p(BrokerEscapedJob.last_handle),
                                ctypes.byref(in_job),
                            )
                            self.assertTrue(checked)
                            snapshot_state["child_in_private_job"] = bool(
                                in_job.value
                            )
                        finally:
                            execution._windows_close_process_handle(child_handle)
                        if broker_handle is not None:
                            while (
                                execution._windows_process_handle_is_running(
                                    broker_handle
                                )
                                and time.monotonic() < child_deadline
                            ):
                                time.sleep(0.005)
                            self.assertFalse(
                                execution._windows_process_handle_is_running(
                                    broker_handle
                                ),
                                "fixture intermediate did not exit",
                            )
                        snapshot_state["spawned"] = True
                    finally:
                        if broker_handle is not None:
                            execution._windows_close_process_handle(broker_handle)
                    return snapshot

                try:
                    job_boundary = (
                        patch.object(execution, "_WindowsJob", BrokerEscapedJob)
                        if mode == "brokered-grandchild-tree" else nullcontext()
                    )
                    snapshot_boundary = (
                        patch.object(
                            execution,
                            "_windows_process_snapshot",
                            side_effect=snapshot_then_spawn_final_child,
                        )
                        if mode == "brokered-grandchild-tree" else nullcontext()
                    )
                    with job_boundary, snapshot_boundary, patch.object(
                        codex_cli._BoundedCapture, "drain", tracked_drain
                    ):
                        with self.assertRaisesRegex(HarnessError, "timed out"):
                            provider.complete(self.request(3.0))
                    captured = json.loads(record.read_text(encoding="utf-8"))
                    inner_cwd = Path(captured["cwd"])
                    self.assertTrue(child_record.exists(), "fixture child was not started")
                    child_identity = json.loads(
                        child_record.read_text(encoding="utf-8")
                    )
                    if mode == "brokered-grandchild-tree":
                        self.assertTrue(
                            snapshot_state["spawned"],
                            "fixture did not force the post-snapshot spawn race",
                        )
                        self.assertFalse(
                            snapshot_state["child_in_private_job"],
                            "fixture child unexpectedly remained in the private job",
                        )
                    self.assertFalse(
                        inner_cwd.exists(),
                        "a contained descendant kept the private cwd locked",
                    )
                    self.assertEqual(
                        drain_counts[0], drain_counts[1],
                        "Codex stdout/stderr pumps outlived the private workspace",
                    )
                finally:
                    # This branch runs only if the regression returns.  It keeps a
                    # failed test from leaving its deliberate 30-second fixture
                    # child behind on a developer machine or CI runner.
                    if inner_cwd is None and record.exists():
                        try:
                            inner_cwd = Path(
                                json.loads(record.read_text(encoding="utf-8"))["cwd"]
                            )
                        except (OSError, ValueError, TypeError, KeyError):
                            pass
                    if (
                        inner_cwd is not None and inner_cwd.exists()
                        and child_record.exists()
                    ):
                        subprocess.run(
                            [
                                "taskkill", "/PID",
                                str(json.loads(
                                    child_record.read_text(encoding="utf-8")
                                )["pid"]),
                                "/T", "/F",
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=2,
                            check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        shutil.rmtree(inner_cwd, ignore_errors=True)

    def test_missing_usage_remains_unreported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, _record = self.make_provider(Path(temporary), "no-usage")
            response = provider.complete(self.request())
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.cached_input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.reasoning_tokens)

    def test_start_permission_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, _record = self.make_provider(Path(temporary))
            with patch("our_harness.providers.codex_cli.subprocess.Popen", side_effect=PermissionError("access denied")):
                with self.assertRaisesRegex(HarnessError, "could not start.*access denied"):
                    provider.complete(self.request())

    def test_native_tools_and_continuations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _config, provider, _record = self.make_provider(Path(temporary))
            request = self.request()
            request = ProviderRequest(**{**request.__dict__, "tools": [{"name": "read"}]})
            with self.assertRaisesRegex(HarnessError, "does not support native tools"):
                provider.complete(request)

    def test_usage_is_subscription_unpriced_without_price_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config, provider, _record = self.make_provider(Path(temporary))
            response = provider.complete(self.request())
            catalog = PriceCatalog(config)
            self.assertIsNone(catalog.preflight("codex-cli", "fixture-codex"))
            usage = catalog.record(
                response,
                run_id="run", node_id="planner", agent_id="planner", role="planner",
                provider_profile_id="subscription", provider="codex-cli", model="fixture-codex", latency_ms=1,
            )
            self.assertIsNone(usage.cost_microusd)
            self.assertEqual(usage.price_status, "subscription-unpriced")
            self.assertIsNone(usage.price_snapshot_id)
            self.assertEqual(usage.input_tokens, 17)
            self.assertEqual(usage.cached_input_tokens, 3)
            self.assertEqual(usage.output_tokens, 5)
            self.assertEqual(usage.reasoning_tokens, 2)

    def test_profile_requires_explicit_chatgpt_auth_and_is_not_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {"providers": {"x": {"kind": "codex-cli", "model": "m", "command": ["codex"]}}}
            with self.assertRaisesRegex(HarnessError, "auth_mode"):
                load_isolated_config(root, base)
            with self.assertRaisesRegex(HarnessError, "provider.name"):
                load_isolated_config(root, {"provider": {"name": "codex-cli"}})

    def test_profile_matches_public_schema_contract(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        schema = json.loads((repository / "harness.schema.json").read_text(encoding="utf-8"))
        properties = schema["$defs"]["providerProfile"]["properties"]
        names = schema["$defs"]["providerProfileName"]["enum"]
        self.assertIn("codex-cli", names)
        self.assertIn("auth_mode", properties)
        self.assertNotIn("codex-cli", schema["properties"]["provider"]["properties"]["name"]["enum"])

    def test_doctor_executes_version_and_login_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, _provider, _record = self.make_provider(root)
            result = run_doctor(config)
            check = next(item for item in result["checks"] if item["name"] == "provider:subscription")
            self.assertEqual(check["level"], "ok")
            self.assertIn("signed in with ChatGPT", check["message"])

    def test_doctor_rejects_effort_not_supported_by_the_selected_catalog_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config, _provider, _record = self.make_provider(
                Path(temporary), reasoning_effort="max",
            )
            result = run_doctor(config)
            check = next(item for item in result["checks"] if item["name"] == "provider:subscription")
        self.assertEqual(check["level"], "fail")
        self.assertIn("does not support reasoning effort max", check["message"])


class WhatATruncatedAnswerReadsAsTests(unittest.TestCase):
    """The harness holds what a tool prints to a size, and that cut can land
    between the two halves of one letter. Read straight through, the last letter
    of a perfectly good answer becomes a black diamond - the app looking broken
    where the tool was fine."""

    WHOLE = "ready · done".encode("utf-8")

    def test_a_letter_cut_in_half_by_the_limit_is_dropped_not_shown_as_damage(self) -> None:
        # That letter is two bytes wide, and the cut lands between them.
        self.assertEqual(codex_cli._as_words(self.WHOLE[:7]), "ready ")

    def test_a_whole_answer_comes_back_exactly_as_it_was(self) -> None:
        self.assertEqual(codex_cli._as_words(self.WHOLE), "ready · done")

    def test_a_four_byte_letter_cut_in_half_is_dropped_too(self) -> None:
        """The one the countdown was one step short of.

        Dropping "up to three bytes" dropped up to two, because the end of a
        countdown is not one of its steps. A four-byte letter cut with three of
        its bytes left over went all the way through to the black diamond this
        is here to prevent.
        """

        whole = "All fine up to here \U0001F600".encode("utf-8")
        for short_by in (1, 2, 3):
            with self.subTest(short_by=short_by):
                self.assertEqual(
                    codex_cli._as_words(whole[:-short_by]), "All fine up to here ")

    def test_something_really_broken_is_still_shown_as_broken(self) -> None:
        """Damage further in than the last few bytes is damage, and is shown as
        damage rather than guessed at."""

        self.assertIn("�", codex_cli._as_words(bytes([0xFF, 0xFE]) + b"x" * 40))


if __name__ == "__main__":
    unittest.main()
