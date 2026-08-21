from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.config import load_isolated_config
from our_harness.doctor import run_doctor
from our_harness.models import HarnessError, ProviderRequest, ResponseFormat
from our_harness.providers import ProviderRegistry, codex_cli
from our_harness.usage import PriceCatalog


FAKE_CODEX = r'''
import json, os, pathlib, sys, time

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
if "login" in args and "status" in args:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if "debug" in args and "models" in args and "--bundled" in args:
    print(json.dumps({"models": [{"slug": "fixture-codex", "display_name": "Fixture Codex"}]}))
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
if mode == "nonzero":
    print("fixture failure", file=sys.stderr)
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
    def make_provider(self, root: Path, mode: str = "ok", *, output_limit: int = 250_000):
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
