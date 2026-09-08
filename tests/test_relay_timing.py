from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, relay_timing, web_chats
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class RelayTimingTests(unittest.TestCase):
    def test_completed_broker_turn_keeps_timings_in_saved_chat_across_reload(self):
        with tempfile.TemporaryDirectory(prefix="relay-timing-") as root:
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
            broker = web_chats.WebChatBroker()
            route = "web:chatgpt-portable-user"
            broker.heartbeat([{"id": route[4:], "provider": "chatgpt"}])
            result = []
            errors = []

            def ask():
                try:
                    result.append(chat.say(config, route, "Who is here?", filed_as="saved chat"))
                except Exception as error:
                    errors.append(error)

            with mock.patch.object(web_chats, "active", return_value=broker):
                thread = threading.Thread(target=ask)
                thread.start()
                pending = []
                for _ in range(200):
                    pending = broker.pending()
                    if pending or errors:
                        break
                    time.sleep(0.01)
                self.assertFalse(errors)
                self.assertEqual(len(pending), 1)
                self.assertTrue(broker.complete(pending[0]["request_id"], answer="Only me.",
                    diagnostics={"timing_version": 1, "prepare_ms": 1200,
                        "submit_ms": 100, "reply_wait_ms": 2050, "first_reply_ms": 250,
                        "capture_tail_ms": 1800, "browser_total_ms": 3350,
                        "untrusted_text": "must not persist"}))
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
            timing = result[0]["answer"]["relay_timing"]
            self.assertEqual(timing["submit_ms"], 100)
            self.assertGreaterEqual(timing["queue_ms"], 0)
            self.assertNotIn("untrusted_text", timing)
            # New config object reloads the authenticated durable transcript.
            reloaded = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
            saved = chat.read_it(reloaded, route, "saved chat")[-1].to_dict()
            self.assertEqual(saved["relay_timing"], timing)
            self.assertEqual(saved["text"], "Only me.")
            self.assertNotIn("relay_timing", chat.Said("them", "Legacy", "then").to_dict())
            self.assertEqual(relay_timing.frozen(json.loads(json.dumps(timing))), timing)

    def test_invalid_or_future_timing_cannot_expose_arbitrary_provider_metadata(self):
        for value in (None, [], {"timing_version": 2, "submit_ms": 1},
                      {"timing_version": 1, "submit_ms": "secret"}):
            self.assertEqual(relay_timing.frozen(value), {})
        self.assertEqual(relay_timing.frozen({"timing_version": "1", "queue_ms": "10",
            "submit_ms": True, "prepare_ms": -2, "first_reply_ms": float("inf"),
            "reply_wait_ms": "9" * 1000, "browser_total_ms": 86_400_001,
            "provider_url": "private"}), {"timing_version": 1, "queue_ms": 10})

    def test_observation_durations_do_not_change_idempotent_turn_identity(self):
        before = chat.Said("them", "Same answer", "then", milliseconds=1,
            relay_timing={"timing_version": 1, "submit_ms": 100})
        after = chat.Said("them", "Same answer", "later", milliseconds=2,
            relay_timing={"timing_version": 1, "submit_ms": 200})
        self.assertEqual(chat._correlated_semantics(before), chat._correlated_semantics(after))


if __name__ == "__main__":
    unittest.main()
