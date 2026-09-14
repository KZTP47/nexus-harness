"""Parent-owned end-to-end history-to-native-payload regression."""
from __future__ import annotations

import base64
import copy
import json
import unittest
from contextlib import nullcontext
from unittest import mock

from our_harness import chat, swarm_runs
from our_harness.config import LoadedConfig
from our_harness.providers.base import OpenAIProvider
import test_attachment_input_fidelity as fixtures


class WorkflowAttachmentPayloadTests(unittest.TestCase):
    setUp = fixtures.AttachmentInputFidelityTests.setUp

    def test_saved_screenshot_reaches_real_native_payload_after_reopen(self):
        for mode in ("chat-completions", "responses"):
            with self.subTest(mode=mode):
                self.config.data["provider"]["api_mode"] = mode
                provider = OpenAIProvider(self.config)
                reply = ({"choices": [{"message": {"content": "received"}, "finish_reason": "stop"}]}
                    if mode == "chat-completions" else {"id": "fixture-response", "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "received"}]}]})
                filed_as = "saved-exact-picture-" + mode
                with mock.patch.object(chat, "create_provider", return_value=provider), \
                        mock.patch.object(chat, "_known_route_setup_problem", return_value=""), \
                        mock.patch.object(swarm_runs, "provider_effect", side_effect=lambda *a, **k: nullcontext()), \
                        mock.patch.object(provider, "_post", return_value=reply) as posted:
                    chat.say(self.config, "", "Inspect the attached screenshot.", filed_as, attachments=[{
                        "name": "Original pixels.png", "type": "image/png",
                        "data": base64.b64encode(self.original).decode(),
                    }])
                    reopened = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
                    chat.say(reopened, "", "Use the exact earlier screenshot. The correct label is PLOQQIZ.", filed_as)
                    payload = posted.call_args.args[1]
                    messages = payload["messages"] if mode == "chat-completions" else payload["input"]
                    visuals = []
                    for message in messages:
                        for part in message.get("content", []) if isinstance(message.get("content"), list) else []:
                            value = part.get("image_url")
                            if value:
                                visuals.append(value["url"] if isinstance(value, dict) else value)
                    self.assertEqual(len(visuals), 1)
                    self.assertEqual(base64.b64decode(visuals[0].partition(",")[2]), self.original)
                    self.assertIn("PLOQQIZ", json.dumps(payload))
                    # A different transcript has no access to these saved images.
                    chat.say(reopened, "", "Inspect any earlier image.", "unrelated-" + mode)
                    other = posted.call_args.args[1]
                    self.assertNotIn("data:image/png;base64", json.dumps(other))


if __name__ == "__main__":
    unittest.main()
