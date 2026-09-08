"""Native transport regressions adapted from t3code image/terminal input cases."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.images import Image, write_png
from our_harness.models import CommandResult, HarnessError, ProviderRequest, ProviderWorkspaceContext
from our_harness.providers import claude_input, codex_cli, subscription_cli
from our_harness.providers.image_inputs import read_image_inputs
from our_harness.providers.input_context import CLI_WORKSPACE_RULES, workspace_instructions
from tests.test_subscription_cli import fake_tool


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-input-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.selected = self.root / "PLOQQIZ & quoted project"
        self.selected.mkdir()
        self.execution = self.root / "private copy"
        self.execution.mkdir()
        self.context = ProviderWorkspaceContext("project-exact", str(self.selected), str(self.execution))
        self.raw = write_png(Image(3, 1, b"\x20\x30\x40\xff" * 3))
        self.image = {"name": "exact screen.png", "type": "image/png",
                      "data": base64.b64encode(self.raw).decode("ascii"),
                      "sha256": hashlib.sha256(self.raw).hexdigest()}

    def request(self, **kwargs):
        return ProviderRequest("SYSTEM" + workspace_instructions(self.context),
                               "Prior screenshot transcription: PLOQGZ",
                               [{"role": "user", "content": "Use PLOQQIZ/tic-tac-toe"}],
                               "fake-model", workspace_context=self.context, **kwargs)

    def provider(self, kind="claude-cli", **settings):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({"name": kind, "model": "fake-model", "command": ["fake-tool"], **settings})
        provider = subscription_cli.SubscriptionCLIProvider(LoadedConfig(data, self.root, [], {}), kind)
        provider._checked = True
        return provider

    @staticmethod
    def result(argv, cwd, stdout):
        return CommandResult(argv, str(cwd), 0, stdout, "", 1)

    def test_workspace_identity_preserves_exact_paths_and_distinguishes_execution_copy(self):
        text = workspace_instructions(self.context)
        payload = json.loads(text.splitlines()[3])
        self.assertEqual(payload["selected_project_path"], str(self.selected))
        self.assertEqual(payload["execution_copy_path"], str(self.execution))
        self.assertIn("screenshot transcription", text)
        self.assertEqual(workspace_instructions(None), "")
        with self.assertRaises(HarnessError):
            workspace_instructions({"project_path": str(self.selected)})

    def test_claude_receives_original_native_image_bytes_without_read_permissions(self):
        calls = []
        def run(argv, **kwargs):
            self.assertTrue(kwargs["cwd"].is_dir())
            self.assertNotEqual(kwargs["cwd"], self.selected)
            self.assertNotEqual(kwargs["cwd"], Path.cwd())
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertNotIn("--add-dir", argv)
            self.assertNotIn("Read", argv)
            self.assertIn(CLI_WORKSPACE_RULES, argv)
            packet = json.loads(kwargs["stdin_text"])
            self.assertEqual(packet["message"]["role"], "user")
            blocks = packet["message"]["content"]
            self.assertEqual(base64.b64decode(blocks[0]["source"]["data"]), self.raw)
            self.assertEqual(blocks[0]["source"]["media_type"], "image/png")
            self.assertEqual(blocks[-1]["type"], "text")
            self.assertIn("PLOQQIZ/tic-tac-toe", blocks[-1]["text"])
            self.assertIn("native image content blocks", blocks[-1]["text"])
            self.assertNotIn("Inspect these exact files", blocks[-1]["text"])
            calls.append(str(kwargs["cwd"]))
            return self.result(argv, kwargs["cwd"], '\n'.join(map(json.dumps, [
                {"type": "assistant", "message": {"content": "progress only"}},
                {"type": "result", "subtype": "success", "is_error": False,
                 "result": "saw original", "usage": {"input_tokens": 7, "output_tokens": 3}},
            ])))
        with patch.object(subscription_cli.SubscriptionCLIProvider, "_command", return_value=["fake"]), \
             patch.object(subscription_cli, "_run_bounded", side_effect=run):
            for _ in range(2):
                answer = self.provider().complete(self.request(attachments=[self.image]))
                self.assertEqual(answer.text, "saw original")
                self.assertEqual(answer.input_tokens, 7)
        self.assertEqual(len(set(calls)), 2)
        self.assertTrue(all(not Path(path).exists() for path in calls))

    def test_real_cli_child_receives_json_and_isolated_cwd(self):
        tool = fake_tool(self.root, "vision-fixture", '''
import base64, hashlib, json, pathlib, sys
if '--version' in sys.argv:
    print('1.0 fake'); raise SystemExit
packet=json.loads(sys.stdin.read())
blocks=packet['message']['content']
out={'digest':hashlib.sha256(base64.b64decode(blocks[0]['source']['data'])).hexdigest(),
     'cwd':str(pathlib.Path.cwd()), 'tools':sys.argv[sys.argv.index('--tools')+1]}
print(json.dumps({'type':'system','subtype':'init'}))
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps(out)}))
''')
        with patch.object(subscription_cli.SubscriptionCLIProvider, "_command", return_value=[str(tool)]):
            response = self.provider().complete(self.request(attachments=[self.image]))
        actual = json.loads(response.text)
        self.assertEqual(actual["digest"], self.image["sha256"])
        self.assertEqual(actual["tools"], "")
        self.assertNotEqual(Path(actual["cwd"]), Path.cwd())
        self.assertFalse(Path(actual["cwd"]).exists())

    def test_unsupported_copilot_images_are_not_sent_as_plaintext_filenames(self):
        with patch.object(subscription_cli.SubscriptionCLIProvider, "_command", return_value=["fake"]), \
             patch.object(subscription_cli, "_run_bounded") as run:
            with self.assertRaisesRegex(HarnessError, "screenshot-input contract"):
                self.provider("copilot-cli").complete(self.request(attachments=[self.image]))
            run.assert_not_called()

    def test_custom_claude_image_protocol_is_not_guessed(self):
        with patch.object(subscription_cli.SubscriptionCLIProvider, "_command", return_value=["fake"]), \
             patch.object(subscription_cli, "_run_bounded") as run:
            with self.assertRaisesRegex(HarnessError, "standard CLI arguments"):
                self.provider(arguments=["-p"]).complete(self.request(attachments=[self.image]))
            run.assert_not_called()

    def test_explicit_probe_working_directory_is_preserved(self):
        provider = self.provider()
        request = replace(self.request(), working_directory=str(self.execution))
        with patch.object(provider, "_complete_in_workspace", return_value="sent") as send:
            self.assertEqual(provider.complete(request), "sent")
            self.assertIs(send.call_args.args[0], request)

    def test_stream_requires_one_terminal_result_and_rejects_errors(self):
        for stream in ['{"type":"assistant"}', '{bad}', '[]',
                       '{"type":"result"}\n{"type":"result"}',
                       '{"type":"result"}\n{"type":"error"}']:
            with self.subTest(stream=stream), self.assertRaises(HarnessError):
                claude_input.terminal_result(stream)
        self.assertEqual(json.loads(claude_input.terminal_result('{"type":"result","result":"ok"}'))["result"], "ok")

    def test_claude_success_subtype_does_not_hide_service_failure(self):
        for payload in [{"subtype": "success", "is_error": False, "api_error_status": 529},
                        {"subtype": "error_max_turns", "is_error": False}]:
            self.assertTrue(subscription_cli._that_went_wrong(subscription_cli.CLAUDE_RECIPE, payload))
        self.assertFalse(subscription_cli._that_went_wrong(subscription_cli.CLAUDE_RECIPE,
                                                         {"subtype": "success", "is_error": False}))

    def test_original_path_bytes_and_digest_are_checked(self):
        path = self.root / "original.png"
        path.write_bytes(self.raw)
        attachment = {key: value for key, value in self.image.items() if key != "data"}
        attachment["path"] = str(path)
        self.assertEqual(read_image_inputs([attachment]), [("image/png", self.raw)])
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(HarnessError, "changed"):
            read_image_inputs([attachment])

    def test_invalid_missing_unsupported_and_oversized_image_input_is_rejected(self):
        for attachment in [{**self.image, "data": "@@"},
                           {"type": "image/png", "path": str(self.root/'missing')},
                           {**self.image, "type": "image/svg+xml"},
                           {**self.image, "data": "A" * 5_333_340}]:
            with self.subTest(kind=attachment["type"]), self.assertRaises(HarnessError):
                read_image_inputs([attachment])

    def _run_codex_image(self, completed=True):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({"name": "codex-cli", "model": "fake-model", "command": ["fake"]})
        provider = codex_cli.CodexCLIProvider(LoadedConfig(data, self.root, [], {}))
        provider._preflight_complete = True
        observed = []
        def run(argv, **kwargs):
            image = Path(argv[argv.index("--image") + 1])
            self.assertEqual(image.parent, kwargs["cwd"])
            self.assertEqual(image.read_bytes(), self.raw)
            observed.append(image)
            Path(argv[argv.index("--output-last-message") + 1]).write_text('{"text":"saw image"}', encoding="utf-8")
            return self.result(argv, kwargs["cwd"], json.dumps({"type": "turn.completed" if completed else "item.completed"}))
        with patch.object(provider, "_command", return_value=["fake"]), \
             patch.object(codex_cli, "_bundled_model_catalog", return_value="{}"), \
             patch.object(codex_cli, "_validate_model_reasoning_effort"), \
             patch.object(codex_cli, "_run_bounded", side_effect=run):
            response = provider.complete(self.request(attachments=[self.image]))
        self.assertTrue(all(not path.exists() for path in observed))
        return response

    def test_codex_data_only_image_is_materialized_at_original_fidelity(self):
        self.assertEqual(self._run_codex_image().text, "saw image")

    def test_codex_result_file_without_terminal_event_is_not_completion(self):
        with self.assertRaisesRegex(HarnessError, "terminal turn.completed"):
            self._run_codex_image(completed=False)


if __name__ == "__main__":
    unittest.main()
