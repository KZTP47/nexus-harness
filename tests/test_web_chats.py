from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace

from our_harness import cancellation
from our_harness.models import ProviderRequest, ResponseFormat
from our_harness.web_chats import WebChatBroker


class WebChatBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = WebChatBroker()
        self.broker.heartbeat([{
            "id": "claude-abcdef123456", "provider": "Claude",
            "title": "A real conversation", "url": "https://claude.ai/chat/one",
        }])

    def request(self, structured: bool = False, with_attachment: bool = False) -> ProviderRequest:
        return ProviderRequest(
            system_prefix="Be useful.", dynamic_context="Agent A may talk to Agent B.",
            messages=[{"role": "user", "content": "Work together on the goal."}],
            model="", timeout_seconds=5,
            response_format=ResponseFormat("answer", {
                "type": "object", "properties": {"done": {"type": "boolean"}},
                "required": ["done"], "additionalProperties": False,
            }) if structured else None,
            attachments=[{
                "name": "reference.png",
                "path": r"C:\project\.harness\chats\attachments\turn\reference.png",
            }] if with_attachment else [],
            conversation_key="pair-chat-0123456789abcdef",
            prefer_existing_conversation=True,
        )

    def test_a_real_pending_turn_blocks_until_electron_returns_the_reply(self) -> None:
        result: list[str] = []
        thread = threading.Thread(target=lambda: result.append(
            self.broker.provider("web:claude-abcdef123456").complete(self.request()).text
        ))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        self.assertEqual(len(pending), 1)
        self.assertIn("Work together on the goal", pending[0]["prompt"])
        self.assertEqual(
            pending[0]["conversation_key"], "pair-chat-0123456789abcdef"
        )
        self.assertTrue(pending[0]["prefer_existing_conversation"])
        self.broker.complete(pending[0]["request_id"], answer="The visible provider reply")
        thread.join(2)
        self.assertEqual(result, ["The visible provider reply"])

    def test_standalone_display_identity_is_mapped_to_a_safe_stable_channel(self) -> None:
        request = replace(
            self.request(),
            conversation_key="New chat - Claude / personal subscription",
        )
        thread = threading.Thread(target=lambda: (
            self.broker.provider("web:claude-abcdef123456").complete(request)
        ))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        key = pending[0]["conversation_key"]
        self.assertTrue(key.startswith("conversation-"))
        self.assertLessEqual(len(key), 160)
        self.broker.complete(pending[0]["request_id"], answer="standalone works")
        thread.join(2)

    def test_attachment_paths_are_forwarded_to_the_electron_courier(self) -> None:
        thread = threading.Thread(target=lambda: (
            self.broker.provider("web:claude-abcdef123456").complete(
                self.request(with_attachment=True)
            )
        ))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        self.assertEqual(pending[0]["attachments"][0]["name"], "reference.png")
        self.assertTrue(pending[0]["attachments"][0]["path"].endswith("reference.png"))
        self.broker.complete(pending[0]["request_id"], answer="I can see the file")
        thread.join(2)

    def test_structured_agent_rounds_are_expressed_as_a_web_prompt(self) -> None:
        caught: list[str] = []
        def ask() -> None:
            self.broker.provider("web:claude-abcdef123456").complete(self.request(True))
        thread = threading.Thread(target=ask)
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        caught.append(pending[0]["prompt"])
        self.broker.complete(pending[0]["request_id"], answer='{"done": true}')
        thread.join(2)
        self.assertIn("Return only JSON", caught[0])
        self.assertIn('"done"', caught[0])
        self.assertLess(caught[0].index("Quoted user request:"), caught[0].index(
            "Authoritative role and turn instructions from Nexus:"
        ))
        self.assertLess(caught[0].index(
            "Authoritative role and turn instructions from Nexus:"
        ), caught[0].index("Return only JSON"))

    def test_a_pending_web_turn_wakes_immediately_when_its_chat_is_stopped(self) -> None:
        token = cancellation.Cancellation()
        errors: list[Exception] = []

        def ask() -> None:
            try:
                with cancellation.use(token):
                    self.broker.provider("web:claude-abcdef123456").complete(self.request())
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=ask)
        thread.start()
        for _ in range(100):
            if self.broker.pending():
                break
            time.sleep(0.01)
        token.cancel()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(str(errors[0]), "Stopped by you.")
        self.assertEqual(self.broker.pending(), [])

    def test_live_web_routes_decorate_agents_without_replacing_desktop_routes(self) -> None:
        standing = {
            "who_can_be_used": [{"route": "claude", "label": "Claude", "ready": True}],
            "board": {"agents": [
                {"id": "a", "who": "claude", "ready": True},
                {"id": "b", "who": "web:claude-abcdef123456", "ready": False},
            ]},
        }
        self.broker.decorate_swarm(standing)
        self.assertEqual(standing["who_can_be_used"][0]["route"], "claude")
        self.assertTrue(standing["board"]["agents"][1]["ready"])
        self.assertEqual(
            standing["board"]["agents"][1]["chat_destination"]["web_chat_id"],
            "claude-abcdef123456",
        )


if __name__ == "__main__":
    unittest.main()
