from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from our_harness import chat, images, swarm_runs
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import CommandResult, ProviderWorkspaceContext
from our_harness.providers.base import OpenAIProvider
from our_harness.providers import codex_cli
from test_document_text import make_docx


class AttachmentInputFidelityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nexus-image-input-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.config.data["provider"].update(name="openai-compatible", model="fixture-model", api_key_env="",
                                            endpoint="http://127.0.0.1:1", api_mode="chat-completions")
        self.original = images.write_png(images.Image(1600, 900, b"\x10\x20\x30\xff" * (1600 * 900)))

    def ingest(self, mime="application/octet-stream", name="Exact path screenshot.PNG"):
        return chat.keep_attachments(self.config, "", [{"name": name, "type": mime,
            "data": base64.b64encode(self.original).decode()}], "isolated-chat")

    def ask(self, provider, files, context):
        selected = self.root / "PLOQQIZ – exact (destination)"
        working = self.root / "independent copy"
        selected.mkdir(exist_ok=True)
        working.mkdir(exist_ok=True)
        with mock.patch.object(chat, "create_provider", return_value=provider), \
                mock.patch.object(chat, "_known_route_setup_problem", return_value=""), \
                mock.patch.object(swarm_runs, "provider_effect", return_value=nullcontext()):
            return chat.ask_once(self.config, "", "Use PLOQQIZ, not PLOQGZ. URL https://example.test/a%20b?q=A+B#Exact",
                context=context, provider_attachments=files,
                workspace_context=ProviderWorkspaceContext("selected", str(selected), str(working)))

    def test_empty_generic_and_incorrect_mime_use_actual_original_image_bytes(self):
        cases = ([(value, "Exact path screenshot.PNG") for value in (
                "", "application/octet-stream", "binary/octet-stream", "image/jpeg")]
                + [("text/plain", "mislabelled.txt"), ("image/jpeg", "mislabelled.jpg")])
        for mime, name in cases:
            with self.subTest(mime=mime, name=name):
                public, files, context = self.ingest(mime, name)
                self.assertEqual(public[0]["type"], "image/png")
                self.assertTrue(public[0]["image"])
                self.assertEqual((public[0]["width"], public[0]["height"]), (1600, 900))
                self.assertEqual(public[0]["sha256"], hashlib.sha256(self.original).hexdigest())
                self.assertNotIn("data", public[0])
                self.assertEqual(Path(files[0]["path"]).suffix, ".png")
                self.assertEqual(Path(files[0]["path"]).read_bytes(), self.original)
                self.assertEqual(base64.b64decode(files[0]["data"]), self.original)
                self.assertIn(json.dumps(name), context)
                self.assertIn('"width": 1600', context)
                self.assertNotIn(files[0]["path"], context)

    def test_nonvision_image_document_keeps_honest_type_without_claiming_visual_input(self):
        public, files, context = chat.keep_attachments(self.config, "", [{"name": "vector.svg",
            "type": "application/octet-stream", "data": base64.b64encode(b"<svg/>").decode()}], "document")
        self.assertEqual(public[0]["type"], "image/svg+xml")
        self.assertFalse(public[0]["image"])
        self.assertEqual(files, [])
        self.assertIn("not supplied as visual input", context)

    def test_false_image_signature_is_rejected_before_provider_input(self):
        with self.assertRaisesRegex(chat.ChatError, "does not contain the image format"):
            chat.keep_attachments(self.config, "", [{"name": "screen.png", "type": "image/png",
                "data": base64.b64encode(b"not an image").decode()}], "invalid")

    def test_ingested_original_reaches_actual_openai_provider_http_payloads(self):
        _public, files, context = self.ingest()
        for mode in ("chat-completions", "responses"):
            with self.subTest(mode=mode):
                self.config.data["provider"]["api_mode"] = mode
                provider = OpenAIProvider(self.config)
                response = ({"choices": [{"message": {"content": "received"}, "finish_reason": "stop"}]}
                    if mode == "chat-completions" else {"id": "response-fixture", "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "received"}]}]})
                with mock.patch.object(provider, "_post", return_value=response) as posted:
                    answer = self.ask(provider, files, context)
                self.assertEqual(answer["text"], "received")
                payload = posted.call_args.args[1]
                messages = payload["messages"] if mode == "chat-completions" else payload["input"]
                content = next(one for one in reversed(messages) if one["role"] == "user")["content"]
                visual = content[1]["image_url"]
                data_url = visual["url"] if isinstance(visual, dict) else visual
                self.assertEqual(base64.b64decode(data_url.partition(",")[2]), self.original)
                self.assertIn("a%20b?q=A+B#Exact", content[0]["text"])
                instructions = payload.get("instructions", messages[0].get("content", ""))
                self.assertIn("explicit typed correction takes precedence", instructions)
                self.assertIn("selected_project_path", instructions)

    def test_ingested_original_reaches_actual_native_codex_image_operand(self):
        _public, files, context = self.ingest()
        provider = codex_cli.CodexCLIProvider(self.config)
        provider._preflight_complete = True
        observed = {}

        def execute(argv, **kwargs):
            image_path = Path(argv[argv.index("--image") + 1])
            observed.update(data=image_path.read_bytes(), prompt=kwargs["stdin_text"], cwd=kwargs["cwd"])
            Path(argv[argv.index("--output-last-message") + 1]).write_text('{"text":"received"}', encoding="utf-8")
            return CommandResult(argv, str(kwargs["cwd"]), 0, '{"type":"turn.completed"}', "", 1)

        catalog = json.dumps({"models": [{"slug": "fixture-model", "supported_reasoning_levels": []}]})
        with mock.patch.object(provider, "_command", return_value=["synthetic-codex"]), \
                mock.patch.object(codex_cli, "_bundled_model_catalog", return_value=catalog), \
                mock.patch.object(codex_cli, "_run_bounded", side_effect=execute):
            answer = self.ask(provider, files, context)
        self.assertEqual(answer["text"], "received")
        self.assertEqual(observed["data"], self.original)
        self.assertIn('"width": 1600', observed["prompt"])
        self.assertIn("PLOQQIZ", observed["prompt"])
        self.assertNotEqual(observed["cwd"], self.root)

    def test_header_parser_handles_webp_forms_gif_and_rotated_jpeg(self):
        samples = [
            (b"GIF89a" + (7).to_bytes(2, "little") + (9).to_bytes(2, "little"), "image/gif", (7, 9)),
            (b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"\x00" * 7 + b"\x9d\x01\x2a"
                + (7).to_bytes(2, "little") + (9).to_bytes(2, "little"), "image/webp", (7, 9)),
            (b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 4 + b"\x2f"
                + (6 | (8 << 14)).to_bytes(4, "little"), "image/webp", (7, 9)),
            (b"RIFF" + b"\x00" * 4 + b"WEBPVP8X" + b"\x00" * 8
                + (6).to_bytes(3, "little") + (8).to_bytes(3, "little"), "image/webp", (7, 9)),
        ]
        exif = b"Exif\x00\x00II\x2a\x00\x08\x00\x00\x00\x01\x00\x12\x01\x03\x00\x01\x00\x00\x00\x06\x00\x00\x00"
        jpeg = b"\xff\xd8\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif
        jpeg += b"\xff\xc0\x00\x08\x08\x00\x09\x00\x07\x01"
        samples.append((jpeg, "image/jpeg", (9, 7)))
        for data, mime, dimensions in samples:
            with self.subTest(mime=mime, prefix=data[:16]):
                found = images.attachment_image_metadata(data)
                self.assertEqual(found, {"type": mime, "width": dimensions[0], "height": dimensions[1]})
        self.assertIsNone(images.attachment_image_metadata(b"not an image"))
        self.assertNotIn("width", images.attachment_image_metadata(jpeg[:8]))

    def test_word_prompt_reaches_openai_and_codex_without_provider_document_support(self):
        marker = "Implement the cafés listed in the Word prompt."
        raw = make_docx(marker)
        _public, files, context = chat.keep_attachments(self.config, "", [{
            "name": "Task.docx", "data": base64.b64encode(raw).decode(),
        }], "word-prompt")
        for mode in ("chat-completions", "responses"):
            self.config.data["provider"]["api_mode"] = mode
            provider = OpenAIProvider(self.config)
            response = ({"choices": [{"message": {"content": "received"}, "finish_reason": "stop"}]}
                if mode == "chat-completions" else {"id": "word-response", "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "received"}]}]})
            with mock.patch.object(provider, "_post", return_value=response) as posted:
                self.ask(provider, files, context)
            self.assertIn(marker, json.dumps(posted.call_args.args[1], ensure_ascii=False))
        provider = codex_cli.CodexCLIProvider(self.config)
        provider._preflight_complete = True
        observed = {}

        def execute(argv, **kwargs):
            observed["prompt"] = kwargs["stdin_text"]
            Path(argv[argv.index("--output-last-message") + 1]).write_text('{"text":"received"}', encoding="utf-8")
            return CommandResult(argv, str(kwargs["cwd"]), 0, '{"type":"turn.completed"}', "", 1)

        catalog = json.dumps({"models": [{"slug": "fixture-model", "supported_reasoning_levels": []}]})
        with mock.patch.object(provider, "_command", return_value=["synthetic-codex"]), \
                mock.patch.object(codex_cli, "_bundled_model_catalog", return_value=catalog), \
                mock.patch.object(codex_cli, "_run_bounded", side_effect=execute):
            self.ask(provider, files, context)
        self.assertIn(marker, observed["prompt"])


if __name__ == "__main__":
    unittest.main()
