from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace

from our_harness import cancellation
from our_harness.models import HarnessError, ProviderOutcomeUnknown, ProviderRequest, ResponseFormat
from our_harness.web_chats import MAX_WEB_CONNECTIONS, WebChatBroker


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

    def test_completion_receipt_is_idempotent_when_http_acknowledgement_is_lost(self) -> None:
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
        request_id = pending[0]["request_id"]
        self.assertTrue(self.broker.complete(request_id, answer="delivered once"))
        thread.join(2)
        # A renderer retry acknowledges the original receipt.  It cannot
        # overwrite the accepted result or cause another provider submission.
        self.assertTrue(self.broker.complete(request_id, answer="conflicting retry"))
        self.assertEqual(result, ["delivered once"])

    def test_unknown_or_expired_completion_is_not_reported_as_accepted(self) -> None:
        self.assertFalse(self.broker.complete("a" * 32, answer="orphaned"))

    def test_unclaimed_or_already_timed_out_request_cannot_be_resurrected(self) -> None:
        result: list[Exception] = []
        short = replace(self.request(), timeout_seconds=0.05)
        thread = threading.Thread(target=lambda: self._ask_route(
            "web:claude-abcdef123456", result, short,
        ))
        thread.start()
        request_id = ""
        for _ in range(100):
            with self.broker._condition:
                if self.broker._requests:
                    request_id = next(iter(self.broker._requests))
                    break
            time.sleep(0.001)
        self.assertTrue(request_id)
        # The renderer has not claimed this request. It cannot fabricate a
        # completion or bypass the broker's exact claim boundary.
        self.assertFalse(self.broker.complete(request_id, answer="too early"))
        pending = self.broker.pending()
        self.assertEqual(pending[0]["request_id"], request_id)
        thread.join(2)
        self.assertTrue(result)
        self.assertFalse(self.broker.complete(request_id, answer="too late"))

    def test_heartbeat_rejects_capacity_overflow_instead_of_hiding_later_routes(self) -> None:
        connections = [{
            "id": f"claude-{number:06d}", "provider": "Claude",
            "title": f"Chat {number}", "url": f"https://claude.ai/chat/{number}",
        } for number in range(MAX_WEB_CONNECTIONS + 1)]
        with self.assertRaisesRegex(HarnessError, "at most"):
            self.broker.heartbeat(connections)

    def test_same_provider_conversations_share_one_physical_relay_slot(self) -> None:
        self.broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        errors: list[Exception] = []
        threads = [
            threading.Thread(target=lambda route=route: self._ask_route(route, errors))
            for route in ("web:claude-abcdef123456", "web:claude-fedcba654321")
        ]
        for held in threads:
            held.start()
        for _ in range(100):
            first = self.broker.pending()
            if first:
                break
            time.sleep(0.01)
        self.assertEqual(len(first), 1)
        self.assertEqual(self.broker.pending(), [])
        self.broker.complete(first[0]["request_id"], answer="first")
        for _ in range(100):
            second = self.broker.pending()
            if second:
                break
            time.sleep(0.01)
        self.assertEqual(len(second), 1)
        self.broker.complete(second[0]["request_id"], answer="second")
        for held in threads:
            held.join(2)
        self.assertEqual(errors, [])

    def test_waiting_for_shared_slot_does_not_spend_provider_service_timeout(self) -> None:
        self.broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        errors: list[Exception] = []
        first_thread = threading.Thread(
            target=lambda: self._ask_route("web:claude-abcdef123456", errors)
        )
        first_thread.start()
        for _ in range(100):
            first = self.broker.pending()
            if first:
                break
            time.sleep(0.01)
        self.assertEqual(len(first), 1)

        short = replace(self.request(), timeout_seconds=1)
        second_thread = threading.Thread(target=lambda: self._ask_route(
            "web:claude-fedcba654321", errors, short,
        ))
        second_thread.start()
        # Record that this turn is queued behind the shared Claude browser
        # session, then spend most of its one-second provider budget there.
        self.assertEqual(self.broker.pending(), [])
        time.sleep(0.6)
        self.assertTrue(second_thread.is_alive())

        self.assertTrue(self.broker.complete(first[0]["request_id"], answer="first"))
        for _ in range(100):
            second = self.broker.pending()
            if second:
                break
            time.sleep(0.01)
        self.assertEqual(len(second), 1)
        with self.broker._condition:
            held = self.broker._requests[second[0]["request_id"]]
            self.assertAlmostEqual(
                held.completion_deadline - held.claimed_at, 1.0, delta=0.05
            )

        # Total time since enqueue now exceeds one second, but the service
        # phase has not. The old single deadline failed this safe completion.
        time.sleep(0.55)
        self.assertTrue(self.broker.complete(second[0]["request_id"], answer="second"))
        first_thread.join(2)
        second_thread.join(2)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])

    def test_shared_slot_queue_admission_timeout_is_safe_and_truthful(self) -> None:
        broker = WebChatBroker(queue_wait_seconds=0.08)
        broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        errors: list[Exception] = []
        first_thread = threading.Thread(target=lambda: (
            broker.provider("web:claude-abcdef123456").complete(self.request())
        ))
        first_thread.start()
        for _ in range(100):
            first = broker.pending()
            if first:
                break
            time.sleep(0.01)
        self.assertEqual(len(first), 1)

        def ask_second() -> None:
            try:
                broker.provider("web:claude-fedcba654321").complete(self.request())
            except Exception as exc:
                errors.append(exc)

        second_thread = threading.Thread(target=ask_second)
        second_thread.start()
        self.assertEqual(broker.pending(), [])
        second_thread.join(1)

        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], HarnessError)
        self.assertNotIsInstance(errors[0], ProviderOutcomeUnknown)
        self.assertIn("shared signed-in browser session busy", str(errors[0]))
        self.assertIn("This turn was not submitted", str(errors[0]))
        self.assertIn("failure_stage=physical_resource_queue", str(errors[0]))
        self.assertIn("failure_code=relay_queue_admission_timeout", str(errors[0]))
        self.assertEqual(broker.pending(), [])

        self.assertTrue(broker.complete(first[0]["request_id"], answer="first"))
        first_thread.join(2)
        self.assertFalse(first_thread.is_alive())

    def test_unpolled_queue_admission_timeout_says_relay_never_claimed(self) -> None:
        broker = WebChatBroker(queue_wait_seconds=0.05)
        broker.heartbeat([{
            "id": "claude-abcdef123456", "provider": "Claude",
            "title": "One", "url": "https://claude.ai/chat/one",
        }])
        errors: list[Exception] = []

        def ask() -> None:
            try:
                broker.provider("web:claude-abcdef123456").complete(self.request())
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=ask)
        thread.start()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], HarnessError)
        self.assertNotIsInstance(errors[0], ProviderOutcomeUnknown)
        self.assertIn("desktop relay did not claim", str(errors[0]))
        self.assertIn("This turn was not submitted", str(errors[0]))
        self.assertIn("failure_stage=desktop_bridge_admission", str(errors[0]))
        self.assertIn("failure_code=relay_not_claimed", str(errors[0]))
        self.assertEqual(broker.pending(), [])

    def test_route_removal_cannot_release_a_claimed_provider_slot(self) -> None:
        self.broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        errors: list[Exception] = []
        first_thread = threading.Thread(
            target=lambda: self._ask_route("web:claude-abcdef123456", errors)
        )
        second_thread = threading.Thread(
            target=lambda: self._ask_route("web:claude-fedcba654321", errors)
        )
        first_thread.start()
        for _ in range(100):
            first = self.broker.pending()
            if first:
                break
            time.sleep(0.01)
        self.assertEqual(len(first), 1)

        second_thread.start()
        # Simulate a route edit/removal while the first provider turn is in
        # flight.  The immutable request resource must keep Claude occupied.
        self.broker.heartbeat([
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        self.assertEqual(self.broker.pending(), [])

        self.broker.complete(first[0]["request_id"], answer="first")
        for _ in range(100):
            second = self.broker.pending()
            if second:
                break
            time.sleep(0.01)
        self.assertEqual(len(second), 1)
        self.broker.complete(second[0]["request_id"], answer="second")
        first_thread.join(2)
        second_thread.join(2)
        self.assertEqual(errors, [])

    def test_different_provider_sessions_can_relay_in_parallel(self) -> None:
        self.broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "gemini-abcdef123456", "provider": "Gemini", "title": "Two",
             "url": "https://gemini.google.com/app/two"},
        ])
        threads = [
            threading.Thread(target=lambda route=route: self._ask_route(route, []))
            for route in ("web:claude-abcdef123456", "web:gemini-abcdef123456")
        ]
        for held in threads:
            held.start()
        for _ in range(100):
            pending = self.broker.pending()
            if len(pending) == 2:
                break
            time.sleep(0.01)
        self.assertEqual(len(pending), 2)
        for one in pending:
            self.broker.complete(one["request_id"], answer="done")
        for held in threads:
            held.join(2)

    def _ask_route(
        self, route: str, errors: list[Exception], request: ProviderRequest | None = None,
    ) -> None:
        try:
            self.broker.provider(route).complete(request or self.request())
        except Exception as exc:
            errors.append(exc)

    def test_incident_sized_web_answer_is_not_sliced_at_200k(self) -> None:
        result: list[str] = []
        answer = "web-result:" + ("x" * 250_000)
        thread = threading.Thread(target=lambda: result.append(
            self.broker.provider("web:claude-abcdef123456").complete(self.request()).text
        ))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        self.broker.complete(pending[0]["request_id"], answer=answer)
        thread.join(2)
        self.assertEqual(result, [answer])

    def test_long_web_failure_is_redacted_before_bounded_head_and_tail_storage(self) -> None:
        errors: list[str] = []
        thread = threading.Thread(target=lambda: self._capture_failure(errors))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        secret = "bearer-secret-value-0123456789"
        cause = "CAUSE-HEAD " + ("x" * 70_000) + f" Bearer {secret} CAUSE-TAIL"
        self.broker.complete(pending[0]["request_id"], error=cause)
        thread.join(2)
        self.assertNotIn(secret, errors[0])
        self.assertIn("[REDACTED]", errors[0])
        self.assertIn("NEXUS_REDACTED_CAUSE_BOUNDARY", errors[0])
        self.assertIn("CAUSE-HEAD", errors[0])
        self.assertIn("CAUSE-TAIL", errors[0])

    def test_acknowledged_reply_timeout_is_a_known_failure_with_diagnostics(self) -> None:
        errors: list[Exception] = []
        thread = threading.Thread(target=lambda: self._capture_exception(errors))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        self.broker.complete(
            pending[0]["request_id"],
            error="Claude accepted the message but no finished reply was observed",
            delivery_state="accepted", failure_code="reply_completion_timeout",
            diagnostics={"reply_seen": True, "stop_visible": True, "polls": 183},
        )
        thread.join(2)

        self.assertIsInstance(errors[0], HarnessError)
        self.assertNotIsInstance(errors[0], ProviderOutcomeUnknown)
        self.assertIn("reply_seen=True", str(errors[0]))
        self.assertIn("stop_visible=True", str(errors[0]))

    def test_only_explicitly_unknown_web_delivery_uses_uncertain_outcome(self) -> None:
        errors: list[Exception] = []
        thread = threading.Thread(target=lambda: self._capture_exception(errors))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        self.broker.complete(
            pending[0]["request_id"], error="Could not match the marked turn",
            delivery_state="unknown", failure_code="turn_match_unknown",
        )
        thread.join(2)

        self.assertIsInstance(errors[0], ProviderOutcomeUnknown)

    def test_claimed_untyped_error_is_conservatively_unknown(self) -> None:
        errors: list[Exception] = []
        thread = threading.Thread(target=lambda: self._capture_exception(errors))
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        self.broker.complete(
            pending[0]["request_id"], error="legacy desktop bridge error"
        )
        thread.join(2)

        self.assertIsInstance(errors[0], ProviderOutcomeUnknown)

    def test_claimed_relay_timeout_is_unknown_instead_of_safe_to_resend(self) -> None:
        errors: list[Exception] = []
        short = replace(self.request(), timeout_seconds=1)

        def ask() -> None:
            try:
                self.broker.provider("web:claude-abcdef123456").complete(short)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=ask)
        thread.start()
        for _ in range(100):
            pending = self.broker.pending()
            if pending:
                break
            time.sleep(0.01)
        self.assertTrue(pending)
        thread.join(2)

        self.assertIsInstance(errors[0], ProviderOutcomeUnknown)
        self.assertIn("relay claimed", str(errors[0]))

    def test_unknown_claim_keeps_provider_slot_until_exact_late_completion(self) -> None:
        broker = WebChatBroker(uncertain_resource_seconds=1)
        broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        first_errors: list[Exception] = []

        def ask_first() -> None:
            try:
                broker.provider("web:claude-abcdef123456").complete(self.request())
            except Exception as exc:
                first_errors.append(exc)

        first_thread = threading.Thread(target=ask_first)
        first_thread.start()
        for _ in range(100):
            first = broker.pending()
            if first:
                break
            time.sleep(0.01)
        self.assertEqual(len(first), 1)
        first_id = first[0]["request_id"]
        with broker._condition:
            broker._requests[first_id].completion_deadline = time.monotonic() + 0.03
            broker._condition.notify_all()
        first_thread.join(1)
        self.assertFalse(first_thread.is_alive())
        self.assertIsInstance(first_errors[0], ProviderOutcomeUnknown)
        with broker._condition:
            self.assertIn(first_id, broker._uncertain_requests)

        second_errors: list[Exception] = []
        second_answers: list[str] = []

        def ask_second() -> None:
            try:
                second_answers.append(broker.provider(
                    "web:claude-fedcba654321"
                ).complete(self.request()).text)
            except Exception as exc:
                second_errors.append(exc)

        second_thread = threading.Thread(target=ask_second)
        second_thread.start()
        for _ in range(100):
            with broker._condition:
                queued = any(
                    one.route == "web:claude-fedcba654321"
                    for one in broker._requests.values()
                )
            if queued:
                break
            time.sleep(0.01)
        self.assertTrue(queued)
        self.assertEqual(
            broker.pending(), [],
            "the uncertain first attempt must continue owning Claude's physical session",
        )
        self.assertTrue(second_thread.is_alive())

        # The exact late terminal receipt releases capacity but cannot rewrite
        # the unknown outcome Python already returned as an accepted success.
        self.assertFalse(broker.complete(first_id, answer="late first answer"))
        self.assertFalse(broker.complete(first_id, answer="late retry"))
        for _ in range(100):
            second = broker.pending()
            if second:
                break
            time.sleep(0.01)
        self.assertEqual(len(second), 1)
        self.assertTrue(broker.complete(second[0]["request_id"], answer="second"))
        second_thread.join(1)
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(second_errors, [])
        self.assertEqual(second_answers, ["second"])

    def test_unknown_resource_fence_expires_and_queue_recovers_without_receipt(self) -> None:
        broker = WebChatBroker(uncertain_resource_seconds=0.06)
        broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        first_errors: list[Exception] = []
        first_thread = threading.Thread(target=lambda: self._ask_with_broker(
            broker, "web:claude-abcdef123456", first_errors,
        ))
        first_thread.start()
        for _ in range(100):
            first = broker.pending()
            if first:
                break
            time.sleep(0.01)
        first_id = first[0]["request_id"]
        with broker._condition:
            broker._requests[first_id].completion_deadline = time.monotonic() + 0.02
            broker._condition.notify_all()
        first_thread.join(1)
        self.assertIsInstance(first_errors[0], ProviderOutcomeUnknown)

        second_errors: list[Exception] = []
        second_thread = threading.Thread(target=lambda: self._ask_with_broker(
            broker, "web:claude-fedcba654321", second_errors,
        ))
        second_thread.start()
        for _ in range(100):
            with broker._condition:
                queued = bool(broker._requests)
            if queued:
                break
            time.sleep(0.01)
        self.assertEqual(broker.pending(), [])
        time.sleep(0.08)
        second = broker.pending()
        self.assertEqual(len(second), 1)
        self.assertTrue(broker.complete(second[0]["request_id"], answer="recovered"))
        second_thread.join(1)
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(second_errors, [])
        with broker._condition:
            self.assertEqual(broker._uncertain_requests, {})

    def test_queue_timeout_behind_unknown_turn_explains_safe_recovery(self) -> None:
        broker = WebChatBroker(
            queue_wait_seconds=0.05, uncertain_resource_seconds=1,
        )
        broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        first_errors: list[Exception] = []
        first_thread = threading.Thread(target=lambda: self._ask_with_broker(
            broker, "web:claude-abcdef123456", first_errors,
        ))
        first_thread.start()
        for _ in range(100):
            first = broker.pending()
            if first:
                break
            time.sleep(0.01)
        first_id = first[0]["request_id"]
        with broker._condition:
            broker._requests[first_id].completion_deadline = time.monotonic() + 0.02
            broker._condition.notify_all()
        first_thread.join(1)

        second_errors: list[Exception] = []
        second_thread = threading.Thread(target=lambda: self._ask_with_broker(
            broker, "web:claude-fedcba654321", second_errors,
        ))
        second_thread.start()
        for _ in range(100):
            with broker._condition:
                queued = bool(broker._requests)
            if queued:
                break
            time.sleep(0.01)
        self.assertEqual(broker.pending(), [])
        second_thread.join(1)

        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(second_errors), 1)
        self.assertNotIsInstance(second_errors[0], ProviderOutcomeUnknown)
        self.assertIn("unknown outcome", str(second_errors[0]))
        self.assertIn("This queued turn was not submitted", str(second_errors[0]))
        self.assertIn("Inspect the affected provider chat", str(second_errors[0]))
        self.assertIn(
            "failure_code=relay_uncertain_resource_timeout", str(second_errors[0])
        )
        self.assertFalse(broker.complete(first_id, error="late terminal receipt"))

    def _ask_with_broker(
        self, broker: WebChatBroker, route: str, errors: list[Exception],
    ) -> None:
        try:
            broker.provider(route).complete(self.request())
        except Exception as exc:
            errors.append(exc)

    def _capture_failure(self, errors: list[str]) -> None:
        try:
            self.broker.provider("web:claude-abcdef123456").complete(self.request())
        except Exception as exc:
            errors.append(str(exc))

    def _capture_exception(self, errors: list[Exception]) -> None:
        try:
            self.broker.provider("web:claude-abcdef123456").complete(self.request())
        except Exception as exc:
            errors.append(exc)

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
        self.assertIn("fenced ```json code block", caught[0])
        self.assertIn("literal characters such as *, _, <, and >", caught[0])
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

    def test_claimed_cancellation_fences_slot_until_confirmed_desktop_stop(self) -> None:
        broker = WebChatBroker(uncertain_resource_seconds=1)
        broker.heartbeat([
            {"id": "claude-abcdef123456", "provider": "Claude", "title": "One",
             "url": "https://claude.ai/chat/one"},
            {"id": "claude-fedcba654321", "provider": "Claude", "title": "Two",
             "url": "https://claude.ai/chat/two"},
        ])
        token = cancellation.Cancellation()
        first_errors: list[Exception] = []

        def ask_first() -> None:
            try:
                with cancellation.use(token):
                    broker.provider("web:claude-abcdef123456").complete(self.request())
            except Exception as exc:
                first_errors.append(exc)

        first_thread = threading.Thread(target=ask_first)
        first_thread.start()
        for _ in range(100):
            first = broker.pending()
            if first:
                break
            time.sleep(0.01)
        first_id = first[0]["request_id"]
        token.cancel()
        first_thread.join(1)
        self.assertFalse(first_thread.is_alive())
        self.assertIsInstance(first_errors[0], cancellation.ChatCancelled)
        with broker._condition:
            self.assertIn(first_id, broker._uncertain_requests)

        second_errors: list[Exception] = []
        second_thread = threading.Thread(target=lambda: self._ask_with_broker(
            broker, "web:claude-fedcba654321", second_errors,
        ))
        second_thread.start()
        for _ in range(100):
            with broker._condition:
                queued = bool(broker._requests)
            if queued:
                break
            time.sleep(0.01)
        self.assertEqual(broker.pending(), [])
        self.assertTrue(second_thread.is_alive())

        # Electron's exact terminal receipt is the positive acknowledgement
        # that the desktop Stop path ended this relay. It releases capacity
        # immediately, but cannot rewrite the cancellation as accepted.
        self.assertFalse(broker.complete(
            first_id, error="Stopped by you.", delivery_state="accepted",
            failure_code="provider_stop_confirmed",
            diagnostics={"provider_stop_confirmed": True},
        ))
        second = broker.pending()
        self.assertEqual(len(second), 1)
        self.assertTrue(broker.complete(second[0]["request_id"], answer="after stop"))
        second_thread.join(1)
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(second_errors, [])

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
